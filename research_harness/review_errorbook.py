"""Cross-run recurrence aggregation for the review Error Book.

Run-local failures remain under ``.harness/review-runs/<run>/state``.  This
module promotes only recurrence keys observed in at least two distinct runs to
the project-level generated Error Book.  It never edits Skills automatically.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Mapping, Optional

import yaml  # type: ignore[import-untyped]

from .config import HarnessSettings
from .review_models import ErrorBookSummary, ReviewErrorEvent


def _atomic_yaml(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = yaml.safe_dump(
        payload,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
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


def _events(path: Path) -> tuple[ReviewErrorEvent, ...]:
    if not path.is_file():
        return ()
    result = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            result.append(ReviewErrorEvent.model_validate(json.loads(line)))
        except Exception as exc:
            raise ValueError(
                f"Invalid review error event in {path} line {line_number}: {exc}"
            ) from exc
    return tuple(result)


def _proposal(key: str, events: Iterable[ReviewErrorEvent]) -> str:
    stages = {item.stage for item in events}
    if "retrieval" in stages:
        return (
            "Review provider isolation/retry policy and add a deterministic fixture "
            "for this recurrence; update search-paper only after human review."
        )
    if "screening" in stages or "skim" in stages:
        return (
            "Tighten the structured-output validator and the source-skim/project-audit "
            "instructions; add the recurring case as an offline fixture."
        )
    if "deep-read" in stages:
        return (
            "Inspect source acquisition and EvidenceCard validation, then consider a "
            "targeted evidence-extract or deterministic parser change."
        )
    if "reasoning" in stages or "assessment" in stages:
        return (
            "Audit independent-source/comparability checks and review-synthesize "
            "instructions; do not relax the evidence boundary automatically."
        )
    return (
        "Inspect the synthesis contract and renderer, add an offline regression test, "
        "and propose a reviewed Skill or script change."
    )


def aggregate_review_error_book(
    settings: HarnessSettings,
    *,
    research_id: Optional[str] = None,
) -> tuple[ErrorBookSummary, ...]:
    """Rebuild the generated cross-run summary from sanitized run events."""

    root = settings.repository_root / ".harness" / "review-runs"
    grouped: dict[str, list[ReviewErrorEvent]] = defaultdict(list)
    if root.is_dir():
        for run_root in sorted(item for item in root.iterdir() if item.is_dir()):
            for event in _events(run_root / "state" / "errors.jsonl"):
                if event.run_id != run_root.name:
                    raise ValueError(
                        f"review error event run_id does not match {run_root.name}"
                    )
                if research_id is None or event.research_id == research_id:
                    grouped[event.recurrence_key].append(event)
    summaries = []
    for key, events in sorted(grouped.items()):
        run_ids = tuple(sorted({item.run_id for item in events}))
        if len(run_ids) < 2:
            continue
        examples = tuple(dict.fromkeys(item.observed[:500] for item in events))[:3]
        summaries.append(
            ErrorBookSummary(
                recurrence_key=key,
                distinct_run_ids=run_ids,
                occurrence_count=len(events),
                affected_stages=tuple(sorted({item.stage for item in events})),
                observed_examples=examples,
                proposed_change=_proposal(key, events),
            )
        )
    destination = (
        settings.repository_root
        / "error_book"
        / "_generated"
        / "review-recurrences.yaml"
    )
    _atomic_yaml(
        destination,
        {
            "schema_version": "0.1",
            "policy": {
                "minimum_distinct_runs": 2,
                "automatic_skill_modification": False,
            },
            "research_id": research_id,
            "recurrences": [item.model_dump(mode="json") for item in summaries],
        },
    )
    return tuple(summaries)


__all__ = ["aggregate_review_error_book"]
