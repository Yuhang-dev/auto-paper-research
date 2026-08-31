"""Skill-driven query planning and candidate screening for paper search.

The language model proposes semantic judgments only.  This module owns query
IDs, search-run paths, lifecycle transitions, coverage recomputation, schema
validation, and atomic publication.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.metadata
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
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
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tools.wiki.indexer import build_index

from .artifacts import SemanticArtifactContext, SemanticArtifactRecorder
from .config import HarnessSettings
from .model_client import create_chat_model
from .research_models import ResearchGap, ResearchSnapshot
from .skill_registry import SkillRegistry, SkillSpec


class SearchRuntimeError(RuntimeError):
    """Raised when a semantic proposal cannot be published safely."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SearchFilterDraft(_StrictModel):
    source: Literal["arxiv", "biorxiv", "medrxiv"] = "arxiv"
    categories: Tuple[str, ...] = ()
    authors: Tuple[str, ...] = ()
    orgs: Tuple[str, ...] = ()
    venues: Tuple[str, ...] = ()
    venue_year: Optional[int] = None
    min_citations: Optional[int] = Field(default=None, ge=0)
    date_search_type: Optional[str] = None
    date_str: Optional[str] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    use_fine_rerank: bool = False
    size: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=10_000)


class SearchQueryDraft(_StrictModel):
    family: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=2, max_length=500)
    purpose: str = Field(min_length=2, max_length=500)
    target_facets: Tuple[str, ...] = Field(min_length=1, max_length=6)
    derived_from: Optional[str] = Field(default=None, max_length=64)
    filters: SearchFilterDraft = Field(default_factory=SearchFilterDraft)

    @field_validator("family")
    @classmethod
    def _normalize_family(cls, value: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
        if not normalized:
            raise ValueError("family must contain an ASCII letter or digit")
        return normalized

    @field_validator("text", "purpose")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("target_facets")
    @classmethod
    def _unique_facets(cls, value: Tuple[str, ...]) -> Tuple[str, ...]:
        cleaned = tuple(dict.fromkeys(item.strip() for item in value if item.strip()))
        if not cleaned:
            raise ValueError("target_facets cannot be empty")
        return cleaned


class SearchPlanDraft(_StrictModel):
    rationale: str = Field(min_length=2, max_length=1_500)
    queries: Tuple[SearchQueryDraft, ...] = Field(min_length=1, max_length=4)
    assumptions: Tuple[str, ...] = Field(default=(), max_length=8)


class CandidateScores(_StrictModel):
    sparsity_alignment: int = Field(ge=0, le=2)
    long_context_alignment: int = Field(ge=0, le=2)
    evidence_value: int = Field(ge=0, le=2)
    engineering_value: int = Field(ge=0, le=2)
    challenge_value: int = Field(ge=0, le=2)

    @property
    def total(self) -> int:
        return sum(
            (
                self.sparsity_alignment,
                self.long_context_alignment,
                self.evidence_value,
                self.engineering_value,
                self.challenge_value,
            )
        )


class CandidateScreening(_StrictModel):
    candidate_id: str = Field(min_length=3, max_length=220)
    label: Literal["core", "adjacent", "background", "exclude"]
    scores: CandidateScores
    reason: str = Field(min_length=3, max_length=800)
    basis: Literal[
        "title-only", "title-and-abstract", "provider-metadata", "manual-note"
    ]
    target_facets: Tuple[str, ...] = Field(default=(), max_length=8)
    select_for_ingest: bool = False

    @model_validator(mode="after")
    def _validate_selection(self) -> "CandidateScreening":
        if self.select_for_ingest and self.label != "core":
            raise ValueError("Only a core candidate can be selected for ingest")
        return self


class CandidateScreeningBatch(_StrictModel):
    screenings: Tuple[CandidateScreening, ...] = Field(min_length=1, max_length=16)


class SearchSemanticEngine(Protocol):
    requires_network: bool

    def plan(
        self,
        *,
        gap: ResearchGap,
        snapshot: ResearchSnapshot,
        skill: SkillSpec,
        scope: Mapping[str, Any],
        prior_queries: Sequence[Mapping[str, Any]],
    ) -> SearchPlanDraft: ...

    def screen(
        self,
        *,
        gap: ResearchGap,
        skill: SkillSpec,
        scope: Mapping[str, Any],
        candidates: Sequence[Mapping[str, Any]],
        existing_papers: Sequence[Mapping[str, Any]],
    ) -> CandidateScreeningBatch: ...


class LangChainSearchSemanticEngine:
    """Run the semantic parts of ``search-paper`` with structured outputs."""

    requires_network = True

    def __init__(self, model: BaseChatModel):
        self.model = model

    def _system(self, skill: SkillSpec, task: str) -> SystemMessage:
        strategy = skill.read_reference("search-strategy.md")
        schema = skill.read_reference("search-output-schema.md")
        return SystemMessage(
            content=(
                "You execute the repository-local search-paper Skill. "
                f"{task} Return only the requested structured object. Never invent "
                "paper metadata or scientific results. Candidate screening is a recall "
                "and prioritization judgment, not evidence verification.\n\n"
                f"SKILL INSTRUCTIONS\n{skill.instructions}\n\n"
                f"SEARCH STRATEGY\n{strategy}\n\n"
                f"SEARCH OUTPUT CONTRACT\n{schema}"
            )
        )

    def plan(
        self,
        *,
        gap: ResearchGap,
        snapshot: ResearchSnapshot,
        skill: SkillSpec,
        scope: Mapping[str, Any],
        prior_queries: Sequence[Mapping[str, Any]],
    ) -> SearchPlanDraft:
        structured = self.model.with_structured_output(
            SearchPlanDraft,
            method="json_mode",
        )
        human = HumanMessage(
            content=json.dumps(
                {
                    "task": "Plan one gap-directed follow-up search round.",
                    "gap": gap.model_dump(mode="json"),
                    "scope": scope,
                    "candidate_facet_coverage": snapshot.taxonomy.candidate_facet_coverage,
                    "evidence_facet_coverage": snapshot.taxonomy.evidence_facet_coverage,
                    "known_method_families": snapshot.taxonomy.method_families,
                    "prior_queries": list(prior_queries),
                    "constraints": {
                        "maximum_new_queries": 4,
                        "avoid_identical_query_and_filter_signatures": True,
                        "include_disconfirming_search_when_relevant": True,
                    },
                    "output_json_schema": SearchPlanDraft.model_json_schema(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        result = structured.invoke(
            [
                self._system(
                    skill, "Plan a bounded query matrix for the supplied gap."
                ),
                human,
            ]
        )
        if isinstance(result, SearchPlanDraft):
            return result
        return SearchPlanDraft.model_validate(result)

    def screen(
        self,
        *,
        gap: ResearchGap,
        skill: SkillSpec,
        scope: Mapping[str, Any],
        candidates: Sequence[Mapping[str, Any]],
        existing_papers: Sequence[Mapping[str, Any]],
    ) -> CandidateScreeningBatch:
        structured = self.model.with_structured_output(
            CandidateScreeningBatch,
            method="json_mode",
        )
        human = HumanMessage(
            content=json.dumps(
                {
                    "task": "Screen every supplied candidate exactly once.",
                    "gap": gap.model_dump(mode="json"),
                    "scope": scope,
                    "existing_wiki_papers": list(existing_papers),
                    "candidates": list(candidates),
                    "constraints": {
                        "return_exact_candidate_ids": True,
                        "do_not_infer_full_paper_results": True,
                        "select_only_core_candidates": True,
                    },
                    "output_json_schema": CandidateScreeningBatch.model_json_schema(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        result = structured.invoke(
            [self._system(skill, "Apply the five-dimension relevance rubric."), human]
        )
        if isinstance(result, CandidateScreeningBatch):
            return result
        return CandidateScreeningBatch.model_validate(result)


@dataclass(frozen=True)
class SearchPlanResult:
    run_path: Path
    query_ids: Tuple[str, ...]
    model_calls: int
    warnings: Tuple[str, ...] = ()
    semantic_artifact_ids: Tuple[str, ...] = ()


@dataclass(frozen=True)
class SearchScreeningResult:
    changed: bool
    triaged_candidates: int
    selected_candidates: int
    excluded_candidates: int
    model_calls: int
    warnings: Tuple[str, ...] = ()
    semantic_artifact_ids: Tuple[str, ...] = ()


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _package_version(distribution: str) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


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
        raise SearchRuntimeError(f"Expected a YAML mapping in {path}")
    return payload


def _render_yaml(payload: Mapping[str, Any]) -> str:
    return yaml.safe_dump(
        dict(payload),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )


def _normalized_query(value: str) -> str:
    return " ".join(value.casefold().split())


def _normalize_arxiv(value: Any) -> str:
    text = str(value or "").strip().casefold()
    text = re.sub(r"^https?://(?:export\.)?arxiv\.org/(?:abs|pdf)/", "", text)
    text = text.removesuffix(".pdf")
    return re.sub(r"v\d+$", "", text)


class SearchRuntime:
    """Publish model proposals only after deterministic search-run validation."""

    def __init__(
        self,
        settings: HarnessSettings,
        *,
        engine: Optional[SearchSemanticEngine] = None,
        timeout_seconds: int = 120,
        screening_batch_size: int = 12,
        max_selected_candidates: int = 3,
        artifact_recorder: Optional[SemanticArtifactRecorder] = None,
    ):
        if timeout_seconds < 1:
            raise ValueError("timeout_seconds must be positive")
        if not 1 <= screening_batch_size <= 16:
            raise ValueError("screening_batch_size must be between 1 and 16")
        if not 1 <= max_selected_candidates <= 20:
            raise ValueError("max_selected_candidates must be between 1 and 20")
        self.settings = settings
        self.registry = SkillRegistry(settings.skills_root)
        self.skill = self.registry.get("search-paper")
        self.engine = engine or self._default_engine(settings)
        self.timeout_seconds = timeout_seconds
        self.screening_batch_size = screening_batch_size
        self.max_selected_candidates = max_selected_candidates
        self.artifact_recorder = artifact_recorder

    @staticmethod
    def _default_engine(settings: HarnessSettings) -> SearchSemanticEngine:
        if not settings.model:
            raise SearchRuntimeError(
                "Search planning/screening needs an injected engine or HARNESS_MODEL/--model."
            )
        return LangChainSearchSemanticEngine(create_chat_model(settings))

    @property
    def requires_network(self) -> bool:
        return bool(self.engine.requires_network)

    def _resource(self, relative_path: str) -> Path:
        resource = next(
            (
                item
                for item in self.skill.resources
                if item.relative_path == relative_path
            ),
            None,
        )
        if resource is None:
            raise SearchRuntimeError(
                f"search-paper does not register required resource {relative_path}"
            )
        return resource.path

    def _safe_runs(
        self, snapshot: ResearchSnapshot
    ) -> Tuple[Tuple[Path, Dict[str, Any]], ...]:
        root = self.settings.repository_root.resolve()
        runs = []
        for relative in snapshot.corpus.search_run_paths:
            path = (root / relative).resolve()
            if _is_within(path, root) and path.is_file():
                runs.append((path, _load_yaml(path)))
        return tuple(
            sorted(
                runs,
                key=lambda item: (
                    int((item[1].get("run") or {}).get("round") or 0),
                    str((item[1].get("run") or {}).get("created_at") or ""),
                    item[0].as_posix(),
                ),
            )
        )

    def _validate_staged(self, path: Path) -> Tuple[str, ...]:
        validator = self._resource("scripts/validate_search_run.py")
        completed = subprocess.run(
            [sys.executable, str(validator), str(path), "--fix-metrics", "--json"],
            cwd=str(self.settings.repository_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout_seconds,
            check=False,
        )
        try:
            issues = json.loads(completed.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise SearchRuntimeError(
                "search-run validator returned non-JSON output: "
                + (completed.stderr or completed.stdout)[:500]
            ) from exc
        errors = [
            item
            for item in issues
            if isinstance(item, Mapping) and item.get("severity") == "error"
        ]
        if completed.returncode or errors:
            detail = "; ".join(
                f"{item.get('path')}: {item.get('message')}" for item in errors[:8]
            )
            raise SearchRuntimeError(
                "search-run validation failed"
                + (f": {detail}" if detail else f" (exit {completed.returncode})")
            )
        return tuple(
            f"{item.get('path')}: {item.get('message')}"
            for item in issues
            if isinstance(item, Mapping) and item.get("severity") == "warning"
        )

    def _publish_new(self, path: Path, payload: Mapping[str, Any]) -> Tuple[str, ...]:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                delete=False,
                dir=str(path.parent),
                prefix=f".{path.stem}-stage-",
                suffix=".yaml",
            ) as handle:
                handle.write(_render_yaml(payload))
                temporary = Path(handle.name)
            warnings = self._validate_staged(temporary)
            if path.exists():
                raise SearchRuntimeError(f"Refusing to overwrite search run: {path}")
            os.replace(temporary, path)
            temporary = None
            return warnings
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def _publish_update(
        self, path: Path, payload: Mapping[str, Any]
    ) -> Tuple[str, ...]:
        temporary: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                delete=False,
                dir=str(path.parent),
                prefix=f".{path.stem}-stage-",
                suffix=".yaml",
            ) as handle:
                handle.write(_render_yaml(payload))
                temporary = Path(handle.name)
            warnings = self._validate_staged(temporary)
            os.replace(temporary, path)
            temporary = None
            return warnings
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def plan_run(
        self,
        *,
        gap: ResearchGap,
        snapshot: ResearchSnapshot,
        max_queries: Optional[int] = None,
        max_candidates: Optional[int] = None,
        artifact_context: Optional[SemanticArtifactContext] = None,
    ) -> SearchPlanResult:
        if max_queries is not None and max_queries < 1:
            raise ValueError("max_queries must be positive")
        if max_candidates is not None and max_candidates < 1:
            raise ValueError("max_candidates must be positive")
        runs = self._safe_runs(snapshot)
        previous = runs[-1][1] if runs else None
        scope = copy.deepcopy((previous or {}).get("scope") or {})
        if not scope:
            scope = {
                "included_concepts": list(gap.search_focus),
                "excluded_concepts": [],
                "required_facets": list(snapshot.taxonomy.required_facets),
                "years": {"from": None, "to": None},
                "venues": [],
                "categories": [],
                "sources": ["arxiv"],
                "assumptions": [],
                "unresolved_questions": list(
                    snapshot.taxonomy.unresolved_scope_questions
                ),
            }
        prior_queries = []
        for _, run in runs:
            for query in run.get("queries") or []:
                if isinstance(query, Mapping):
                    prior_queries.append(
                        {
                            "id": query.get("id"),
                            "family": query.get("family"),
                            "text": query.get("text"),
                            "purpose": query.get("purpose"),
                            "target_facets": query.get("target_facets") or [],
                            "filters": query.get("filters") or {},
                            "status": (query.get("execution") or {}).get("status"),
                        }
                    )
        draft = self.engine.plan(
            gap=gap,
            snapshot=snapshot,
            skill=self.skill,
            scope=scope,
            prior_queries=prior_queries[-80:],
        )
        artifact_ids: list[str] = []
        if self.artifact_recorder is not None and artifact_context is not None:
            artifact = self.artifact_recorder.record(
                kind="search-plan",
                context=artifact_context,
                skill=self.skill,
                schema_resources=(
                    "references/search-strategy.md",
                    "references/search-output-schema.md",
                    "assets/search-run-template.yaml",
                ),
                output=draft,
            )
            artifact_ids.append(artifact.artifact_id)
        previous_texts = {
            _normalized_query(str(item.get("text") or "")) for item in prior_queries
        }
        unique_queries = []
        seen = set(previous_texts)
        required_facets = set(str(item) for item in scope.get("required_facets") or [])
        for query in draft.queries:
            signature = _normalized_query(query.text)
            if not signature or signature in seen:
                continue
            unknown_facets = set(query.target_facets) - required_facets
            if required_facets and unknown_facets:
                raise SearchRuntimeError(
                    "Search plan targets facets outside the research scope: "
                    + ", ".join(sorted(unknown_facets))
                )
            seen.add(signature)
            unique_queries.append(query)
            if max_queries is not None and len(unique_queries) >= max_queries:
                break
        if not unique_queries:
            raise SearchRuntimeError("Search planner produced only duplicate queries")

        template = yaml.safe_load(
            self.skill.read_resource("assets/search-run-template.yaml")
        )
        if not isinstance(template, dict):
            raise SearchRuntimeError("search-run template is not a YAML mapping")
        round_number = (
            max(
                (int((run.get("run") or {}).get("round") or 0) for _, run in runs),
                default=0,
            )
            + 1
        )
        now = _utc_now()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"{snapshot.research_id}-{stamp}-r{round_number:02d}"
        destination = (
            self.settings.research_root
            / snapshot.research_id
            / "search-runs"
            / f"{run_id}.yaml"
        ).resolve()
        if not _is_within(destination, self.settings.research_root.resolve()):
            raise SearchRuntimeError("Planned search-run path escapes research root")

        provider_source = str(
            ((previous or {}).get("run") or {}).get("provider", {}).get("source")
            or (scope.get("sources") or ["arxiv"])[0]
        )
        question = str(
            ((previous or {}).get("run") or {}).get("question") or gap.question
        )
        template["run"].update(
            {
                "id": run_id,
                "topic_slug": snapshot.research_id,
                "question": question,
                "created_at": now,
                "updated_at": now,
                "status": "planned",
                "round": round_number,
                "stop_reason": None,
            }
        )
        template["run"]["provider"].update(
            {
                "name": "deepxiv",
                "interface": "deepxiv-sdk",
                "package_version": _package_version("deepxiv-sdk"),
                "source": provider_source,
            }
        )
        template["run"]["budget"].update(
            {
                "max_queries": len(unique_queries),
                "max_candidates": max_candidates,
                "max_rounds": max(
                    round_number,
                    int(
                        ((previous or {}).get("run") or {})
                        .get("budget", {})
                        .get("max_rounds")
                        or 3
                    ),
                ),
            }
        )
        template["scope"] = scope
        assumptions = list(scope.get("assumptions") or [])
        assumptions.extend(draft.assumptions)
        scope["assumptions"] = list(
            dict.fromkeys(str(item) for item in assumptions if item)
        )
        template["seeds"] = copy.deepcopy((previous or {}).get("seeds") or [])
        queries = []
        for index, query in enumerate(unique_queries, start=1):
            query_id = f"R{round_number:02d}Q{index:02d}"
            queries.append(
                {
                    "id": query_id,
                    "round": round_number,
                    "family": query.family,
                    "text": query.text,
                    "purpose": query.purpose,
                    "target_facets": list(query.target_facets),
                    "derived_from": query.derived_from,
                    "filters": query.filters.model_dump(mode="json"),
                    "execution": {
                        "status": "planned",
                        "executed_at": None,
                        "provider_total_count": None,
                        "retrieved_count": None,
                        "retained_count": None,
                        "raw_result_path": None,
                        "error_id": None,
                    },
                }
            )
        template["queries"] = queries
        template["candidates"] = []
        facet_to_queries: Dict[str, list[str]] = {}
        for query in queries:
            for facet in query["target_facets"]:
                facet_to_queries.setdefault(facet, []).append(query["id"])
        template["coverage"]["facets"] = [
            {
                "name": facet,
                "status": "missing",
                "candidate_ids": [],
                "note": f"Gap-directed round {round_number}; awaiting retrieval.",
                "next_query": "/".join(facet_to_queries.get(facet, [])) or None,
            }
            for facet in scope.get("required_facets") or []
        ]
        template["coverage"]["gaps"] = [gap.question]
        template["limitations"] = list(
            dict.fromkeys(
                [
                    *list((previous or {}).get("limitations") or []),
                    "Candidate screening is based on metadata or abstracts, not full-paper evidence.",
                ]
            )
        )
        template["loop_review"]["proposed_rules"] = [
            f"Round {round_number} planner rationale: {draft.rationale}"
        ]
        warnings = self._publish_new(destination, template)
        if self.artifact_recorder is not None:
            self.artifact_recorder.link_publication(
                artifact_ids,
                action_id=artifact_context.action_id if artifact_context else "",
                changed_sources=(
                    destination.relative_to(self.settings.repository_root).as_posix(),
                ),
            )
        return SearchPlanResult(
            run_path=destination,
            query_ids=tuple(query["id"] for query in queries),
            model_calls=1,
            warnings=warnings,
            semantic_artifact_ids=tuple(artifact_ids),
        )

    def _existing_papers(self) -> Tuple[Mapping[str, Any], ...]:
        index = build_index(self.settings.wiki_root, self.settings.wiki_meta_root)
        records = []
        for entity_id, entity in sorted(index.unique_entities().items()):
            if entity.entity_type != "paper":
                continue
            records.append(
                {
                    "id": entity_id,
                    "title": entity.title,
                    "identifiers": entity.metadata.get("identifiers") or {},
                    "status": entity.metadata.get("status"),
                }
            )
        return tuple(records)

    def _wiki_paper_match(
        self,
        candidate: Mapping[str, Any],
        existing: Sequence[Mapping[str, Any]],
    ) -> Optional[str]:
        source = str(candidate.get("source") or "")
        source_id = (
            _normalize_arxiv(candidate.get("source_id")) if source == "arxiv" else ""
        )
        doi = str(candidate.get("doi") or "").strip().casefold()
        normalized_title = " ".join(
            str(candidate.get("title") or "").casefold().split()
        )
        for record in existing:
            identifiers = record.get("identifiers") or {}
            if source_id and _normalize_arxiv(identifiers.get("arxiv")) == source_id:
                return str(record.get("id"))
            if doi and str(identifiers.get("doi") or "").strip().casefold() == doi:
                return str(record.get("id"))
            title = " ".join(str(record.get("title") or "").casefold().split())
            if normalized_title and title == normalized_title:
                return str(record.get("id"))
        return None

    @staticmethod
    def _candidate_payload(candidate: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            key: candidate.get(key)
            for key in (
                "candidate_id",
                "title",
                "authors",
                "year",
                "venue",
                "abstract",
                "tldr",
                "categories",
                "citation_count",
                "paper_url",
                "pdf_url",
                "repository_url",
            )
        }

    @staticmethod
    def _recompute_candidate_coverage(run: Dict[str, Any]) -> None:
        candidates = {
            str(item.get("candidate_id")): item
            for item in run.get("candidates") or []
            if isinstance(item, Mapping) and item.get("candidate_id")
        }
        for facet in (run.get("coverage") or {}).get("facets") or []:
            if not isinstance(facet, dict) or not facet.get("name"):
                continue
            name = str(facet["name"])
            matched = []
            core_count = 0
            for candidate_id, candidate in candidates.items():
                relevance = candidate.get("relevance") or {}
                label = relevance.get("label")
                if name not in (candidate.get("target_facets") or []):
                    continue
                if label not in {"core", "adjacent", "background"}:
                    continue
                matched.append(candidate_id)
                core_count += int(label == "core")
            facet["candidate_ids"] = sorted(matched)
            if core_count >= 2:
                facet["status"] = "covered"
            elif matched:
                facet["status"] = "partial"
            else:
                facet["status"] = "missing"
            facet["note"] = (
                f"Deterministic screening aggregation: {core_count} core and "
                f"{len(matched) - core_count} other retained candidates."
            )

    def screen_run(
        self,
        *,
        run_path: Path,
        gap: ResearchGap,
        artifact_context: Optional[SemanticArtifactContext] = None,
    ) -> SearchScreeningResult:
        root = self.settings.repository_root.resolve()
        path = run_path.resolve()
        if not _is_within(path, root) or not path.is_file():
            raise SearchRuntimeError(
                "Search-run screening path is unavailable or unsafe"
            )
        run = _load_yaml(path)
        raw_candidates = [
            item
            for item in run.get("candidates") or []
            if isinstance(item, dict)
            and (item.get("relevance") or {}).get("label") is None
            and item.get("review_state")
            not in {"staged-for-wiki", "ingested", "excluded"}
        ]
        if not raw_candidates:
            return SearchScreeningResult(False, 0, 0, 0, 0)
        existing = self._existing_papers()
        screenings: Dict[str, CandidateScreening] = {}
        artifact_ids: list[str] = []
        model_calls = 0
        for offset in range(0, len(raw_candidates), self.screening_batch_size):
            batch = raw_candidates[offset : offset + self.screening_batch_size]
            result = self.engine.screen(
                gap=gap,
                skill=self.skill,
                scope=run.get("scope") or {},
                candidates=[self._candidate_payload(item) for item in batch],
                existing_papers=existing,
            )
            model_calls += 1
            expected = {str(item.get("candidate_id")) for item in batch}
            returned = {item.candidate_id for item in result.screenings}
            if expected != returned:
                missing = sorted(expected - returned)
                unexpected = sorted(returned - expected)
                raise SearchRuntimeError(
                    "Candidate screener must return every requested ID exactly once; "
                    f"missing={missing}, unexpected={unexpected}"
                )
            if len(returned) != len(result.screenings):
                raise SearchRuntimeError("Candidate screener returned duplicate IDs")
            if self.artifact_recorder is not None and artifact_context is not None:
                artifact = self.artifact_recorder.record(
                    kind="candidate-screening",
                    context=artifact_context.with_updates(
                        search_run_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                        source_ids=tuple(sorted(expected)),
                    ),
                    skill=self.skill,
                    schema_resources=(
                        "references/search-strategy.md",
                        "references/search-output-schema.md",
                    ),
                    output=result,
                )
                artifact_ids.append(artifact.artifact_id)
            screenings.update({item.candidate_id: item for item in result.screenings})

        queries = {
            str(item.get("id")): item
            for item in run.get("queries") or []
            if isinstance(item, Mapping) and item.get("id")
        }
        required_facets = set((run.get("scope") or {}).get("required_facets") or [])
        for candidate in raw_candidates:
            candidate_id = str(candidate.get("candidate_id"))
            screening = screenings[candidate_id]
            facets = list(screening.target_facets)
            for discovery in candidate.get("discovered_by") or []:
                if not isinstance(discovery, Mapping):
                    continue
                query = queries.get(str(discovery.get("query_id") or ""), {})
                facets.extend(query.get("target_facets") or [])
            facets = list(dict.fromkeys(str(item) for item in facets if item))
            if required_facets:
                facets = [item for item in facets if item in required_facets]
            candidate["target_facets"] = facets
            candidate["relevance"] = {
                "label": screening.label,
                "scores": screening.scores.model_dump(mode="json"),
                "reason": screening.reason,
                "basis": (
                    "title-and-abstract"
                    if str(candidate.get("abstract") or "").strip()
                    else "provider-metadata"
                ),
            }
            existing_id = self._wiki_paper_match(candidate, existing)
            if existing_id:
                candidate["existing_wiki_id"] = existing_id
            if screening.label == "exclude":
                candidate["review_state"] = "excluded"
                candidate["exclusion_reason"] = screening.reason
            else:
                candidate["review_state"] = "abstract-screened"

        currently_selected = sum(
            isinstance(item, Mapping)
            and item.get("review_state") == "selected-for-ingest"
            for item in run.get("candidates") or []
        )
        capacity = max(0, self.max_selected_candidates - currently_selected)
        eligible = []
        for candidate in raw_candidates:
            screening = screenings[str(candidate.get("candidate_id"))]
            if (
                screening.label == "core"
                and not candidate.get("existing_wiki_id")
                and candidate.get("review_state") == "abstract-screened"
            ):
                eligible.append(
                    (
                        -int(screening.select_for_ingest),
                        -screening.scores.total,
                        str(candidate.get("candidate_id")),
                        candidate,
                    )
                )
        selected = 0
        for _, _, _, candidate in sorted(eligible, key=lambda item: item[:3])[
            :capacity
        ]:
            candidate["review_state"] = "selected-for-ingest"
            selected += 1

        self._recompute_candidate_coverage(run)
        now = _utc_now()
        run_header = run.get("run")
        if isinstance(run_header, dict):
            run_header["updated_at"] = now
            remaining = any(
                isinstance(item, Mapping)
                and (item.get("relevance") or {}).get("label") is None
                for item in run.get("candidates") or []
            )
            run_header["status"] = "needs-review" if remaining else "partial"
        warnings = self._publish_update(path, run)
        if self.artifact_recorder is not None:
            self.artifact_recorder.link_publication(
                artifact_ids,
                action_id=artifact_context.action_id if artifact_context else "",
                changed_sources=(
                    path.relative_to(self.settings.repository_root).as_posix(),
                ),
            )
        return SearchScreeningResult(
            changed=True,
            triaged_candidates=len(raw_candidates),
            selected_candidates=selected,
            excluded_candidates=sum(
                screenings[str(item.get("candidate_id"))].label == "exclude"
                for item in raw_candidates
            ),
            model_calls=model_calls,
            warnings=warnings,
            semantic_artifact_ids=tuple(artifact_ids),
        )


__all__ = [
    "CandidateScreening",
    "CandidateScreeningBatch",
    "CandidateScores",
    "LangChainSearchSemanticEngine",
    "SearchFilterDraft",
    "SearchPlanDraft",
    "SearchPlanResult",
    "SearchQueryDraft",
    "SearchRuntime",
    "SearchRuntimeError",
    "SearchScreeningResult",
    "SearchSemanticEngine",
]
