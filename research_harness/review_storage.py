"""Atomic artifact storage and research-scope loading for review runs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence, Type, TypeVar

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel

from .config import HarnessSettings
from .review_models import (
    EvidenceCard,
    DiscoveryRecord,
    NonConsensusAssessment,
    PromotionManifest,
    QueryPlan,
    ResearchUncertainty,
    RetrievalQuery,
    ReviewCoverageMatrix,
    ReviewErrorEvent,
    ReviewGap,
    ReviewReadiness,
    ReviewRunConfig,
    ReviewScope,
    ReviewSynthesisDraft,
    SourceMaterial,
    SourceRecord,
    SourceScreening,
    SourceSkim,
    TrajectoryEvent,
    UnderstandingClaim,
)
from .progress import read_progress
from .text_normalization import normalize_data, normalize_text


T = TypeVar("T", bound=BaseModel)
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,199}$")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
            handle.write(normalize_text(content))
            temporary = Path(handle.name)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _json_payload(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _json_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_payload(item) for item in value]
    return value


def _atomic_json(path: Path, value: Any) -> None:
    rendered = json.dumps(
        normalize_data(_json_payload(value)), ensure_ascii=False, indent=2, sort_keys=True
    ) + "\n"
    _atomic_text(path, rendered)


def _atomic_yaml(path: Path, value: Any) -> None:
    rendered = yaml.safe_dump(
        normalize_data(_json_payload(value)),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    _atomic_text(path, rendered)


def _atomic_jsonl(path: Path, values: Iterable[Any]) -> None:
    lines = [
        json.dumps(normalize_data(_json_payload(item)), ensure_ascii=False, sort_keys=True)
        for item in values
    ]
    _atomic_text(path, "\n".join(lines) + ("\n" if lines else ""))


def _read_jsonl(path: Path, model: Type[T]) -> tuple[T, ...]:
    if not path.is_file():
        return ()
    result = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            result.append(model.model_validate(json.loads(line)))
        except Exception as exc:
            raise ValueError(f"Invalid {path.name} line {line_number}: {exc}") from exc
    return tuple(result)


def _frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    match = re.match(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n", text, re.DOTALL)
    if not match:
        raise ValueError("scope.md has malformed YAML frontmatter")
    payload = yaml.safe_load(match.group(1)) or {}
    if not isinstance(payload, dict):
        raise ValueError("scope.md frontmatter must be a mapping")
    return payload, text[match.end() :]


def _section(body: str, heading: str) -> str:
    pattern = re.compile(
        rf"^##\s+{re.escape(heading)}\s*$\r?\n(.*?)(?=^##\s+|\Z)",
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    match = pattern.search(body)
    return match.group(1).strip() if match else ""


def _bullets(text: str) -> tuple[str, ...]:
    values = []
    for line in text.splitlines():
        match = re.match(r"^\s*(?:[-*]|\d+[.)])\s+(.*\S)\s*$", line)
        if match:
            values.append(match.group(1).strip())
    return tuple(dict.fromkeys(values))


def load_review_scope(settings: HarnessSettings, research_id: str) -> ReviewScope:
    """Load the human-authored scope without consulting the Wiki graph."""

    if not SAFE_NAME.fullmatch(research_id):
        raise ValueError("research_id must use safe ASCII filename characters")
    root = (settings.research_root / research_id).resolve()
    if not _is_within(root, settings.research_root.resolve()) or not root.is_dir():
        raise FileNotFoundError(f"Research directory not found: {root}")
    scope_path = root / "scope.md"
    if not scope_path.is_file():
        raise FileNotFoundError(f"Review scope file not found: {scope_path}")
    metadata, body = _frontmatter(scope_path.read_text(encoding="utf-8-sig"))
    title = str(metadata.get("title") or "").strip()
    if not title:
        heading = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
        title = heading.group(1).strip() if heading else research_id
    primary = _section(body, "Primary question")
    question = next(
        (line.strip() for line in primary.splitlines() if line.strip()), ""
    )
    if not question:
        raise ValueError("scope.md must define a Primary question")
    included = _bullets(_section(body, "Core scope"))
    boundary = _bullets(_section(body, "Boundary scope"))
    excluded = _bullets(_section(body, "Exclusions"))
    hypotheses = _bullets(_section(body, "Candidate non-consensus hypotheses"))
    criteria_path = root / "done-criteria.yaml"
    required_facets: tuple[str, ...] = ()
    if criteria_path.is_file():
        criteria = yaml.safe_load(criteria_path.read_text(encoding="utf-8-sig")) or {}
        facets = criteria.get("facet_requirements") if isinstance(criteria, dict) else {}
        if isinstance(facets, dict):
            required_facets = tuple(str(item) for item in facets)
    if not required_facets:
        raise ValueError("review scope requires facet_requirements in done-criteria.yaml")
    seed_queries = []
    search_root = root / "search-runs"
    if search_root.is_dir():
        for path in sorted(search_root.glob("*.yaml")):
            payload = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
            if not isinstance(payload, dict):
                continue
            for query in payload.get("queries") or []:
                if not isinstance(query, Mapping):
                    continue
                value = " ".join(str(query.get("text") or "").split())
                if value and value not in seed_queries:
                    seed_queries.append(value)
    return ReviewScope(
        research_id=research_id,
        title=title,
        question=question,
        required_facets=required_facets,
        included_concepts=tuple(dict.fromkeys((*included, *boundary))),
        excluded_concepts=excluded,
        candidate_hypotheses=hypotheses,
        seed_queries=tuple(seed_queries[:12]),
    )


def load_review_seed_sources(
    settings: HarnessSettings,
    research_id: str,
    manifest_path: Path,
) -> tuple[SourceRecord, ...]:
    """Load a curated, exact-identity paper seed manifest for a review run."""

    path = (
        manifest_path
        if manifest_path.is_absolute()
        else settings.repository_root / manifest_path
    ).resolve()
    if not _is_within(path, settings.repository_root.resolve()):
        raise ValueError("review seed manifest must stay inside the repository")
    if not path.is_file() or path.suffix.casefold() not in {".yaml", ".yml"}:
        raise FileNotFoundError(f"Review seed manifest not found: {path}")
    payload = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    if not isinstance(payload, Mapping):
        raise ValueError("review seed manifest must contain a YAML mapping")
    if payload.get("schema_version") != "review-seeds-0.1":
        raise ValueError("review seed manifest schema_version must be review-seeds-0.1")
    if str(payload.get("research_id") or "").strip() != research_id:
        raise ValueError("review seed manifest research_id does not match the run")
    curated_at = str(payload.get("curated_at") or "").strip()
    curated_year_match = re.match(r"^(\d{4})-", curated_at)
    if not curated_year_match:
        raise ValueError("review seed manifest requires an ISO curated_at timestamp")
    curated_year = int(curated_year_match.group(1))
    rows = payload.get("sources") or []
    if not isinstance(rows, list) or not rows:
        raise ValueError("review seed manifest requires at least one source")
    result = []
    for rank, row in enumerate(rows, start=1):
        if not isinstance(row, Mapping):
            raise ValueError(f"review seed source {rank} must be a mapping")
        arxiv_id = str(row.get("arxiv_id") or "").strip()
        if not re.fullmatch(r"\d{4}\.\d{4,5}", arxiv_id):
            raise ValueError(f"review seed source {rank} has an invalid arXiv ID")
        title = " ".join(str(row.get("title") or "").split())
        if not title:
            raise ValueError(f"review seed source {rank} requires a title")
        year = int(row.get("year") or 0)
        if year < curated_year - 2 or year > curated_year:
            raise ValueError(
                f"review seed {arxiv_id} must be from the curation year or prior two years"
            )
        authors = tuple(
            " ".join(str(item).split())
            for item in (row.get("authors") or [])
            if str(item).strip()
        )
        if not authors:
            raise ValueError(f"review seed {arxiv_id} requires authors")
        source_role = str(row.get("source_role") or "primary-study").strip()
        if source_role not in {
            "survey",
            "primary-study",
            "benchmark",
            "reproduction",
            "background",
        }:
            raise ValueError(f"review seed {arxiv_id} has an invalid source_role")
        rationale = " ".join(str(row.get("rationale") or "").split())
        if not rationale:
            raise ValueError(f"review seed {arxiv_id} requires a rationale")
        target_facets = tuple(
            dict.fromkeys(
                " ".join(str(item).split())
                for item in (row.get("target_facets") or [])
                if str(item).strip()
            )
        )
        result.append(
            SourceRecord(
                source_id=f"paper:arxiv:{arxiv_id}",
                source_type="paper",
                provider="manual",
                title=title,
                canonical_url=f"https://arxiv.org/abs/{arxiv_id}",
                authors=authors,
                year=year,
                venue=(str(row.get("venue") or "").strip() or None),
                snippet=rationale,
                arxiv_id=arxiv_id,
                pdf_url=f"https://arxiv.org/pdf/{arxiv_id}",
                repository=(str(row.get("repository") or "").strip() or None),
                version="arxiv-latest-at-seed-run",
                target_facets=target_facets,
                discoveries=(
                    DiscoveryRecord(
                        query_id=f"SEED{rank:02d}",
                        provider="manual",
                        rank=rank,
                        retrieved_at=curated_at,
                    ),
                ),
                metadata={
                    "review_seed": True,
                    "source_role": source_role,
                    "selection_rationale": rationale,
                },
            )
        )
    identifiers = [item.source_id for item in result]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("review seed manifest contains duplicate arXiv IDs")
    return tuple(sorted(result, key=lambda item: item.source_id))


class ReviewArtifactStore:
    """Own working artifacts and the final review bundle for one run."""

    def __init__(self, settings: HarnessSettings, config: ReviewRunConfig):
        if not SAFE_NAME.fullmatch(config.run_id):
            raise ValueError("review run_id must use safe ASCII filename characters")
        self.settings = settings
        self.config = config
        self.working_root = (
            settings.repository_root / ".harness" / "review-runs" / config.run_id
        ).resolve()
        if not _is_within(self.working_root, settings.repository_root.resolve()):
            raise ValueError("review working root escapes the repository")
        self.delivery_root = (
            self.working_root / "deliverables"
            if config.canary
            else settings.research_root
            / config.research_id
            / "reviews"
            / config.run_id
        ).resolve()
        allowed_delivery_root = (
            self.working_root if config.canary else settings.research_root.resolve()
        )
        if not _is_within(self.delivery_root, allowed_delivery_root):
            raise ValueError("review delivery root escapes its allowed root")

    @property
    def config_path(self) -> Path:
        return self.working_root / "run-config.yaml"

    @property
    def sources_path(self) -> Path:
        return self.delivery_root / "source-manifest.jsonl"

    @property
    def skims_path(self) -> Path:
        return self.delivery_root / "source-skims.jsonl"

    @property
    def cards_path(self) -> Path:
        return self.delivery_root / "evidence-cards.jsonl"

    @property
    def assessments_path(self) -> Path:
        return self.delivery_root / "nonconsensus-assessments.yaml"

    @property
    def trajectory_path(self) -> Path:
        return self.delivery_root / "trajectory-summary.jsonl"

    @property
    def promotion_path(self) -> Path:
        return self.delivery_root / "promotion-manifest.yaml"

    @property
    def technology_map_path(self) -> Path:
        return self.delivery_root / "technology-map.yaml"

    @property
    def coverage_path(self) -> Path:
        return self.delivery_root / "coverage-matrix.yaml"

    @property
    def gaps_path(self) -> Path:
        return self.delivery_root / "research-gaps.yaml"

    @property
    def report_path(self) -> Path:
        return self.delivery_root / "review.md"

    @property
    def progress_path(self) -> Path:
        return self._internal("progress.json")

    @property
    def synthesis_path(self) -> Path:
        return self._internal("synthesis-draft.json")

    def progress(self) -> Optional[dict[str, Any]]:
        return read_progress(self.progress_path)

    def initialize(self) -> None:
        self.working_root.mkdir(parents=True, exist_ok=True)
        self.delivery_root.mkdir(parents=True, exist_ok=True)
        if self.config_path.is_file():
            existing = ReviewRunConfig.model_validate(
                yaml.safe_load(self.config_path.read_text(encoding="utf-8-sig"))
            )
            if existing != self.config:
                raise ValueError("review run_id already exists with a different config")
        else:
            _atomic_yaml(self.config_path, self.config)
        _atomic_yaml(self.delivery_root / "run-config.yaml", self.config)

    @classmethod
    def load_config(
        cls, settings: HarnessSettings, run_id: str
    ) -> ReviewRunConfig:
        if not SAFE_NAME.fullmatch(run_id):
            raise ValueError("review run_id must use safe ASCII filename characters")
        path = settings.repository_root / ".harness" / "review-runs" / run_id / "run-config.yaml"
        if not path.is_file():
            raise FileNotFoundError(f"Review run config not found: {path}")
        return ReviewRunConfig.model_validate(
            yaml.safe_load(path.read_text(encoding="utf-8-sig"))
        )

    def _internal(self, name: str) -> Path:
        return self.working_root / "state" / name

    def sources(self) -> tuple[SourceRecord, ...]:
        return _read_jsonl(self.sources_path, SourceRecord)

    def write_sources(self, values: Sequence[SourceRecord]) -> None:
        _atomic_jsonl(self.sources_path, sorted(values, key=lambda item: item.source_id))

    def queries(self) -> tuple[RetrievalQuery, ...]:
        return _read_jsonl(self._internal("queries.jsonl"), RetrievalQuery)

    def write_queries(self, values: Sequence[RetrievalQuery]) -> None:
        _atomic_jsonl(
            self._internal("queries.jsonl"),
            sorted(values, key=lambda item: (item.round, item.id)),
        )

    def write_query_plan(self, plan: QueryPlan) -> None:
        _atomic_json(self._internal(f"query-plan-round-{plan.queries[0].round}.json"), plan)

    def screenings(self) -> tuple[SourceScreening, ...]:
        return _read_jsonl(self._internal("screenings.jsonl"), SourceScreening)

    def write_screenings(self, values: Sequence[SourceScreening]) -> None:
        _atomic_jsonl(
            self._internal("screenings.jsonl"),
            sorted(values, key=lambda item: item.source_id),
        )

    def skims(self) -> tuple[SourceSkim, ...]:
        return _read_jsonl(self.skims_path, SourceSkim)

    def write_skims(self, values: Sequence[SourceSkim]) -> None:
        _atomic_jsonl(self.skims_path, sorted(values, key=lambda item: item.source_id))

    def cards(self) -> tuple[EvidenceCard, ...]:
        return _read_jsonl(self.cards_path, EvidenceCard)

    def write_cards(self, values: Sequence[EvidenceCard]) -> None:
        _atomic_jsonl(self.cards_path, sorted(values, key=lambda item: item.card_id))

    def claims(self) -> tuple[UnderstandingClaim, ...]:
        return _read_jsonl(self._internal("understanding-claims.jsonl"), UnderstandingClaim)

    def write_claims(self, values: Sequence[UnderstandingClaim]) -> None:
        _atomic_jsonl(
            self._internal("understanding-claims.jsonl"),
            sorted(values, key=lambda item: item.claim_id),
        )

    def uncertainties(self) -> tuple[ResearchUncertainty, ...]:
        return _read_jsonl(self._internal("uncertainties.jsonl"), ResearchUncertainty)

    def write_uncertainties(self, values: Sequence[ResearchUncertainty]) -> None:
        _atomic_jsonl(
            self._internal("uncertainties.jsonl"),
            sorted(values, key=lambda item: item.uncertainty_id),
        )

    def assessments(self) -> tuple[NonConsensusAssessment, ...]:
        if not self.assessments_path.is_file():
            return ()
        payload = yaml.safe_load(self.assessments_path.read_text(encoding="utf-8-sig")) or []
        if not isinstance(payload, list):
            raise ValueError("nonconsensus-assessments.yaml must contain a list")
        return tuple(NonConsensusAssessment.model_validate(item) for item in payload)

    def write_assessments(self, values: Sequence[NonConsensusAssessment]) -> None:
        _atomic_yaml(
            self.assessments_path,
            [item.model_dump(mode="json") for item in sorted(values, key=lambda item: item.assessment_id)],
        )

    def trajectory(self) -> tuple[TrajectoryEvent, ...]:
        return _read_jsonl(self.trajectory_path, TrajectoryEvent)

    def append_trajectory(self, event: TrajectoryEvent) -> None:
        current = list(self.trajectory())
        if any(item.sequence == event.sequence for item in current):
            current = [item for item in current if item.sequence != event.sequence]
        current.append(event)
        _atomic_jsonl(self.trajectory_path, sorted(current, key=lambda item: item.sequence))

    def round_gains(self) -> tuple[dict[str, Any], ...]:
        path = self._internal("round-gains.jsonl")
        if not path.is_file():
            return ()
        return tuple(
            json.loads(line)
            for line in path.read_text(encoding="utf-8-sig").splitlines()
            if line.strip()
        )

    def write_round_gains(self, values: Sequence[Mapping[str, Any]]) -> None:
        _atomic_jsonl(self._internal("round-gains.jsonl"), values)

    def write_readiness(self, readiness: ReviewReadiness) -> None:
        _atomic_json(self._internal("readiness.json"), readiness)

    def readiness(self) -> Optional[ReviewReadiness]:
        path = self._internal("readiness.json")
        if not path.is_file():
            return None
        return ReviewReadiness.model_validate(json.loads(path.read_text(encoding="utf-8-sig")))

    def write_synthesis_draft(self, draft: ReviewSynthesisDraft) -> None:
        _atomic_json(self.synthesis_path, draft)

    def synthesis_draft(self) -> Optional[ReviewSynthesisDraft]:
        if not self.synthesis_path.is_file():
            return None
        return ReviewSynthesisDraft.model_validate(
            json.loads(self.synthesis_path.read_text(encoding="utf-8-sig"))
        )

    def write_report(self, content: str) -> None:
        _atomic_text(self.report_path, content.rstrip() + "\n")

    def write_technology_map(self, payload: Mapping[str, Any]) -> None:
        _atomic_yaml(self.technology_map_path, payload)

    def technology_map(self) -> dict[str, Any]:
        if not self.technology_map_path.is_file():
            return {}
        payload = yaml.safe_load(
            self.technology_map_path.read_text(encoding="utf-8-sig")
        ) or {}
        if not isinstance(payload, dict):
            raise ValueError("technology-map.yaml must contain a mapping")
        return payload

    def write_coverage(self, coverage: ReviewCoverageMatrix) -> None:
        _atomic_yaml(self.coverage_path, coverage)

    def coverage(self) -> Optional[ReviewCoverageMatrix]:
        if not self.coverage_path.is_file():
            return None
        return ReviewCoverageMatrix.model_validate(
            yaml.safe_load(self.coverage_path.read_text(encoding="utf-8-sig")) or {}
        )

    def write_gaps(self, gaps: Sequence[ReviewGap]) -> None:
        _atomic_yaml(self.gaps_path, [item.model_dump(mode="json") for item in gaps])

    def gaps(self) -> tuple[ReviewGap, ...]:
        if not self.gaps_path.is_file():
            return ()
        payload = yaml.safe_load(self.gaps_path.read_text(encoding="utf-8-sig")) or []
        if not isinstance(payload, list):
            raise ValueError("research-gaps.yaml must contain a list")
        return tuple(ReviewGap.model_validate(item) for item in payload)

    def write_promotion_manifest(self, manifest: PromotionManifest) -> None:
        _atomic_yaml(self.promotion_path, manifest)

    def load_promotion_manifest(self, path: Optional[Path] = None) -> PromotionManifest:
        selected = (path or self.promotion_path).resolve()
        if not selected.is_file():
            raise FileNotFoundError(f"Promotion manifest not found: {selected}")
        manifest = PromotionManifest.model_validate(
            yaml.safe_load(selected.read_text(encoding="utf-8-sig"))
        )
        if manifest.research_id != self.config.research_id:
            raise ValueError("promotion manifest research_id does not match the review run")
        if manifest.run_id != self.config.run_id:
            raise ValueError("promotion manifest run_id does not match the review run")
        if manifest.max_promotions > self.config.max_promotions:
            raise ValueError("promotion manifest exceeds the review run promotion budget")
        return manifest

    def error_events(self) -> tuple[ReviewErrorEvent, ...]:
        return _read_jsonl(self._internal("errors.jsonl"), ReviewErrorEvent)

    def append_error(self, event: ReviewErrorEvent) -> None:
        current = list(self.error_events())
        current.append(event)
        _atomic_jsonl(self._internal("errors.jsonl"), current)

    def material_path(self, source_id: str) -> Path:
        label = re.sub(r"[^A-Za-z0-9_.-]+", "-", source_id).strip("-.")[:60]
        suffix = hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:12]
        return self.working_root / "materials" / f"{label or 'source'}-{suffix}.json"

    def write_material(self, material: SourceMaterial) -> None:
        _atomic_json(self.material_path(material.source_id), material)

    def material(self, source_id: str) -> Optional[SourceMaterial]:
        path = self.material_path(source_id)
        if not path.is_file():
            return None
        return SourceMaterial.model_validate(json.loads(path.read_text(encoding="utf-8-sig")))

    def deep_read_completed(self) -> tuple[str, ...]:
        path = self._internal("deep-read-completed.json")
        if not path.is_file():
            return ()
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, list):
            raise ValueError("deep-read-completed.json must contain a list")
        return tuple(str(item) for item in payload)

    def mark_deep_read_completed(self, source_id: str) -> None:
        values = tuple(dict.fromkeys((*self.deep_read_completed(), source_id)))
        _atomic_json(self._internal("deep-read-completed.json"), sorted(values))

    def write_promotion_verification(
        self, source_id: str, payload: Mapping[str, Any]
    ) -> str:
        label = re.sub(r"[^A-Za-z0-9_.-]+", "-", source_id).strip("-.")[:60]
        suffix = hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:12]
        path = self._internal("promotion-verification") / (
            f"{label or 'source'}-{suffix}.json"
        )
        _atomic_json(path, payload)
        return self.relative(path)

    def relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.settings.repository_root.resolve()).as_posix()
        except ValueError:
            return str(path.resolve())


__all__ = [
    "ReviewArtifactStore",
    "load_review_scope",
]
