"""Derived trajectory exports from LangGraph's SQLite checkpoint truth."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Optional

import yaml  # type: ignore[import-untyped]


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "model_dump"):
        return _json_safe(value.model_dump(mode="json"))
    if hasattr(value, "isoformat") and callable(value.isoformat):
        return value.isoformat()
    return value


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-.")
    if not slug:
        raise ValueError("Trajectory thread must contain a safe filename character")
    return slug[:120]


def _atomic_lines(path: Path, records: Iterable[Mapping[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Optional[Path] = None
    count = 0
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
            for record in records:
                handle.write(
                    json.dumps(_json_safe(record), ensure_ascii=False, sort_keys=True)
                )
                handle.write("\n")
                count += 1
            temporary = Path(handle.name)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return count


def export_checkpoint_trajectory(
    history: Iterable[Any],
    *,
    destination: Path,
    research_id: str,
    thread_id: str,
) -> int:
    """Export chronological records; never acts as runtime control state."""

    snapshots = list(history)
    snapshots.reverse()
    records = []
    for sequence, snapshot in enumerate(snapshots, start=1):
        configurable = (snapshot.config or {}).get("configurable", {})
        records.append(
            {
                "schema_version": "0.1",
                "research_id": research_id,
                "thread_id": thread_id,
                "sequence": sequence,
                "checkpoint_id": configurable.get("checkpoint_id"),
                "created_at": snapshot.created_at,
                "next": list(snapshot.next),
                "metadata": snapshot.metadata,
                "values": snapshot.values,
            }
        )
    return _atomic_lines(destination, records)


def ensure_annotation_sidecar(path: Path, *, research_id: str, thread_id: str) -> None:
    if path.exists():
        return
    payload = {
        "schema_version": "0.1",
        "research_id": research_id,
        "thread_id": thread_id,
        "annotations": [],
        "field_contract": {
            "identity": {
                "target_type": (
                    "control-action | candidate | ingest-entity | verification"
                ),
                "target_id": "Action, candidate, entity, or semantic artifact ID.",
                "source_sha256": "SHA-256 of the exact reviewed source or artifact.",
                "reviewer": "Human reviewer identifier.",
                "reviewed_at": "ISO-8601 review timestamp.",
                "note": "Adjudication rationale.",
            },
            "control-action": {
                "action_correct": "yes | acceptable | no",
                "preferred_action": "Preferred ResearchAction when action_correct=no.",
            },
            "candidate": {
                "relevance_correct": "true | false",
                "expected_label": "core | adjacent | background | exclude",
            },
            "ingest-entity": {
                "extraction_correct": "true | false",
                "locator_correct": "true | false",
            },
            "verification": {
                "verdict_correct": "true | false",
                "promotion_correct": "true | false",
            },
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
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
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def annotation_freshness(
    annotation: Mapping[str, Any],
    *,
    current_source_sha256: str,
) -> Literal["current", "stale", "invalid"]:
    """Compare a sidecar annotation with the immutable source under review."""

    recorded = str(annotation.get("source_sha256") or "").strip().casefold()
    current = current_source_sha256.strip().casefold()
    sha256_pattern = re.compile(r"^[0-9a-f]{64}$")
    if not sha256_pattern.fullmatch(recorded) or not sha256_pattern.fullmatch(current):
        return "invalid"
    return "current" if recorded == current else "stale"


def trajectory_directory(
    research_root: Path,
    research_id: str,
    thread_id: str,
) -> Path:
    destination = (
        research_root.resolve() / research_id / "trajectories" / _safe_slug(thread_id)
    ).resolve()
    try:
        destination.relative_to(research_root.resolve())
    except ValueError as exc:
        raise ValueError("Trajectory destination escapes the research root") from exc
    return destination


__all__ = [
    "annotation_freshness",
    "ensure_annotation_sidecar",
    "export_checkpoint_trajectory",
    "trajectory_directory",
]
