"""Deterministic text normalization for external research material."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


_SURROGATES = re.compile(r"[\ud800-\udfff]")


def normalize_text(value: str) -> str:
    """Replace isolated UTF-16 surrogate code units with U+FFFD."""

    return _SURROGATES.sub("\ufffd", value)


def normalize_data(value: Any) -> Any:
    """Apply UTF-8-safe text normalization recursively to JSON-like data."""

    if isinstance(value, str):
        return normalize_text(value)
    if isinstance(value, Mapping):
        return {
            normalize_text(str(key)): normalize_data(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [normalize_data(item) for item in value]
    return value


__all__ = ["normalize_data", "normalize_text"]
