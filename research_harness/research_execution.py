"""Deterministic action executors for the autonomous outer research loop."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

import yaml  # type: ignore[import-untyped]

from .config import HarnessSettings
from .research_models import (
    ResearchActionResult,
    ResearchDecision,
    ResearchGap,
    ResearchSnapshot,
)
from .skill_registry import SkillRegistry


QUERY_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
ELIGIBLE_SEARCH_STATUSES = {"planned", "blocked-credential"}
EXECUTED_SEARCH_STATUSES = {"succeeded", "empty", "failed"}


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


class DeterministicActionExecutor:
    """Dispatch a finite action set without an LLM or a smart Skill router."""

    def __init__(self, settings: HarnessSettings, *, timeout_seconds: int = 300):
        if timeout_seconds < 1:
            raise ValueError("timeout_seconds must be positive")
        self.settings = settings
        self.timeout_seconds = timeout_seconds
        self.registry = SkillRegistry(settings.skills_root)

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
        return ResearchActionResult(
            action_id=action_id,
            action=decision.action,
            target_gap_id=decision.target_gap_id,
            status="blocked",
            outcome="unsupported",
            attempted=False,
            summary=(
                f"Action {decision.action!r} has no deterministic V1 executor. "
                "Only the search-paper execution path is enabled."
            ),
            error_codes=("unsupported-action",),
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
        run_path, query_ids = self._select_search_run(snapshot, gap)
        if run_path is None or not query_ids:
            return self._blocked_search(
                decision=decision,
                action_id=action_id,
                code="search-plan-required",
                summary=(
                    "No eligible planned query is bound to this gap. Create or extend a "
                    "validated search-run before executing another search action."
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

        changed_sources = []
        if _content_hash(run_path) != before_hash:
            changed_sources.append(_relative_path(self.settings, run_path))
        for query in after_queries.values():
            raw_path = (query.get("execution") or {}).get("raw_result_path")
            if raw_path and str(raw_path) not in before_raw_paths:
                changed_sources.append(str(raw_path).replace("\\", "/"))

        metrics = {
            "queries_selected": len(query_ids),
            "queries_attempted": provider_calls,
            "queries_succeeded": succeeded,
            "queries_failed": failures,
            "empty_results": empty,
            "new_candidates": new_candidates,
        }
        attempted = True
        if completed.returncode == 0:
            status = "success"
            outcome = "positive" if new_candidates else "negative_research_result"
        else:
            status = "partial" if succeeded else "failed"
            outcome = "tool_failure"
        summary = (
            f"search-paper processed {len(query_ids)} planned queries: "
            f"{succeeded} succeeded/empty, {failures} failed, "
            f"{new_candidates} new unique candidates."
        )
        error_codes = list(_error_codes(after, query_ids))
        if completed.returncode and not error_codes:
            error_codes.append(f"deepxiv-exit-{completed.returncode}")
        return ResearchActionResult(
            action_id=action_id,
            action="search",
            target_gap_id=decision.target_gap_id,
            status=status,
            outcome=outcome,
            attempted=attempted,
            tool_calls=provider_calls,
            changed_sources=tuple(dict.fromkeys(changed_sources)),
            summary=summary,
            error_codes=tuple(error_codes),
            metrics=metrics,
        )
