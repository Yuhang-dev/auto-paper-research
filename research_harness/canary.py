"""Isolated, bounded online Canary runs for the research Harness."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Literal, Mapping, Optional, TypedDict, cast

import yaml  # type: ignore[import-untyped]
from langgraph.graph import END, START, StateGraph

from .artifacts import SemanticArtifactRecorder
from .canary_models import (
    CanaryLimits,
    CanaryReachedStage,
    CanaryRunReport,
    SearchExecutionLimits,
)
from .config import HarnessSettings
from .persistence import HarnessPersistence
from .paper_sources import ArxivPaperSourceAcquirer
from .research_evaluation import inspect_research, resolve_research_directory
from .research_execution import DeterministicActionExecutor
from .research_models import (
    ResearchAction,
    ResearchActionResult,
    ResearchDecision,
    ResearchGap,
    ResearchSnapshot,
    GapType,
)
from .search_runtime import SearchRuntime
from .trajectory import ensure_annotation_sidecar, export_checkpoint_trajectory


RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
ELIGIBLE_QUERY_STATUSES = {"planned", "blocked-credential", "failed"}
STAGE_RANK = {
    "not-started": 0,
    "retrieval": 1,
    "screening": 2,
    "ingest": 3,
    "verification": 4,
    "revision": 5,
    "reverification": 6,
    "analysis": 7,
}


class CanaryError(RuntimeError):
    """Raised when a Canary cannot start without violating its contract."""


class CanaryGraphState(TypedDict, total=False):
    research_id: str
    phase: str
    stage_reached: str
    snapshot: Dict[str, Any]
    action_results: list[Dict[str, Any]]
    error_codes: list[str]


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
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
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            temporary = Path(handle.name)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _atomic_yaml(path: Path, payload: Mapping[str, Any]) -> None:
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
            yaml.safe_dump(
                dict(payload),
                handle,
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
            )
            temporary = Path(handle.name)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _load_yaml(path: Path) -> Dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise CanaryError(f"Expected a YAML mapping in {path}")
    return payload


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    if not root.exists():
        return digest.hexdigest()
    for path in sorted(
        (item for item in root.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(root).as_posix(),
    ):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        content = path.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _select_source_run(
    settings: HarnessSettings,
    research_id: str,
    source_run: Optional[Path],
) -> Path:
    research_directory = resolve_research_directory(settings, research_id)
    search_root = (research_directory / "search-runs").resolve()
    if source_run is not None:
        path = (
            source_run
            if source_run.is_absolute()
            else settings.repository_root / source_run
        )
        path = path.resolve()
        try:
            path.relative_to(search_root)
        except ValueError as exc:
            raise CanaryError(
                "Canary source run must stay inside the selected research search-runs directory"
            ) from exc
        if path.suffix.casefold() not in {".yaml", ".yml"}:
            raise CanaryError("Canary source run must be a YAML file")
        if not path.is_file():
            raise FileNotFoundError(f"Canary source run not found: {path}")
        return path
    candidates = sorted([*search_root.glob("*.yaml"), *search_root.glob("*.yml")])
    for path in candidates:
        payload = _load_yaml(path)
        if any(
            isinstance(query, Mapping)
            and str((query.get("execution") or {}).get("status") or "planned")
            in ELIGIBLE_QUERY_STATUSES
            for query in payload.get("queries") or []
        ):
            return path.resolve()
    raise CanaryError("No search run with an eligible query is available for Canary")


def _reset_query(query: Mapping[str, Any]) -> Dict[str, Any]:
    result = dict(query)
    result["execution"] = {
        "status": "planned",
        "executed_at": None,
        "provider_total_count": None,
        "retrieved_count": None,
        "retained_count": None,
        "raw_result_path": None,
        "error_id": None,
    }
    return result


def _sanitize_run(
    payload: Mapping[str, Any],
    *,
    run_id: str,
    limits: CanaryLimits,
) -> Dict[str, Any]:
    run = json.loads(json.dumps(payload, ensure_ascii=False))
    eligible = [
        query
        for query in run.get("queries") or []
        if isinstance(query, Mapping)
        and str((query.get("execution") or {}).get("status") or "planned")
        in ELIGIBLE_QUERY_STATUSES
    ]
    selected = eligible[: limits.max_planned_queries]
    if not selected:
        raise CanaryError("The selected source run has no eligible Canary query")
    now = _utc_now()
    header = run.setdefault("run", {})
    header.update(
        {
            "id": f"canary-{run_id}",
            "created_at": now,
            "updated_at": now,
            "status": "planned",
            "stop_reason": None,
        }
    )
    budget = header.setdefault("budget", {})
    budget.update(
        {
            "max_queries": len(selected),
            "max_candidates": limits.max_new_unique_candidates,
            "max_provider_query_calls": limits.max_provider_query_calls,
            "max_new_unique_candidates": limits.max_new_unique_candidates,
            "provider_max_retries": limits.provider_max_retries,
        }
    )
    header.pop("execution_totals", None)
    run["queries"] = [_reset_query(query) for query in selected]
    run["candidates"] = []
    coverage = run.setdefault("coverage", {})
    for facet in coverage.get("facets") or []:
        if isinstance(facet, dict):
            facet["status"] = "missing"
            facet["candidate_ids"] = []
            facet["note"] = "Canary retrieval has not run."
    coverage["metrics"] = {
        "executed_queries": 0,
        "raw_retrieved_hits": 0,
        "unique_candidates": 0,
        "duplicate_rate": 0.0,
        "relevance_counts": {
            "core": 0,
            "adjacent": 0,
            "background": 0,
            "exclude": 0,
            "untriaged": 0,
        },
        "missing_metadata_count": 0,
        "new_core_by_round": [],
    }
    run["errors"] = []
    run["citation_expansion"] = {
        "performed": False,
        "provider": None,
        "records": [],
        "gap_reason": None,
    }
    return run


def prepare_canary_workspace(
    settings: HarnessSettings,
    *,
    research_id: str,
    run_id: str,
    limits: CanaryLimits,
    source_run: Optional[Path] = None,
) -> tuple[HarnessSettings, Path, Path]:
    """Create a fresh isolated copy and return settings, root, and run path."""

    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError("run_id must use 1-80 safe ASCII filename characters")
    canary_root = settings.repository_root / ".harness" / "canary" / run_id
    if canary_root.exists():
        raise FileExistsError(f"Canary run already exists: {canary_root}")
    source = _select_source_run(settings, research_id, source_run)
    workspace = canary_root / "workspace"
    wiki_root = workspace / "wiki"
    research_root = workspace / "research"
    target_research = research_root / research_id
    target_runs = target_research / "search-runs"
    target_runs.mkdir(parents=True, exist_ok=False)
    shutil.copytree(settings.wiki_root, wiki_root)
    for name in ("scope.md", "done-criteria.yaml"):
        candidate = settings.research_root / research_id / name
        if candidate.is_file():
            shutil.copy2(candidate, target_research / name)
    isolated_run = target_runs / "canary-source.yaml"
    _atomic_yaml(
        isolated_run,
        _sanitize_run(_load_yaml(source), run_id=run_id, limits=limits),
    )
    isolated = HarnessSettings(
        repository_root=settings.repository_root,
        wiki_root=wiki_root,
        wiki_meta_root=wiki_root / "_meta",
        skills_root=settings.skills_root,
        research_root=research_root,
        database_path=canary_root / "canary.sqlite3",
        model=settings.model,
        model_base_url=settings.model_base_url,
        workspace_id=f"canary:{run_id}",
        context_token_budget=settings.context_token_budget,
        max_tool_iterations=settings.max_tool_iterations,
        tool_output_chars=settings.tool_output_chars,
    )
    isolated.validate()
    return isolated, canary_root, isolated_run


def _gap_for(
    action: ResearchAction,
    snapshot: ResearchSnapshot,
    run_path: Path,
) -> ResearchGap:
    query_ids: tuple[str, ...] = ()
    if action == "search":
        payload = _load_yaml(run_path)
        query_ids = tuple(
            str(query.get("id"))
            for query in payload.get("queries") or []
            if isinstance(query, Mapping) and query.get("id")
        )
    gap_type = cast(
        GapType,
        {
            "search": "coverage_gap",
            "ingest": "evidence_gap",
            "verify": "evidence_gap",
            "revise_evidence": "evidence_gap",
            "analyze_claims": "contradiction_gap",
        }[action],
    )
    return ResearchGap(
        id=f"canary:{action}",
        key=f"canary-{action}",
        type=gap_type,
        question=f"Can the bounded Canary complete the {action} observation boundary?",
        priority=1.0,
        reasons=("Explicit Canary stage request.",),
        evidence={"planned_query_ids": query_ids} if query_ids else {},
        recommended_action=action,
        search_focus=("sparse attention", "long context", "engineering evidence"),
        blocking=False,
    )


def _decision(action: ResearchAction, gap: ResearchGap) -> ResearchDecision:
    return ResearchDecision(
        action=action,
        target_gap_id=gap.id,
        reason="Explicit bounded Canary stage sequence.",
        expected_information_gain=1.0,
    )


def _build_canary_graph(
    *,
    settings: HarnessSettings,
    research_id: str,
    run_path: Path,
    limits: CanaryLimits,
    executor: Any,
    persistence: HarnessPersistence,
):
    if persistence.checkpointer is None:
        raise RuntimeError("Canary persistence must be open")

    def bootstrap(state: CanaryGraphState) -> Dict[str, Any]:
        snapshot = inspect_research(settings, research_id)
        return {
            "research_id": research_id,
            "phase": "search",
            "stage_reached": "not-started",
            "snapshot": snapshot.model_dump(mode="json"),
            "action_results": [],
            "error_codes": [],
        }

    def action_node(action: ResearchAction, reached: str):
        def execute(state: CanaryGraphState) -> Dict[str, Any]:
            results = list(state.get("action_results") or [])
            if len(results) >= limits.max_actions:
                return {
                    "phase": "stopped",
                    "error_codes": [*state.get("error_codes", []), "max-actions"],
                }
            snapshot = ResearchSnapshot.model_validate(state["snapshot"])
            gap = _gap_for(action, snapshot, run_path)
            result = executor.execute(
                decision=_decision(action, gap),
                gap=gap,
                snapshot=snapshot,
                action_id=f"canary-action-{len(results) + 1:04d}",
                allow_network=True,
            )
            results.append(result.model_dump(mode="json"))
            errors = [*state.get("error_codes", []), *result.error_codes]
            boundary_completed = result.outcome in {
                "positive",
                "negative_research_result",
            }
            return {
                "phase": reached if boundary_completed else "stopped",
                "stage_reached": (
                    reached if boundary_completed else state["stage_reached"]
                ),
                "action_results": results,
                "error_codes": list(dict.fromkeys(errors)),
            }

        return execute

    single_ingest = action_node("ingest", "ingest")

    def ingest_batch(state: CanaryGraphState) -> Dict[str, Any]:
        """Read a bounded paper batch before the graph advances to publication/QA."""

        if limits.max_papers_ingested == 1:
            return single_ingest(state)
        results = list(state.get("action_results") or [])
        errors = list(state.get("error_codes") or [])
        snapshot = ResearchSnapshot.model_validate(state["snapshot"])
        completed = 0
        failed = False
        while completed < limits.max_papers_ingested:
            if len(results) >= limits.max_actions:
                errors.append("max-actions")
                failed = True
                break
            if snapshot.corpus.selected_for_ingest == 0:
                break
            gap = _gap_for("ingest", snapshot, run_path)
            result = executor.execute(
                decision=_decision("ingest", gap),
                gap=gap,
                snapshot=snapshot,
                action_id=f"canary-action-{len(results) + 1:04d}",
                allow_network=True,
            )
            results.append(result.model_dump(mode="json"))
            errors.extend(result.error_codes)
            if result.outcome not in {"positive", "negative_research_result"}:
                failed = True
                break
            completed += 1
            snapshot = inspect_research(settings, research_id)
        return {
            "phase": "stopped" if failed or completed == 0 else "ingest",
            "stage_reached": (
                "ingest" if completed else state.get("stage_reached", "screening")
            ),
            "snapshot": snapshot.model_dump(mode="json"),
            "action_results": results,
            "error_codes": list(dict.fromkeys(errors)),
        }

    def observe(next_phase: str):
        def inspect(state: CanaryGraphState) -> Dict[str, Any]:
            return {
                "phase": next_phase,
                "snapshot": inspect_research(settings, research_id).model_dump(
                    mode="json"
                ),
            }

        return inspect

    def action_route(target_stage: str, continue_node: str):
        def route(state: CanaryGraphState) -> str:
            result = ResearchActionResult.model_validate(state["action_results"][-1])
            if result.outcome not in {"positive", "negative_research_result"}:
                return "stop"
            if STAGE_RANK[target_stage] <= STAGE_RANK[state["stage_reached"]]:
                return "stop"
            return continue_node

        return route

    search_reached = (
        "retrieval"
        if SearchExecutionLimits.from_canary(limits).stop_after == "retrieval"
        else "screening"
    )
    builder = StateGraph(CanaryGraphState)
    builder.add_node("bootstrap", bootstrap)
    builder.add_node("search", action_node("search", search_reached))
    builder.add_node("observe_search", observe("ingest"))
    builder.add_node("ingest", ingest_batch)
    builder.add_node("observe_ingest", observe("verification"))
    builder.add_node("verify", action_node("verify", "verification"))
    builder.add_node("observe_verify", observe("revision"))
    execute_revision = action_node("revise_evidence", "revision")

    def revise_or_skip(state: CanaryGraphState) -> Dict[str, Any]:
        snapshot = ResearchSnapshot.model_validate(state["snapshot"])
        if snapshot.evidence.revision_candidates == 0:
            return {
                "phase": "revision",
                "stage_reached": "revision",
            }
        return execute_revision(state)

    builder.add_node("revise", revise_or_skip)
    builder.add_node("observe_revision", observe("reverification"))
    execute_reverification = action_node("verify", "reverification")

    def reverify_or_skip(state: CanaryGraphState) -> Dict[str, Any]:
        revised = any(
            item.get("action") == "revise_evidence"
            and item.get("outcome") == "positive"
            for item in state.get("action_results") or []
        )
        if not revised:
            return {
                "phase": "reverification",
                "stage_reached": "reverification",
            }
        return execute_reverification(state)

    builder.add_node("reverify", reverify_or_skip)
    builder.add_node("observe_reverify", observe("analysis"))
    builder.add_node("analyze", action_node("analyze_claims", "analysis"))
    builder.add_edge(START, "bootstrap")
    builder.add_edge("bootstrap", "search")
    builder.add_conditional_edges(
        "search",
        action_route(limits.stop_after, "observe"),
        {"stop": END, "observe": "observe_search"},
    )
    builder.add_edge("observe_search", "ingest")
    builder.add_conditional_edges(
        "ingest",
        action_route(limits.stop_after, "observe"),
        {"stop": END, "observe": "observe_ingest"},
    )
    builder.add_edge("observe_ingest", "verify")
    builder.add_conditional_edges(
        "verify",
        action_route(limits.stop_after, "observe"),
        {"stop": END, "observe": "observe_verify"},
    )
    builder.add_edge("observe_verify", "revise")
    builder.add_conditional_edges(
        "revise",
        action_route(limits.stop_after, "observe"),
        {"stop": END, "observe": "observe_revision"},
    )
    builder.add_edge("observe_revision", "reverify")
    builder.add_conditional_edges(
        "reverify",
        action_route(limits.stop_after, "observe"),
        {"stop": END, "observe": "observe_reverify"},
    )
    builder.add_edge("observe_reverify", "analyze")
    builder.add_edge("analyze", END)
    return builder.compile(
        checkpointer=persistence.checkpointer,
        name="bounded-research-canary-v0.1",
    )


def run_canary(
    settings: HarnessSettings,
    *,
    research_id: str,
    run_id: str,
    limits: CanaryLimits,
    source_run: Optional[Path] = None,
) -> CanaryRunReport:
    """Run one bounded Canary in a fresh workspace and emit a durable report."""

    started_wall = _utc_now()
    started = time.monotonic()
    if STAGE_RANK[limits.stop_after] >= STAGE_RANK["screening"] and not settings.model:
        raise CanaryError(
            "screening or later Canary stages require HARNESS_MODEL/--model"
        )
    formal_hash_before = _tree_hash(settings.wiki_root) + _tree_hash(
        settings.research_root / research_id
    )
    isolated, canary_root, run_path = prepare_canary_workspace(
        settings,
        research_id=research_id,
        run_id=run_id,
        limits=limits,
        source_run=source_run,
    )
    recorder = SemanticArtifactRecorder(
        settings.repository_root,
        canary_root / "artifacts",
        model_name=settings.model,
        model_base_url=settings.normalized_model_base_url,
    )
    runtime = (
        SearchRuntime(
            isolated,
            timeout_seconds=min(limits.deadline_seconds, 180),
            artifact_recorder=recorder,
            max_selected_candidates=limits.max_selected_candidates,
        )
        if settings.model
        else None
    )
    executor = DeterministicActionExecutor(
        isolated,
        timeout_seconds=min(limits.deadline_seconds, 300),
        search_runtime=runtime,
        search_limits=SearchExecutionLimits.from_canary(limits),
        artifact_recorder=recorder,
        defer_wiki=limits.wiki_write_mode == "deferred",
        stage_root=canary_root / "artifacts" / "staged",
        paper_source_acquirer=ArxivPaperSourceAcquirer(
            settings.repository_root,
            destination_root=canary_root / "workspace" / "sources" / "papers",
            timeout_seconds=min(limits.deadline_seconds, 120),
        ),
    )
    initial_snapshot = inspect_research(isolated, research_id)
    trajectory_path = canary_root / "trajectory.jsonl"
    with HarnessPersistence(isolated) as persistence:
        graph = _build_canary_graph(
            settings=isolated,
            research_id=research_id,
            run_path=run_path,
            limits=limits,
            executor=executor,
            persistence=persistence,
        )
        thread_id = f"canary:{research_id}:{run_id}"
        config = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": 40,
        }
        state = graph.invoke({"research_id": research_id}, config)
        export_checkpoint_trajectory(
            graph.get_state_history(config),
            destination=trajectory_path,
            research_id=research_id,
            thread_id=thread_id,
        )
    ensure_annotation_sidecar(
        canary_root / "human-annotations.yaml",
        research_id=research_id,
        thread_id=thread_id,
    )

    action_results = tuple(state.get("action_results") or [])
    last_result = (
        ResearchActionResult.model_validate(action_results[-1])
        if action_results
        else None
    )
    reached = cast(
        CanaryReachedStage,
        str(state.get("stage_reached") or "not-started"),
    )
    sandbox_snapshot = inspect_research(isolated, research_id)
    formal_hash_after = _tree_hash(settings.wiki_root) + _tree_hash(
        settings.research_root / research_id
    )
    search_result = next(
        (
            ResearchActionResult.model_validate(item)
            for item in action_results
            if item.get("action") == "search"
        ),
        None,
    )
    provider_calls = (
        search_result.metrics.get("queries_attempted", 0) if search_result else 0
    )
    new_candidates = (
        search_result.metrics.get("new_candidates", 0) if search_result else 0
    )
    completed_ingests = sum(
        item.get("action") == "ingest"
        and item.get("outcome") in {"positive", "negative_research_result"}
        for item in action_results
    )
    invariants = {
        "formal_source_truth_unchanged": formal_hash_before == formal_hash_after,
        "workspace_isolated": run_path.resolve().is_relative_to(
            canary_root.resolve()
        ),
        "max_actions_respected": len(action_results) <= limits.max_actions,
        "provider_query_call_limit_respected": (
            provider_calls <= limits.max_provider_query_calls
        ),
        "new_unique_candidate_limit_respected": (
            new_candidates <= limits.max_new_unique_candidates
        ),
        "paper_ingest_limit_respected": (
            completed_ingests <= limits.max_papers_ingested
        ),
        "deferred_wiki_unchanged": (
            limits.wiki_write_mode != "deferred"
            or sandbox_snapshot.wiki_source_hash == initial_snapshot.wiki_source_hash
        ),
    }
    reached_target = STAGE_RANK[reached] >= STAGE_RANK[limits.stop_after]
    if last_result is not None and last_result.status == "blocked":
        status: Literal["passed", "failed", "blocked", "timeout"] = "blocked"
    elif last_result is not None and (
        last_result.status == "failed" or last_result.outcome == "tool_failure"
    ):
        status = "failed"
    elif reached_target and all(invariants.values()):
        status = "passed"
    else:
        status = "failed"
    manifest = recorder.manifest_path
    report = CanaryRunReport(
        run_id=run_id,
        research_id=research_id,
        status=status,
        stage_reached=reached,
        stop_after=limits.stop_after,
        started_at=started_wall,
        finished_at=_utc_now(),
        duration_seconds=max(0.0, time.monotonic() - started),
        workspace_root=canary_root.relative_to(settings.repository_root).as_posix(),
        limits=limits,
        action_results=action_results,
        invariants=invariants,
        error_codes=tuple(state.get("error_codes") or []),
        semantic_manifest=(
            manifest.relative_to(settings.repository_root).as_posix()
            if manifest.is_file()
            else None
        ),
        trajectory_path=trajectory_path.relative_to(
            settings.repository_root
        ).as_posix(),
    )
    _atomic_json(canary_root / "report.json", report.model_dump(mode="json"))
    return report


__all__ = [
    "CanaryError",
    "prepare_canary_workspace",
    "run_canary",
]
