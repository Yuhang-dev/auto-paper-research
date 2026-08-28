"""Configuration and storage-path safety for the research harness."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional, Union


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = REPOSITORY_ROOT / ".harness" / "research-harness.sqlite3"


def _positive_int(value: str, name: str, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def resolve_database_path(value: Union[str, Path]) -> Path:
    """Resolve a persistent SQLite file and reject storage on Windows C:."""

    if str(value).strip() == ":memory:":
        raise ValueError("Harness persistence must use a file, not :memory:")
    raw_path = Path(value).expanduser()
    if not raw_path.is_absolute():
        raw_path = REPOSITORY_ROOT / raw_path
    resolved = raw_path.resolve()
    if resolved.drive.casefold() == "c:":
        raise ValueError(
            "HARNESS_DB_PATH cannot be on C:. Use the project D: drive or another data drive."
        )
    if resolved.suffix.casefold() not in {".db", ".sqlite", ".sqlite3"}:
        raise ValueError("HARNESS_DB_PATH must end in .db, .sqlite, or .sqlite3")
    return resolved


@dataclass(frozen=True)
class HarnessSettings:
    """Static process configuration; secrets remain in environment variables."""

    repository_root: Path = REPOSITORY_ROOT
    wiki_root: Path = REPOSITORY_ROOT / "wiki"
    wiki_meta_root: Path = REPOSITORY_ROOT / "wiki" / "_meta"
    skills_root: Path = REPOSITORY_ROOT / "skills"
    research_root: Path = REPOSITORY_ROOT / "research"
    database_path: Path = DEFAULT_DB_PATH
    model: Optional[str] = None
    workspace_id: str = "long-context-sparse-models"
    context_token_budget: int = 6000
    max_tool_iterations: int = 6
    tool_output_chars: int = 12000

    @classmethod
    def from_env(
        cls,
        *,
        database_path: Optional[Union[str, Path]] = None,
        model: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> "HarnessSettings":
        raw_database = database_path or os.getenv("HARNESS_DB_PATH") or DEFAULT_DB_PATH
        raw_context_tokens = os.getenv("HARNESS_CONTEXT_TOKENS", "6000")
        raw_max_iterations = os.getenv("HARNESS_MAX_TOOL_ITERATIONS", "6")
        raw_tool_chars = os.getenv("HARNESS_TOOL_OUTPUT_CHARS", "12000")
        settings = cls(
            database_path=resolve_database_path(raw_database),
            model=model or os.getenv("HARNESS_MODEL") or None,
            workspace_id=(
                workspace_id
                or os.getenv("HARNESS_WORKSPACE_ID")
                or "long-context-sparse-models"
            ),
            context_token_budget=_positive_int(
                raw_context_tokens,
                "HARNESS_CONTEXT_TOKENS",
                minimum=512,
                maximum=200000,
            ),
            max_tool_iterations=_positive_int(
                raw_max_iterations,
                "HARNESS_MAX_TOOL_ITERATIONS",
                minimum=1,
                maximum=30,
            ),
            tool_output_chars=_positive_int(
                raw_tool_chars,
                "HARNESS_TOOL_OUTPUT_CHARS",
                minimum=1000,
                maximum=100000,
            ),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        database_path = resolve_database_path(self.database_path)
        if database_path != self.database_path.resolve():
            raise ValueError("database_path must be normalized")
        if not self.workspace_id.strip():
            raise ValueError("workspace_id cannot be empty")
        if len(self.workspace_id) > 120:
            raise ValueError("workspace_id cannot exceed 120 characters")
        if not 512 <= self.context_token_budget <= 200000:
            raise ValueError("context_token_budget must be between 512 and 200000")
        if not 1 <= self.max_tool_iterations <= 30:
            raise ValueError("max_tool_iterations must be between 1 and 30")
        if not 1000 <= self.tool_output_chars <= 100000:
            raise ValueError("tool_output_chars must be between 1000 and 100000")
        if self.model and self.model.strip().casefold() not in {
            "deepseek-v4-flash",
            "openai:deepseek-v4-flash",
        }:
            raise ValueError(
                "This project permits only the DeepSeek deepseek-v4-flash model"
            )
        if not self.wiki_root.is_dir():
            raise FileNotFoundError(f"Wiki root does not exist: {self.wiki_root}")
        if not self.wiki_meta_root.is_dir():
            raise FileNotFoundError(f"Wiki metadata root does not exist: {self.wiki_meta_root}")
        if not self.skills_root.is_dir():
            raise FileNotFoundError(f"Skills root does not exist: {self.skills_root}")
        if not self.research_root.is_dir():
            raise FileNotFoundError(f"Research root does not exist: {self.research_root}")

    def with_model(self, model: str) -> "HarnessSettings":
        return replace(self, model=model)

    def ensure_storage_directory(self) -> Path:
        path = resolve_database_path(self.database_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
