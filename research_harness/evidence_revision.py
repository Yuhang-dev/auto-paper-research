"""Bounded, Skill-driven revision of verifier-retained Wiki evidence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
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

import yaml  # type: ignore[import-untyped]
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tools.wiki.indexer import WikiIndex, build_index
from tools.wiki.models import Entity
from tools.wiki.writer import WikiSourceWriter, render_wiki_page

from .artifacts import SemanticArtifactContext, SemanticArtifactRecorder
from .config import HarnessSettings
from .evidence_verification import _render_excerpt
from .ingest_models import EvidenceLocator, JsonScalar, PaperDocument
from .model_client import create_chat_model
from .paper_ingest import extract_pdf_document
from .research_models import ResearchGap, ResearchSnapshot
from .skill_registry import SkillRegistry, SkillSpec


RevisionReason = Literal[
    "source-contradiction",
    "locator-page-mismatch",
    "invalid-locator",
]
RevisionEntityType = Literal["method", "claim"]
MAX_EVIDENCE_REVISIONS = 2


class EvidenceRevisionError(RuntimeError):
    """Raised when a semantic revision violates a deterministic gate."""


class EvidenceRevisionPreconditionError(EvidenceRevisionError):
    """Raised before a revision attempt can safely begin."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceRevisionUpdates(_StrictModel):
    """The only Wiki fields the semantic reviser may propose changing."""

    definition: Optional[str] = Field(default=None, min_length=3, max_length=4_000)
    statement: Optional[str] = Field(default=None, min_length=3, max_length=4_000)
    scope: Optional[Dict[str, JsonScalar]] = Field(default=None, min_length=1)
    evidence: Optional[EvidenceLocator] = None

    @model_validator(mode="after")
    def _at_least_one_update(self) -> "EvidenceRevisionUpdates":
        if not self.model_dump(exclude_none=True):
            raise ValueError("a revision must propose at least one allowed field")
        return self


class EvidenceRevisionDraft(_StrictModel):
    entity_id: str = Field(min_length=3, max_length=220)
    entity_type: RevisionEntityType
    paper_id: str = Field(pattern=r"^paper:[a-z0-9][a-z0-9-]*$")
    reason_code: RevisionReason
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_pages: Tuple[int, ...] = Field(min_length=1, max_length=12)
    rationale: str = Field(min_length=10, max_length=2_000)
    updates: EvidenceRevisionUpdates

    @field_validator("source_pages")
    @classmethod
    def _unique_source_pages(cls, value: Tuple[int, ...]) -> Tuple[int, ...]:
        if any(page < 1 for page in value):
            raise ValueError("source_pages must be positive PDF page numbers")
        if len(set(value)) != len(value):
            raise ValueError("source_pages cannot contain duplicates")
        return value


class EvidenceSemanticReviser(Protocol):
    requires_network: bool

    def revise(
        self,
        *,
        skill: SkillSpec,
        contract: str,
        entity: Mapping[str, Any],
        verification_feedback: Mapping[str, Any],
        allowed_fields: Sequence[str],
        source_contract: Mapping[str, Any],
        excerpt: str,
    ) -> EvidenceRevisionDraft: ...


class LangChainEvidenceSemanticReviser:
    """Use a chat model only for one bounded source-grounded correction."""

    requires_network = True

    def __init__(self, model: BaseChatModel):
        self.model = model

    def revise(
        self,
        *,
        skill: SkillSpec,
        contract: str,
        entity: Mapping[str, Any],
        verification_feedback: Mapping[str, Any],
        allowed_fields: Sequence[str],
        source_contract: Mapping[str, Any],
        excerpt: str,
    ) -> EvidenceRevisionDraft:
        structured = self.model.with_structured_output(
            EvidenceRevisionDraft,
            method="json_mode",
        )
        system = SystemMessage(
            content=(
                "You execute the repository-local revise-evidence Skill. Correct exactly "
                "one retained Wiki entity from the supplied page-aware PDF excerpt. Return "
                "only the requested structured object. Never self-verify, relax an evidence "
                "gate, invent a source page, or change a field outside allowed_fields.\n\n"
                f"SKILL INSTRUCTIONS\n{skill.instructions}\n\n"
                f"REVISION CONTRACT\n{contract}"
            )
        )
        human = HumanMessage(
            content=(
                json.dumps(
                    {
                        "task": "Revise one verifier-retained Wiki entity.",
                        "entity": entity,
                        "verification_feedback": verification_feedback,
                        "allowed_fields": list(allowed_fields),
                        "source_contract": dict(source_contract),
                        "constraints": {
                            "return_exact_entity_and_paper_ids": True,
                            "use_only_excerpt_pages": True,
                            "source_pages_must_support_every_update": True,
                            "do_not_mark_verified": True,
                        },
                        "output_json_schema": EvidenceRevisionDraft.model_json_schema(),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n\nPAGE-AWARE SOURCE EXCERPT\n"
                + excerpt
            )
        )
        result = structured.invoke([system, human])
        if isinstance(result, EvidenceRevisionDraft):
            return result
        return EvidenceRevisionDraft.model_validate(result)


@dataclass(frozen=True)
class EvidenceRevisionResult:
    target_id: str
    paper_id: str
    reason_code: RevisionReason
    status: Literal["published", "no-change"]
    updated_fields: Tuple[str, ...]
    changed_paths: Tuple[str, ...]
    model_calls: int
    semantic_artifact_ids: Tuple[str, ...] = ()


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
        raise EvidenceRevisionPreconditionError(f"Expected a YAML mapping in {path}")
    return payload


def _history(entity: Entity) -> Tuple[Mapping[str, Any], ...]:
    raw = entity.metadata.get("revision_history")
    if not isinstance(raw, list):
        return ()
    return tuple(item for item in raw if isinstance(item, Mapping))


def evidence_revision_reason(entity: Entity) -> Optional[RevisionReason]:
    """Return the deterministic reason that makes an entity revision-eligible."""

    if (
        entity.entity_type not in {"method", "claim"}
        or entity.metadata.get("status") != "needs-review"
        or len(_history(entity)) >= MAX_EVIDENCE_REVISIONS
    ):
        return None
    verification = entity.metadata.get("verification")
    if not isinstance(verification, Mapping) or not verification.get("source_sha256"):
        return None
    verdict = str(verification.get("verdict") or "").casefold()
    rationale = str(verification.get("rationale") or "").casefold()
    raw_gate_codes = verification.get("gate_codes")
    gate_codes: set[str] = set()
    if isinstance(raw_gate_codes, list):
        gate_codes = {str(value).casefold() for value in raw_gate_codes if value}
    if verdict == "contradicted":
        return "source-contradiction"
    if (
        "locator-page-not-inspected" in gate_codes
        or "semantic decision did not inspect the cited page" in rationale
    ):
        return "locator-page-mismatch"
    if (
        gate_codes.intersection({"evidence-locator-invalid", "locator-page-absent"})
        or "evidence locator is missing or out of range" in rationale
        or "cited evidence page was absent from the excerpt" in rationale
    ):
        return "invalid-locator"
    return None


def evidence_revision_candidates(index: WikiIndex) -> Tuple[Entity, ...]:
    reason_rank = {
        "source-contradiction": 0,
        "locator-page-mismatch": 1,
        "invalid-locator": 2,
    }
    return tuple(
        sorted(
            (
                entity
                for entity in index.unique_entities().values()
                if evidence_revision_reason(entity) is not None
            ),
            key=lambda item: (
                reason_rank.get(evidence_revision_reason(item), 9),
                item.entity_id or "",
            ),
        )
    )


def evidence_revision_exhausted(index: WikiIndex) -> Tuple[Entity, ...]:
    return tuple(
        sorted(
            (
                entity
                for entity in index.unique_entities().values()
                if entity.entity_type in {"method", "claim"}
                and entity.metadata.get("status") == "needs-review"
                and len(_history(entity)) >= MAX_EVIDENCE_REVISIONS
            ),
            key=lambda item: item.entity_id or "",
        )
    )


def _allowed_fields(entity_type: str, reason: RevisionReason) -> Tuple[str, ...]:
    if reason in {"locator-page-mismatch", "invalid-locator"}:
        return ("evidence",)
    if entity_type == "method":
        return ("definition", "evidence")
    return ("statement", "scope", "evidence")


def _entity_record(entity: Entity) -> Dict[str, Any]:
    return {
        "id": entity.entity_id,
        "type": entity.entity_type,
        "title": entity.title,
        "path": entity.relative_path,
        "metadata": entity.metadata,
        "body": entity.body[:6_000],
    }


def _paper_id(index: WikiIndex, entity: Entity) -> Optional[str]:
    if entity.entity_type == "claim":
        direct = str(entity.metadata.get("source_paper") or "")
        if direct.startswith("paper:"):
            return direct
    entity_id = str(entity.entity_id or "")
    paper_ids = sorted(
        {
            edge.source
            for edge in index.edges
            if edge.target == entity_id
            and edge.source.startswith("paper:")
            and edge.relation in {"proposes", "states"}
        }
        | {
            str(link.target)
            for link in index.links
            if link.source == entity_id
            and link.target
            and str(link.target).startswith("paper:")
        }
    )
    return paper_ids[0] if paper_ids else None


def _paper_sources(
    settings: HarnessSettings,
    snapshot: ResearchSnapshot,
) -> Mapping[str, Path]:
    root = settings.repository_root.resolve()
    records: Dict[str, Path] = {}
    for relative in snapshot.corpus.search_run_paths:
        path = (root / relative).resolve()
        if not _is_within(path, root) or not path.is_file():
            continue
        run = _load_yaml(path)
        for candidate in run.get("candidates") or []:
            if not isinstance(candidate, Mapping):
                continue
            ingest = candidate.get("ingest")
            if not isinstance(ingest, Mapping):
                continue
            paper_id = str(ingest.get("paper_id") or "")
            local_pdf = str(candidate.get("local_pdf_path") or "")
            pdf_path = (root / local_pdf).resolve() if local_pdf else root
            if (
                candidate.get("review_state") == "ingested"
                and paper_id.startswith("paper:")
                and local_pdf
                and _is_within(pdf_path, root)
                and pdf_path.suffix.casefold() == ".pdf"
                and pdf_path.is_file()
            ):
                records.setdefault(paper_id, pdf_path)
    return records


def _evidence_mapping(locator: EvidenceLocator) -> Dict[str, Any]:
    return locator.model_dump(mode="python", exclude_none=True)


def _render_method(metadata: Mapping[str, Any], paper_id: str) -> str:
    implementations = metadata.get("implementations")
    bullets = (
        "\n".join(f"- {item}" for item in implementations)
        if isinstance(implementations, list) and implementations
        else "- Not recorded."
    )
    locator = EvidenceLocator.model_validate(metadata["evidence"])
    return f"""# {metadata['title']}

## Definition

{metadata['definition']}

## Provenance

- Paper: [[{paper_id}]]
- Evidence: {locator.render()}

## Implementations

{bullets}
"""


def _render_claim(metadata: Mapping[str, Any], paper_id: str) -> str:
    raw_evidence = metadata.get("evidence")
    locator = (
        EvidenceLocator.model_validate(raw_evidence).render()
        if isinstance(raw_evidence, Mapping)
        else "Not located."
    )
    return f"""# {metadata['statement']}

## Statement

{metadata['statement']}

## Attribution and evidence

- Attribution: {metadata.get('attribution') or 'agent'}
- Evidence type: {metadata.get('evidence_type') or 'not-recorded'}
- Evidence status: {metadata.get('evidence_status') or 'unlocated'}
- Locator: {locator}
- Source paper: [[{paper_id}]]
"""


class EvidenceRevisionPipeline:
    """Revise one eligible entity, reset it to draft, and require re-verification."""

    def __init__(
        self,
        settings: HarnessSettings,
        *,
        reviser: Optional[EvidenceSemanticReviser] = None,
        now: Optional[Callable[[], datetime]] = None,
        artifact_recorder: Optional[SemanticArtifactRecorder] = None,
    ):
        self.settings = settings
        self.registry = SkillRegistry(settings.skills_root)
        self.skill = self.registry.get("revise-evidence")
        self.reviser = reviser or self._default_reviser(settings)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.writer = WikiSourceWriter(settings.wiki_root, settings.wiki_meta_root)
        self.artifact_recorder = artifact_recorder

    @staticmethod
    def _default_reviser(settings: HarnessSettings) -> EvidenceSemanticReviser:
        if not settings.model:
            raise EvidenceRevisionPreconditionError(
                "Evidence revision needs an injected reviser or HARNESS_MODEL/--model."
            )
        return LangChainEvidenceSemanticReviser(create_chat_model(settings))

    @property
    def requires_network(self) -> bool:
        return bool(self.reviser.requires_network)

    def _target(
        self,
        *,
        gap: ResearchGap,
        snapshot: ResearchSnapshot,
    ) -> Tuple[Entity, RevisionReason, str, Path]:
        index = build_index(self.settings.wiki_root, self.settings.wiki_meta_root)
        if index.source_hash != snapshot.wiki_source_hash:
            raise EvidenceRevisionPreconditionError(
                "Wiki source changed after inspection; replan before revising evidence."
            )
        requested = set(gap.evidence.get("entity_ids", ()))
        candidates = [
            entity
            for entity in evidence_revision_candidates(index)
            if not requested or entity.entity_id in requested
        ]
        sources = _paper_sources(self.settings, snapshot)
        for entity in candidates:
            reason = evidence_revision_reason(entity)
            paper_id = _paper_id(index, entity)
            if reason and paper_id and paper_id in sources:
                return entity, reason, paper_id, sources[paper_id]
        raise EvidenceRevisionPreconditionError(
            "No eligible verifier-retained method or claim has an available local PDF."
        )

    def revise_next(
        self,
        *,
        gap: ResearchGap,
        snapshot: ResearchSnapshot,
        artifact_context: Optional[SemanticArtifactContext] = None,
    ) -> EvidenceRevisionResult:
        entity, reason, paper_id, pdf_path = self._target(
            gap=gap,
            snapshot=snapshot,
        )
        verification = entity.metadata.get("verification")
        if not isinstance(verification, Mapping):
            raise EvidenceRevisionPreconditionError(
                "Revision target has no retained verification feedback."
            )
        document: PaperDocument = extract_pdf_document(
            pdf_path,
            self.settings.repository_root,
        )
        if verification.get("source_sha256") != document.sha256:
            raise EvidenceRevisionPreconditionError(
                "The local PDF changed after verification; verify the source again first."
            )
        requested_pages = [
            int(value)
            for value in verification.get("pdf_pages") or []
            if isinstance(value, int) and value > 0
        ]
        raw_evidence = entity.metadata.get("evidence")
        if isinstance(raw_evidence, Mapping) and isinstance(
            raw_evidence.get("pdf_page"), int
        ):
            requested_pages.append(int(raw_evidence["pdf_page"]))
        excerpt, retained_pages = _render_excerpt(document, requested_pages)
        allowed = _allowed_fields(str(entity.entity_type), reason)
        source_contract = {
            "path": document.source_path,
            "sha256": document.sha256,
            "pdf_pages": len(document.pages),
            "excerpt_pages": retained_pages,
        }
        draft = self.reviser.revise(
            skill=self.skill,
            contract=self.skill.read_reference("revision-contract.md"),
            entity=_entity_record(entity),
            verification_feedback=dict(verification),
            allowed_fields=allowed,
            source_contract=source_contract,
            excerpt=excerpt,
        )
        if (
            draft.entity_id != entity.entity_id
            or draft.entity_type != entity.entity_type
            or draft.paper_id != paper_id
            or draft.reason_code != reason
            or draft.source_sha256 != document.sha256
        ):
            raise EvidenceRevisionError(
                "Reviser changed the target identity, reason, paper, or source hash."
            )
        if not set(draft.source_pages).issubset(set(retained_pages)):
            raise EvidenceRevisionError(
                "Reviser cited a PDF page outside the supplied excerpt."
            )
        proposed = draft.updates.model_dump(mode="python", exclude_none=True)
        unexpected = set(proposed) - set(allowed)
        if unexpected:
            raise EvidenceRevisionError(
                "Reviser changed fields outside the deterministic allow-list: "
                + ", ".join(sorted(unexpected))
            )
        if "evidence" in proposed:
            locator = EvidenceLocator.model_validate(proposed["evidence"])
            if locator.pdf_page not in draft.source_pages:
                raise EvidenceRevisionError(
                    "The revised evidence locator page must be one of source_pages."
                )
            proposed["evidence"] = _evidence_mapping(locator)

        changed = {
            name: value
            for name, value in proposed.items()
            if entity.metadata.get(name) != value
        }
        artifact_ids: list[str] = []
        if self.artifact_recorder is not None and artifact_context is not None:
            artifact = self.artifact_recorder.record(
                kind="evidence-revision",
                context=artifact_context.with_updates(
                    pdf_sha256=document.sha256,
                    source_ids=(str(entity.entity_id), paper_id),
                ),
                skill=self.skill,
                schema_resources=("references/revision-contract.md",),
                output=draft,
                validation_details={
                    "allowed_fields": allowed,
                    "changed_fields": tuple(sorted(changed)),
                },
            )
            artifact_ids.append(artifact.artifact_id)
        if not changed:
            return EvidenceRevisionResult(
                target_id=str(entity.entity_id),
                paper_id=paper_id,
                reason_code=reason,
                status="no-change",
                updated_fields=(),
                changed_paths=(),
                model_calls=1,
                semantic_artifact_ids=tuple(artifact_ids),
            )

        timestamp = self.now().astimezone(timezone.utc).isoformat(timespec="seconds")
        metadata = dict(entity.metadata)
        metadata.update(changed)
        metadata["status"] = "draft"
        metadata["updated_at"] = timestamp
        if entity.entity_type == "claim":
            metadata["assessment"] = "open"
            if "statement" in changed:
                metadata["title"] = str(changed["statement"])[:160]
        prior_verification = metadata.pop("verification")
        history = [dict(item) for item in _history(entity)]
        history.append(
            {
                "revised_at": timestamp,
                "skill": "revise-evidence",
                "reason_code": reason,
                "source_sha256": document.sha256,
                "pdf_pages": list(draft.source_pages),
                "updated_fields": sorted(changed),
                "rationale": draft.rationale,
                "prior_verification": prior_verification,
            }
        )
        metadata["revision_history"] = history
        body = (
            _render_method(metadata, paper_id)
            if entity.entity_type == "method"
            else _render_claim(metadata, paper_id)
        )
        report = self.writer.publish(
            {entity.relative_path: render_wiki_page(metadata, body)},
            allow_overwrite=True,
        )
        if self.artifact_recorder is not None and artifact_context is not None:
            self.artifact_recorder.link_publication(
                artifact_ids,
                action_id=artifact_context.action_id,
                changed_sources=tuple(f"wiki/{path}" for path in report.changed_paths),
            )
        return EvidenceRevisionResult(
            target_id=str(entity.entity_id),
            paper_id=paper_id,
            reason_code=reason,
            status="published" if report.changed_paths else "no-change",
            updated_fields=tuple(sorted(changed)),
            changed_paths=report.changed_paths,
            model_calls=1,
            semantic_artifact_ids=tuple(artifact_ids),
        )


__all__ = [
    "EvidenceRevisionDraft",
    "EvidenceRevisionError",
    "EvidenceRevisionPipeline",
    "EvidenceRevisionPreconditionError",
    "EvidenceRevisionResult",
    "EvidenceRevisionUpdates",
    "EvidenceSemanticReviser",
    "LangChainEvidenceSemanticReviser",
    "MAX_EVIDENCE_REVISIONS",
    "evidence_revision_candidates",
    "evidence_revision_exhausted",
    "evidence_revision_reason",
]
