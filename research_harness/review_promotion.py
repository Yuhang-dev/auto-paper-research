"""Explicit handoff from a review bundle into deferred Wiki ingestion."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import yaml  # type: ignore[import-untyped]

from .config import HarnessSettings
from .evidence_verification import LangChainEvidenceSemanticVerifier
from .ingest_models import IngestCandidate
from .model_client import ReviewModelBundle, create_profile_chat_model
from .paper_ingest import LangChainPaperDraftExtractor, PaperIngestPipeline
from .review_models import EvidenceCard, PromotionItem, PromotionManifest, SourceRecord
from .review_storage import ReviewArtifactStore
from .skill_registry import SkillRegistry
from .staged_ingest import StagedPaperStore


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _atomic_yaml(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = yaml.safe_dump(
        dict(payload), allow_unicode=True, sort_keys=False, default_flow_style=False
    )
    temporary: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            delete=False,
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as handle:
            handle.write(rendered)
            temporary = Path(handle.name)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_error(exc: BaseException) -> str:
    message = f"{type(exc).__name__}: {exc}"
    for name in (
        "OPENAI_API_KEY",
        "HARNESS_FAST_API_KEY",
        "HARNESS_REASONING_API_KEY",
        "DEEPXIV_TOKEN",
        "SEMANTIC_SCHOLAR_API_KEY",
        "S2_API_KEY",
        "TAVILY_API_KEY",
        "GITHUB_TOKEN",
        "DEEPSEEK_API_KEY",
    ):
        secret = os.getenv(name, "")
        if secret:
            message = message.replace(secret, "[REDACTED]")
    return message[:1200]


class ReviewPromoter:
    """Run approved report-critical papers through the existing ingest Skill."""

    def __init__(
        self,
        settings: HarnessSettings,
        store: ReviewArtifactStore,
    ):
        self.settings = settings
        self.store = store
        self.registry = SkillRegistry(settings.skills_root)
        self.verify_skill = self.registry.get("verify-evidence")
        self.ingest_skill = self.registry.get("ingest-paper")

    def _candidate_run_path(self) -> Path:
        return self.store.delivery_root / "promotion-candidates.yaml"

    def _candidate_record(self, source: SourceRecord) -> dict[str, Any]:
        return {
            "candidate_id": source.source_id,
            "source": "arxiv" if source.arxiv_id else source.provider,
            "source_id": source.arxiv_id or source.source_id,
            "title": source.title,
            "authors": list(source.authors),
            "year": source.year,
            "venue": source.venue,
            "abstract": source.abstract,
            "paper_url": source.canonical_url,
            "pdf_url": source.pdf_url,
            "doi": source.doi,
            "local_pdf_path": source.local_path,
            "target_facets": list(source.target_facets),
            "review_state": "selected-for-ingest",
        }

    def _write_candidate_run(
        self,
        sources: Sequence[SourceRecord],
        existing: Optional[Mapping[str, Any]] = None,
    ) -> Path:
        path = self._candidate_run_path()
        payload = dict(existing or {})
        prior = {
            str(item.get("candidate_id")): item
            for item in payload.get("candidates") or []
            if isinstance(item, Mapping) and item.get("candidate_id")
        }
        for source in sources:
            prior.setdefault(source.source_id, self._candidate_record(source))
        payload.update(
            {
                "schema_version": "review-promotion-0.1",
                "run": {
                    "id": self.store.config.run_id,
                    "research_id": self.store.config.research_id,
                    "source": "review-promotion-manifest",
                },
                "candidates": [prior[key] for key in sorted(prior)],
            }
        )
        _atomic_yaml(path, payload)
        return path

    def _candidate(
        self, source: SourceRecord, candidate_run_path: Path
    ) -> IngestCandidate:
        if source.source_type != "paper":
            raise ValueError("only paper sources can enter PaperIngestDraft promotion")
        if not source.local_path:
            raise ValueError(f"paper {source.source_id} has no deep-read local PDF")
        pdf_path = (self.settings.repository_root / source.local_path).resolve()
        if not _is_within(pdf_path, self.settings.repository_root.resolve()) or not pdf_path.is_file():
            raise ValueError(f"paper PDF is unavailable or unsafe: {source.local_path}")
        if source.content_sha256 and _sha256(pdf_path) != source.content_sha256:
            raise ValueError(f"paper PDF hash changed since deep read: {source.source_id}")
        return IngestCandidate(
            candidate_id=source.source_id,
            title=source.title,
            source="arxiv" if source.arxiv_id else source.provider,
            source_id=source.arxiv_id or source.source_id,
            authors=source.authors,
            year=source.year,
            venue=source.venue,
            abstract=source.abstract,
            paper_url=source.canonical_url,
            pdf_url=source.pdf_url,
            doi=source.doi,
            local_pdf_path=source.local_path,
            target_facets=source.target_facets,
            search_run_path=candidate_run_path.relative_to(
                self.settings.repository_root
            ).as_posix(),
        )

    @staticmethod
    def _promotion_issues(
        item: PromotionItem,
        source: Optional[SourceRecord],
        cards: Mapping[str, EvidenceCard],
    ) -> tuple[str, ...]:
        issues = []
        if source is None:
            return ("source-missing",)
        if source.source_type != "paper":
            issues.append("not-a-paper")
        if not source.local_path:
            issues.append("local-pdf-missing")
        if not source.content_sha256:
            issues.append("source-hash-missing")
        if not item.evidence_card_ids:
            issues.append("promotion-has-no-evidence-card")
        selected_cards = [cards.get(card_id) for card_id in item.evidence_card_ids]
        if any(card is None for card in selected_cards):
            issues.append("evidence-card-missing")
        for card in (value for value in selected_cards if value is not None):
            if card.source_id != source.source_id:
                issues.append("evidence-card-source-mismatch")
            if source.content_sha256 and card.source_sha256 != source.content_sha256:
                issues.append("evidence-card-hash-mismatch")
        return tuple(dict.fromkeys(issues))

    def _verify_evidence_cards(
        self,
        *,
        item: PromotionItem,
        source: SourceRecord,
        cards: Mapping[str, EvidenceCard],
        verifier: LangChainEvidenceSemanticVerifier,
    ) -> dict[str, Any]:
        material = self.store.material(source.source_id)
        if material is None:
            raise ValueError(
                f"promotion source has no retained deep-read material: {source.source_id}"
            )
        if source.content_sha256 != material.sha256:
            raise ValueError(
                f"promotion source material hash changed: {source.source_id}"
            )
        selected = [cards[card_id] for card_id in item.evidence_card_ids]
        paper_id = "paper:promotion-" + hashlib.sha256(
            source.source_id.encode("utf-8")
        ).hexdigest()[:16]
        decision = verifier.verify_paper(
            skill=self.verify_skill,
            evidence_policy=self.ingest_skill.read_reference("evidence-policy.md"),
            paper_id=paper_id,
            source_contract={
                "source_id": source.source_id,
                "canonical_url": source.canonical_url,
                "local_path": source.local_path,
                "sha256": material.sha256,
                "page_count": material.page_count,
                "excerpt_pages": list(material.selected_pages),
            },
            entities=[
                {
                    "entity_id": card.card_id,
                    "statement": card.statement,
                    "source_url": card.source_url,
                    "source_version": card.source_version,
                    "source_sha256": card.source_sha256,
                    "attribution": card.attribution,
                    "evidence_type": card.evidence_type,
                    "method": card.method,
                    "model": card.model,
                    "benchmark": card.benchmark,
                    "task": card.task,
                    "context_length": card.context_length,
                    "metric": card.metric,
                    "value": card.value,
                    "unit": card.unit,
                    "conditions": card.conditions,
                    "locator": card.locator.model_dump(mode="json"),
                }
                for card in selected
            ],
            excerpt=material.text,
        )
        expected = {card.card_id for card in selected}
        returned = {value.entity_id for value in decision.decisions}
        if returned != expected:
            raise ValueError(
                "promotion verifier must decide every approved EvidenceCard exactly once"
            )
        failures = [
            value
            for value in decision.decisions
            if value.verdict != "supported"
            or not value.pdf_pages
            or (
                material.selected_pages
                and not set(value.pdf_pages) <= set(material.selected_pages)
            )
        ]
        verification_payload = {
            "schema_version": "0.1",
            "source_id": source.source_id,
            "paper_id_proposal": paper_id,
            "source_sha256": material.sha256,
            "skill": self.verify_skill.name,
            "decisions": [
                value.model_dump(mode="json") for value in decision.decisions
            ],
            "passed": not failures,
        }
        verification_path = self.store.write_promotion_verification(
            source.source_id, verification_payload
        )
        if failures:
            summary = ", ".join(
                f"{value.entity_id}:{value.verdict}" for value in failures
            )
            raise ValueError(
                "approved promotion evidence did not pass independent source "
                f"verification: {summary}"
            )
        return {
            "path": verification_path,
            "decisions": len(decision.decisions),
            "model_calls": 1,
        }

    def preview(self, manifest_path: Optional[Path] = None) -> dict[str, Any]:
        manifest = self.store.load_promotion_manifest(manifest_path)
        sources = {item.source_id: item for item in self.store.sources()}
        cards = {item.card_id: item for item in self.store.cards()}
        approved = [
            item for item in manifest.items if item.approved or item.status == "approved"
        ]
        rows = []
        for item in manifest.items:
            source = sources.get(item.source_id)
            issues = self._promotion_issues(item, source, cards)
            rows.append(
                {
                    "source_id": item.source_id,
                    "approved": bool(item.approved or item.status == "approved"),
                    "status": item.status,
                    "ready": not issues,
                    "issues": list(issues),
                    "local_pdf_path": source.local_path if source else None,
                    "evidence_card_ids": list(item.evidence_card_ids),
                }
            )
        return {
            "research_id": manifest.research_id,
            "run_id": manifest.run_id,
            "execute": False,
            "suggested_items": len(manifest.items),
            "approved_items": len(approved),
            "items": rows,
            "wiki_changed": False,
            "verification_skill": self.verify_skill.name,
        }

    def execute(
        self,
        *,
        manifest_path: Optional[Path] = None,
        allow_network: bool,
    ) -> dict[str, Any]:
        if not allow_network:
            raise ValueError("review promotion requires explicit --allow-network")
        manifest = self.store.load_promotion_manifest(manifest_path)
        approved = [
            item for item in manifest.items if item.approved or item.status == "approved"
        ]
        if not approved:
            raise ValueError(
                "No promotion item is approved. Edit promotion-manifest.yaml and set "
                "approved: true before --execute"
            )
        if len(approved) > manifest.max_promotions:
            raise ValueError("approved promotion items exceed max_promotions")
        sources = {item.source_id: item for item in self.store.sources()}
        cards = {item.card_id: item for item in self.store.cards()}
        approved_sources = []
        for item in approved:
            source = sources.get(item.source_id)
            issues = self._promotion_issues(item, source, cards)
            if issues:
                raise ValueError(
                    f"promotion source {item.source_id} failed evidence-boundary "
                    f"verification: {', '.join(issues)}"
                )
            assert source is not None
            approved_sources.append(source)
        candidate_path = self._write_candidate_run(approved_sources)
        bundle = ReviewModelBundle.from_env(
            self.settings,
            allow_single_model_fallback=self.store.config.allow_single_model_fallback,
            require_reasoning=self.store.config.profile != "smoke",
        )
        if (
            self.store.config.model_fingerprint
            and self.store.config.model_fingerprint != bundle.fingerprint
        ):
            raise ValueError(
                "review promotion requires the same recorded model profiles"
            )
        reasoning_model = create_profile_chat_model(bundle.reasoning)
        extractor = LangChainPaperDraftExtractor(reasoning_model)
        verifier = LangChainEvidenceSemanticVerifier(reasoning_model)
        stage_store = StagedPaperStore(
            self.settings.repository_root,
            self.store.working_root / "artifacts" / "staged",
        )
        pipeline = PaperIngestPipeline(
            self.settings,
            extractor=extractor,
            stage_store=stage_store,
        )
        updated_by_id = {item.source_id: item for item in manifest.items}
        results = []
        for item, source in zip(approved, approved_sources):
            try:
                verification = self._verify_evidence_cards(
                    item=item,
                    source=source,
                    cards=cards,
                    verifier=verifier,
                )
                candidate = self._candidate(source, candidate_path)
                result = pipeline.ingest(
                    candidate,
                    defer_wiki=True,
                    validate_deferred=True,
                    research_id=manifest.research_id,
                )
                updated_by_id[item.source_id] = item.model_copy(
                    update={
                        "approved": True,
                        "status": "staged",
                        "stage_id": result.stage_id,
                        "error": None,
                    }
                )
                results.append(
                    {
                        "source_id": item.source_id,
                        "status": "staged",
                        "stage_id": result.stage_id,
                        "paper_id_proposal": result.paper_id,
                        "model_calls": result.model_calls + verification["model_calls"],
                        "verification": {
                            "status": "evidence-and-shadow-schema-passed",
                            "artifact": verification["path"],
                            "decisions": verification["decisions"],
                        },
                    }
                )
                candidate_payload = yaml.safe_load(
                    candidate_path.read_text(encoding="utf-8-sig")
                )
                for record in candidate_payload.get("candidates") or []:
                    if record.get("candidate_id") == item.source_id:
                        record["review_state"] = "staged-for-wiki"
                        record["staged_ingest"] = {
                            "stage_id": result.stage_id,
                            "staged_path": result.staged_path,
                            "paper_id_proposal": result.paper_id,
                            "verification_artifact": verification["path"],
                        }
                _atomic_yaml(candidate_path, candidate_payload)
            except Exception as exc:
                safe_error = _safe_error(exc)
                updated_by_id[item.source_id] = item.model_copy(
                    update={
                        "approved": True,
                        "status": "failed",
                        "error": safe_error,
                    }
                )
                results.append(
                    {
                        "source_id": item.source_id,
                        "status": "failed",
                        "error": safe_error,
                    }
                )
        updated = manifest.model_copy(
            update={
                "items": tuple(
                    updated_by_id[item.source_id] for item in manifest.items
                )
            }
        )
        self.store.write_promotion_manifest(updated)
        return {
            "research_id": manifest.research_id,
            "run_id": manifest.run_id,
            "execute": True,
            "papers_processed": len(results),
            "staged": sum(item["status"] == "staged" for item in results),
            "failed": sum(item["status"] == "failed" for item in results),
            "wiki_changed": False,
            "verification_skill": self.verify_skill.name,
            "staging_root": self.store.relative(stage_store.root),
            "results": results,
        }


__all__ = ["ReviewPromoter"]
