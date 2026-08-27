"""Deterministic action executors for the autonomous outer research loop."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Protocol, Sequence, Tuple

import yaml  # type: ignore[import-untyped]

from .config import HarnessSettings
from .evidence_verification import (
    EvidenceVerificationPipeline,
    EvidenceVerificationResult,
    VerificationPreconditionError,
)
from .ingest_models import IngestCandidate, PaperIngestResult
from .nonconsensus_analysis import (
    NonConsensusAnalysisPipeline,
    NonConsensusAnalysisResult,
    NonConsensusPreconditionError,
)
from .paper_ingest import PaperIngestPipeline
from .paper_sources import (
    ArxivPaperSourceAcquirer,
    PaperSourceAcquirer,
)
from .research_models import (
    ActionOutcome,
    ActionStatus,
    ResearchAction,
    ResearchActionResult,
    ResearchDecision,
    ResearchGap,
    ResearchSnapshot,
)
from .skill_registry import SkillRegistry
from .search_runtime import SearchRuntime


QUERY_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
ELIGIBLE_SEARCH_STATUSES = {"planned", "blocked-credential"}
EXECUTED_SEARCH_STATUSES = {"succeeded", "empty", "failed"}


class IngestPipeline(Protocol):
    @property
    def requires_network(self) -> bool: ...

    def ingest(
        self,
        candidate: IngestCandidate,
        *,
        preview: bool = False,
    ) -> PaperIngestResult: ...


class VerificationPipeline(Protocol):
    @property
    def requires_network(self) -> bool: ...

    def verify_next(
        self,
        *,
        gap: ResearchGap,
        snapshot: ResearchSnapshot,
    ) -> EvidenceVerificationResult: ...


class ClaimAnalysisPipeline(Protocol):
    @property
    def requires_network(self) -> bool: ...

    def analyze(
        self,
        *,
        gap: ResearchGap,
        snapshot: ResearchSnapshot,
    ) -> NonConsensusAnalysisResult: ...


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
        raise ValueError(f"Expected a YAML mapping in {path}")
    return payload


def _write_yaml_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    rendered = yaml.safe_dump(
        dict(payload),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
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
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _query_map(run: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    return {
        str(query.get("id")): query
        for query in (run.get("queries") or [])
        if isinstance(query, Mapping) and query.get("id")
    }


def _candidate_ids(run: Mapping[str, Any]) -> set[str]:
    return {
        str(candidate.get("candidate_id"))
        for candidate in (run.get("candidates") or [])
        if isinstance(candidate, Mapping) and candidate.get("candidate_id")
    }


def _content_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _relative_path(settings: HarnessSettings, path: Path) -> str:
    return path.resolve().relative_to(settings.repository_root.resolve()).as_posix()


def _error_codes(run: Mapping[str, Any], query_ids: Sequence[str]) -> Tuple[str, ...]:
    selected = set(query_ids)
    codes = []
    for item in run.get("errors") or []:
        if not isinstance(item, Mapping):
            continue
        query_id = item.get("query_id")
        if query_id and str(query_id) not in selected:
            continue
        value = item.get("error_class") or item.get("code") or item.get("id")
        if value:
            codes.append(str(value))
    return tuple(dict.fromkeys(codes))


def _safe_error(exc: BaseException, *, limit: int = 500) -> str:
    message = str(exc)
    for name in ("DEEPXIV_TOKEN", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
        secret = os.getenv(name, "")
        if secret:
            message = message.replace(secret, "[REDACTED]")
    return f"{type(exc).__name__}: {message[:limit]}"


class DeterministicActionExecutor:
    """Dispatch a finite action set without an LLM or a smart Skill router."""

    def __init__(
        self,
        settings: HarnessSettings,
        *,
        timeout_seconds: int = 300,
        ingest_pipeline: Optional[IngestPipeline] = None,
        search_runtime: Optional[SearchRuntime] = None,
        paper_source_acquirer: Optional[PaperSourceAcquirer] = None,
        verification_pipeline: Optional[VerificationPipeline] = None,
        claim_analysis_pipeline: Optional[ClaimAnalysisPipeline] = None,
    ):
        if timeout_seconds < 1:
            raise ValueError("timeout_seconds must be positive")
        self.settings = settings
        self.timeout_seconds = timeout_seconds
        self.registry = SkillRegistry(settings.skills_root)
        self._ingest_pipeline = ingest_pipeline
        self._search_runtime = search_runtime
        self._verification_pipeline = verification_pipeline
        self._claim_analysis_pipeline = claim_analysis_pipeline
        self._paper_source_acquirer = paper_source_acquirer or ArxivPaperSourceAcquirer(
            settings.repository_root,
            timeout_seconds=min(timeout_seconds, 120),
        )

    @property
    def supported_actions(self) -> frozenset[ResearchAction]:
        actions: set[ResearchAction] = {"search"}
        if self._ingest_pipeline is not None or self.settings.model:
            actions.add("ingest")
        if self._verification_pipeline is not None or self.settings.model:
            actions.add("verify")
        if self._claim_analysis_pipeline is not None or self.settings.model:
            actions.add("analyze_claims")
        return frozenset(actions)

    def execute(
        self,
        *,
        decision: ResearchDecision,
        gap: Optional[ResearchGap],
        snapshot: ResearchSnapshot,
        action_id: str,
        allow_network: bool,
    ) -> ResearchActionResult:
        if decision.action == "search":
            if gap is None:
                raise ValueError("A search decision must resolve to its target gap")
            return self._execute_search(
                decision=decision,
                gap=gap,
                snapshot=snapshot,
                action_id=action_id,
                allow_network=allow_network,
            )
        if decision.action == "ingest":
            if gap is None:
                raise ValueError("An ingest decision must resolve to its target gap")
            return self._execute_ingest(
                decision=decision,
                gap=gap,
                snapshot=snapshot,
                action_id=action_id,
                allow_network=allow_network,
            )
        if decision.action == "verify":
            if gap is None:
                raise ValueError("A verify decision must resolve to its target gap")
            return self._execute_verify(
                decision=decision,
                gap=gap,
                snapshot=snapshot,
                action_id=action_id,
                allow_network=allow_network,
            )
        if decision.action == "analyze_claims":
            if gap is None:
                raise ValueError(
                    "An analyze_claims decision must resolve to its target gap"
                )
            return self._execute_analyze_claims(
                decision=decision,
                gap=gap,
                snapshot=snapshot,
                action_id=action_id,
                allow_network=allow_network,
            )
        return ResearchActionResult(
            action_id=action_id,
            action=decision.action,
            target_gap_id=decision.target_gap_id,
            status="blocked",
            outcome="unsupported",
            attempted=False,
            summary=(
                f"Action {decision.action!r} has no deterministic V1 executor. "
                "No deterministic executor is enabled for this action."
            ),
            error_codes=("unsupported-action",),
        )

    def _blocked_ingest(
        self,
        *,
        decision: ResearchDecision,
        action_id: str,
        code: str,
        summary: str,
    ) -> ResearchActionResult:
        return ResearchActionResult(
            action_id=action_id,
            action="ingest",
            target_gap_id=decision.target_gap_id,
            status="blocked",
            outcome="precondition_blocked",
            attempted=False,
            summary=summary,
            error_codes=(code,),
        )

    def _candidate_facets(
        self,
        candidate: Mapping[str, Any],
        run: Mapping[str, Any],
    ) -> Tuple[str, ...]:
        facets = list(candidate.get("target_facets") or [])
        queries = _query_map(run)
        for discovery in candidate.get("discovered_by") or []:
            if not isinstance(discovery, Mapping):
                continue
            query = queries.get(str(discovery.get("query_id") or ""), {})
            facets.extend(query.get("target_facets") or [])
        return tuple(dict.fromkeys(str(value) for value in facets if value))

    def _select_ingest_candidate(
        self,
        snapshot: ResearchSnapshot,
        gap: ResearchGap,
    ) -> Optional[IngestCandidate]:
        for _, _, relative, raw, facets in self._selected_candidate_records(
            snapshot, gap
        ):
            if not raw.get("local_pdf_path"):
                continue
            try:
                return IngestCandidate(
                    candidate_id=str(raw.get("candidate_id") or ""),
                    title=str(raw.get("title") or ""),
                    source=str(raw.get("source") or ""),
                    source_id=str(raw.get("source_id") or ""),
                    authors=tuple(str(value) for value in raw.get("authors") or []),
                    year=raw.get("year"),
                    venue=raw.get("venue"),
                    abstract=raw.get("abstract"),
                    paper_url=raw.get("paper_url"),
                    pdf_url=raw.get("pdf_url"),
                    doi=raw.get("doi"),
                    local_pdf_path=str(raw.get("local_pdf_path")),
                    target_facets=facets,
                    search_run_path=relative,
                )
            except (TypeError, ValueError):
                continue
        return None

    def _selected_candidate_records(
        self,
        snapshot: ResearchSnapshot,
        gap: ResearchGap,
    ) -> list[tuple[int, str, str, Mapping[str, Any], Tuple[str, ...]]]:
        repository_root = self.settings.repository_root.resolve()
        selections: list[tuple[int, str, str, Mapping[str, Any], Tuple[str, ...]]] = []
        for relative in sorted(snapshot.corpus.search_run_paths):
            run_path = (repository_root / relative).resolve()
            if not _is_within(run_path, repository_root) or not run_path.is_file():
                continue
            run = _load_yaml(run_path)
            for raw in run.get("candidates") or []:
                if not isinstance(raw, Mapping):
                    continue
                if raw.get("review_state") != "selected-for-ingest":
                    continue
                facets = self._candidate_facets(raw, run)
                focus = {value.casefold() for value in gap.search_focus}
                score = sum(value.casefold() in focus for value in facets)
                selections.append(
                    (
                        -score,
                        str(raw.get("candidate_id") or ""),
                        relative,
                        raw,
                        facets,
                    )
                )
        return sorted(selections, key=lambda item: item[:3])

    def _acquire_selected_pdf(
        self,
        snapshot: ResearchSnapshot,
        gap: ResearchGap,
    ) -> Optional[str]:
        repository_root = self.settings.repository_root.resolve()
        for _, _, relative, raw, _ in self._selected_candidate_records(snapshot, gap):
            if raw.get("local_pdf_path"):
                continue
            acquired = self._paper_source_acquirer.acquire(raw)
            run_path = (repository_root / relative).resolve()
            run = _load_yaml(run_path)
            records = run.get("candidates")
            if not isinstance(records, list):
                raise ValueError("Candidate search-run has no candidate list")
            matched = next(
                (
                    item
                    for item in records
                    if isinstance(item, dict)
                    and item.get("candidate_id") == raw.get("candidate_id")
                ),
                None,
            )
            if matched is None or matched.get("review_state") != "selected-for-ingest":
                raise ValueError("Selected candidate changed during PDF acquisition")
            matched["local_pdf_path"] = acquired.relative_path
            matched["source_acquisition"] = {
                "source_url": acquired.source_url,
                "sha256": acquired.sha256,
                "size_bytes": acquired.size_bytes,
                "downloaded": acquired.downloaded,
                "acquired_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            header = run.get("run")
            if isinstance(header, dict):
                header["updated_at"] = datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                )
            _write_yaml_atomic(run_path, run)
            return _relative_path(self.settings, run_path)
        return None

    def _promote_screened_core_candidate(
        self,
        snapshot: ResearchSnapshot,
        gap: ResearchGap,
    ) -> Optional[str]:
        root = self.settings.repository_root.resolve()
        focus = {value.casefold() for value in gap.search_focus}
        choices = []
        for relative in sorted(snapshot.corpus.search_run_paths):
            path = (root / relative).resolve()
            if not _is_within(path, root) or not path.is_file():
                continue
            run = _load_yaml(path)
            for candidate in run.get("candidates") or []:
                if not isinstance(candidate, Mapping):
                    continue
                if candidate.get("review_state") != "abstract-screened":
                    continue
                if (candidate.get("relevance") or {}).get("label") != "core":
                    continue
                if candidate.get("existing_wiki_id"):
                    continue
                facets = self._candidate_facets(candidate, run)
                facet_score = sum(value.casefold() in focus for value in facets)
                scores = (candidate.get("relevance") or {}).get("scores") or {}
                total_score = sum(
                    int(value)
                    for value in scores.values()
                    if isinstance(value, int) and not isinstance(value, bool)
                )
                choices.append(
                    (
                        -facet_score,
                        -total_score,
                        str(candidate.get("candidate_id") or ""),
                        relative,
                    )
                )
        if not choices:
            return None
        _, _, candidate_id, relative = sorted(choices)[0]
        path = (root / relative).resolve()
        run = _load_yaml(path)
        matched = next(
            (
                item
                for item in run.get("candidates") or []
                if isinstance(item, dict) and item.get("candidate_id") == candidate_id
            ),
            None,
        )
        if matched is None or matched.get("review_state") != "abstract-screened":
            raise ValueError("Core candidate changed during ingest handoff selection")
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        matched["review_state"] = "selected-for-ingest"
        matched["selection"] = {
            "selected_at": timestamp,
            "selected_by": "deterministic-core-handoff",
            "target_gap_id": gap.id,
        }
        header = run.get("run")
        if isinstance(header, dict):
            header["updated_at"] = timestamp
        _write_yaml_atomic(path, run)
        return _relative_path(self.settings, path)

    def _pipeline(self) -> IngestPipeline:
        pipeline = self._ingest_pipeline
        if pipeline is None:
            pipeline = PaperIngestPipeline(self.settings)
            self._ingest_pipeline = pipeline
        return pipeline

    def _semantic_search_runtime(self) -> Optional[SearchRuntime]:
        runtime = self._search_runtime
        if runtime is None and self.settings.model:
            runtime = SearchRuntime(
                self.settings,
                timeout_seconds=min(self.timeout_seconds, 180),
            )
            self._search_runtime = runtime
        return runtime

    def _verification(self) -> VerificationPipeline:
        pipeline = self._verification_pipeline
        if pipeline is None:
            pipeline = EvidenceVerificationPipeline(self.settings)
            self._verification_pipeline = pipeline
        return pipeline

    def _execute_verify(
        self,
        *,
        decision: ResearchDecision,
        gap: ResearchGap,
        snapshot: ResearchSnapshot,
        action_id: str,
        allow_network: bool,
    ) -> ResearchActionResult:
        try:
            pipeline = self._verification()
        except VerificationPreconditionError as exc:
            return ResearchActionResult(
                action_id=action_id,
                action="verify",
                target_gap_id=decision.target_gap_id,
                status="blocked",
                outcome="precondition_blocked",
                attempted=False,
                summary=str(exc),
                error_codes=("verification-model-required",),
            )
        if pipeline.requires_network and not allow_network:
            return ResearchActionResult(
                action_id=action_id,
                action="verify",
                target_gap_id=decision.target_gap_id,
                status="blocked",
                outcome="precondition_blocked",
                attempted=False,
                summary=(
                    "Semantic evidence verification requires explicit network "
                    "authorization for this invocation."
                ),
                error_codes=("verification-network-disabled",),
            )
        try:
            result = pipeline.verify_next(gap=gap, snapshot=snapshot)
        except VerificationPreconditionError as exc:
            return ResearchActionResult(
                action_id=action_id,
                action="verify",
                target_gap_id=decision.target_gap_id,
                status="blocked",
                outcome="precondition_blocked",
                attempted=False,
                summary=str(exc),
                error_codes=("verification-target-not-ready",),
            )
        except Exception as exc:
            return ResearchActionResult(
                action_id=action_id,
                action="verify",
                target_gap_id=decision.target_gap_id,
                status="failed",
                outcome="tool_failure",
                attempted=True,
                tool_calls=1,
                summary=f"verify-evidence failed: {_safe_error(exc)}",
                error_codes=("verification-failed",),
            )
        changed_sources = tuple(f"wiki/{path}" for path in result.changed_paths)
        positive = bool(result.verified_entity_ids)
        return ResearchActionResult(
            action_id=action_id,
            action="verify",
            target_gap_id=decision.target_gap_id,
            status="success",
            outcome="positive" if positive else "negative_research_result",
            attempted=True,
            tool_calls=result.model_calls,
            changed_sources=changed_sources,
            summary=(
                f"verify-evidence processed {result.target_kind} {result.target_id}: "
                f"{len(result.verified_entity_ids)} verified and "
                f"{len(result.unresolved_entity_ids)} retained for review."
            ),
            error_codes=(),
            metrics={
                "verification_targets": 1,
                "entities_verified": len(result.verified_entity_ids),
                "entities_unresolved": len(result.unresolved_entity_ids),
                "pages_changed": len(result.changed_paths),
            },
        )

    def _claim_analysis(self) -> ClaimAnalysisPipeline:
        pipeline = self._claim_analysis_pipeline
        if pipeline is None:
            pipeline = NonConsensusAnalysisPipeline(self.settings)
            self._claim_analysis_pipeline = pipeline
        return pipeline

    def _execute_analyze_claims(
        self,
        *,
        decision: ResearchDecision,
        gap: ResearchGap,
        snapshot: ResearchSnapshot,
        action_id: str,
        allow_network: bool,
    ) -> ResearchActionResult:
        try:
            pipeline = self._claim_analysis()
        except NonConsensusPreconditionError as exc:
            return ResearchActionResult(
                action_id=action_id,
                action="analyze_claims",
                target_gap_id=decision.target_gap_id,
                status="blocked",
                outcome="precondition_blocked",
                attempted=False,
                summary=str(exc),
                error_codes=("claim-analysis-model-required",),
            )
        if pipeline.requires_network and not allow_network:
            return ResearchActionResult(
                action_id=action_id,
                action="analyze_claims",
                target_gap_id=decision.target_gap_id,
                status="blocked",
                outcome="precondition_blocked",
                attempted=False,
                summary=(
                    "Semantic claim comparison requires explicit network authorization "
                    "for this invocation."
                ),
                error_codes=("claim-analysis-network-disabled",),
            )
        try:
            result = pipeline.analyze(gap=gap, snapshot=snapshot)
        except NonConsensusPreconditionError as exc:
            return ResearchActionResult(
                action_id=action_id,
                action="analyze_claims",
                target_gap_id=decision.target_gap_id,
                status="blocked",
                outcome="precondition_blocked",
                attempted=False,
                summary=str(exc),
                error_codes=("claim-analysis-inputs-not-ready",),
            )
        except Exception as exc:
            return ResearchActionResult(
                action_id=action_id,
                action="analyze_claims",
                target_gap_id=decision.target_gap_id,
                status="failed",
                outcome="tool_failure",
                attempted=True,
                tool_calls=1,
                summary=f"analyze-claims failed: {_safe_error(exc)}",
                error_codes=("claim-analysis-failed",),
            )
        changed_sources = tuple(f"wiki/{path}" for path in result.changed_paths)
        return ResearchActionResult(
            action_id=action_id,
            action="analyze_claims",
            target_gap_id=decision.target_gap_id,
            status="success",
            outcome="positive",
            attempted=True,
            tool_calls=result.model_calls,
            changed_sources=changed_sources,
            summary=(
                f"analyze-claims created {result.assessment_id} with result "
                f"{result.result}; it remains needs-review pending independent verification."
            ),
            error_codes=(),
            metrics={
                "assessments_created": 1,
                "pages_changed": len(result.changed_paths),
            },
        )

    def _mark_candidate_ingested(
        self,
        candidate: IngestCandidate,
        result: PaperIngestResult,
    ) -> str:
        if result.status not in {"published", "no-change"}:
            raise ValueError("Only a published ingestion can close a candidate handoff")
        repository_root = self.settings.repository_root.resolve()
        run_path = (repository_root / candidate.search_run_path).resolve()
        if not _is_within(run_path, repository_root) or not run_path.is_file():
            raise ValueError("Candidate search-run path is unavailable or unsafe")
        run = _load_yaml(run_path)
        records = run.get("candidates")
        if not isinstance(records, list):
            raise ValueError("Candidate search-run has no candidate list")
        matched = None
        for record in records:
            if (
                isinstance(record, dict)
                and record.get("candidate_id") == candidate.candidate_id
            ):
                matched = record
                break
        if matched is None:
            raise ValueError("Selected candidate disappeared from its search-run")
        if matched.get("review_state") not in {"selected-for-ingest", "ingested"}:
            raise ValueError("Selected candidate changed state during ingestion")
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        matched["review_state"] = "ingested"
        matched["ingest"] = {
            "paper_id": result.paper_id,
            "status": result.status,
            "ingested_at": timestamp,
            "wiki_paths": [f"wiki/{path}" for path in result.changed_paths],
            "diagnostic_codes": list(result.diagnostic_codes),
        }
        run_metadata = run.get("run")
        if isinstance(run_metadata, dict):
            run_metadata["updated_at"] = timestamp
        _write_yaml_atomic(run_path, run)
        return _relative_path(self.settings, run_path)

    def _execute_ingest(
        self,
        *,
        decision: ResearchDecision,
        gap: ResearchGap,
        snapshot: ResearchSnapshot,
        action_id: str,
        allow_network: bool,
    ) -> ResearchActionResult:
        pipeline = self._pipeline()
        if pipeline.requires_network and not allow_network:
            return self._blocked_ingest(
                decision=decision,
                action_id=action_id,
                code="network-disabled",
                summary=(
                    "The configured semantic paper extractor requires network access. "
                    "Re-run with explicit network authorization or inject a local extractor."
                ),
            )
        candidate = self._select_ingest_candidate(snapshot, gap)
        promotion_source: Optional[str] = None
        if candidate is None:
            promotion_source = self._promote_screened_core_candidate(snapshot, gap)
            if promotion_source:
                candidate = self._select_ingest_candidate(snapshot, gap)
        acquisition_source: Optional[str] = None
        acquisition_calls = 0
        if candidate is None:
            selected = self._selected_candidate_records(snapshot, gap)
            missing_source = any(not item[3].get("local_pdf_path") for item in selected)
            if not missing_source:
                return self._blocked_ingest(
                    decision=decision,
                    action_id=action_id,
                    code="selected-local-pdf-required",
                    summary=(
                        "No selected-for-ingest candidate has valid metadata and an explicit "
                        "repository-relative local_pdf_path."
                    ),
                )
            if not allow_network and self._paper_source_acquirer.requires_network:
                return self._blocked_ingest(
                    decision=decision,
                    action_id=action_id,
                    code="paper-source-network-disabled",
                    summary=(
                        "A selected candidate needs an explicit local PDF. Re-run with "
                        "network authorization to acquire its public arXiv source, or set "
                        "local_pdf_path manually."
                    ),
                )
            try:
                acquisition_source = self._acquire_selected_pdf(snapshot, gap)
                acquisition_calls = 1
                candidate = self._select_ingest_candidate(snapshot, gap)
            except Exception as exc:
                return ResearchActionResult(
                    action_id=action_id,
                    action="ingest",
                    target_gap_id=decision.target_gap_id,
                    status="failed",
                    outcome="tool_failure",
                    attempted=True,
                    tool_calls=1,
                    summary=(
                        "Selected paper source acquisition failed: "
                        f"{_safe_error(exc)}"
                    ),
                    changed_sources=(promotion_source,) if promotion_source else (),
                    error_codes=("paper-source-acquisition-failed",),
                    metrics={"paper_sources_attempted": 1},
                )
            if candidate is None:
                return ResearchActionResult(
                    action_id=action_id,
                    action="ingest",
                    target_gap_id=decision.target_gap_id,
                    status="failed",
                    outcome="tool_failure",
                    attempted=True,
                    tool_calls=acquisition_calls,
                    changed_sources=tuple(
                        value
                        for value in (promotion_source, acquisition_source)
                        if value
                    ),
                    summary="Paper source was acquired but did not produce a valid ingest handoff.",
                    error_codes=("paper-source-handoff-invalid",),
                    metrics={"paper_sources_acquired": int(bool(acquisition_source))},
                )
        try:
            result = pipeline.ingest(candidate)
        except Exception as exc:
            return ResearchActionResult(
                action_id=action_id,
                action="ingest",
                target_gap_id=decision.target_gap_id,
                status="failed",
                outcome="tool_failure",
                attempted=True,
                tool_calls=1 + acquisition_calls,
                changed_sources=tuple(
                    dict.fromkeys(
                        value
                        for value in (promotion_source, acquisition_source)
                        if value
                    )
                ),
                summary=f"ingest-paper failed: {_safe_error(exc)}",
                error_codes=("ingest-paper-failed",),
                metrics={
                    "candidates_selected": 1,
                    "paper_sources_acquired": int(bool(acquisition_source)),
                },
            )
        wiki_sources = tuple(f"wiki/{relative}" for relative in result.changed_paths)
        try:
            handoff_source = self._mark_candidate_ingested(candidate, result)
        except Exception as exc:
            return ResearchActionResult(
                action_id=action_id,
                action="ingest",
                target_gap_id=decision.target_gap_id,
                status="partial",
                outcome="tool_failure",
                attempted=True,
                tool_calls=1 + acquisition_calls,
                changed_sources=tuple(
                    dict.fromkeys(
                        (
                            *wiki_sources,
                            *((promotion_source,) if promotion_source else ()),
                            *((acquisition_source,) if acquisition_source else ()),
                        )
                    )
                ),
                summary=(
                    f"ingest-paper published {result.paper_id}, but the candidate "
                    f"handoff state could not be updated: {_safe_error(exc, limit=400)}"
                ),
                error_codes=("ingest-handoff-update-failed",),
                metrics={
                    "candidates_selected": 1,
                    "entities_created": len(result.created_entity_ids),
                    "pages_changed": len(wiki_sources),
                    "paper_sources_acquired": int(bool(acquisition_source)),
                },
            )
        changed_sources = tuple(
            dict.fromkeys(
                (
                    *wiki_sources,
                    handoff_source,
                    *((promotion_source,) if promotion_source else ()),
                    *((acquisition_source,) if acquisition_source else ()),
                )
            )
        )
        return ResearchActionResult(
            action_id=action_id,
            action="ingest",
            target_gap_id=decision.target_gap_id,
            status="success",
            outcome="positive",
            attempted=True,
            tool_calls=1 + acquisition_calls,
            changed_sources=changed_sources,
            summary=(
                f"ingest-paper processed {result.candidate_id} into {result.paper_id}: "
                f"{len(result.created_entity_ids)} entities created, "
                f"{len(result.reused_entity_ids)} reused, "
                f"{len(wiki_sources)} Wiki source pages changed, and the candidate "
                "handoff was closed."
            ),
            error_codes=(),
            metrics={
                "candidates_selected": 1,
                "entities_created": len(result.created_entity_ids),
                "entities_reused": len(result.reused_entity_ids),
                "pages_changed": len(wiki_sources),
                "candidate_states_updated": 1,
                "pdf_pages": result.pdf_pages,
                "paper_sources_acquired": int(bool(acquisition_source)),
            },
        )

    def _blocked_search(
        self,
        *,
        decision: ResearchDecision,
        action_id: str,
        code: str,
        summary: str,
    ) -> ResearchActionResult:
        return ResearchActionResult(
            action_id=action_id,
            action="search",
            target_gap_id=decision.target_gap_id,
            status="blocked",
            outcome="precondition_blocked",
            attempted=False,
            summary=summary,
            error_codes=(code,),
        )

    def _select_search_run(
        self,
        snapshot: ResearchSnapshot,
        gap: ResearchGap,
    ) -> Tuple[Optional[Path], Tuple[str, ...]]:
        requested = tuple(gap.evidence.get("planned_query_ids", ()))
        requested_set = set(requested)
        repository_root = self.settings.repository_root.resolve()
        for relative in sorted(snapshot.corpus.search_run_paths):
            path = (repository_root / relative).resolve()
            if not _is_within(path, repository_root) or not path.is_file():
                continue
            run = _load_yaml(path)
            queries = _query_map(run)
            eligible = tuple(
                query_id
                for query_id, query in queries.items()
                if (not requested_set or query_id in requested_set)
                and str((query.get("execution") or {}).get("status") or "planned")
                in ELIGIBLE_SEARCH_STATUSES
            )
            if eligible:
                return path, eligible
        return None, ()

    def _execute_search(
        self,
        *,
        decision: ResearchDecision,
        gap: ResearchGap,
        snapshot: ResearchSnapshot,
        action_id: str,
        allow_network: bool,
    ) -> ResearchActionResult:
        runtime = self._semantic_search_runtime()
        run_path, query_ids = self._select_search_run(snapshot, gap)
        needs_plan = run_path is None or not query_ids
        if needs_plan and runtime is None:
            return self._blocked_search(
                decision=decision,
                action_id=action_id,
                code="search-plan-required",
                summary=(
                    "No eligible planned query is bound to this gap, and no semantic "
                    "search planner is configured. Set HARNESS_MODEL/--model or create "
                    "a validated search-run manually."
                ),
            )
        if not allow_network:
            return self._blocked_search(
                decision=decision,
                action_id=action_id,
                code="network-disabled",
                summary=(
                    "DeepXiv execution is disabled for this invocation. Re-run with "
                    "explicit network authorization after reviewing the planned queries."
                ),
            )
        token = os.getenv("DEEPXIV_TOKEN", "")
        if not token:
            return self._blocked_search(
                decision=decision,
                action_id=action_id,
                code="deepxiv-token-missing",
                summary="DEEPXIV_TOKEN is not configured; the search-run was not modified.",
            )
        if importlib.util.find_spec("deepxiv_sdk") is None:
            return self._blocked_search(
                decision=decision,
                action_id=action_id,
                code="deepxiv-sdk-missing",
                summary="deepxiv-sdk is unavailable in the active Python environment.",
            )

        planning_model_calls = 0
        planned_source: Optional[str] = None
        planning_warnings: Tuple[str, ...] = ()
        if needs_plan:
            assert runtime is not None
            if runtime.requires_network and not allow_network:
                return self._blocked_search(
                    decision=decision,
                    action_id=action_id,
                    code="search-planner-network-disabled",
                    summary="Semantic query planning requires explicit network authorization.",
                )
            try:
                planned = runtime.plan_run(gap=gap, snapshot=snapshot)
            except Exception as exc:
                return ResearchActionResult(
                    action_id=action_id,
                    action="search",
                    target_gap_id=decision.target_gap_id,
                    status="failed",
                    outcome="tool_failure",
                    attempted=True,
                    tool_calls=1,
                    summary=f"search-paper query planning failed: {_safe_error(exc)}",
                    error_codes=("search-planning-failed",),
                    metrics={"queries_planned": 0},
                )
            run_path = planned.run_path
            query_ids = planned.query_ids
            planning_model_calls = planned.model_calls
            planning_warnings = planned.warnings
            planned_source = _relative_path(self.settings, run_path)

        assert run_path is not None and query_ids

        skill = self.registry.get("search-paper")
        script_resource = next(
            (
                resource
                for resource in skill.resources_in("scripts")
                if resource.relative_path == "scripts/deepxiv_search.py"
            ),
            None,
        )
        if script_resource is None:
            return self._blocked_search(
                decision=decision,
                action_id=action_id,
                code="search-executor-missing",
                summary="search-paper does not register scripts/deepxiv_search.py.",
            )

        before = _load_yaml(run_path)
        before_hash = _content_hash(run_path)
        before_queries = _query_map(before)
        before_candidates = _candidate_ids(before)
        before_raw_paths = {
            str((query.get("execution") or {}).get("raw_result_path"))
            for query in before_queries.values()
            if (query.get("execution") or {}).get("raw_result_path")
        }
        command = [
            sys.executable,
            str(script_resource.path),
            "--run",
            str(run_path),
        ]
        for query_id in query_ids:
            if not QUERY_ID_PATTERN.fullmatch(query_id):
                return self._blocked_search(
                    decision=decision,
                    action_id=action_id,
                    code="invalid-query-id",
                    summary=f"Search-run contains an unsafe query ID: {query_id!r}.",
                )
            command.extend(["--query-id", query_id])

        child_environment = dict(os.environ)
        child_environment["PYTHONUTF8"] = "1"
        child_environment.setdefault(
            "TIKTOKEN_CACHE_DIR",
            str(self.settings.repository_root / ".harness" / "tiktoken-cache"),
        )
        try:
            completed = subprocess.run(
                command,
                cwd=str(self.settings.repository_root),
                env=child_environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ResearchActionResult(
                action_id=action_id,
                action="search",
                target_gap_id=decision.target_gap_id,
                status="failed",
                outcome="tool_failure",
                attempted=True,
                tool_calls=1,
                summary=f"search-paper timed out after {self.timeout_seconds} seconds.",
                error_codes=("deepxiv-timeout",),
                metrics={"queries_selected": len(query_ids)},
            )

        after = _load_yaml(run_path)
        after_queries = _query_map(after)
        after_candidates = _candidate_ids(after)
        query_statuses = {
            query_id: str(
                (after_queries.get(query_id, {}).get("execution") or {}).get("status")
                or "unknown"
            )
            for query_id in query_ids
        }
        provider_calls = 0
        for query_id in query_ids:
            before_execution = before_queries.get(query_id, {}).get("execution") or {}
            after_execution = after_queries.get(query_id, {}).get("execution") or {}
            if (
                after_execution.get("executed_at")
                and after_execution.get("executed_at")
                != before_execution.get("executed_at")
                and query_statuses[query_id] in EXECUTED_SEARCH_STATUSES
            ):
                provider_calls += 1
        succeeded = sum(
            status in {"succeeded", "empty"} for status in query_statuses.values()
        )
        failures = sum(status == "failed" for status in query_statuses.values())
        empty = sum(status == "empty" for status in query_statuses.values())
        new_candidates = len(after_candidates - before_candidates)

        screening_calls = 0
        triaged_candidates = 0
        selected_candidates = 0
        excluded_candidates = 0
        screening_warnings: Tuple[str, ...] = ()
        screening_error: Optional[str] = None
        if runtime is not None and completed.returncode == 0:
            try:
                screened = runtime.screen_run(run_path=run_path, gap=gap)
                screening_calls = screened.model_calls
                triaged_candidates = screened.triaged_candidates
                selected_candidates = screened.selected_candidates
                excluded_candidates = screened.excluded_candidates
                screening_warnings = screened.warnings
                if screened.changed:
                    after = _load_yaml(run_path)
                    after_queries = _query_map(after)
                    after_candidates = _candidate_ids(after)
            except Exception as exc:
                screening_calls = 1
                screening_error = _safe_error(exc)

        changed_sources = []
        if planned_source:
            changed_sources.append(planned_source)
        if _content_hash(run_path) != before_hash:
            changed_sources.append(_relative_path(self.settings, run_path))
        for query in after_queries.values():
            raw_path = (query.get("execution") or {}).get("raw_result_path")
            if raw_path and str(raw_path) not in before_raw_paths:
                changed_sources.append(str(raw_path).replace("\\", "/"))

        metrics = {
            "queries_planned": len(query_ids) if planned_source else 0,
            "queries_selected": len(query_ids),
            "queries_attempted": provider_calls,
            "queries_succeeded": succeeded,
            "queries_failed": failures,
            "empty_results": empty,
            "new_candidates": new_candidates,
            "candidates_triaged": triaged_candidates,
            "candidates_selected_for_ingest": selected_candidates,
            "candidates_excluded": excluded_candidates,
        }
        attempted = True
        if completed.returncode == 0 and screening_error is None:
            status: ActionStatus = "success"
            outcome: ActionOutcome = (
                "positive"
                if new_candidates or triaged_candidates or selected_candidates
                else "negative_research_result"
            )
        elif completed.returncode == 0:
            status = "partial"
            outcome = "tool_failure"
        else:
            status = "partial" if succeeded else "failed"
            outcome = "tool_failure"
        summary = (
            f"search-paper processed {len(query_ids)} planned queries"
            + (" after creating a new validated search round" if planned_source else "")
            + ": "
            f"{succeeded} succeeded/empty, {failures} failed, "
            f"{new_candidates} new unique candidates, {triaged_candidates} screened, "
            f"and {selected_candidates} selected for ingest."
        )
        error_codes = list(_error_codes(after, query_ids))
        if completed.returncode and not error_codes:
            error_codes.append(f"deepxiv-exit-{completed.returncode}")
        if screening_error:
            summary += f" Candidate screening failed: {screening_error}"
            error_codes.append("candidate-screening-failed")
        warning_count = len(planning_warnings) + len(screening_warnings)
        if warning_count:
            summary += f" Search-run validation retained {warning_count} warning(s)."
        return ResearchActionResult(
            action_id=action_id,
            action="search",
            target_gap_id=decision.target_gap_id,
            status=status,
            outcome=outcome,
            attempted=attempted,
            tool_calls=provider_calls + planning_model_calls + screening_calls,
            changed_sources=tuple(dict.fromkeys(changed_sources)),
            summary=summary,
            error_codes=tuple(error_codes),
            metrics=metrics,
        )
