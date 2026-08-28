"""Strict data contracts for deterministic outer-loop research control."""

from __future__ import annotations

from typing import Any, Dict, Literal, Mapping, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, model_validator


FacetStatus = Literal["missing", "partial", "covered", "not-required"]
RequiredFacetStatus = Literal["partial", "covered"]
NonConsensusResult = Literal[
    "supported-consensus",
    "contested",
    "insufficient-evidence",
]
GapType = Literal[
    "coverage_gap",
    "evidence_gap",
    "contradiction_gap",
    "benchmark_gap",
    "context_gap",
    "model_gap",
    "engineering_gap",
    "replication_gap",
    "schema_gap",
    "workflow_gap",
]
ResearchAction = Literal[
    "search",
    "ingest",
    "analyze_claims",
    "expand_citations",
    "verify",
    "synthesize",
    "finish",
]
ActionStatus = Literal["success", "partial", "failed", "blocked"]
ActionOutcome = Literal[
    "positive",
    "negative_research_result",
    "tool_failure",
    "precondition_blocked",
    "unsupported",
]
StopReason = Literal[
    "completed",
    "budget_exhausted",
    "blocked",
    "stalled",
    "human_review_required",
]
TARGET_REQUIRED_ACTIONS = frozenset(
    {"search", "ingest", "analyze_claims", "expand_citations", "verify"}
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SearchYield(StrictModel):
    run_id: str
    round: int = Field(ge=1)
    new_core_papers: int = Field(ge=0)
    valid_discovery_round: bool = True
    invalid_reasons: Tuple[str, ...] = ()
    query_statuses: Dict[str, int] = Field(default_factory=dict)
    screening_complete: bool = True

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_novelty_yield(cls, value: Any) -> Any:
        if isinstance(value, Mapping) and "novelty_yield" in value:
            payload = dict(value)
            legacy = float(payload.pop("novelty_yield"))
            current = float(payload.get("new_core_papers", 0))
            if legacy != current:
                raise ValueError(
                    "legacy novelty_yield must equal new_core_papers for SearchYield"
                )
            return payload
        return value


class CorpusSnapshot(StrictModel):
    search_run_count: int = Field(ge=0)
    search_runs_by_status: Dict[str, int]
    query_count: int = Field(ge=0)
    query_statuses: Dict[str, int]
    pending_queries: int = Field(ge=0)
    blocked_queries: int = Field(ge=0)
    planned_query_ids: Tuple[str, ...] = ()
    blocked_query_ids: Tuple[str, ...] = ()
    unique_candidates: int = Field(ge=0)
    candidates_by_relevance: Dict[str, int]
    core_candidates: int = Field(ge=0)
    selected_for_ingest: int = Field(ge=0)
    ingested_papers: int = Field(ge=0)
    verified_papers: int = Field(ge=0)
    search_run_paths: Tuple[str, ...] = ()
    search_yields: Tuple[SearchYield, ...] = ()
    declared_search_gaps: Tuple[str, ...] = ()


class TaxonomySnapshot(StrictModel):
    required_facets: Tuple[str, ...] = ()
    candidate_facet_coverage: Dict[str, FacetStatus]
    candidate_facet_counts: Dict[str, int]
    evidence_facet_coverage: Dict[str, FacetStatus]
    evidence_facet_counts: Dict[str, int]
    facet_next_queries: Dict[str, Tuple[str, ...]]
    method_entities: int = Field(ge=0)
    method_families: Dict[str, int]
    unclassified_methods: int = Field(ge=0)
    unresolved_scope_questions: Tuple[str, ...] = ()


class EvidenceSnapshot(StrictModel):
    experiments_total: int = Field(ge=0)
    verified_experiments: int = Field(ge=0)
    experiments_with_evidence_locator: int = Field(ge=0)
    evidence_locator_ratio: float = Field(ge=0, le=1)
    claims_total: int = Field(ge=0)
    verified_claims: int = Field(ge=0)
    claims_with_evidence: int = Field(ge=0)
    claims_by_assessment: Dict[str, int]
    contested_claims: int = Field(ge=0)
    nonconsensus_assessments: int = Field(ge=0)
    verified_nonconsensus_assessments: int = Field(ge=0)
    assessments_by_result: Dict[str, int]
    benchmarks_total: int = Field(ge=0)
    benchmark_ids: Tuple[str, ...] = ()
    models_total: int = Field(ge=0)
    model_families: Dict[str, int]
    context_length_buckets: Dict[str, int]
    engineering_metrics: Dict[str, int]


class QualitySnapshot(StrictModel):
    schema_errors: int = Field(ge=0)
    schema_warnings: int = Field(ge=0)
    diagnostic_codes: Dict[str, int]
    duplicate_entity_ids: int = Field(ge=0)
    unresolved_wikilinks: int = Field(ge=0)


class ResearchSnapshot(StrictModel):
    schema_version: Literal["0.2"] = "0.2"
    snapshot_id: str
    research_id: str
    wiki_source_hash: str
    corpus: CorpusSnapshot
    taxonomy: TaxonomySnapshot
    evidence: EvidenceSnapshot
    quality: QualitySnapshot


class ResearchGap(StrictModel):
    id: str
    key: str
    type: GapType
    question: str
    priority: float = Field(ge=0, le=1)
    reasons: Tuple[str, ...]
    evidence: Dict[str, Tuple[str, ...]] = Field(default_factory=dict)
    recommended_action: ResearchAction
    search_focus: Tuple[str, ...] = ()
    blocking: bool = False
    status: Literal["open", "unresolved", "resolved"] = "open"


class ResearchDecision(StrictModel):
    action: ResearchAction
    target_gap_id: Optional[str] = None
    reason: str
    expected_information_gain: float = Field(ge=0, le=1)
    source: Literal["deterministic-v0", "structured-llm"] = "deterministic-v0"

    @model_validator(mode="after")
    def _validate_target(self) -> "ResearchDecision":
        if self.action == "finish" and self.target_gap_id is not None:
            raise ValueError("A finish decision cannot target an open gap")
        if self.action in TARGET_REQUIRED_ACTIONS and not self.target_gap_id:
            raise ValueError(f"A {self.action} decision must target an open gap")
        return self


class ResearchActionResult(StrictModel):
    """Deterministic execution result passed back to the outer controller."""

    action_id: str
    action: ResearchAction
    target_gap_id: Optional[str] = None
    status: ActionStatus
    outcome: ActionOutcome
    attempted: bool
    tool_calls: int = Field(default=0, ge=0)
    changed_sources: Tuple[str, ...] = ()
    summary: str = ""
    error_codes: Tuple[str, ...] = ()
    semantic_artifact_ids: Tuple[str, ...] = ()
    metrics: Dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_execution_semantics(self) -> "ResearchActionResult":
        if self.action in TARGET_REQUIRED_ACTIONS and not self.target_gap_id:
            raise ValueError(f"A {self.action} result must retain its target gap")
        if not self.attempted and self.tool_calls:
            raise ValueError("A non-attempted action cannot report tool calls")
        if (
            self.outcome in {"positive", "negative_research_result"}
            and not self.attempted
        ):
            raise ValueError(f"Outcome {self.outcome} requires an attempted action")
        if self.outcome in {"precondition_blocked", "unsupported"}:
            if self.status != "blocked" or self.attempted:
                raise ValueError(
                    f"Outcome {self.outcome} must be blocked and non-attempted"
                )
        if self.outcome == "tool_failure" and self.status not in {"partial", "failed"}:
            raise ValueError("A tool failure must have partial or failed status")
        return self


class ActionAttemptStats(StrictModel):
    """Per ``(gap, action)`` counters used to prevent blind repetition."""

    attempt_key: str
    target_gap_id: str
    action: ResearchAction
    attempts: int = Field(default=0, ge=0)
    no_progress: int = Field(default=0, ge=0)
    tool_failures: int = Field(default=0, ge=0)
    negative_results: int = Field(default=0, ge=0)
    last_action_id: Optional[str] = None
    last_status: Optional[ActionStatus] = None


class NonConsensusAssessment(StrictModel):
    """Research product stored as a Wiki assessment page, not controller state."""

    id: str
    question: str
    result: NonConsensusResult
    claim_ids: Tuple[str, ...]
    evidence_ids: Tuple[str, ...]
    method_family: Optional[str] = None
    benchmark_ids: Tuple[str, ...] = ()
    rationale: str
    verified: bool = False

    @model_validator(mode="after")
    def _validate_content(self) -> "NonConsensusAssessment":
        for field_name in ("id", "question", "rationale"):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(f"{field_name} cannot be blank")
        if len(set(self.claim_ids)) != len(self.claim_ids):
            raise ValueError("claim_ids cannot contain duplicates")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("evidence_ids cannot contain duplicates")
        if len(set(self.benchmark_ids)) != len(self.benchmark_ids):
            raise ValueError("benchmark_ids cannot contain duplicates")
        if not self.id.startswith("assessment:"):
            raise ValueError("id must use the assessment: prefix")
        for field_name, values, prefix in (
            ("claim_ids", self.claim_ids, "claim:"),
            ("evidence_ids", self.evidence_ids, "experiment:"),
            ("benchmark_ids", self.benchmark_ids, "benchmark:"),
        ):
            if any(not value.startswith(prefix) for value in values):
                raise ValueError(f"{field_name} entries must use the {prefix} prefix")
        if self.verified and (not self.claim_ids or not self.evidence_ids):
            raise ValueError(
                "A verified assessment requires at least one claim and evidence ID"
            )
        return self


class DoneCriteria(StrictModel):
    schema_version: Literal["0.2"] = "0.2"
    status: Literal["draft", "active"] = "draft"
    facet_requirements: Dict[str, RequiredFacetStatus]
    minimum_method_families: int = Field(default=5, ge=0)
    minimum_core_candidates: int = Field(default=40, ge=0)
    minimum_ingested_papers: int = Field(default=20, ge=0)
    minimum_verified_papers: int = Field(default=10, ge=0)
    minimum_experiments: int = Field(default=50, ge=0)
    minimum_verified_claims: int = Field(default=30, ge=0)
    minimum_evidence_locator_ratio: float = Field(default=0.9, ge=0, le=1)
    maximum_schema_errors: int = Field(default=0, ge=0)
    require_nonconsensus_review: bool = True
    minimum_verified_nonconsensus_assessments: int = Field(default=3, ge=0)
    context_bucket_requirements: Dict[str, int] = Field(
        default_factory=lambda: {"8K-32K": 3, "32K-64K": 3, ">=64K": 3}
    )
    engineering_metric_requirements: Dict[str, int] = Field(
        default_factory=lambda: {"latency": 3, "memory": 3}
    )
    require_no_open_blocking_gaps: bool = True
    minimum_completed_search_rounds: int = Field(default=2, ge=1)
    saturation_window: int = Field(default=2, ge=1)
    saturation_novelty_threshold: float = Field(default=0.0, ge=0)
    max_research_iterations: int = Field(default=30, ge=1)
    max_search_runs: int = Field(default=20, ge=1)
    max_ingested_papers: int = Field(default=100, ge=1)
    max_tool_calls: int = Field(default=500, ge=1)
    max_no_progress_rounds: int = Field(default=3, ge=1)

    @model_validator(mode="after")
    def _validate_saturation_window(self) -> "DoneCriteria":
        if self.saturation_window > self.minimum_completed_search_rounds:
            raise ValueError(
                "saturation_window cannot exceed minimum_completed_search_rounds"
            )
        if any(not str(name).strip() for name in self.facet_requirements):
            raise ValueError("facet_requirements cannot contain blank names")
        if self.require_nonconsensus_review and (
            self.minimum_verified_nonconsensus_assessments < 1
        ):
            raise ValueError(
                "minimum_verified_nonconsensus_assessments must be positive when review is required"
            )
        for name, count in {
            **self.context_bucket_requirements,
            **self.engineering_metric_requirements,
        }.items():
            if not str(name).strip() or count < 0:
                raise ValueError(
                    "evidence dimension requirements need names and nonnegative counts"
                )
        return self


class DoneCheck(StrictModel):
    complete: bool
    coverage_passed: bool
    quality_passed: bool
    saturation_passed: bool
    blocking_gaps_passed: bool
    blocking_gap_ids: Tuple[str, ...] = ()
    budget_exhausted: bool
    stop_reason: Optional[StopReason] = None
    failures: Tuple[str, ...] = ()
    budget_hits: Tuple[str, ...] = ()


class ProgressMeasurement(StrictModel):
    baseline: bool
    action_attempted: bool
    changed: bool
    deltas: Mapping[str, int]
    progress_score: float = Field(ge=0)
    made_progress: bool
    no_progress_rounds: int = Field(ge=0)
    changed_sources: Tuple[str, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_progress_score(cls, value: Any) -> Any:
        if isinstance(value, Mapping) and "novelty_yield" in value:
            payload = dict(value)
            legacy = payload.pop("novelty_yield")
            if "progress_score" in payload and payload["progress_score"] != legacy:
                raise ValueError("progress_score conflicts with legacy novelty_yield")
            payload["progress_score"] = legacy
            return payload
        return value

    @property
    def novelty_yield(self) -> float:
        """Backward-compatible accessor; serialized state uses progress_score."""

        return self.progress_score
