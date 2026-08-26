#!/usr/bin/env python
"""Create a search-run YAML record from the Skill template."""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from search_common import load_yaml, package_version, utc_now, write_yaml_atomic


SKILL_DIR = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
TEMPLATE_PATH = SKILL_DIR / "assets" / "search-run-template.yaml"


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Initialize a traceable paper-search run without network access."
    )
    parser.add_argument("--topic-slug", required=True, help="Lowercase filesystem slug.")
    parser.add_argument("--question", required=True, help="Primary research question.")
    parser.add_argument(
        "--query",
        action="append",
        default=[],
        help="Optional first-pass query. Repeat to add several queries.",
    )
    parser.add_argument(
        "--source",
        choices=("arxiv", "biorxiv", "medrxiv"),
        default="arxiv",
        help="DeepXiv retrieval source.",
    )
    parser.add_argument("--max-queries", type=int, default=8)
    parser.add_argument("--max-rounds", type=int, default=3)
    parser.add_argument(
        "--output",
        type=Path,
        help="Output path relative to the repository root. A safe default is generated.",
    )
    return parser.parse_args(argv)


def validate_slug(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value):
        raise ValueError(
            "topic-slug must contain lowercase ASCII letters, digits, and single hyphens"
        )
    return value


def ensure_within_repository(path: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(REPOSITORY_ROOT)
    except ValueError as exc:
        raise ValueError(f"Output must stay inside repository root: {REPOSITORY_ROOT}") from exc
    return resolved


def query_record(query_id: str, text: str, source: str) -> Dict[str, Any]:
    return {
        "id": query_id,
        "round": 1,
        "family": "unclassified",
        "text": text,
        "purpose": None,
        "target_facets": [],
        "derived_from": None,
        "filters": {
            "source": source,
            "categories": [],
            "authors": [],
            "orgs": [],
            "venues": [],
            "venue_year": None,
            "min_citations": None,
            "date_search_type": None,
            "date_str": None,
            "date_from": None,
            "date_to": None,
            "use_fine_rerank": False,
            "size": 20,
            "offset": 0,
        },
        "execution": {
            "status": "planned",
            "executed_at": None,
            "provider_total_count": None,
            "retrieved_count": None,
            "retained_count": None,
            "raw_result_path": None,
            "error_id": None,
        },
    }


def create_run(args: argparse.Namespace) -> Path:
    topic_slug = validate_slug(args.topic_slug)
    question = args.question.strip()
    if not question:
        raise ValueError("question cannot be empty")
    if not 1 <= args.max_queries <= 100:
        raise ValueError("max-queries must be between 1 and 100")
    if not 1 <= args.max_rounds <= 20:
        raise ValueError("max-rounds must be between 1 and 20")
    if len(args.query) > args.max_queries:
        raise ValueError("initial query count exceeds max-queries")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{topic_slug}-{timestamp}"
    if args.output:
        raw_output = args.output
        if not raw_output.is_absolute():
            raw_output = REPOSITORY_ROOT / raw_output
    else:
        raw_output = (
            REPOSITORY_ROOT
            / "research"
            / topic_slug
            / "search-runs"
            / f"{run_id}.yaml"
        )
    output_path = ensure_within_repository(raw_output)
    if output_path.exists():
        raise FileExistsError(f"Refusing to overwrite existing run: {output_path}")

    run = load_yaml(TEMPLATE_PATH)
    now = utc_now()
    run["run"].update(
        {
            "id": run_id,
            "topic_slug": topic_slug,
            "question": question,
            "created_at": now,
            "updated_at": now,
            "status": "planned",
            "round": 1,
        }
    )
    run["run"]["provider"].update(
        {
            "name": "deepxiv",
            "interface": "deepxiv-sdk",
            "package_version": package_version("deepxiv-sdk"),
            "source": args.source,
        }
    )
    run["run"]["budget"].update(
        {
            "max_queries": args.max_queries,
            "max_rounds": args.max_rounds,
        }
    )
    run["scope"]["sources"] = [args.source]
    run["queries"] = [
        query_record(f"Q{index:02d}", text.strip(), args.source)
        for index, text in enumerate(args.query, start=1)
        if text.strip()
    ]

    write_yaml_atomic(output_path, run)
    return output_path


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        output_path = create_run(args)
    except (ValueError, FileExistsError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
