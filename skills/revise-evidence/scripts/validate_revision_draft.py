"""Validate a structured evidence revision JSON draft without writing Wiki pages."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from research_harness.evidence_revision import EvidenceRevisionDraft  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("draft", type=Path)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = json.loads(args.draft.read_text(encoding="utf-8-sig"))
        validated = EvidenceRevisionDraft.model_validate(payload)
    except Exception as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(result["error"], file=sys.stderr)
        return 1
    result = {
        "ok": True,
        "entity_id": validated.entity_id,
        "reason_code": validated.reason_code,
        "updated_fields": sorted(
            validated.updates.model_dump(exclude_none=True)
        ),
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"VALID {result['entity_id']} {result['reason_code']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
