#!/usr/bin/env python
"""Validate a search-paper YAML record and optionally repair its metrics."""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from search_common import (
    RELEVANCE_LABELS,
    RELEVANCE_SCORE_FIELDS,
    SCHEMA_VERSION,
    load_yaml,
    query_signature,
    recompute_metrics,
    write_yaml_atomic,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
RUN_STATUSES = {
    "planned",
    "running",
    "partial",
    "complete",
    "blocked-credential",
    "blocked-provider",
    "needs-review",
}
QUERY_STATUSES = {
    "planned",
    "succeeded",
    "empty",
    "failed",
    "skipped-duplicate",
    "blocked-credential",
}
REVIEW_STATES = {
    "metadata-only",
    "abstract-screened",
    "selected-for-ingest",
    "excluded",
    "needs-review",
}
COVERAGE_STATUSES = {"covered", "partial", "missing", "not-required"}
RELEVANCE_BASES = {None, "title-only", "title-and-abstract", "provider-metadata", "manual-note"}
SECRET_KEY_NAMES = {
    "token",
    "deepxiv_token",
    "api_key",
    "apikey",
    "authorization",
    "password",
    "secret",
}
PLACEHOLDER_PATTERN = re.compile(r"<[A-Za-z][^>\n]{0,100}>")
SECRET_VALUE_PATTERNS = (
    re.compile(r"DEEPXIV_TOKEN\s*=", re.IGNORECASE),
    re.compile(r"authorization\s*:\s*bearer\s+\S+", re.IGNORECASE),
    re.compile(r"(?:api[_-]?key|token)\s*[=:]\s*\S+", re.IGNORECASE),
)


@dataclass(frozen=True)
class Issue:
    severity: str
    path: str
    message: str


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate a search-paper run.")
    parser.add_argument("run", type=Path)
    parser.add_argument(
        "--fix-metrics",
        action="store_true",
        help="Replace coverage.metrics with deterministic recomputed values.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings as a failing validation result.",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable issues.")
    return parser.parse_args(argv)


def _issue(issues: List[Issue], severity: str, path: str, message: str) -> None:
    issues.append(Issue(severity=severity, path=path, message=message))


def _walk(value: Any, path: str = "$") -> Iterable[Tuple[str, Any, Optional[str]]]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            child = f"{path}.{key_text}"
            yield child, item, key_text
            yield from _walk(item, child)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            child = f"{path}[{index}]"
            yield child, item, None
            yield from _walk(item, child)


def _validate_secrets_and_placeholders(run: Mapping[str, Any], issues: List[Issue]) -> None:
    for path, value, key in _walk(run):
        if key and key.casefold() in SECRET_KEY_NAMES and value not in (None, "", [], {}):
            _issue(issues, "error", path, "Credential-like field must not contain a value.")
        if isinstance(value, str):
            if PLACEHOLDER_PATTERN.search(value):
                _issue(issues, "error", path, "Unresolved template placeholder.")
            if any(pattern.search(value) for pattern in SECRET_VALUE_PATTERNS):
                _issue(issues, "error", path, "Possible credential material detected.")


def _validate_run_header(run: Mapping[str, Any], issues: List[Issue]) -> None:
    if str(run.get("schema_version")) != SCHEMA_VERSION:
        _issue(
            issues,
            "error",
            "$.schema_version",
            f"Expected schema version {SCHEMA_VERSION}.",
        )

    header = run.get("run")
    if not isinstance(header, Mapping):
        _issue(issues, "error", "$.run", "Required mapping is missing.")
        return

    for field in ("id", "topic_slug", "question", "created_at", "updated_at"):
        if not header.get(field):
            _issue(issues, "error", f"$.run.{field}", "Required value is missing.")

    status = header.get("status")
    if status not in RUN_STATUSES:
        _issue(issues, "error", "$.run.status", f"Unsupported status: {status!r}.")
    if status == "complete" and not header.get("stop_reason"):
        _issue(issues, "error", "$.run.stop_reason", "Complete runs require a stop reason.")

    provider = header.get("provider")
    if not isinstance(provider, Mapping):
        _issue(issues, "error", "$.run.provider", "Provider mapping is required.")
    else:
        for field in ("name", "interface", "package_version", "source"):
            if not provider.get(field):
                _issue(issues, "error", f"$.run.provider.{field}", "Required value is missing.")

    budget = header.get("budget")
    if not isinstance(budget, Mapping):
        _issue(issues, "error", "$.run.budget", "Budget mapping is required.")
    else:
        max_queries = budget.get("max_queries")
        if not isinstance(max_queries, int) or max_queries < 1:
            _issue(issues, "error", "$.run.budget.max_queries", "Must be a positive integer.")
        elif isinstance(run.get("queries"), list) and len(run["queries"]) > max_queries:
            _issue(issues, "error", "$.queries", "Query count exceeds run.budget.max_queries.")


def _validate_scope(run: Mapping[str, Any], issues: List[Issue]) -> None:
    scope = run.get("scope")
    if not isinstance(scope, Mapping):
        _issue(issues, "error", "$.scope", "Scope mapping is required.")
        return
    for field in ("included_concepts", "excluded_concepts", "required_facets", "assumptions"):
        if not isinstance(scope.get(field), list):
            _issue(issues, "error", f"$.scope.{field}", "Expected a list.")
    if not scope.get("included_concepts"):
        _issue(issues, "warning", "$.scope.included_concepts", "Search scope is not yet explicit.")
    if not scope.get("required_facets"):
        _issue(issues, "warning", "$.scope.required_facets", "No coverage facets are defined.")


def _validate_queries(
    run: Mapping[str, Any],
    issues: List[Issue],
) -> Tuple[Set[str], Dict[str, int]]:
    queries = run.get("queries")
    if not isinstance(queries, list):
        _issue(issues, "error", "$.queries", "Expected a list.")
        return set(), {}
    if not queries:
        _issue(issues, "warning", "$.queries", "No search queries are planned.")

    query_ids: Set[str] = set()
    query_rounds: Dict[str, int] = {}
    signatures: Dict[str, str] = {}
    default_source = str(((run.get("run") or {}).get("provider") or {}).get("source") or "arxiv")

    for index, query in enumerate(queries):
        path = f"$.queries[{index}]"
        if not isinstance(query, Mapping):
            _issue(issues, "error", path, "Query must be a mapping.")
            continue
        query_id = str(query.get("id") or "")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", query_id):
            _issue(issues, "error", f"{path}.id", "Query ID is missing or filesystem-unsafe.")
        elif query_id in query_ids:
            _issue(issues, "error", f"{path}.id", f"Duplicate query ID {query_id}.")
        else:
            query_ids.add(query_id)

        round_value = query.get("round")
        if not isinstance(round_value, int) or round_value < 1:
            _issue(issues, "error", f"{path}.round", "Round must be a positive integer.")
        else:
            query_rounds[query_id] = round_value

        text = str(query.get("text") or "").strip()
        if not text:
            _issue(issues, "error", f"{path}.text", "Query text is required.")
        elif len(text) > 500:
            _issue(issues, "error", f"{path}.text", "DeepXiv query exceeds 500 characters.")
        if not query.get("family"):
            _issue(issues, "warning", f"{path}.family", "Query family is missing.")
        if not query.get("purpose"):
            _issue(issues, "warning", f"{path}.purpose", "Query purpose is missing.")

        filters = query.get("filters")
        if not isinstance(filters, Mapping):
            _issue(issues, "error", f"{path}.filters", "Filters mapping is required.")
        else:
            size = filters.get("size", 20)
            offset = filters.get("offset", 0)
            if not isinstance(size, int) or not 1 <= size <= 100:
                _issue(issues, "error", f"{path}.filters.size", "Size must be 1 to 100.")
            if not isinstance(offset, int) or not 0 <= offset <= 10000:
                _issue(issues, "error", f"{path}.filters.offset", "Offset must be 0 to 10000.")

        execution = query.get("execution")
        if not isinstance(execution, Mapping):
            _issue(issues, "error", f"{path}.execution", "Execution mapping is required.")
        else:
            status = execution.get("status")
            if status not in QUERY_STATUSES:
                _issue(issues, "error", f"{path}.execution.status", f"Unsupported status: {status!r}.")
            if status in {"succeeded", "empty", "failed"} and not execution.get("executed_at"):
                _issue(issues, "error", f"{path}.execution.executed_at", "Executed query needs a timestamp.")
            raw_path = execution.get("raw_result_path")
            if raw_path:
                candidate_path = Path(str(raw_path))
                if candidate_path.is_absolute() or ".." in candidate_path.parts:
                    _issue(issues, "error", f"{path}.execution.raw_result_path", "Raw path must be relative and safe.")

        signature = query_signature(query, default_source)
        predecessor = signatures.get(signature)
        if predecessor:
            status = (query.get("execution") or {}).get("status")
            if status != "skipped-duplicate":
                _issue(
                    issues,
                    "warning",
                    path,
                    f"Same text and filters as {predecessor}; mark or justify the duplicate.",
                )
        else:
            signatures[signature] = query_id

    return query_ids, query_rounds


def _validate_candidates(
    run: Mapping[str, Any],
    query_ids: Set[str],
    issues: List[Issue],
) -> Set[str]:
    candidates = run.get("candidates")
    if not isinstance(candidates, list):
        _issue(issues, "error", "$.candidates", "Expected a list.")
        return set()

    candidate_ids: Set[str] = set()
    for index, candidate in enumerate(candidates):
        path = f"$.candidates[{index}]"
        if not isinstance(candidate, Mapping):
            _issue(issues, "error", path, "Candidate must be a mapping.")
            continue

        candidate_id = str(candidate.get("candidate_id") or "")
        source = str(candidate.get("source") or "")
        source_id = str(candidate.get("source_id") or "")
        expected_id = f"{source}:{source_id}" if source and source_id else ""
        if not candidate_id:
            _issue(issues, "error", f"{path}.candidate_id", "Candidate ID is required.")
        elif candidate_id in candidate_ids:
            _issue(issues, "error", f"{path}.candidate_id", f"Duplicate candidate {candidate_id}.")
        else:
            candidate_ids.add(candidate_id)
        if expected_id and candidate_id != expected_id:
            _issue(issues, "error", f"{path}.candidate_id", f"Expected {expected_id}.")
        if candidate.get("status") != "candidate":
            _issue(issues, "error", f"{path}.status", "Search output status must be candidate.")
        if not candidate.get("title"):
            _issue(issues, "warning", f"{path}.title", "Provider title is missing.")
        if not candidate.get("authors"):
            _issue(issues, "warning", f"{path}.authors", "Provider authors are missing.")
        if not candidate.get("year"):
            _issue(issues, "warning", f"{path}.year", "Publication year is missing.")

        discoveries = candidate.get("discovered_by")
        if not isinstance(discoveries, list) or not discoveries:
            _issue(issues, "error", f"{path}.discovered_by", "At least one discovery record is required.")
        else:
            for discovery_index, discovery in enumerate(discoveries):
                discovery_path = f"{path}.discovered_by[{discovery_index}]"
                if not isinstance(discovery, Mapping):
                    _issue(issues, "error", discovery_path, "Discovery must be a mapping.")
                    continue
                if str(discovery.get("query_id")) not in query_ids:
                    _issue(issues, "error", f"{discovery_path}.query_id", "Unknown query ID.")
                rank = discovery.get("provider_rank")
                if not isinstance(rank, int) or rank < 1:
                    _issue(issues, "error", f"{discovery_path}.provider_rank", "Rank must be positive.")

        relevance = candidate.get("relevance")
        if not isinstance(relevance, Mapping):
            _issue(issues, "error", f"{path}.relevance", "Relevance mapping is required.")
        else:
            label = relevance.get("label")
            if label is None:
                _issue(issues, "warning", f"{path}.relevance.label", "Candidate is untriaged.")
            elif label not in RELEVANCE_LABELS:
                _issue(issues, "error", f"{path}.relevance.label", f"Unsupported label: {label!r}.")
            scores = relevance.get("scores")
            if not isinstance(scores, Mapping):
                _issue(issues, "error", f"{path}.relevance.scores", "Scores mapping is required.")
            else:
                for field in RELEVANCE_SCORE_FIELDS:
                    score = scores.get(field)
                    if score is not None and (not isinstance(score, int) or not 0 <= score <= 2):
                        _issue(issues, "error", f"{path}.relevance.scores.{field}", "Score must be 0, 1, 2, or null.")
            basis = relevance.get("basis")
            if basis not in RELEVANCE_BASES:
                _issue(issues, "error", f"{path}.relevance.basis", f"Unsupported basis: {basis!r}.")
            if label is not None and not relevance.get("reason"):
                _issue(issues, "error", f"{path}.relevance.reason", "Triaged candidate needs a reason.")

        review_state = candidate.get("review_state")
        if review_state not in REVIEW_STATES:
            _issue(issues, "error", f"{path}.review_state", f"Unsupported state: {review_state!r}.")
        if review_state == "excluded" and not candidate.get("exclusion_reason"):
            _issue(issues, "error", f"{path}.exclusion_reason", "Excluded candidate needs a reason.")

    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, Mapping):
            continue
        for related in candidate.get("possible_version_of", []) or []:
            if related not in candidate_ids:
                _issue(
                    issues,
                    "error",
                    f"$.candidates[{index}].possible_version_of",
                    f"Unknown candidate ID {related}.",
                )
    return candidate_ids


def _validate_coverage(
    run: Mapping[str, Any],
    candidate_ids: Set[str],
    issues: List[Issue],
) -> None:
    coverage = run.get("coverage")
    if not isinstance(coverage, Mapping):
        _issue(issues, "error", "$.coverage", "Coverage mapping is required.")
        return
    facets = coverage.get("facets")
    if not isinstance(facets, list):
        _issue(issues, "error", "$.coverage.facets", "Expected a list.")
        return
    facet_names: Set[str] = set()
    for index, facet in enumerate(facets):
        path = f"$.coverage.facets[{index}]"
        if not isinstance(facet, Mapping):
            _issue(issues, "error", path, "Facet must be a mapping.")
            continue
        if not facet.get("name"):
            _issue(issues, "error", f"{path}.name", "Facet name is required.")
        else:
            name = str(facet.get("name"))
            if name in facet_names:
                _issue(issues, "error", f"{path}.name", f"Duplicate coverage facet {name}.")
            facet_names.add(name)
        if facet.get("status") not in COVERAGE_STATUSES:
            _issue(issues, "error", f"{path}.status", "Unsupported coverage status.")
        for candidate_id in facet.get("candidate_ids", []) or []:
            if candidate_id not in candidate_ids:
                _issue(issues, "error", f"{path}.candidate_ids", f"Unknown candidate {candidate_id}.")

    required_facets = ((run.get("scope") or {}).get("required_facets") or [])
    for required in required_facets:
        if str(required) not in facet_names:
            _issue(
                issues,
                "error",
                "$.coverage.facets",
                f"Required facet {required!r} has no coverage record.",
            )


def validate_run(run: Mapping[str, Any]) -> Tuple[List[Issue], Dict[str, Any]]:
    issues: List[Issue] = []
    _validate_secrets_and_placeholders(run, issues)
    _validate_run_header(run, issues)
    _validate_scope(run, issues)
    query_ids, _ = _validate_queries(run, issues)
    candidate_ids = _validate_candidates(run, query_ids, issues)
    _validate_coverage(run, candidate_ids, issues)

    copy_for_metrics = copy.deepcopy(dict(run))
    expected_metrics = recompute_metrics(copy_for_metrics)
    actual_metrics = ((run.get("coverage") or {}).get("metrics"))
    if actual_metrics != expected_metrics:
        _issue(issues, "error", "$.coverage.metrics", "Stored metrics do not match deterministic recomputation.")

    status = (run.get("run") or {}).get("status")
    if status == "complete":
        untriaged = expected_metrics["relevance_counts"]["untriaged"]
        if untriaged:
            _issue(issues, "error", "$.run.status", "Complete run contains untriaged candidates.")
        missing_facets = [
            facet.get("name")
            for facet in (run.get("coverage") or {}).get("facets", []) or []
            if isinstance(facet, Mapping) and facet.get("status") == "missing"
        ]
        if missing_facets:
            _issue(issues, "warning", "$.coverage.facets", "Complete run still has missing facets.")

    return issues, expected_metrics


def _resolve_run_path(value: Path) -> Path:
    path = value if value.is_absolute() else REPOSITORY_ROOT / value
    resolved = path.resolve()
    try:
        resolved.relative_to(REPOSITORY_ROOT)
    except ValueError as exc:
        raise ValueError(f"Run must stay inside repository root: {REPOSITORY_ROOT}") from exc
    return resolved


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        path = _resolve_run_path(args.run)
        run = load_yaml(path)
        issues, expected_metrics = validate_run(run)
        if args.fix_metrics:
            run.setdefault("coverage", {})["metrics"] = expected_metrics
            write_yaml_atomic(path, run)
            issues, _ = validate_run(run)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps([asdict(issue) for issue in issues], ensure_ascii=False, indent=2))
    else:
        for issue in issues:
            print(f"{issue.severity.upper()}: {issue.path}: {issue.message}")
        errors = sum(issue.severity == "error" for issue in issues)
        warnings = sum(issue.severity == "warning" for issue in issues)
        print(f"Validation: {errors} error(s), {warnings} warning(s)")

    has_errors = any(issue.severity == "error" for issue in issues)
    has_warnings = any(issue.severity == "warning" for issue in issues)
    return 1 if has_errors or (args.strict and has_warnings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
