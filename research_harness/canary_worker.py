"""Subprocess entry point used to enforce a Canary wall-clock deadline."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from .canary import run_canary
from .canary_models import CanaryLimits
from .config import HarnessSettings


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Internal bounded Canary worker.")
    parser.add_argument("--request", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    request = json.loads(args.request.read_text(encoding="utf-8-sig"))
    settings = HarnessSettings.from_env(
        database_path=request.get("base_database_path"),
        model=request.get("model"),
        workspace_id=request.get("workspace_id"),
    )
    report = run_canary(
        settings,
        research_id=str(request["research_id"]),
        run_id=str(request["run_id"]),
        limits=CanaryLimits.model_validate(request["limits"]),
        source_run=(
            Path(str(request["source_run"])) if request.get("source_run") else None
        ),
    )
    print(json.dumps(report.model_dump(mode="json"), ensure_ascii=False))
    return 0 if report.status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
