"""LangGraph state and immutable invocation context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, TypedDict

from langgraph.graph import MessagesState


class HarnessState(MessagesState, total=False):
    iteration: int
    tool_failures: int
    stop_reason: str
    context_message_count: int
    recalled_memory_count: int
    last_tools: List[str]


@dataclass(frozen=True)
class HarnessContext:
    workspace_id: str
    allow_network: bool = False


class ResearchState(TypedDict, total=False):
    """Outer-loop control state; domain entities remain in Wiki/search files."""

    research_id: str
    phase: str
    control_passes: int
    research_iterations: int
    snapshot: Dict[str, Any]
    previous_snapshot: Dict[str, Any]
    gaps: List[Dict[str, Any]]
    progress: Dict[str, Any]
    evaluation: Dict[str, Any]
    decision: Dict[str, Any]
    current_gap: Dict[str, Any]
    current_action: Dict[str, Any]
    action_result: Dict[str, Any]
    action_sequence: int
    decision_history: List[Dict[str, Any]]
    action_history: List[Dict[str, Any]]
    attempts_by_gap_action: Dict[str, Dict[str, Any]]
    gap_history: List[Dict[str, Any]]
    search_history: List[Dict[str, Any]]
    no_progress_rounds: int
    tool_calls: int
    allow_network: bool
    model_runtime_fingerprint: str
    stop_reason: str


class ReviewState(TypedDict, total=False):
    """Compact checkpoint state; review content remains in artifact files."""

    research_id: str
    run_id: str
    thread_id: str
    phase: str
    round_number: int
    allow_network: bool
    model_fingerprint: str
    source_ids: List[str]
    screening_ids: List[str]
    selected_skim_ids: List[str]
    skim_ids: List[str]
    selected_deep_read_ids: List[str]
    deep_read_ids: List[str]
    evidence_card_ids: List[str]
    claim_ids: List[str]
    uncertainty_ids: List[str]
    assessment_ids: List[str]
    round_start: Dict[str, Any]
    round_gains: List[Dict[str, Any]]
    readiness: Dict[str, Any]
    trajectory_sequence: int
    report_path: str
    promotion_manifest_path: str
    stop_reason: str
    completed: bool
