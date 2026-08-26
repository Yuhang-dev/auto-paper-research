"""Deterministic helpers for the search-paper Skill.

This module contains no LLM calls. It normalizes provider metadata, merges
exact stable-ID duplicates, links possible paper versions, sanitizes errors,
and computes reproducible search-process metrics.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple

import yaml


SCHEMA_VERSION = "0.1"
RELEVANCE_LABELS = ("core", "adjacent", "background", "exclude")
RELEVANCE_SCORE_FIELDS = (
    "sparsity_alignment",
    "long_context_alignment",
    "evidence_value",
    "engineering_value",
    "challenge_value",
)
EXECUTED_QUERY_STATUSES = {"succeeded", "empty", "failed"}


def utc_now() -> str:
    """Return an ISO 8601 UTC timestamp without fractional seconds."""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def package_version(distribution: str) -> str:
    """Return an installed distribution version or "unknown"."""

    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return "unknown"


def load_yaml(path: Path) -> Dict[str, Any]:
    """Load a YAML mapping and reject empty or non-mapping files."""

    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Expected a YAML mapping in {path}")
    return data


def _atomic_text_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Optional[Path] = None
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
            handle.write(text)
            temporary_path = Path(handle.name)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def write_yaml_atomic(path: Path, data: Mapping[str, Any]) -> None:
    """Write UTF-8 YAML atomically in the target directory."""

    rendered = yaml.safe_dump(
        dict(data),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=120,
    )
    _atomic_text_write(path, rendered)


def write_json_atomic(path: Path, data: Any) -> None:
    """Write provider JSON atomically without ASCII escaping."""

    rendered = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    _atomic_text_write(path, rendered)


def normalize_title(value: Any) -> str:
    """Normalize a title for conservative exact-title comparison."""

    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    text = re.sub(r"[\W_]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def normalize_doi(value: Any) -> Optional[str]:
    """Normalize a DOI for exact identifier matching."""

    if value in (None, ""):
        return None
    text = str(value).strip()
    text = re.sub(r"^doi:\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text, flags=re.IGNORECASE)
    text = text.strip().casefold()
    return text or None


def canonical_source_id(source: str, value: Any) -> Tuple[str, str]:
    """Return the canonical and exact returned provider identifiers."""

    if value is None or not str(value).strip():
        raise ValueError("Missing source identifier")

    exact = str(value).strip()
    cleaned = exact
    source = source.casefold()

    if source == "arxiv":
        cleaned = re.sub(r"^arxiv:\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(
            r"^https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = cleaned.split("?", 1)[0].split("#", 1)[0]
        cleaned = re.sub(r"\.pdf$", "", cleaned, flags=re.IGNORECASE)
        canonical = re.sub(r"v\d+$", "", cleaned, flags=re.IGNORECASE)
    elif source in {"biorxiv", "medrxiv"}:
        cleaned = re.sub(r"^https?://doi\.org/", "", cleaned, flags=re.IGNORECASE)
        canonical = re.sub(r"v\d+$", "", cleaned, flags=re.IGNORECASE)
    else:
        canonical = cleaned

    canonical = canonical.strip()
    if not canonical:
        raise ValueError("Source identifier became empty after normalization")
    return canonical, exact


def _first(item: Mapping[str, Any], names: Sequence[str]) -> Any:
    for name in names:
        value = item.get(name)
        if value not in (None, "", [], {}):
            return value
    return None


def _source_id_value(item: Mapping[str, Any], source: str) -> Any:
    candidates = {
        "arxiv": ("arxiv_id", "source_id"),
        "biorxiv": ("biorxiv_id", "doi", "source_id"),
        "medrxiv": ("medrxiv_id", "doi", "source_id"),
    }.get(source, ("source_id",))
    return _first(item, candidates)


def _string_list(value: Any) -> List[str]:
    if value in (None, "", [], {}):
        return []
    values = value if isinstance(value, (list, tuple, set)) else [value]
    normalized: List[str] = []
    for entry in values:
        if isinstance(entry, Mapping):
            name = _first(entry, ("name", "author_name", "full_name"))
            if name is None:
                continue
            entry = name
        text = str(entry).strip()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _integer(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _year(item: Mapping[str, Any], date: Any) -> Optional[int]:
    for value in (item.get("year"), date, item.get("venue_year")):
        if value is None:
            continue
        match = re.search(r"\b(?:19|20)\d{2}\b", str(value))
        if match:
            return int(match.group(0))
    return None


def empty_relevance() -> Dict[str, Any]:
    return {
        "label": None,
        "scores": {field: None for field in RELEVANCE_SCORE_FIELDS},
        "reason": None,
        "basis": None,
    }


def normalize_provider_item(
    item: Mapping[str, Any],
    source: str,
    query_id: str,
    rank: int,
    retrieved_at: str,
) -> Dict[str, Any]:
    """Normalize one DeepXiv result into a search-run candidate."""

    source = source.casefold()
    canonical_id, exact_id = canonical_source_id(source, _source_id_value(item, source))
    date = _first(item, ("date", "publish_at", "published_at", "publication_date"))
    doi = normalize_doi(_first(item, ("doi", "DOI")))
    title = _first(item, ("title", "paper_title"))

    paper_url = _first(item, ("paper_url", "url", "abs_url"))
    pdf_url = _first(item, ("pdf_url", "src_url"))
    if source == "arxiv":
        paper_url = paper_url or f"https://arxiv.org/abs/{canonical_id}"
        pdf_url = pdf_url or f"https://arxiv.org/pdf/{canonical_id}"

    candidate_id = f"{source}:{canonical_id}"
    alternate_identifiers: Dict[str, List[str]] = {
        "candidate_ids": [candidate_id],
        "returned_source_ids": [exact_id],
    }
    if doi:
        alternate_identifiers["dois"] = [str(doi).strip()]

    return {
        "candidate_id": candidate_id,
        "status": "candidate",
        "source": source,
        "source_id": canonical_id,
        "title": str(title).strip() if title is not None else None,
        "authors": _string_list(item.get("authors")),
        "date": str(date).strip() if date is not None else None,
        "year": _year(item, date),
        "venue": _first(item, ("venue", "journal", "conference")),
        "venue_year": _integer(item.get("venue_year")),
        "abstract": _first(item, ("abstract", "summary")),
        "tldr": item.get("tldr"),
        "categories": _string_list(item.get("categories")),
        "citation_count": _integer(_first(item, ("citation_count", "citation", "citations"))),
        "paper_url": paper_url,
        "pdf_url": pdf_url,
        "doi": str(doi).strip() if doi else None,
        "github_url": _first(item, ("github_url", "code_url", "repository_url")),
        "alternate_identifiers": alternate_identifiers,
        "discovered_by": [
            {
                "query_id": query_id,
                "provider_rank": rank,
                "provider_score": item.get("score"),
                "returned_source_id": exact_id,
                "retrieved_at": retrieved_at,
            }
        ],
        "relevance": empty_relevance(),
        "review_state": "metadata-only",
        "exclusion_reason": None,
        "duplicate_of": None,
        "possible_version_of": [],
        "metadata_conflicts": [],
    }


def normalize_response(
    response: Mapping[str, Any],
    source: str,
    query_id: str,
    retrieved_at: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Normalize a DeepXiv response and return candidates plus skipped rows."""

    records = response.get("result", [])
    if records is None:
        records = []
    if not isinstance(records, list):
        raise ValueError("DeepXiv response field 'result' is not a list")

    candidates: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for rank, item in enumerate(records, start=1):
        if not isinstance(item, Mapping):
            skipped.append({"rank": rank, "reason": "result-is-not-a-mapping"})
            continue
        try:
            candidates.append(normalize_provider_item(item, source, query_id, rank, retrieved_at))
        except ValueError as exc:
            skipped.append({"rank": rank, "reason": str(exc)})
    return candidates, skipped


def _merge_unique(existing: Iterable[Any], incoming: Iterable[Any]) -> List[Any]:
    merged: List[Any] = list(existing)
    for value in incoming:
        if value not in merged:
            merged.append(value)
    return merged


def _record_conflict(candidate: MutableMapping[str, Any], field: str, current: Any, incoming: Any) -> None:
    conflicts = candidate.setdefault("metadata_conflicts", [])
    record = {"field": field, "kept": current, "alternate": incoming}
    if record not in conflicts:
        conflicts.append(record)


def merge_exact_candidate(existing: Dict[str, Any], incoming: Mapping[str, Any]) -> Dict[str, Any]:
    """Merge records sharing an exact canonical candidate ID."""

    if existing.get("candidate_id") != incoming.get("candidate_id"):
        raise ValueError("Cannot exact-merge candidates with different IDs")

    existing["discovered_by"] = _merge_unique(
        existing.get("discovered_by", []),
        incoming.get("discovered_by", []),
    )

    for field in ("authors", "categories", "possible_version_of"):
        existing[field] = _merge_unique(existing.get(field, []), incoming.get(field, []))

    existing_ids = existing.setdefault("alternate_identifiers", {})
    for key, values in incoming.get("alternate_identifiers", {}).items():
        existing_ids[key] = _merge_unique(existing_ids.get(key, []), values)

    for field in (
        "title",
        "date",
        "year",
        "venue",
        "venue_year",
        "abstract",
        "tldr",
        "citation_count",
        "paper_url",
        "pdf_url",
        "doi",
        "github_url",
    ):
        current = existing.get(field)
        alternate = incoming.get(field)
        if current in (None, "", [], {}):
            existing[field] = alternate
        elif alternate not in (None, "", [], {}) and current != alternate:
            _record_conflict(existing, field, current, alternate)

    return existing


def merge_candidates(
    existing_candidates: Sequence[Mapping[str, Any]],
    new_candidates: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Merge exact stable-ID duplicates while retaining first-seen ordering."""

    ordered: List[Dict[str, Any]] = [dict(candidate) for candidate in existing_candidates]
    by_id: Dict[str, Dict[str, Any]] = {
        str(candidate.get("candidate_id")): candidate
        for candidate in ordered
        if candidate.get("candidate_id")
    }
    by_doi: Dict[str, Dict[str, Any]] = {
        str(candidate.get("doi")).casefold(): candidate
        for candidate in ordered
        if candidate.get("doi")
    }

    for raw_candidate in new_candidates:
        candidate = dict(raw_candidate)
        candidate_id = candidate.get("candidate_id")
        if not candidate_id:
            continue
        if candidate_id in by_id:
            merge_exact_candidate(by_id[candidate_id], candidate)
        elif candidate.get("doi") and str(candidate.get("doi")).casefold() in by_doi:
            target = by_doi[str(candidate.get("doi")).casefold()]
            original_candidate_id = str(candidate_id)
            candidate.setdefault("alternate_identifiers", {}).setdefault(
                "candidate_ids", []
            ).append(original_candidate_id)
            candidate["candidate_id"] = target["candidate_id"]
            merge_exact_candidate(target, candidate)
            by_id[original_candidate_id] = target
        else:
            ordered.append(candidate)
            by_id[candidate_id] = candidate
            if candidate.get("doi"):
                by_doi[str(candidate.get("doi")).casefold()] = candidate

    return ordered


def link_possible_versions(candidates: Sequence[MutableMapping[str, Any]]) -> None:
    """Link, but never merge, exact-title/year records with different IDs."""

    groups: Dict[Tuple[str, int], List[MutableMapping[str, Any]]] = {}
    for candidate in candidates:
        title_key = normalize_title(candidate.get("title"))
        year = candidate.get("year")
        if len(title_key) < 15 or not isinstance(year, int):
            continue
        groups.setdefault((title_key, year), []).append(candidate)

    for group in groups.values():
        if len(group) < 2:
            continue
        ids = [candidate.get("candidate_id") for candidate in group if candidate.get("candidate_id")]
        for candidate in group:
            current_id = candidate.get("candidate_id")
            related = [candidate_id for candidate_id in ids if candidate_id != current_id]
            candidate["possible_version_of"] = _merge_unique(
                candidate.get("possible_version_of", []),
                related,
            )


def recompute_metrics(run: MutableMapping[str, Any]) -> Dict[str, Any]:
    """Compute deterministic search-process metrics and update the run."""

    queries = run.get("queries", []) or []
    candidates = run.get("candidates", []) or []
    executed_queries = 0
    raw_hits = 0
    query_rounds: Dict[str, int] = {}

    for query in queries:
        query_id = query.get("id")
        query_rounds[str(query_id)] = int(query.get("round") or 1)
        execution = query.get("execution", {}) or {}
        if execution.get("status") in EXECUTED_QUERY_STATUSES:
            executed_queries += 1
        count = execution.get("retrieved_count")
        if isinstance(count, int) and count >= 0:
            raw_hits += count

    relevance_counts = {label: 0 for label in RELEVANCE_LABELS}
    relevance_counts["untriaged"] = 0
    missing_metadata_count = 0
    core_by_round: Dict[int, int] = {}

    for candidate in candidates:
        label = (candidate.get("relevance") or {}).get("label")
        if label in RELEVANCE_LABELS:
            relevance_counts[label] += 1
        else:
            relevance_counts["untriaged"] += 1

        if not candidate.get("title") or not candidate.get("authors") or not candidate.get("year"):
            missing_metadata_count += 1

        if label == "core":
            discovery_rounds = [
                query_rounds.get(str(item.get("query_id")), 1)
                for item in candidate.get("discovered_by", [])
            ]
            first_round = min(discovery_rounds) if discovery_rounds else 1
            core_by_round[first_round] = core_by_round.get(first_round, 0) + 1

    unique_count = len(candidates)
    duplicates = max(raw_hits - unique_count, 0)
    duplicate_rate = round(duplicates / raw_hits, 6) if raw_hits else 0.0

    metrics = {
        "executed_queries": executed_queries,
        "raw_retrieved_hits": raw_hits,
        "unique_candidates": unique_count,
        "duplicate_rate": duplicate_rate,
        "relevance_counts": relevance_counts,
        "missing_metadata_count": missing_metadata_count,
        "new_core_by_round": [
            {"round": number, "count": core_by_round[number]}
            for number in sorted(core_by_round)
        ],
    }
    coverage = run.setdefault("coverage", {})
    coverage["metrics"] = metrics
    return metrics


def sanitize_message(message: Any, secrets: Optional[Sequence[str]] = None) -> str:
    """Redact known secrets and common credential representations."""

    text = str(message)
    for secret in secrets or ():
        if secret:
            text = text.replace(secret, "<redacted>")

    for pattern in (
        r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+",
        r"(?i)((?:api[_-]?key|token)\s*[=:]\s*)[^&\s,;]+",
        r"(?i)([?&](?:api[_-]?key|token)=)[^&\s]+",
    ):
        text = re.sub(pattern, r"\1<redacted>", text)
    return text


def next_error_id(errors: Sequence[Mapping[str, Any]]) -> str:
    highest = 0
    for error in errors:
        match = re.fullmatch(r"E(\d+)", str(error.get("id", "")))
        if match:
            highest = max(highest, int(match.group(1)))
    return f"E{highest + 1:03d}"


def append_error(
    run: MutableMapping[str, Any],
    *,
    phase: str,
    error_class: str,
    message: Any,
    effect: str,
    recovery: str,
    recurrence_key: str,
    query_id: Optional[str] = None,
    secrets: Optional[Sequence[str]] = None,
) -> str:
    errors = run.setdefault("errors", [])
    error_id = next_error_id(errors)
    provider = (run.get("run", {}).get("provider", {}) or {})
    errors.append(
        {
            "id": error_id,
            "timestamp": utc_now(),
            "phase": phase,
            "provider": provider.get("name"),
            "package_version": provider.get("package_version"),
            "query_id": query_id,
            "error_class": error_class,
            "message": sanitize_message(message, secrets=secrets),
            "effect": effect,
            "recovery": recovery,
            "recurrence_key": recurrence_key,
        }
    )
    return error_id


def query_signature(query: Mapping[str, Any], default_source: str = "arxiv") -> str:
    """Create a deterministic signature for duplicate-query detection."""

    payload = {
        "text": " ".join(str(query.get("text") or "").casefold().split()),
        "source": str((query.get("filters") or {}).get("source") or default_source).casefold(),
        "filters": query.get("filters") or {},
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
