"""Skill-driven comparison of verified claims and experimental evidence."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import (
    Any,
    Callable,
    Dict,
    Literal,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
)

from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tools.wiki.indexer import WikiIndex, build_index
from tools.wiki.models import Entity, listify
from tools.wiki.writer import WikiSourceWriter, render_wiki_page

from .config import HarnessSettings
from .research_models import NonConsensusResult, ResearchGap, ResearchSnapshot
from .skill_registry import SkillRegistry, SkillSpec


AlignmentStatus = Literal["aligned", "partially-aligned", "mismatched", "unknown"]


class NonConsensusAnalysisError(RuntimeError):
    """Raised when an assessment draft violates the comparison contract."""


class NonConsensusPreconditionError(NonConsensusAnalysisError):
    """Raised when verified evidence is not ready for semantic comparison."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ConditionAlignment(_StrictModel):
    dimension: str = Field(min_length=1, max_length=80)
    status: AlignmentStatus
    values: Tuple[str, ...] = Field(default=(), max_length=20)
    note: str = Field(min_length=2, max_length=800)

    @field_validator("dimension")
    @classmethod
    def _normalize_dimension(cls, value: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
        if not normalized:
            raise ValueError("dimension must contain an ASCII letter or digit")
        return normalized


class NonConsensusAssessmentDraft(_StrictModel):
    question: str = Field(min_length=5, max_length=500)
    result: NonConsensusResult
    claim_ids: Tuple[str, ...] = Field(min_length=1, max_length=24)
    evidence_ids: Tuple[str, ...] = Field(min_length=1, max_length=40)
    method_family: Optional[str] = Field(default=None, max_length=160)
    benchmark_ids: Tuple[str, ...] = Field(default=(), max_length=20)
    rationale: str = Field(min_length=10, max_length=3_000)
    condition_alignment: Tuple[ConditionAlignment, ...] = Field(
        min_length=1, max_length=20
    )

    @model_validator(mode="after")
    def _validate_comparison(self) -> "NonConsensusAssessmentDraft":
        for values, prefix, name in (
            (self.claim_ids, "claim:", "claim_ids"),
            (self.evidence_ids, "experiment:", "evidence_ids"),
            (self.benchmark_ids, "benchmark:", "benchmark_ids"),
        ):
            if any(not value.startswith(prefix) for value in values):
                raise ValueError(f"{name} must use canonical {prefix} IDs")
            if len(values) != len(set(values)):
                raise ValueError(f"{name} cannot contain duplicates")
        if self.result in {"supported-consensus", "contested"} and (
            len(self.claim_ids) < 2 or len(self.evidence_ids) < 2
        ):
            raise ValueError(
                f"{self.result} requires at least two claims and two experiments"
            )
        return self


class ClaimSemanticAnalyzer(Protocol):
    requires_network: bool

    def analyze(
        self,
        *,
        skill: SkillSpec,
        gap: ResearchGap,
        claims: Sequence[Mapping[str, Any]],
        experiments: Sequence[Mapping[str, Any]],
        existing_assessments: Sequence[Mapping[str, Any]],
    ) -> NonConsensusAssessmentDraft: ...


class LangChainClaimSemanticAnalyzer:
    """Ask a model for one structured comparison over verified inputs only."""

    requires_network = True

    def __init__(self, model: BaseChatModel):
        self.model = model

    def analyze(
        self,
        *,
        skill: SkillSpec,
        gap: ResearchGap,
        claims: Sequence[Mapping[str, Any]],
        experiments: Sequence[Mapping[str, Any]],
        existing_assessments: Sequence[Mapping[str, Any]],
    ) -> NonConsensusAssessmentDraft:
        policy = skill.read_reference("comparison-policy.md")
        structured = self.model.with_structured_output(
            NonConsensusAssessmentDraft,
            method="json_mode",
        )
        system = SystemMessage(
            content=(
                "You execute the repository-local analyze-claims Skill. Return only one "
                "structured assessment draft. Every input entity is already verified, "
                "but verification does not make different conditions comparable. Do not "
                "force a contested result. Use insufficient-evidence whenever material "
                "condition mismatches prevent a defensible comparison.\n\n"
                f"SKILL INSTRUCTIONS\n{skill.instructions}\n\n"
                f"COMPARISON POLICY\n{policy}"
            )
        )
        human = HumanMessage(
            content=json.dumps(
                {
                    "research_gap": gap.model_dump(mode="json"),
                    "verified_claims": list(claims),
                    "verified_experiments": list(experiments),
                    "existing_assessments": list(existing_assessments),
                    "task": (
                        "Choose one novel, bounded question. Cite only supplied IDs and "
                        "make condition alignment explicit."
                    ),
                    "output_json_schema": NonConsensusAssessmentDraft.model_json_schema(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        result = structured.invoke([system, human])
        if isinstance(result, NonConsensusAssessmentDraft):
            return result
        return NonConsensusAssessmentDraft.model_validate(result)


@dataclass(frozen=True)
class NonConsensusAnalysisResult:
    assessment_id: str
    status: Literal["published", "no-change"]
    result: NonConsensusResult
    changed_paths: Tuple[str, ...]
    diagnostic_codes: Tuple[str, ...]
    model_calls: int


def _entity_record(entity: Entity, *, body_chars: int = 4_000) -> Dict[str, Any]:
    body = entity.body.strip()
    if len(body) > body_chars:
        body = body[:body_chars] + "\n[body truncated]"
    return {
        "id": entity.entity_id,
        "type": entity.entity_type,
        "title": entity.title,
        "metadata": entity.metadata,
        "body": body,
    }


def _fingerprint(
    question: str,
    result: str,
    claim_ids: Sequence[str],
    evidence_ids: Sequence[str],
) -> str:
    payload = json.dumps(
        {
            "question": " ".join(question.casefold().split()),
            "result": result,
            "claim_ids": sorted(claim_ids),
            "evidence_ids": sorted(evidence_ids),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _slugify(value: str, fallback: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = normalized.encode("ascii", errors="ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.casefold()).strip("-")
    return (slug or fallback)[:90].rstrip("-")


def _escape_table(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


class NonConsensusAnalysisPipeline:
    """Create one needs-review assessment from verified evidence."""

    CORE_CONTESTED_DIMENSIONS = frozenset(
        {"question", "task", "benchmark", "metric", "intervention", "method"}
    )

    def __init__(
        self,
        settings: HarnessSettings,
        *,
        analyzer: Optional[ClaimSemanticAnalyzer] = None,
        now: Optional[Callable[[], datetime]] = None,
        max_claims: int = 24,
        max_experiments: int = 40,
    ):
        if max_claims < 1 or max_experiments < 1:
            raise ValueError("analysis limits must be positive")
        self.settings = settings
        self.registry = SkillRegistry(settings.skills_root)
        self.skill = self.registry.get("analyze-claims")
        self.analyzer = analyzer or self._default_analyzer(settings)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.max_claims = max_claims
        self.max_experiments = max_experiments
        self.writer = WikiSourceWriter(settings.wiki_root, settings.wiki_meta_root)

    @staticmethod
    def _default_analyzer(settings: HarnessSettings) -> ClaimSemanticAnalyzer:
        if not settings.model:
            raise NonConsensusPreconditionError(
                "Claim analysis needs an injected analyzer or HARNESS_MODEL/--model."
            )
        return LangChainClaimSemanticAnalyzer(init_chat_model(settings.model))

    @property
    def requires_network(self) -> bool:
        return bool(self.analyzer.requires_network)

    @staticmethod
    def _verified_inputs(
        index: WikiIndex,
    ) -> Tuple[Tuple[Entity, ...], Tuple[Entity, ...]]:
        unique = index.unique_entities()
        experiments = {
            entity_id: entity
            for entity_id, entity in unique.items()
            if entity.entity_type == "experiment"
            and entity.metadata.get("status") == "verified"
        }
        claim_ids = {
            edge.target
            for edge in index.edges
            if edge.source in experiments
            and edge.relation in {"supports", "contradicts"}
        }
        claims = tuple(
            entity
            for entity_id, entity in sorted(unique.items())
            if entity_id in claim_ids
            and entity.entity_type == "claim"
            and entity.metadata.get("status") == "verified"
        )
        linked_experiment_ids = {
            edge.source
            for edge in index.edges
            if edge.target in {item.entity_id for item in claims}
            and edge.relation in {"supports", "contradicts"}
        }
        linked_experiments = tuple(
            experiments[value]
            for value in sorted(linked_experiment_ids)
            if value in experiments
        )
        return claims, linked_experiments

    @staticmethod
    def _existing_assessments(index: WikiIndex) -> Tuple[Mapping[str, Any], ...]:
        records = []
        for entity_id, entity in sorted(index.unique_entities().items()):
            if entity.entity_type != "assessment":
                continue
            fingerprint = _fingerprint(
                str(entity.metadata.get("question") or ""),
                str(entity.metadata.get("result") or ""),
                tuple(str(value) for value in entity.metadata.get("claim_ids") or []),
                tuple(
                    str(value) for value in entity.metadata.get("evidence_ids") or []
                ),
            )
            records.append(
                {
                    "id": entity_id,
                    "question": entity.metadata.get("question"),
                    "result": entity.metadata.get("result"),
                    "claim_ids": entity.metadata.get("claim_ids") or [],
                    "evidence_ids": entity.metadata.get("evidence_ids") or [],
                    "status": entity.metadata.get("status"),
                    "fingerprint": fingerprint,
                }
            )
        return tuple(records)

    @staticmethod
    def _augment_experiment(index: WikiIndex, entity: Entity) -> Dict[str, Any]:
        record = _entity_record(entity)
        unique = index.unique_entities()
        metadata = entity.metadata
        record["resolved_conditions"] = {
            "paper": (
                _entity_record(unique[str(metadata.get("paper"))])
                if str(metadata.get("paper")) in unique
                else None
            ),
            "methods": [
                _entity_record(unique[str(value)])
                for value in listify(metadata.get("method"))
                if str(value) in unique
            ],
            "models": [
                _entity_record(unique[str(value)])
                for value in listify(metadata.get("model"))
                if str(value) in unique
            ],
            "benchmark": (
                _entity_record(unique[str(metadata.get("benchmark"))])
                if str(metadata.get("benchmark")) in unique
                else None
            ),
        }
        return record

    @staticmethod
    def _validate_contested_alignment(draft: NonConsensusAssessmentDraft) -> None:
        if draft.result != "contested":
            return
        blocking = [
            item.dimension
            for item in draft.condition_alignment
            if item.status == "mismatched"
            and item.dimension in NonConsensusAnalysisPipeline.CORE_CONTESTED_DIMENSIONS
        ]
        if blocking:
            raise NonConsensusAnalysisError(
                "A contested assessment has mismatched core comparison dimensions: "
                + ", ".join(sorted(blocking))
            )

    def analyze(
        self,
        *,
        gap: ResearchGap,
        snapshot: ResearchSnapshot,
    ) -> NonConsensusAnalysisResult:
        del snapshot
        index = build_index(self.settings.wiki_root, self.settings.wiki_meta_root)
        claims, experiments = self._verified_inputs(index)
        if not claims or not experiments:
            raise NonConsensusPreconditionError(
                "Claim analysis requires at least one verified claim linked to one verified experiment."
            )
        claims = claims[: self.max_claims]
        experiments = experiments[: self.max_experiments]
        existing = self._existing_assessments(index)
        draft = self.analyzer.analyze(
            skill=self.skill,
            gap=gap,
            claims=[_entity_record(item) for item in claims],
            experiments=[self._augment_experiment(index, item) for item in experiments],
            existing_assessments=existing,
        )
        self._validate_contested_alignment(draft)
        available_claims = {str(item.entity_id) for item in claims}
        available_experiments = {str(item.entity_id) for item in experiments}
        if not set(draft.claim_ids) <= available_claims:
            raise NonConsensusAnalysisError(
                "Analyzer cited a claim outside the verified bundle"
            )
        if not set(draft.evidence_ids) <= available_experiments:
            raise NonConsensusAnalysisError(
                "Analyzer cited an experiment outside the verified bundle"
            )
        unique = index.unique_entities()
        derived_benchmarks = {
            str(unique[value].metadata.get("benchmark")) for value in draft.evidence_ids
        }
        if not set(draft.benchmark_ids) <= derived_benchmarks:
            raise NonConsensusAnalysisError(
                "Analyzer cited a benchmark not used by the selected experiments"
            )
        fingerprint = _fingerprint(
            draft.question,
            draft.result,
            draft.claim_ids,
            draft.evidence_ids,
        )
        existing_fingerprints = {str(item["fingerprint"]) for item in existing}
        if fingerprint in existing_fingerprints:
            raise NonConsensusAnalysisError(
                "Analyzer reproduced an existing assessment fingerprint"
            )
        slug = _slugify(draft.question, f"comparison-{fingerprint[:12]}")
        assessment_id = f"assessment:{slug}-{fingerprint[:8]}"
        if assessment_id in unique:
            raise NonConsensusAnalysisError(
                f"Assessment ID already exists: {assessment_id}"
            )
        timestamp = self.now().astimezone(timezone.utc).isoformat(timespec="seconds")
        selected_entities = [
            unique[value] for value in (*draft.claim_ids, *draft.evidence_ids)
        ]
        facets = sorted(
            {
                "limitations-and-counter-evidence",
                *(
                    str(facet)
                    for entity in selected_entities
                    for facet in entity.metadata.get("facets") or []
                ),
            }
        )
        metadata = {
            "schema_version": "0.2",
            "id": assessment_id,
            "type": "assessment",
            "title": draft.question,
            "aliases": [],
            "status": "needs-review",
            "created_at": timestamp,
            "updated_at": timestamp,
            "facets": facets,
            "question": draft.question,
            "result": draft.result,
            "claim_ids": list(draft.claim_ids),
            "evidence_ids": list(draft.evidence_ids),
            "method_family": draft.method_family,
            "benchmark_ids": list(draft.benchmark_ids),
            "rationale": draft.rationale,
            "verified": False,
            "analysis": {
                "skill": "analyze-claims",
                "fingerprint": fingerprint,
                "condition_alignment": [
                    item.model_dump(mode="json") for item in draft.condition_alignment
                ],
            },
            "relations": {},
        }
        alignment_rows = "\n".join(
            "| "
            + " | ".join(
                (
                    _escape_table(item.dimension),
                    _escape_table(item.status),
                    _escape_table(", ".join(item.values) or "not recorded"),
                    _escape_table(item.note),
                )
            )
            + " |"
            for item in draft.condition_alignment
        )
        body = f"""# {draft.question}

## Result

`{draft.result}`

## Claims considered

{chr(10).join(f'- [[{value}]]' for value in draft.claim_ids)}

## Evidence considered

{chr(10).join(f'- [[{value}]]' for value in draft.evidence_ids)}

## Condition alignment

| Dimension | Alignment | Values | Note |
|---|---|---|---|
{alignment_rows}

## Rationale

{draft.rationale}

## Verification state

This assessment is `needs-review`. A separate `verify-evidence` pass must
independently confirm the comparison before it becomes verified.
"""
        relative_path = f"assessments/{assessment_id.split(':', 1)[1]}.md"
        report = self.writer.publish(
            {relative_path: render_wiki_page(metadata, body)},
            allow_overwrite=False,
        )
        return NonConsensusAnalysisResult(
            assessment_id=assessment_id,
            status="published" if report.changed_paths else "no-change",
            result=draft.result,
            changed_paths=report.changed_paths,
            diagnostic_codes=tuple(
                dict.fromkeys(item.code for item in report.diagnostics)
            ),
            model_calls=1,
        )


__all__ = [
    "AlignmentStatus",
    "ClaimSemanticAnalyzer",
    "ConditionAlignment",
    "LangChainClaimSemanticAnalyzer",
    "NonConsensusAnalysisError",
    "NonConsensusAnalysisPipeline",
    "NonConsensusAnalysisResult",
    "NonConsensusAssessmentDraft",
    "NonConsensusPreconditionError",
]
