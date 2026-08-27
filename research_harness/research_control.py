"""Checkpointed deterministic control passes and the autonomous V1 research loop."""

from __future__ import annotations

import os
import re
from pathlib import Path
from types import TracebackType
from typing import AbstractSet, Any, Callable, Dict, Optional, Protocol, Type, Union

from langgraph.graph import END, START, StateGraph

from .config import HarnessSettings
from .persistence import HarnessPersistence
from .research_evaluation import (
    check_done,
    decide_next_action,
    evaluate_gaps,
    inspect_research,
    load_done_criteria,
    measure_progress,
)
from .research_execution import DeterministicActionExecutor
from .research_models import (
    ActionAttemptStats,
    DoneCheck,
    DoneCriteria,
    ProgressMeasurement,
    ResearchAction,
    ResearchActionResult,
    ResearchDecision,
    ResearchGap,
    ResearchSnapshot,
)
from .state import ResearchState


THREAD_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
CHECKPOINT_NAMESPACE = "research-control-v0.2"
AUTONOMOUS_CHECKPOINT_NAMESPACE = "research-loop-v1.2"
ResearchInspector = Callable[[HarnessSettings, str], ResearchSnapshot]


class ResearchActionExecutor(Protocol):
    @property
    def supported_actions(self) -> AbstractSet[ResearchAction]: ...

    def execute(
        self,
        *,
        decision: ResearchDecision,
        gap: Optional[ResearchGap],
        snapshot: ResearchSnapshot,
        action_id: str,
        allow_network: bool,
    ) -> ResearchActionResult: ...


def _checkpoint_config(
    namespace: str,
    research_id: str,
    thread_id: str,
    *,
    recursion_limit: Optional[int] = None,
) -> Dict[str, Any]:
    config: Dict[str, Any] = {
        "configurable": {
            "thread_id": f"{namespace}:{research_id}:{thread_id}",
        }
    }
    if recursion_limit is not None:
        config["recursion_limit"] = recursion_limit
    return config


def _selected_thread(research_id: str, thread_id: Optional[str]) -> str:
    selected = thread_id or f"research:{research_id}"
    if not THREAD_ID_PATTERN.fullmatch(selected):
        raise ValueError(
            "thread_id must be 1-200 ASCII letters, digits, dots, colons, underscores, or hyphens"
        )
    return selected


def _gaps(state: ResearchState) -> tuple[ResearchGap, ...]:
    return tuple(ResearchGap.model_validate(item) for item in state.get("gaps", []))


def _record_decision(
    state: ResearchState,
    decision: ResearchDecision,
) -> Dict[str, Any]:
    history = list(state.get("decision_history") or [])
    history.append(
        {
            "control_pass": int(state.get("control_passes") or 0),
            "research_iteration": int(state.get("research_iterations") or 0),
            "snapshot_id": state["snapshot"]["snapshot_id"],
            **decision.model_dump(mode="json"),
        }
    )
    current_gap: Dict[str, Any] = next(
        (
            gap.model_dump(mode="json")
            for gap in _gaps(state)
            if gap.id == decision.target_gap_id
        ),
        {},
    )
    return {
        "decision": decision.model_dump(mode="json"),
        "current_action": decision.model_dump(mode="json"),
        "current_gap": current_gap,
        "decision_history": history[-100:],
    }


def build_research_control_graph(
    *,
    settings: HarnessSettings,
    research_id: str,
    criteria: DoneCriteria,
    persistence: HarnessPersistence,
):
    """Compile one read-only inspect/evaluate/measure/check/decide pass."""

    if persistence.checkpointer is None:
        raise RuntimeError(
            "Harness persistence must be open before compiling the graph"
        )

    def prepare(state: ResearchState) -> Dict[str, Any]:
        return {
            "research_id": research_id,
            "phase": "inspect",
            "stop_reason": "",
        }

    def inspect(state: ResearchState) -> Dict[str, Any]:
        previous = state.get("snapshot")
        snapshot = inspect_research(settings, research_id)
        result: Dict[str, Any] = {
            "phase": "evaluate",
            "control_passes": int(state.get("control_passes") or 0) + 1,
            "snapshot": snapshot.model_dump(mode="json"),
            "search_history": [
                item.model_dump(mode="json") for item in snapshot.corpus.search_yields
            ][-100:],
        }
        if previous:
            result["previous_snapshot"] = previous
        return result

    def evaluate(state: ResearchState) -> Dict[str, Any]:
        snapshot = ResearchSnapshot.model_validate(state["snapshot"])
        gaps = evaluate_gaps(snapshot, criteria)
        history = list(state.get("gap_history") or [])
        history.append(
            {
                "control_pass": int(state.get("control_passes") or 0),
                "snapshot_id": snapshot.snapshot_id,
                "gap_ids": [gap.id for gap in gaps],
                "open_gap_count": len(gaps),
            }
        )
        return {
            "phase": "measure",
            "gaps": [gap.model_dump(mode="json") for gap in gaps],
            "gap_history": history[-100:],
        }

    def measure(state: ResearchState) -> Dict[str, Any]:
        before_payload = state.get("previous_snapshot")
        before = (
            ResearchSnapshot.model_validate(before_payload) if before_payload else None
        )
        after = ResearchSnapshot.model_validate(state["snapshot"])
        progress = measure_progress(
            before,
            after,
            previous_no_progress_rounds=int(state.get("no_progress_rounds") or 0),
            action_attempted=False,
        )
        return {
            "phase": "check",
            "progress": progress.model_dump(mode="json"),
        }

    def done(state: ResearchState) -> Dict[str, Any]:
        snapshot = ResearchSnapshot.model_validate(state["snapshot"])
        evaluation = check_done(
            snapshot,
            criteria,
            _gaps(state),
            research_iteration=int(state.get("research_iterations") or 0),
            tool_calls=int(state.get("tool_calls") or 0),
            no_progress_rounds=int(state.get("no_progress_rounds") or 0),
        )
        return {
            "phase": "decide",
            "evaluation": evaluation.model_dump(mode="json"),
            "stop_reason": evaluation.stop_reason or "",
        }

    def decide(state: ResearchState) -> Dict[str, Any]:
        evaluation = DoneCheck.model_validate(state["evaluation"])
        decision = decide_next_action(
            _gaps(state),
            evaluation,
            attempts_by_gap_action=state.get("attempts_by_gap_action"),
            max_no_progress_per_gap_action=criteria.max_no_progress_rounds,
        )
        return {
            "phase": "stopped" if evaluation.stop_reason else "planned",
            **_record_decision(state, decision),
        }

    builder = StateGraph(ResearchState)
    builder.add_node("prepare", prepare)
    builder.add_node("inspect_research", inspect)
    builder.add_node("evaluate_gaps", evaluate)
    builder.add_node("measure_progress", measure)
    builder.add_node("check_done", done)
    builder.add_node("decide_next_action", decide)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "inspect_research")
    builder.add_edge("inspect_research", "evaluate_gaps")
    builder.add_edge("evaluate_gaps", "measure_progress")
    builder.add_edge("measure_progress", "check_done")
    builder.add_edge("check_done", "decide_next_action")
    builder.add_edge("decide_next_action", END)
    return builder.compile(
        checkpointer=persistence.checkpointer,
        name="research-control-pass",
    )


def build_autonomous_research_graph(
    *,
    settings: HarnessSettings,
    research_id: str,
    criteria: DoneCriteria,
    persistence: HarnessPersistence,
    action_executor: ResearchActionExecutor,
    inspector: ResearchInspector = inspect_research,
):
    """Compile ``inspect -> decide -> execute -> observe`` with hard stop routes."""

    if persistence.checkpointer is None:
        raise RuntimeError(
            "Harness persistence must be open before compiling the graph"
        )
    supported_actions = frozenset(action_executor.supported_actions)

    def bootstrap(state: ResearchState) -> Dict[str, Any]:
        return {
            "research_id": research_id,
            "phase": "inspect",
            "allow_network": bool(state.get("allow_network")),
            "stop_reason": "",
            "action_result": {},
        }

    def inspect(state: ResearchState) -> Dict[str, Any]:
        previous = state.get("snapshot")
        snapshot = inspector(settings, research_id)
        result: Dict[str, Any] = {
            "phase": "observed",
            "control_passes": int(state.get("control_passes") or 0) + 1,
            "snapshot": snapshot.model_dump(mode="json"),
            "search_history": [
                item.model_dump(mode="json") for item in snapshot.corpus.search_yields
            ][-100:],
        }
        if previous:
            result["previous_snapshot"] = previous
        return result

    def route_after_inspect(state: ResearchState) -> str:
        payload = state.get("action_result") or {}
        return "measure" if payload.get("attempted") else "evaluate"

    def measure(state: ResearchState) -> Dict[str, Any]:
        before_payload = state.get("previous_snapshot")
        before = (
            ResearchSnapshot.model_validate(before_payload) if before_payload else None
        )
        after = ResearchSnapshot.model_validate(state["snapshot"])
        progress = measure_progress(
            before,
            after,
            previous_no_progress_rounds=0,
            action_attempted=True,
        )
        return {
            "phase": "update-attempt",
            "progress": progress.model_dump(mode="json"),
        }

    def update_attempt(state: ResearchState) -> Dict[str, Any]:
        result = ResearchActionResult.model_validate(state["action_result"])
        progress = ProgressMeasurement.model_validate(state["progress"])
        attempts = dict(state.get("attempts_by_gap_action") or {})
        if not result.attempted or not result.target_gap_id:
            return {"phase": "evaluate", "attempts_by_gap_action": attempts}
        key = f"{result.target_gap_id}:{result.action}"
        previous_payload = attempts.get(key)
        previous = (
            ActionAttemptStats.model_validate(previous_payload)
            if previous_payload
            else ActionAttemptStats(
                attempt_key=key,
                target_gap_id=result.target_gap_id,
                action=result.action,
            )
        )
        stats = previous.model_copy(
            update={
                "attempts": previous.attempts + 1,
                "no_progress": (
                    0 if progress.made_progress else previous.no_progress + 1
                ),
                "tool_failures": previous.tool_failures
                + int(result.outcome == "tool_failure"),
                "negative_results": previous.negative_results
                + int(result.outcome == "negative_research_result"),
                "last_action_id": result.action_id,
                "last_status": result.status,
            }
        )
        attempts[key] = stats.model_dump(mode="json")
        return {
            "phase": "evaluate",
            "attempts_by_gap_action": attempts,
            "no_progress_rounds": stats.no_progress,
        }

    def evaluate(state: ResearchState) -> Dict[str, Any]:
        snapshot = ResearchSnapshot.model_validate(state["snapshot"])
        gaps = evaluate_gaps(snapshot, criteria)
        history = list(state.get("gap_history") or [])
        history.append(
            {
                "control_pass": int(state.get("control_passes") or 0),
                "research_iteration": int(state.get("research_iterations") or 0),
                "snapshot_id": snapshot.snapshot_id,
                "gap_ids": [gap.id for gap in gaps],
                "open_gap_count": len(gaps),
            }
        )
        return {
            "phase": "check",
            "gaps": [gap.model_dump(mode="json") for gap in gaps],
            "gap_history": history[-100:],
        }

    def done(state: ResearchState) -> Dict[str, Any]:
        snapshot = ResearchSnapshot.model_validate(state["snapshot"])
        evaluation = check_done(
            snapshot,
            criteria,
            _gaps(state),
            research_iteration=int(state.get("research_iterations") or 0),
            tool_calls=int(state.get("tool_calls") or 0),
            no_progress_rounds=int(state.get("no_progress_rounds") or 0),
            attempts_by_gap_action=state.get("attempts_by_gap_action"),
            supported_actions=supported_actions,
        )
        return {
            "phase": "checked",
            "evaluation": evaluation.model_dump(mode="json"),
            "stop_reason": evaluation.stop_reason or "",
        }

    def route_after_done(state: ResearchState) -> str:
        evaluation = DoneCheck.model_validate(state["evaluation"])
        return "stop" if evaluation.stop_reason else "continue"

    def decide(state: ResearchState) -> Dict[str, Any]:
        decision = decide_next_action(
            _gaps(state),
            DoneCheck.model_validate(state["evaluation"]),
            attempts_by_gap_action=state.get("attempts_by_gap_action"),
            max_no_progress_per_gap_action=criteria.max_no_progress_rounds,
            supported_actions=supported_actions,
        )
        return {"phase": "execute", **_record_decision(state, decision)}

    def execute(state: ResearchState) -> Dict[str, Any]:
        decision = ResearchDecision.model_validate(state["decision"])
        gaps = _gaps(state)
        gap = next((item for item in gaps if item.id == decision.target_gap_id), None)
        sequence = int(state.get("action_sequence") or 0) + 1
        action_id = f"action-{sequence:04d}"
        try:
            result = action_executor.execute(
                decision=decision,
                gap=gap,
                snapshot=ResearchSnapshot.model_validate(state["snapshot"]),
                action_id=action_id,
                allow_network=bool(state.get("allow_network")),
            )
        except Exception as exc:
            message = str(exc)
            for name in ("DEEPXIV_TOKEN", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
                secret = os.getenv(name, "")
                if secret:
                    message = message.replace(secret, "[REDACTED]")
            result = ResearchActionResult(
                action_id=action_id,
                action=decision.action,
                target_gap_id=decision.target_gap_id,
                status="failed",
                outcome="tool_failure",
                attempted=True,
                summary=f"Executor raised {type(exc).__name__}: {message}",
                error_codes=("executor-exception",),
            )
        iterations = int(state.get("research_iterations") or 0) + int(result.attempted)
        history = list(state.get("action_history") or [])
        history.append(
            {
                "action_id": action_id,
                "control_pass": int(state.get("control_passes") or 0),
                "research_iteration": iterations,
                "snapshot_id": state["snapshot"]["snapshot_id"],
                "decision": decision.model_dump(mode="json"),
                "result": result.model_dump(mode="json"),
            }
        )
        return {
            "phase": "observe" if result.attempted else "stopped",
            "action_result": result.model_dump(mode="json"),
            "action_sequence": sequence,
            "action_history": history[-100:],
            "research_iterations": iterations,
            "tool_calls": int(state.get("tool_calls") or 0) + result.tool_calls,
            "stop_reason": "" if result.attempted else "blocked",
        }

    def route_after_execute(state: ResearchState) -> str:
        result = ResearchActionResult.model_validate(state["action_result"])
        return "observe" if result.attempted else "stop"

    def finalize_done(state: ResearchState) -> Dict[str, Any]:
        decision = decide_next_action(
            _gaps(state),
            DoneCheck.model_validate(state["evaluation"]),
            attempts_by_gap_action=state.get("attempts_by_gap_action"),
            max_no_progress_per_gap_action=criteria.max_no_progress_rounds,
            supported_actions=supported_actions,
        )
        return {"phase": "stopped", **_record_decision(state, decision)}

    def finalize_execution_stop(state: ResearchState) -> Dict[str, Any]:
        return {"phase": "stopped", "stop_reason": "blocked"}

    builder = StateGraph(ResearchState)
    builder.add_node("bootstrap", bootstrap)
    builder.add_node("inspect_research", inspect)
    builder.add_node("measure_progress", measure)
    builder.add_node("update_attempt_state", update_attempt)
    builder.add_node("evaluate_gaps", evaluate)
    builder.add_node("check_done", done)
    builder.add_node("decide_next_action", decide)
    builder.add_node("execute_action", execute)
    builder.add_node("finalize_done", finalize_done)
    builder.add_node("finalize_execution_stop", finalize_execution_stop)
    builder.add_edge(START, "bootstrap")
    builder.add_edge("bootstrap", "inspect_research")
    builder.add_conditional_edges(
        "inspect_research",
        route_after_inspect,
        {"measure": "measure_progress", "evaluate": "evaluate_gaps"},
    )
    builder.add_edge("measure_progress", "update_attempt_state")
    builder.add_edge("update_attempt_state", "evaluate_gaps")
    builder.add_edge("evaluate_gaps", "check_done")
    builder.add_conditional_edges(
        "check_done",
        route_after_done,
        {"stop": "finalize_done", "continue": "decide_next_action"},
    )
    builder.add_edge("decide_next_action", "execute_action")
    builder.add_conditional_edges(
        "execute_action",
        route_after_execute,
        {"observe": "inspect_research", "stop": "finalize_execution_stop"},
    )
    builder.add_edge("finalize_done", END)
    builder.add_edge("finalize_execution_stop", END)
    return builder.compile(
        checkpointer=persistence.checkpointer,
        name="autonomous-research-loop-v1",
    )


class ResearchController:
    """Lifecycle wrapper for a persisted, non-mutating outer-loop control pass."""

    def __init__(
        self,
        settings: Optional[HarnessSettings] = None,
        *,
        research_id: str,
        criteria_path: Optional[Union[str, Path]] = None,
    ):
        self.settings = settings or HarnessSettings.from_env()
        self.settings.validate()
        self.research_id = research_id
        self.criteria_path = criteria_path
        self.criteria = load_done_criteria(self.settings, research_id, criteria_path)
        self.persistence = HarnessPersistence(self.settings)
        self.graph = None

    def open(self) -> "ResearchController":
        if self.graph is not None:
            return self
        self.persistence.open()
        try:
            self.graph = build_research_control_graph(
                settings=self.settings,
                research_id=self.research_id,
                criteria=self.criteria,
                persistence=self.persistence,
            )
        except Exception:
            self.close()
            raise
        return self

    def close(self) -> None:
        self.graph = None
        self.persistence.close()

    def invoke(self, *, thread_id: Optional[str] = None) -> Dict[str, Any]:
        if self.graph is None:
            raise RuntimeError("ResearchController is not open")
        selected = _selected_thread(self.research_id, thread_id)
        return self.graph.invoke(
            {"research_id": self.research_id},
            _checkpoint_config(CHECKPOINT_NAMESPACE, self.research_id, selected),
        )

    def get_state(self, thread_id: Optional[str] = None):
        if self.graph is None:
            raise RuntimeError("ResearchController is not open")
        selected = _selected_thread(self.research_id, thread_id)
        return self.graph.get_state(
            _checkpoint_config(CHECKPOINT_NAMESPACE, self.research_id, selected)
        )

    def __enter__(self) -> "ResearchController":
        return self.open()

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        self.close()


class AutonomousResearchController:
    """Persist and run the deterministic V1 research loop until a hard stop route."""

    def __init__(
        self,
        settings: Optional[HarnessSettings] = None,
        *,
        research_id: str,
        criteria_path: Optional[Union[str, Path]] = None,
        action_executor: Optional[ResearchActionExecutor] = None,
        inspector: ResearchInspector = inspect_research,
    ):
        self.settings = settings or HarnessSettings.from_env()
        self.settings.validate()
        self.research_id = research_id
        self.criteria_path = criteria_path
        self.criteria = load_done_criteria(self.settings, research_id, criteria_path)
        self.action_executor = action_executor or DeterministicActionExecutor(
            self.settings
        )
        self.inspector = inspector
        self.persistence = HarnessPersistence(self.settings)
        self.graph = None

    def open(self) -> "AutonomousResearchController":
        if self.graph is not None:
            return self
        self.persistence.open()
        try:
            self.graph = build_autonomous_research_graph(
                settings=self.settings,
                research_id=self.research_id,
                criteria=self.criteria,
                persistence=self.persistence,
                action_executor=self.action_executor,
                inspector=self.inspector,
            )
        except Exception:
            self.close()
            raise
        return self

    def close(self) -> None:
        self.graph = None
        self.persistence.close()

    def invoke(
        self,
        *,
        thread_id: Optional[str] = None,
        allow_network: bool = False,
    ) -> Dict[str, Any]:
        if self.graph is None:
            raise RuntimeError("AutonomousResearchController is not open")
        selected = _selected_thread(self.research_id, thread_id)
        recursion_limit = self.criteria.max_research_iterations * 8 + 32
        return self.graph.invoke(
            {"research_id": self.research_id, "allow_network": allow_network},
            _checkpoint_config(
                AUTONOMOUS_CHECKPOINT_NAMESPACE,
                self.research_id,
                selected,
                recursion_limit=recursion_limit,
            ),
        )

    def get_state(self, thread_id: Optional[str] = None):
        if self.graph is None:
            raise RuntimeError("AutonomousResearchController is not open")
        selected = _selected_thread(self.research_id, thread_id)
        return self.graph.get_state(
            _checkpoint_config(
                AUTONOMOUS_CHECKPOINT_NAMESPACE,
                self.research_id,
                selected,
            )
        )

    def __enter__(self) -> "AutonomousResearchController":
        return self.open()

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        self.close()
