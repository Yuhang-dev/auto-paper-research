"""Skill-driven evidence verification with deterministic lifecycle gates."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Literal,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
)

import yaml  # type: ignore[import-untyped]
from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tools.wiki.indexer import WikiIndex, build_index
from tools.wiki.models import Entity, listify
from tools.wiki.writer import WikiSourceWriter, render_wiki_page

from .config import HarnessSettings
from .ingest_models import PaperDocument
from .paper_ingest import extract_pdf_document
from .research_models import NonConsensusResult, ResearchGap, ResearchSnapshot
from .skill_registry import SkillRegistry, SkillSpec


VerificationVerdict = Literal["supported", "contradicted", "insufficient"]
ClaimAssessment = Literal["open", "supported", "contested", "refuted"]


class EvidenceVerificationError(RuntimeError):
    """Raised when a verification proposal violates a deterministic gate."""


class VerificationPreconditionError(EvidenceVerificationError):
    """Raised before a semantic verification attempt can safely begin."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EntityVerificationDecision(_StrictModel):
    entity_id: str = Field(min_length=3, max_length=220)
    verdict: VerificationVerdict
    rationale: str = Field(min_length=3, max_length=1_500)
    pdf_pages: Tuple[int, ...] = Field(default=(), max_length=24)
    claim_assessment: Optional[ClaimAssessment] = None

    @field_validator("pdf_pages")
    @classmethod
    def _unique_pages(cls, value: Tuple[int, ...]) -> Tuple[int, ...]:
        if any(page < 1 for page in value):
            raise ValueError("pdf_pages must contain positive viewer page numbers")
        return tuple(dict.fromkeys(value))

    @model_validator(mode="after")
    def _claim_fields_match_id(self) -> "EntityVerificationDecision":
        if self.claim_assessment is not None and not self.entity_id.startswith(
            "claim:"
        ):
            raise ValueError("claim_assessment is allowed only for a claim entity")
        return self


class PaperVerificationDraft(_StrictModel):
    paper_id: str = Field(pattern=r"^paper:[a-z0-9][a-z0-9-]*$")
    decisions: Tuple[EntityVerificationDecision, ...] = Field(
        min_length=1, max_length=80
    )

    @model_validator(mode="after")
    def _unique_entities(self) -> "PaperVerificationDraft":
        values = [item.entity_id for item in self.decisions]
        if len(values) != len(set(values)):
            raise ValueError("decisions cannot contain duplicate entity IDs")
        return self


class AssessmentVerificationDraft(_StrictModel):
    assessment_id: str = Field(pattern=r"^assessment:[a-z0-9][a-z0-9-]*$")
    verdict: VerificationVerdict
    confirmed_result: NonConsensusResult
    rationale: str = Field(min_length=3, max_length=2_000)
    claim_ids: Tuple[str, ...] = Field(min_length=1)
    evidence_ids: Tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_ids(self) -> "AssessmentVerificationDraft":
        if any(not value.startswith("claim:") for value in self.claim_ids):
            raise ValueError("claim_ids must use canonical claim: IDs")
        if any(not value.startswith("experiment:") for value in self.evidence_ids):
            raise ValueError("evidence_ids must use canonical experiment: IDs")
        if len(self.claim_ids) != len(set(self.claim_ids)):
            raise ValueError("claim_ids cannot contain duplicates")
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("evidence_ids cannot contain duplicates")
        return self


class EvidenceSemanticVerifier(Protocol):
    requires_network: bool

    def verify_paper(
        self,
        *,
        skill: SkillSpec,
        evidence_policy: str,
        paper_id: str,
        source_contract: Mapping[str, Any],
        entities: Sequence[Mapping[str, Any]],
        excerpt: str,
    ) -> PaperVerificationDraft: ...

    def verify_assessment(
        self,
        *,
        skill: SkillSpec,
        assessment: Mapping[str, Any],
        claims: Sequence[Mapping[str, Any]],
        experiments: Sequence[Mapping[str, Any]],
    ) -> AssessmentVerificationDraft: ...


class LangChainEvidenceSemanticVerifier:
    """Use a chat model only for bounded source-to-record comparisons."""

    requires_network = True

    def __init__(self, model: BaseChatModel):
        self.model = model

    @staticmethod
    def _system(skill: SkillSpec, contract: str) -> SystemMessage:
        return SystemMessage(
            content=(
                "You execute the repository-local verify-evidence Skill. Return only "
                "the requested structured verification object. Do not rewrite Wiki "
                "pages, invent page numbers, or treat plausibility as verification. "
                "For quantitative experiments, compare the recorded result together "
                "with model, benchmark, metric, context, sparsity, baseline, and units.\n\n"
                f"SKILL INSTRUCTIONS\n{skill.instructions}\n\n"
                f"VERIFICATION CONTRACT\n{contract}"
            )
        )

    def verify_paper(
        self,
        *,
        skill: SkillSpec,
        evidence_policy: str,
        paper_id: str,
        source_contract: Mapping[str, Any],
        entities: Sequence[Mapping[str, Any]],
        excerpt: str,
    ) -> PaperVerificationDraft:
        contract = skill.read_reference("verification-contract.md")
        structured = self.model.with_structured_output(
            PaperVerificationDraft,
            method="json_mode",
        )
        human = HumanMessage(
            content=(
                json.dumps(
                    {
                        "task": "Verify every supplied entity exactly once.",
                        "paper_id": paper_id,
                        "source_contract": source_contract,
                        "entities": list(entities),
                        "constraints": {
                            "return_exact_entity_ids": True,
                            "supported_requires_source_pages": True,
                        },
                        "output_json_schema": PaperVerificationDraft.model_json_schema(),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n\nEVIDENCE POLICY\n"
                + evidence_policy
                + "\n\nPAGE-AWARE SOURCE EXCERPT\n"
                + excerpt
            )
        )
        result = structured.invoke([self._system(skill, contract), human])
        if isinstance(result, PaperVerificationDraft):
            return result
        return PaperVerificationDraft.model_validate(result)

    def verify_assessment(
        self,
        *,
        skill: SkillSpec,
        assessment: Mapping[str, Any],
        claims: Sequence[Mapping[str, Any]],
        experiments: Sequence[Mapping[str, Any]],
    ) -> AssessmentVerificationDraft:
        contract = skill.read_reference("verification-contract.md")
        structured = self.model.with_structured_output(
            AssessmentVerificationDraft,
            method="json_mode",
        )
        human = HumanMessage(
            content=json.dumps(
                {
                    "task": (
                        "Independently verify whether the assessment result follows from "
                        "the supplied verified claims and experiments under aligned conditions."
                    ),
                    "assessment": assessment,
                    "claims": list(claims),
                    "experiments": list(experiments),
                    "output_json_schema": AssessmentVerificationDraft.model_json_schema(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        result = structured.invoke([self._system(skill, contract), human])
        if isinstance(result, AssessmentVerificationDraft):
            return result
        return AssessmentVerificationDraft.model_validate(result)


@dataclass(frozen=True)
class EvidenceVerificationResult:
    target_id: str
    target_kind: Literal["paper-bundle", "assessment"]
    status: Literal["published", "no-change"]
    verified_entity_ids: Tuple[str, ...]
    unresolved_entity_ids: Tuple[str, ...]
    changed_paths: Tuple[str, ...]
    diagnostic_codes: Tuple[str, ...]
    model_calls: int


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _load_yaml(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as handle:
        payload = yaml.safe_load(handle)
    if not isinstance(payload, dict):
        raise EvidenceVerificationError(f"Expected YAML mapping in {path}")
    return payload


def _entity_record(entity: Entity, *, body_chars: int = 6_000) -> Dict[str, Any]:
    body = entity.body.strip()
    if len(body) > body_chars:
        body = body[:body_chars] + "\n[body truncated]"
    return {
        "id": entity.entity_id,
        "type": entity.entity_type,
        "path": entity.relative_path,
        "metadata": entity.metadata,
        "body": body,
    }


def _experiment_pdf_page(entity: Entity) -> Optional[int]:
    evidence = entity.metadata.get("evidence")
    if not isinstance(evidence, Mapping):
        return None
    for key in ("pdf_page", "page"):
        value = evidence.get(key)
        if isinstance(value, int) and value > 0:
            return value
    locator = str(evidence.get("locator") or "")
    match = re.search(r"(?:PDF\s*)?p(?:age)?\.?\s*(\d+)", locator, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _numeric_value_visible(entity: Entity, document: PaperDocument, page: int) -> bool:
    result = entity.metadata.get("result")
    if not isinstance(result, Mapping) or result.get("value") in (None, ""):
        return False
    text = document.pages[page - 1].text
    value = result.get("value")
    candidates = {str(value).strip()}
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        candidates.add(f"{float(value):g}")
        candidates.add(f"{float(value):.1f}")
        candidates.add(f"{float(value):.2f}")
    compact = text.replace(",", "")
    return any(
        token
        and re.search(
            rf"(?<![\d.]){re.escape(token.replace(',', ''))}(?![\d.])",
            compact,
        )
        for token in candidates
    )


def _render_excerpt(
    document: PaperDocument,
    requested_pages: Iterable[int],
    *,
    max_chars: int = 90_000,
) -> Tuple[str, Tuple[int, ...]]:
    selected: set[int] = {1, min(2, len(document.pages))}
    selected.update(
        page for page in requested_pages if 1 <= page <= len(document.pages)
    )
    parts: list[str] = []
    retained: list[int] = []
    for page in sorted(selected):
        header = f"--- PDF p. {page} ---\n"
        text = document.pages[page - 1].text
        current = sum(len(item) + 1 for item in parts)
        available = max_chars - current - len(header)
        if available <= 64:
            break
        if len(text) > available:
            text = text[: max(0, available - 24)] + "\n[page text truncated]"
        parts.append(header + text)
        retained.append(page)
    return "\n".join(parts), tuple(retained)


class EvidenceVerificationPipeline:
    """Verify one paper bundle or assessment and publish guarded status updates."""

    def __init__(
        self,
        settings: HarnessSettings,
        *,
        verifier: Optional[EvidenceSemanticVerifier] = None,
        now: Optional[Callable[[], datetime]] = None,
    ):
        self.settings = settings
        self.registry = SkillRegistry(settings.skills_root)
        self.skill = self.registry.get("verify-evidence")
        self.ingest_skill = self.registry.get("ingest-paper")
        self.verifier = verifier or self._default_verifier(settings)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.writer = WikiSourceWriter(settings.wiki_root, settings.wiki_meta_root)

    @staticmethod
    def _default_verifier(settings: HarnessSettings) -> EvidenceSemanticVerifier:
        if not settings.model:
            raise VerificationPreconditionError(
                "Evidence verification needs an injected verifier or HARNESS_MODEL/--model."
            )
        return LangChainEvidenceSemanticVerifier(init_chat_model(settings.model))

    @property
    def requires_network(self) -> bool:
        return bool(self.verifier.requires_network)

    def _paper_sources(self, snapshot: ResearchSnapshot) -> Tuple[Tuple[str, str], ...]:
        root = self.settings.repository_root.resolve()
        records = []
        for relative in snapshot.corpus.search_run_paths:
            path = (root / relative).resolve()
            if not _is_within(path, root) or not path.is_file():
                continue
            run = _load_yaml(path)
            for candidate in run.get("candidates") or []:
                if not isinstance(candidate, Mapping):
                    continue
                ingest = candidate.get("ingest") or {}
                paper_id = str(ingest.get("paper_id") or "")
                local_pdf = str(candidate.get("local_pdf_path") or "")
                if (
                    candidate.get("review_state") == "ingested"
                    and paper_id
                    and local_pdf
                ):
                    records.append((paper_id, local_pdf))
        return tuple(dict.fromkeys(records))

    @staticmethod
    def _paper_closure(index: WikiIndex, paper_id: str) -> Tuple[Entity, ...]:
        unique = index.unique_entities()
        paper = unique.get(paper_id)
        if paper is None or paper.entity_type != "paper":
            return ()
        ids = {paper_id}
        experiments = [
            entity
            for entity in unique.values()
            if entity.entity_type == "experiment"
            and str(entity.metadata.get("paper") or "") == paper_id
        ]
        ids.update(entity.entity_id for entity in experiments if entity.entity_id)
        for experiment in experiments:
            for field in ("method", "model", "benchmark"):
                ids.update(
                    str(value) for value in listify(experiment.metadata.get(field))
                )
            relations = experiment.metadata.get("relations") or {}
            if isinstance(relations, Mapping):
                for relation in ("supports", "contradicts"):
                    ids.update(str(value) for value in listify(relations.get(relation)))
        paper_relations = paper.metadata.get("relations") or {}
        if isinstance(paper_relations, Mapping):
            for relation in ("proposes", "reports"):
                ids.update(
                    str(value) for value in listify(paper_relations.get(relation))
                )
        return tuple(unique[value] for value in sorted(ids) if value in unique)

    @staticmethod
    def _target_entities(closure: Sequence[Entity]) -> Tuple[Entity, ...]:
        return tuple(
            entity
            for entity in closure
            if entity.entity_id
            and entity.metadata.get("status") in {"candidate", "draft", "needs-review"}
        )

    def _paper_target(
        self, snapshot: ResearchSnapshot
    ) -> Tuple[str, Path, WikiIndex, Tuple[Entity, ...]]:
        index = build_index(self.settings.wiki_root, self.settings.wiki_meta_root)
        root = self.settings.repository_root.resolve()
        candidates = []
        for paper_id, relative_pdf in self._paper_sources(snapshot):
            closure = self._paper_closure(index, paper_id)
            targets = self._target_entities(closure)
            if not targets:
                continue
            path = (root / relative_pdf).resolve()
            if (
                not _is_within(path, root)
                or path.suffix.casefold() != ".pdf"
                or not path.is_file()
            ):
                continue
            candidates.append((-len(targets), paper_id, path, targets))
        if not candidates:
            raise VerificationPreconditionError(
                "No ingested paper bundle with an available local PDF needs verification."
            )
        _, paper_id, path, targets = sorted(candidates, key=lambda item: item[:2])[0]
        return paper_id, path, index, targets

    @staticmethod
    def _entity_precheck(
        entity: Entity,
        document: PaperDocument,
        index: WikiIndex,
    ) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "current_status": entity.metadata.get("status"),
            "schema_version": entity.metadata.get("schema_version"),
        }
        if entity.entity_type == "experiment":
            page = _experiment_pdf_page(entity)
            locator = (entity.metadata.get("evidence") or {}).get("locator")
            result.update(
                {
                    "evidence_locator": locator,
                    "pdf_page": page,
                    "locator_in_range": bool(page and page <= len(document.pages)),
                    "result_value_visible_on_page": bool(
                        page
                        and page <= len(document.pages)
                        and _numeric_value_visible(entity, document, page)
                    ),
                }
            )
        if entity.entity_type == "claim" and entity.entity_id:
            edges = [
                edge
                for edge in index.edges
                if edge.target == entity.entity_id
                and edge.relation in {"supports", "contradicts"}
            ]
            result["structured_evidence_ids"] = [edge.source for edge in edges]
        return result

    def _verify_paper(self, snapshot: ResearchSnapshot) -> EvidenceVerificationResult:
        paper_id, pdf_path, index, targets = self._paper_target(snapshot)
        document = extract_pdf_document(pdf_path, self.settings.repository_root)
        prechecks = {
            entity.entity_id: self._entity_precheck(entity, document, index)
            for entity in targets
            if entity.entity_id
        }
        requested_pages = [
            int(check["pdf_page"])
            for check in prechecks.values()
            if isinstance(check.get("pdf_page"), int)
        ]
        excerpt, retained_pages = _render_excerpt(document, requested_pages)
        records = []
        for entity in targets:
            record = _entity_record(entity)
            record["deterministic_precheck"] = prechecks.get(entity.entity_id, {})
            records.append(record)
        draft = self.verifier.verify_paper(
            skill=self.skill,
            evidence_policy=self.ingest_skill.read_reference("evidence-policy.md"),
            paper_id=paper_id,
            source_contract={
                "path": document.source_path,
                "sha256": document.sha256,
                "pdf_pages": len(document.pages),
                "excerpt_pages": retained_pages,
            },
            entities=records,
            excerpt=excerpt,
        )
        if draft.paper_id != paper_id:
            raise EvidenceVerificationError("Verifier returned the wrong paper ID")
        expected_ids = {entity.entity_id for entity in targets if entity.entity_id}
        returned_ids = {item.entity_id for item in draft.decisions}
        if returned_ids != expected_ids:
            raise EvidenceVerificationError(
                "Verifier must decide every supplied entity exactly once; "
                f"missing={sorted(expected_ids - returned_ids)}, "
                f"unexpected={sorted(returned_ids - expected_ids)}"
            )
        decisions = {item.entity_id: item for item in draft.decisions}
        all_entities = index.unique_entities()
        verified_after = {
            entity_id
            for entity_id, entity in all_entities.items()
            if entity.metadata.get("status") == "verified"
        }
        for entity in targets:
            if entity.entity_type == "experiment" and entity.entity_id:
                decision = decisions[entity.entity_id]
                check = prechecks[entity.entity_id]
                if (
                    decision.verdict == "supported"
                    and check.get("locator_in_range")
                    and check.get("result_value_visible_on_page")
                    and decision.pdf_pages
                ):
                    verified_after.add(entity.entity_id)

        timestamp = self.now().astimezone(timezone.utc).isoformat(timespec="seconds")
        pages: Dict[str, str] = {}
        verified: list[str] = []
        unresolved: list[str] = []
        for entity in targets:
            entity_id = str(entity.entity_id)
            decision = decisions[entity_id]
            if any(page > len(document.pages) for page in decision.pdf_pages):
                raise EvidenceVerificationError(
                    f"Verifier returned an out-of-range PDF page for {entity_id}"
                )
            check = prechecks[entity_id]
            promotable = decision.verdict == "supported" and bool(decision.pdf_pages)
            gate_reasons = []
            if entity.entity_type == "experiment":
                if not check.get("locator_in_range"):
                    promotable = False
                    gate_reasons.append("evidence locator is missing or out of range")
                if not check.get("result_value_visible_on_page"):
                    promotable = False
                    gate_reasons.append(
                        "recorded result value is absent from the cited page"
                    )
            if entity.entity_type == "claim":
                evidence_ids = set(check.get("structured_evidence_ids") or [])
                if not evidence_ids.intersection(verified_after):
                    promotable = False
                    gate_reasons.append("no linked experiment is verified")
                if decision.claim_assessment not in {
                    "supported",
                    "contested",
                    "refuted",
                }:
                    promotable = False
                    gate_reasons.append("claim assessment is unresolved")
            metadata = dict(entity.metadata)
            metadata["updated_at"] = timestamp
            metadata["status"] = "verified" if promotable else "needs-review"
            if entity.entity_type == "claim" and decision.claim_assessment:
                metadata["assessment"] = decision.claim_assessment
            rationale = decision.rationale
            if gate_reasons:
                rationale += " Deterministic gate: " + "; ".join(gate_reasons) + "."
            metadata["verification"] = {
                "skill": "verify-evidence",
                "verdict": decision.verdict,
                "verified_at": timestamp,
                "source_sha256": document.sha256,
                "pdf_pages": list(decision.pdf_pages),
                "rationale": rationale,
                "prechecks": check,
            }
            pages[entity.relative_path] = render_wiki_page(metadata, entity.body)
            (verified if promotable else unresolved).append(entity_id)
        report = self.writer.publish(pages, allow_overwrite=True)
        return EvidenceVerificationResult(
            target_id=paper_id,
            target_kind="paper-bundle",
            status="published" if report.changed_paths else "no-change",
            verified_entity_ids=tuple(verified),
            unresolved_entity_ids=tuple(unresolved),
            changed_paths=report.changed_paths,
            diagnostic_codes=tuple(
                dict.fromkeys(item.code for item in report.diagnostics)
            ),
            model_calls=1,
        )

    @staticmethod
    def _assessment_target(index: WikiIndex) -> Entity:
        candidates = [
            entity
            for entity in index.unique_entities().values()
            if entity.entity_type == "assessment"
            and entity.metadata.get("status") in {"draft", "needs-review"}
            and entity.metadata.get("verified") is not True
        ]
        if not candidates:
            raise VerificationPreconditionError(
                "No draft non-consensus assessment is ready for verification."
            )
        return sorted(candidates, key=lambda item: item.entity_id or "")[0]

    def _verify_assessment(self) -> EvidenceVerificationResult:
        index = build_index(self.settings.wiki_root, self.settings.wiki_meta_root)
        assessment = self._assessment_target(index)
        unique = index.unique_entities()
        claim_ids = tuple(
            str(value) for value in assessment.metadata.get("claim_ids") or []
        )
        evidence_ids = tuple(
            str(value) for value in assessment.metadata.get("evidence_ids") or []
        )
        if not claim_ids or not evidence_ids:
            raise VerificationPreconditionError(
                "Assessment verification requires claim and experiment IDs."
            )
        missing = [
            value for value in (*claim_ids, *evidence_ids) if value not in unique
        ]
        if missing:
            raise VerificationPreconditionError(
                "Assessment references missing Wiki entities: " + ", ".join(missing)
            )
        unverified = [
            value
            for value in (*claim_ids, *evidence_ids)
            if unique[value].metadata.get("status") != "verified"
        ]
        if unverified:
            raise VerificationPreconditionError(
                "Assessment inputs must be verified first: " + ", ".join(unverified)
            )
        draft = self.verifier.verify_assessment(
            skill=self.skill,
            assessment=_entity_record(assessment),
            claims=[_entity_record(unique[value]) for value in claim_ids],
            experiments=[_entity_record(unique[value]) for value in evidence_ids],
        )
        assessment_id = str(assessment.entity_id)
        if draft.assessment_id != assessment_id:
            raise EvidenceVerificationError("Verifier returned the wrong assessment ID")
        if set(draft.claim_ids) != set(claim_ids) or set(draft.evidence_ids) != set(
            evidence_ids
        ):
            raise EvidenceVerificationError(
                "Assessment verifier must compare the complete cited evidence set"
            )
        supported = (
            draft.verdict == "supported"
            and draft.confirmed_result == assessment.metadata.get("result")
        )
        timestamp = self.now().astimezone(timezone.utc).isoformat(timespec="seconds")
        metadata = dict(assessment.metadata)
        metadata["updated_at"] = timestamp
        metadata["status"] = "verified" if supported else "needs-review"
        metadata["verified"] = supported
        metadata["verification"] = {
            "skill": "verify-evidence",
            "verdict": draft.verdict,
            "verified_at": timestamp,
            "rationale": draft.rationale,
            "confirmed_result": draft.confirmed_result,
            "verified_inputs": [*claim_ids, *evidence_ids],
        }
        report = self.writer.publish(
            {assessment.relative_path: render_wiki_page(metadata, assessment.body)},
            allow_overwrite=True,
        )
        return EvidenceVerificationResult(
            target_id=assessment_id,
            target_kind="assessment",
            status="published" if report.changed_paths else "no-change",
            verified_entity_ids=(assessment_id,) if supported else (),
            unresolved_entity_ids=() if supported else (assessment_id,),
            changed_paths=report.changed_paths,
            diagnostic_codes=tuple(
                dict.fromkeys(item.code for item in report.diagnostics)
            ),
            model_calls=1,
        )

    def verify_next(
        self,
        *,
        gap: ResearchGap,
        snapshot: ResearchSnapshot,
    ) -> EvidenceVerificationResult:
        prefer_assessment = (
            gap.type == "contradiction_gap" or gap.key == "nonconsensus-review"
        )
        if prefer_assessment:
            try:
                return self._verify_assessment()
            except VerificationPreconditionError:
                return self._verify_paper(snapshot)
        try:
            return self._verify_paper(snapshot)
        except VerificationPreconditionError as paper_error:
            try:
                return self._verify_assessment()
            except VerificationPreconditionError:
                raise paper_error


__all__ = [
    "AssessmentVerificationDraft",
    "ClaimAssessment",
    "EntityVerificationDecision",
    "EvidenceSemanticVerifier",
    "EvidenceVerificationError",
    "EvidenceVerificationPipeline",
    "EvidenceVerificationResult",
    "LangChainEvidenceSemanticVerifier",
    "PaperVerificationDraft",
    "VerificationPreconditionError",
    "VerificationVerdict",
]
