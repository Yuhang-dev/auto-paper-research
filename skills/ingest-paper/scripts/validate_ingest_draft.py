"""Validate a PaperIngestDraft JSON file without calling a model or mutating Wiki."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from research_harness.ingest_models import PaperIngestDraft  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("draft", type=Path, help="Path to one UTF-8 JSON draft")
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        dest="output_format",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = json.loads(args.draft.read_text(encoding="utf-8-sig"))
        draft = PaperIngestDraft.model_validate(payload)
    except Exception as exc:
        error_result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if args.output_format == "json":
            print(json.dumps(error_result, ensure_ascii=False, indent=2))
        else:
            print(error_result["error"], file=sys.stderr)
        return 1

    counts: Dict[str, int] = {
        "methods": len(draft.methods),
        "benchmarks": len(draft.benchmarks),
        "models": len(draft.models),
        "claims": len(draft.claims),
        "experiments": len(draft.experiments),
    }
    result: Dict[str, Any] = {
        "ok": True,
        "candidate_id": draft.candidate_id,
        "counts": counts,
    }
    if args.output_format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            f"VALID {draft.candidate_id}: "
            + ", ".join(f"{key}={value}" for key, value in counts.items())
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
