"""Strict contracts for bounded online Canary execution."""

from __future__ import annotations

from typing import Any, Dict, Literal, Tuple

from pydantic import BaseModel, ConfigDict, Field, model_validator


CanaryStage = Literal[
    "retrieval",
    "screening",
    "ingest",
    "verification",
    "revision",
    "reverification",
    "analysis",
]
CanaryReachedStage = Literal[
    "not-started",
    "retrieval",
    "screening",
    "ingest",
    "verification",
    "revision",
    "reverification",
    "analysis",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CanaryLimits(_StrictModel):
    schema_version: Literal["0.1"] = "0.1"
    max_planned_queries: int = Field(default=1, ge=1, le=8)
    max_provider_query_calls: int = Field(default=1, ge=1, le=8)
    max_new_unique_candidates: int = Field(default=5, ge=1, le=100)
    max_selected_candidates: int = Field(default=3, ge=1, le=20)
    max_papers_ingested: int = Field(default=1, ge=1, le=10)
    max_actions: int = Field(default=3, ge=1, le=20)
    deadline_seconds: int = Field(default=300, ge=30, le=3_600)
    provider_max_retries: int = Field(default=0, ge=0, le=10)
    wiki_write_mode: Literal["immediate", "deferred"] = "immediate"
    stop_after: CanaryStage = "screening"

    @model_validator(mode="after")
    def _validate_stage_budget(self) -> "CanaryLimits":
        ingestion_actions = self.max_papers_ingested
        required_actions = {
            "retrieval": 1,
            "screening": 1,
            "ingest": 1 + ingestion_actions,
            "verification": 2 + ingestion_actions,
            "revision": 3 + ingestion_actions,
            "reverification": 4 + ingestion_actions,
            "analysis": 5 + ingestion_actions,
        }[self.stop_after]
        if self.max_actions < required_actions:
            raise ValueError(
                f"stop_after={self.stop_after} requires at least {required_actions} actions"
            )
        if self.max_provider_query_calls > self.max_planned_queries:
            raise ValueError(
                "max_provider_query_calls cannot exceed max_planned_queries"
            )
        if self.max_papers_ingested > self.max_selected_candidates:
            raise ValueError(
                "max_papers_ingested cannot exceed max_selected_candidates"
            )
        if self.wiki_write_mode == "deferred" and self.stop_after not in {
            "retrieval",
            "screening",
            "ingest",
        }:
            raise ValueError(
                "Deferred Wiki mode stops after ingest staging; publish staged drafts "
                "before verification."
            )
        return self


class SearchExecutionLimits(_StrictModel):
    max_planned_queries: int = Field(ge=1, le=8)
    max_provider_query_calls: int = Field(ge=1, le=8)
    max_new_unique_candidates: int = Field(ge=1, le=100)
    provider_max_retries: int = Field(default=0, ge=0, le=10)
    stop_after: Literal["retrieval", "screening"] = "screening"

    @classmethod
    def from_canary(cls, limits: CanaryLimits) -> "SearchExecutionLimits":
        return cls(
            max_planned_queries=limits.max_planned_queries,
            max_provider_query_calls=limits.max_provider_query_calls,
            max_new_unique_candidates=limits.max_new_unique_candidates,
            provider_max_retries=limits.provider_max_retries,
            stop_after=(
                "retrieval" if limits.stop_after == "retrieval" else "screening"
            ),
        )


class CanaryRunReport(_StrictModel):
    schema_version: Literal["0.1"] = "0.1"
    run_id: str
    research_id: str
    status: Literal["passed", "failed", "blocked", "timeout"]
    stage_reached: CanaryReachedStage
    stop_after: CanaryStage
    started_at: str
    finished_at: str
    duration_seconds: float = Field(ge=0)
    workspace_root: str
    limits: CanaryLimits
    action_results: Tuple[Dict[str, Any], ...] = ()
    invariants: Dict[str, bool] = Field(default_factory=dict)
    error_codes: Tuple[str, ...] = ()
    semantic_manifest: str | None = None
    trajectory_path: str | None = None


__all__ = [
    "CanaryLimits",
    "CanaryRunReport",
    "CanaryReachedStage",
    "CanaryStage",
    "SearchExecutionLimits",
]
