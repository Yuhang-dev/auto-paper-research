"""Read-only research Wiki engine."""

from .indexer import build_index
from .validator import validate_index

__all__ = ["build_index", "validate_index"]
