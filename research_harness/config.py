"""Configuration and storage-path safety for the research harness."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional, Union
from urllib.parse import urlparse


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = REPOSITORY_ROOT / ".harness" / "research-harness.sqlite3"


def _model_base_url_from_env() -> Optional[str]:
    configured = os.getenv("HARNESS_MODEL_BASE_URL", "").strip()
    if configured:
        return configured
    langchain_value = os.getenv("OPENAI_API_BASE", "").strip()
    standard_value = os.getenv("OPENAI_BASE_URL", "").strip()
    if (
        langchain_value
        and standard_value
        and langchain_value.rstrip("/") != standard_value.rstrip("/")
    ):
        raise ValueError(
            "OPENAI_API_BASE and OPENAI_BASE_URL must match when both are set"
        )
    return langchain_value or standard_value or None


def _validate_model_base_url(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise ValueError(
            "HARNESS_MODEL_BASE_URL must be an absolute http(s) "
            "OpenAI-compatible endpoint"
        )
    if parsed.username or parsed.password:
        raise ValueError(
            "HARNESS_MODEL_BASE_URL must not contain credentials; "
            "use OPENAI_API_KEY"
        )
    if parsed.query or parsed.fragment:
        raise ValueError(
            "HARNESS_MODEL_BASE_URL must not contain a query string or fragment"
        )


def _positive_int(value: str, name: str, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def resolve_database_path(value: Union[str, Path]) -> Path:
    """Resolve a persistent SQLite file on any writable drive."""

    if str(value).strip() == ":memory:":
        raise ValueError("Harness persistence must use a file, not :memory:")
    raw_path = Path(value).expanduser()
    if not raw_path.is_absolute():
        raw_path = REPOSITORY_ROOT / raw_path
    resolved = raw_path.resolve()
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
    model_base_url: Optional[str] = None
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
        model_base_url: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> "HarnessSettings":
        raw_database = database_path or os.getenv("HARNESS_DB_PATH") or DEFAULT_DB_PATH
        raw_context_tokens = os.getenv("HARNESS_CONTEXT_TOKENS", "6000")
        raw_max_iterations = os.getenv("HARNESS_MAX_TOOL_ITERATIONS", "6")
        raw_tool_chars = os.getenv("HARNESS_TOOL_OUTPUT_CHARS", "12000")
        settings = cls(
            database_path=resolve_database_path(raw_database),
            model=model or os.getenv("HARNESS_MODEL") or None,
            model_base_url=(
                model_base_url.strip()
                if model_base_url and model_base_url.strip()
                else _model_base_url_from_env()
            ),
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
        if self.model_base_url:
            _validate_model_base_url(self.model_base_url)
        if self.model:
            if not self.model.strip().startswith("openai:"):
                raise ValueError(
                    "HARNESS_MODEL must use openai:<served-model-name> for an "
                    "OpenAI-compatible endpoint"
                )
            if not self.openai_model_name:
                raise ValueError("HARNESS_MODEL must include a served model name")
            if not self.model_base_url:
                raise ValueError(
                    "HARNESS_MODEL_BASE_URL, OPENAI_API_BASE, or OPENAI_BASE_URL "
                    "is required when HARNESS_MODEL is configured"
                )
        if not self.wiki_root.is_dir():
            raise FileNotFoundError(f"Wiki root does not exist: {self.wiki_root}")
        if not self.wiki_meta_root.is_dir():
            raise FileNotFoundError(f"Wiki metadata root does not exist: {self.wiki_meta_root}")
        if not self.skills_root.is_dir():
            raise FileNotFoundError(f"Skills root does not exist: {self.skills_root}")
        if not self.research_root.is_dir():
            raise FileNotFoundError(f"Research root does not exist: {self.research_root}")

    def with_model(
        self,
        model: str,
        *,
        model_base_url: Optional[str] = None,
    ) -> "HarnessSettings":
        updated = replace(
            self,
            model=model,
            model_base_url=model_base_url or self.model_base_url,
        )
        updated.validate()
        return updated

    @property
    def openai_model_name(self) -> Optional[str]:
        if not self.model or ":" not in self.model:
            return None
        return self.model.split(":", 1)[1].strip() or None

    @property
    def model_endpoint_host(self) -> Optional[str]:
        if not self.model_base_url:
            return None
        return urlparse(self.model_base_url).hostname

    @property
    def normalized_model_base_url(self) -> Optional[str]:
        if not self.model_base_url:
            return None
        return self.model_base_url.rstrip("/")

    @property
    def model_runtime_fingerprint(self) -> str:
        """Identify the non-secret model runtime used by resumable graphs."""

        payload = json.dumps(
            {
                "adapter": "langchain-openai",
                "model": self.openai_model_name,
                "base_url": self.normalized_model_base_url,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def ensure_storage_directory(self) -> Path:
        path = resolve_database_path(self.database_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
