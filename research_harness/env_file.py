"""Load local Harness configuration into the process environment."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional, Tuple


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = REPOSITORY_ROOT / ".env.local"
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_local_env(path: Optional[Path] = None) -> Tuple[str, ...]:
    """Load KEY=VALUE entries while preserving variables set by the shell."""

    env_path = path or DEFAULT_ENV_FILE
    if not env_path.is_file():
        return ()

    loaded = []
    for line_number, raw_line in enumerate(
        env_path.read_text(encoding="utf-8-sig").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"Invalid environment entry at {env_path}:{line_number}")
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not _ENV_NAME.fullmatch(name):
            raise ValueError(
                f"Invalid environment variable name at {env_path}:{line_number}"
            )
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if name in os.environ:
            continue
        os.environ[name] = value
        loaded.append(name)
    return tuple(loaded)


__all__ = ["DEFAULT_ENV_FILE", "load_local_env"]
