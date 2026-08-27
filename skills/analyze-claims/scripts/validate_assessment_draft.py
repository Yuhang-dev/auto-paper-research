"""Validate a structured non-consensus assessment JSON draft."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from research_harness.nonconsensus_analysis import (  # noqa: E402
    NonConsensusAssessmentDraft,
)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("draft", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        payload = json.loads(args.draft.read_text(encoding="utf-8-sig"))
        draft = NonConsensusAssessmentDraft.model_validate(payload)
    except Exception as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(result["error"], file=sys.stderr)
        return 1
    result = {
        "ok": True,
        "result": draft.result,
        "claims": len(draft.claim_ids),
        "experiments": len(draft.evidence_ids),
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            f"VALID {draft.result}: claims={len(draft.claim_ids)}, "
            f"experiments={len(draft.evidence_ids)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
