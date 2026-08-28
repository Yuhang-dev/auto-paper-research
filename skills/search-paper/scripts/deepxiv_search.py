#!/usr/bin/env python
"""Execute planned DeepXiv queries and update a search-run YAML record."""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, MutableMapping, Optional, Sequence, Tuple

from search_common import (
    append_error,
    link_possible_versions,
    load_yaml,
    merge_candidates,
    normalize_response,
    package_version,
    query_signature,
    recompute_metrics,
    sanitize_message,
    utc_now,
    write_json_atomic,
    write_yaml_atomic,
)

class _UnavailableProviderError(Exception):
    """Placeholder until deepxiv-sdk is imported for an authorized execution."""


APIError = _UnavailableProviderError
AuthenticationError = _UnavailableProviderError
BadRequestError = _UnavailableProviderError
RateLimitError = _UnavailableProviderError
ServerError = _UnavailableProviderError
Reader = None
DEEPXIV_IMPORT_ERROR = None


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
ELIGIBLE_STATUSES = {"planned", "blocked-credential"}


def _load_deepxiv_sdk() -> None:
    """Import the provider only when a non-dry run has credentials."""

    global APIError
    global AuthenticationError
    global BadRequestError
    global RateLimitError
    global Reader
    global ServerError
    global DEEPXIV_IMPORT_ERROR

    if Reader is not None:
        return
    try:
        from deepxiv_sdk import (
            APIError as ProviderAPIError,
            AuthenticationError as ProviderAuthenticationError,
            BadRequestError as ProviderBadRequestError,
            RateLimitError as ProviderRateLimitError,
            Reader as ProviderReader,
            ServerError as ProviderServerError,
        )
    except Exception as import_error:  # Provider import may initialize local assets.
        DEEPXIV_IMPORT_ERROR = import_error
        return
    APIError = ProviderAPIError
    AuthenticationError = ProviderAuthenticationError
    BadRequestError = ProviderBadRequestError
    RateLimitError = ProviderRateLimitError
    Reader = ProviderReader
    ServerError = ProviderServerError
    DEEPXIV_IMPORT_ERROR = None


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run planned DeepXiv retrieval queries with resumable YAML updates."
    )
    parser.add_argument("--run", required=True, type=Path, help="Search-run YAML path.")
    parser.add_argument(
        "--query-id",
        action="append",
        default=[],
        help="Execute only this query ID. Repeat for several IDs.",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Allow queries currently marked failed to run again.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print eligible calls without credentials, network, or file changes.",
    )
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument(
        "--max-provider-query-calls",
        type=int,
        help="Hard cap on Reader.search calls made by this invocation.",
    )
    parser.add_argument(
        "--max-new-unique-candidates",
        type=int,
        help=(
            "Hard cap on new unique candidates accepted by this invocation; "
            "duplicate provenance may still be merged."
        ),
    )
    parser.add_argument("--retry-delay", type=float, default=1.0)
    parser.add_argument(
        "--raw-dir",
        type=Path,
        help="Raw JSON directory inside the repository. Defaults beside the run.",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop after the first provider or normalization failure.",
    )
    return parser.parse_args(argv)


def _inside_repository(path: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(REPOSITORY_ROOT)
    except ValueError as exc:
        raise ValueError(f"Path must stay inside repository root: {REPOSITORY_ROOT}") from exc
    return resolved


def _run_path(value: Path) -> Path:
    path = value if value.is_absolute() else REPOSITORY_ROOT / value
    path = _inside_repository(path)
    if not path.is_file():
        raise FileNotFoundError(f"Search run not found: {path}")
    return path


def _raw_directory(args: argparse.Namespace, run_path: Path, run: Mapping[str, Any]) -> Path:
    if args.raw_dir:
        raw = args.raw_dir if args.raw_dir.is_absolute() else REPOSITORY_ROOT / args.raw_dir
    else:
        run_id = str((run.get("run") or {}).get("id") or run_path.stem)
        raw = run_path.parent / "raw" / run_id
    return _inside_repository(raw)


def _selected_queries(run: Mapping[str, Any], args: argparse.Namespace) -> List[MutableMapping[str, Any]]:
    allowed = set(ELIGIBLE_STATUSES)
    if args.retry_failed:
        allowed.add("failed")

    requested = set(args.query_id)
    queries = run.get("queries", []) or []
    selected: List[MutableMapping[str, Any]] = []
    for query in queries:
        query_id = str(query.get("id") or "")
        status = str((query.get("execution") or {}).get("status") or "planned")
        if requested and query_id not in requested:
            continue
        if status in allowed:
            selected.append(query)

    missing = requested - {str(query.get("id")) for query in queries}
    if missing:
        raise ValueError(f"Unknown query IDs: {', '.join(sorted(missing))}")
    return selected


def _duplicate_predecessors(
    run: Mapping[str, Any],
    selected: Sequence[Mapping[str, Any]],
    default_source: str,
) -> Dict[str, str]:
    selected_ids = {str(query.get("id")) for query in selected}
    seen: Dict[str, str] = {}
    duplicates: Dict[str, str] = {}

    for query in run.get("queries", []) or []:
        query_id = str(query.get("id") or "")
        signature = query_signature(query, default_source)
        status = str((query.get("execution") or {}).get("status") or "planned")
        if query_id not in selected_ids and status in {"succeeded", "empty"}:
            seen.setdefault(signature, query_id)

    for query in selected:
        query_id = str(query.get("id") or "")
        signature = query_signature(query, default_source)
        if signature in seen:
            duplicates[query_id] = seen[signature]
        else:
            seen[signature] = query_id
    return duplicates


def _search_kwargs(
    query: Mapping[str, Any],
    default_source: str,
    *,
    size_cap: Optional[int] = None,
) -> Dict[str, Any]:
    filters = query.get("filters") or {}
    requested_size = int(filters.get("size") or 20)
    effective_size = (
        min(requested_size, size_cap) if size_cap is not None else requested_size
    )
    kwargs: Dict[str, Any] = {
        "query": str(query.get("text") or "").strip(),
        "size": effective_size,
        "offset": int(filters.get("offset") or 0),
        "source": str(filters.get("source") or default_source),
        "use_fine_rerank": bool(filters.get("use_fine_rerank", False)),
    }

    mappings = (
        ("categories", "categories"),
        ("authors", "authors"),
        ("orgs", "orgs"),
        ("venues", "venue"),
        ("venue_year", "venue_year"),
        ("min_citations", "min_citation"),
        ("date_search_type", "date_search_type"),
        ("date_str", "date_str"),
        ("date_from", "date_from"),
        ("date_to", "date_to"),
    )
    for source_name, argument_name in mappings:
        value = filters.get(source_name)
        if value not in (None, "", [], {}):
            kwargs[argument_name] = value
    return kwargs


def _merge_candidates_bounded(
    existing: Sequence[Mapping[str, Any]],
    incoming: Sequence[Mapping[str, Any]],
    *,
    remaining_new_capacity: Optional[int],
) -> Tuple[List[Dict[str, Any]], int, int]:
    """Merge provider-ranked candidates while bounding new unique identities.

    Exact-ID and DOI duplicates are always merged so their discovery provenance is
    retained. New identities are accepted in provider-rank/candidate-id order only
    while capacity remains.
    """

    merged = merge_candidates(existing, [])
    accepted_new = 0
    skipped_new = 0
    ordered = sorted(
        incoming,
        key=lambda candidate: (
            int(
                ((candidate.get("discovered_by") or [{}])[0]).get(
                    "provider_rank", 2**31 - 1
                )
            ),
            str(candidate.get("candidate_id") or ""),
        ),
    )
    for candidate in ordered:
        before_ids = {
            str(item.get("candidate_id"))
            for item in merged
            if item.get("candidate_id")
        }
        trial = merge_candidates(merged, [candidate])
        after_ids = {
            str(item.get("candidate_id"))
            for item in trial
            if item.get("candidate_id")
        }
        adds_identity = len(after_ids) > len(before_ids)
        if (
            adds_identity
            and remaining_new_capacity is not None
            and accepted_new >= remaining_new_capacity
        ):
            skipped_new += 1
            continue
        merged = trial
        if adds_identity:
            accepted_new += 1
    return merged, accepted_new, skipped_new


def _safe_payload(value: Any, token: str) -> Any:
    if isinstance(value, Mapping):
        cleaned: Dict[str, Any] = {}
        for key, item in value.items():
            if str(key).casefold() in {"token", "api_key", "apikey", "authorization"}:
                cleaned[str(key)] = "<redacted>"
            else:
                cleaned[str(key)] = _safe_payload(item, token)
        return cleaned
    if isinstance(value, list):
        return [_safe_payload(item, token) for item in value]
    if isinstance(value, tuple):
        return [_safe_payload(item, token) for item in value]
    if isinstance(value, str):
        return sanitize_message(value, secrets=[token])
    return value


def _coerce_total(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _existing_error(run: Mapping[str, Any], recurrence_key: str) -> Optional[str]:
    for error in run.get("errors", []) or []:
        if error.get("recurrence_key") == recurrence_key:
            return str(error.get("id"))
    return None


def _mark_missing_token(
    run: MutableMapping[str, Any],
    selected: Sequence[MutableMapping[str, Any]],
) -> None:
    recurrence_key = "deepxiv:missing-token"
    error_id = _existing_error(run, recurrence_key)
    if error_id is None:
        error_id = append_error(
            run,
            phase="authentication",
            error_class="MissingCredential",
            message="DEEPXIV_TOKEN is not configured",
            effect="No provider queries were executed.",
            recovery="Set DEEPXIV_TOKEN outside the repository and rerun.",
            recurrence_key=recurrence_key,
        )
    for query in selected:
        execution = query.setdefault("execution", {})
        execution["status"] = "blocked-credential"
        execution["error_id"] = error_id
    run["run"]["status"] = "blocked-credential"
    run["run"]["stop_reason"] = "missing-deepxiv-token"
    run["run"]["updated_at"] = utc_now()


def _record_query_failure(
    run: MutableMapping[str, Any],
    query: MutableMapping[str, Any],
    exc: Exception,
    token: str,
) -> str:
    query_id = str(query.get("id"))
    error_class = type(exc).__name__
    error_id = append_error(
        run,
        phase="provider-search",
        error_class=error_class,
        message=exc,
        effect=f"Query {query_id} produced no new candidates.",
        recovery="Review credentials, filters, provider status, and the safe error message.",
        recurrence_key=f"deepxiv:{error_class.casefold()}",
        query_id=query_id,
        secrets=[token],
    )
    execution = query.setdefault("execution", {})
    execution.update(
        {
            "status": "failed",
            "executed_at": utc_now(),
            "error_id": error_id,
        }
    )
    return error_id


def _dry_run(selected: Sequence[Mapping[str, Any]], default_source: str) -> None:
    if not selected:
        print("No eligible queries.")
        return
    for query in selected:
        kwargs = _search_kwargs(query, default_source)
        print(
            f"{query.get('id')}: source={kwargs['source']} size={kwargs['size']} "
            f"offset={kwargs['offset']} query={kwargs['query']!r}"
        )


def execute(args: argparse.Namespace) -> int:
    if args.timeout < 1:
        raise ValueError("timeout must be positive")
    if not 0 <= args.max_retries <= 10:
        raise ValueError("max-retries must be between 0 and 10")
    if args.retry_delay < 0:
        raise ValueError("retry-delay cannot be negative")
    requested_provider_calls = getattr(args, "max_provider_query_calls", None)
    if requested_provider_calls is not None and requested_provider_calls < 1:
        raise ValueError("max-provider-query-calls must be positive")
    requested_new_candidates = getattr(args, "max_new_unique_candidates", None)
    if requested_new_candidates is not None and requested_new_candidates < 0:
        raise ValueError("max-new-unique-candidates cannot be negative")

    run_path = _run_path(args.run)
    run = load_yaml(run_path)
    budget = (run.get("run") or {}).get("budget", {}) or {}
    recorded_provider_calls = budget.get("max_provider_query_calls")
    if recorded_provider_calls is not None and (
        not isinstance(recorded_provider_calls, int) or recorded_provider_calls < 1
    ):
        raise ValueError(
            "run.budget.max_provider_query_calls must be null or positive"
        )
    recorded_new_candidates = budget.get("max_new_unique_candidates")
    if recorded_new_candidates is not None and (
        not isinstance(recorded_new_candidates, int) or recorded_new_candidates < 0
    ):
        raise ValueError(
            "run.budget.max_new_unique_candidates must be null or non-negative"
        )
    recorded_retries = budget.get("provider_max_retries")
    if recorded_retries is not None and (
        not isinstance(recorded_retries, int) or not 0 <= recorded_retries <= 10
    ):
        raise ValueError(
            "run.budget.provider_max_retries must be null or between 0 and 10"
        )
    max_provider_calls = (
        recorded_provider_calls
        if requested_provider_calls is None
        else requested_provider_calls
        if recorded_provider_calls is None
        else min(requested_provider_calls, recorded_provider_calls)
    )
    max_new_candidates = (
        recorded_new_candidates
        if requested_new_candidates is None
        else requested_new_candidates
        if recorded_new_candidates is None
        else min(requested_new_candidates, recorded_new_candidates)
    )
    effective_retries = (
        args.max_retries
        if recorded_retries is None
        else min(args.max_retries, recorded_retries)
    )
    provider = (run.get("run") or {}).setdefault("provider", {})
    provider["package_version"] = package_version("deepxiv-sdk")
    default_source = str(provider.get("source") or "arxiv")
    selected = _selected_queries(run, args)

    if args.dry_run:
        _dry_run(selected, default_source)
        return 0

    if not selected:
        print("No eligible queries. Use --retry-failed to retry failed calls.")
        return 0

    duplicate_map = _duplicate_predecessors(run, selected, default_source)
    for query in selected:
        query_id = str(query.get("id"))
        if query_id in duplicate_map:
            execution = query.setdefault("execution", {})
            execution.update(
                {
                    "status": "skipped-duplicate",
                    "duplicate_of": duplicate_map[query_id],
                    "executed_at": None,
                }
            )
    selected = [query for query in selected if str(query.get("id")) not in duplicate_map]

    if max_provider_calls is not None:
        selected = selected[:max_provider_calls]

    max_queries = budget.get("max_queries")
    if isinstance(max_queries, int):
        planned_count = len(run.get("queries", []) or [])
        if planned_count > max_queries:
            raise ValueError(
                f"Query plan contains {planned_count} records but max_queries is {max_queries}"
            )
    total_candidate_budget = budget.get("max_candidates")
    if total_candidate_budget is not None:
        if not isinstance(total_candidate_budget, int) or total_candidate_budget < 0:
            raise ValueError("run.budget.max_candidates must be null or non-negative")
        existing_unique = len(
            {
                str(candidate.get("candidate_id"))
                for candidate in run.get("candidates", []) or []
                if candidate.get("candidate_id")
            }
        )
        remaining_total_capacity = max(0, total_candidate_budget - existing_unique)
        max_new_candidates = (
            remaining_total_capacity
            if max_new_candidates is None
            else min(max_new_candidates, remaining_total_capacity)
        )

    if not selected:
        run["run"].setdefault("budget", {}).update(
            {
                "max_provider_query_calls": max_provider_calls,
                "max_new_unique_candidates": max_new_candidates,
                "provider_max_retries": effective_retries,
            }
        )
        run["run"]["execution_totals"] = {
            "provider_query_calls": 0,
            "new_unique_candidates": 0,
        }
        run["run"]["updated_at"] = utc_now()
        recompute_metrics(run)
        write_yaml_atomic(run_path, run)
        print("No queries executed after duplicate and budget checks.")
        return 0

    if max_new_candidates == 0:
        run["run"].setdefault("budget", {}).update(
            {
                "max_provider_query_calls": max_provider_calls,
                "max_new_unique_candidates": 0,
                "provider_max_retries": effective_retries,
            }
        )
        run["run"]["execution_totals"] = {
            "provider_query_calls": 0,
            "new_unique_candidates": 0,
        }
        run["run"]["status"] = "partial"
        run["run"]["stop_reason"] = "new-candidate-budget-reached"
        run["run"]["updated_at"] = utc_now()
        recompute_metrics(run)
        write_yaml_atomic(run_path, run)
        print("No queries executed because the new-candidate budget is exhausted.")
        return 0

    token = os.getenv("DEEPXIV_TOKEN")
    if not token:
        _mark_missing_token(run, selected)
        recompute_metrics(run)
        write_yaml_atomic(run_path, run)
        print("ERROR: DEEPXIV_TOKEN is not configured.", file=sys.stderr)
        return 3

    _load_deepxiv_sdk()
    if Reader is None:
        raise RuntimeError(
            f"deepxiv-sdk is unavailable in this Python environment: {DEEPXIV_IMPORT_ERROR}"
        )

    reader = Reader(
        token=token,
        timeout=args.timeout,
        max_retries=effective_retries,
        retry_delay=args.retry_delay,
    )
    raw_dir = _raw_directory(args, run_path, run)
    run["run"]["status"] = "running"
    run["run"]["stop_reason"] = None
    run["run"]["updated_at"] = utc_now()
    write_yaml_atomic(run_path, run)

    failures = 0
    successes = 0
    provider_calls = 0
    initial_candidate_ids = {
        str(candidate.get("candidate_id"))
        for candidate in run.get("candidates", []) or []
        if candidate.get("candidate_id")
    }
    accepted_new_total = 0
    stop_now = False
    for query in selected:
        query_id = str(query.get("id"))
        remaining_capacity = (
            None
            if max_new_candidates is None
            else max(0, max_new_candidates - accepted_new_total)
        )
        if remaining_capacity == 0:
            run["run"]["stop_reason"] = "new-candidate-budget-reached"
            break
        kwargs = _search_kwargs(
            query,
            default_source,
            size_cap=remaining_capacity,
        )
        retrieved_at = utc_now()
        print(f"Executing {query_id}: {kwargs['query']}")
        try:
            provider_calls += 1
            response = reader.search(**kwargs)
            if not isinstance(response, Mapping):
                raise ValueError("DeepXiv returned a non-mapping response")

            raw_path = raw_dir / f"{re.sub(r'[^A-Za-z0-9_.-]+', '_', query_id)}.json"
            write_json_atomic(raw_path, _safe_payload(response, token))

            normalized, skipped = normalize_response(
                response,
                source=str(kwargs["source"]),
                query_id=query_id,
                retrieved_at=retrieved_at,
            )
            before_ids = {
                candidate.get("candidate_id")
                for candidate in run.get("candidates", []) or []
            }
            (
                run["candidates"],
                accepted_new,
                skipped_for_budget,
            ) = _merge_candidates_bounded(
                run.get("candidates", []) or [],
                normalized,
                remaining_new_capacity=remaining_capacity,
            )
            accepted_new_total += accepted_new
            link_possible_versions(run["candidates"])
            after_ids = {candidate.get("candidate_id") for candidate in run["candidates"]}

            result_list = response.get("result") or []
            retrieved_count = len(result_list) if isinstance(result_list, list) else 0
            execution = query.setdefault("execution", {})
            execution.update(
                {
                    "status": "succeeded" if retrieved_count else "empty",
                    "executed_at": retrieved_at,
                    "provider_total_count": _coerce_total(
                        response.get("total_count"),
                        retrieved_count,
                    ),
                    "retrieved_count": retrieved_count,
                    "retained_count": len(normalized),
                    "new_unique_count": len(after_ids - before_ids),
                    "effective_size": int(kwargs["size"]),
                    "budget_skipped_new_count": skipped_for_budget,
                    "raw_result_path": raw_path.relative_to(REPOSITORY_ROOT).as_posix(),
                    "error_id": None,
                }
            )

            if skipped:
                append_error(
                    run,
                    phase="normalization",
                    error_class="SkippedProviderRows",
                    message=f"{len(skipped)} rows lacked a usable mapping or stable source ID",
                    effect="Skipped malformed rows while retaining valid candidates.",
                    recovery="Inspect the raw response and extend normalization only if recurrent.",
                    recurrence_key="deepxiv:skipped-provider-rows",
                    query_id=query_id,
                )
                execution["skipped_rows"] = skipped
            successes += 1

        except (AuthenticationError, BadRequestError, RateLimitError, ServerError, APIError, ValueError) as exc:
            failures += 1
            _record_query_failure(run, query, exc, token)
            if isinstance(exc, AuthenticationError):
                run["run"]["status"] = "blocked-credential"
                run["run"]["stop_reason"] = "deepxiv-authentication-failed"
                stop_now = True
            elif isinstance(exc, RateLimitError):
                run["run"]["stop_reason"] = "deepxiv-rate-limit"
                stop_now = True
            elif args.fail_fast:
                stop_now = True
        except Exception as exc:
            failures += 1
            _record_query_failure(run, query, exc, token)
            if args.fail_fast:
                stop_now = True

        run["run"]["updated_at"] = utc_now()
        recompute_metrics(run)
        write_yaml_atomic(run_path, run)
        if stop_now:
            break

    final_candidate_ids = {
        str(candidate.get("candidate_id"))
        for candidate in run.get("candidates", []) or []
        if candidate.get("candidate_id")
    }
    actual_new_total = len(final_candidate_ids - initial_candidate_ids)
    if max_new_candidates is not None and actual_new_total > max_new_candidates:
        raise RuntimeError("new unique candidate hard limit was violated")
    run["run"].setdefault("budget", {}).update(
        {
            "max_provider_query_calls": max_provider_calls,
            "max_new_unique_candidates": max_new_candidates,
            "provider_max_retries": effective_retries,
        }
    )
    run["run"]["execution_totals"] = {
        "provider_query_calls": provider_calls,
        "new_unique_candidates": actual_new_total,
    }

    if run["run"].get("status") != "blocked-credential":
        remaining_planned = any(
            (query.get("execution") or {}).get("status") in ELIGIBLE_STATUSES
            for query in run.get("queries", []) or []
        )
        if failures or remaining_planned:
            run["run"]["status"] = "partial"
        elif successes and (run.get("coverage", {}).get("metrics", {}).get("relevance_counts", {}).get("untriaged", 0)):
            run["run"]["status"] = "needs-review"
        elif successes:
            run["run"]["status"] = "needs-review"
        else:
            run["run"]["status"] = "planned"

    run["run"]["updated_at"] = utc_now()
    recompute_metrics(run)
    write_yaml_atomic(run_path, run)
    print(f"Updated {run_path}")
    return 1 if failures else 0


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        return execute(args)
    except (FileNotFoundError, ValueError, RuntimeError, OSError) as exc:
        print(
            f"ERROR: {sanitize_message(exc, secrets=[os.getenv('DEEPXIV_TOKEN', '')])}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
