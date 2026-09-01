"""Explicit OpenAI-compatible chat-model construction."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Optional

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from .config import HarnessSettings


def _runtime_int(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _model_runtime(role: Optional[str] = None) -> tuple[int, int]:
    role_name = str(role or "").strip().upper()
    default_timeout = 180 if role_name == "FAST" else 300
    timeout_name = (
        f"HARNESS_{role_name}_MODEL_TIMEOUT_SECONDS"
        if role_name
        else "HARNESS_MODEL_TIMEOUT_SECONDS"
    )
    retries_name = (
        f"HARNESS_{role_name}_MODEL_MAX_RETRIES"
        if role_name
        else "HARNESS_MODEL_MAX_RETRIES"
    )
    timeout_seconds = _runtime_int(
        timeout_name,
        _runtime_int(
            "HARNESS_MODEL_TIMEOUT_SECONDS",
            default_timeout,
            minimum=30,
            maximum=1800,
        ),
        minimum=30,
        maximum=1800,
    )
    max_retries = _runtime_int(
        retries_name,
        _runtime_int(
            "HARNESS_MODEL_MAX_RETRIES",
            2,
            minimum=0,
            maximum=6,
        ),
        minimum=0,
        maximum=6,
    )
    return timeout_seconds, max_retries


@dataclass(frozen=True)
class ModelProfile:
    """One OpenAI-compatible endpoint without serializing its credential."""

    role: str
    model: str
    base_url: str
    api_key: str

    @property
    def served_model_name(self) -> str:
        return self.model.split(":", 1)[1].strip()

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "adapter": "langchain-openai",
                "role": self.role,
                "model": self.served_model_name,
                "base_url": self.base_url.rstrip("/"),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ReviewModelBundle:
    fast: ModelProfile
    reasoning: ModelProfile
    single_model_fallback: bool = False

    @property
    def fingerprint(self) -> str:
        payload = f"{self.fast.fingerprint}:{self.reasoning.fingerprint}"
        return hashlib.sha256(payload.encode("ascii")).hexdigest()

    @classmethod
    def from_env(
        cls,
        settings: HarnessSettings,
        *,
        allow_single_model_fallback: bool,
        require_reasoning: bool,
    ) -> "ReviewModelBundle":
        fast_model = os.getenv("HARNESS_FAST_MODEL", "").strip() or settings.model
        fast_base = (
            os.getenv("HARNESS_FAST_MODEL_BASE_URL", "").strip()
            or settings.model_base_url
        )
        fast_key = (
            os.getenv("HARNESS_FAST_API_KEY", "").strip()
            or os.getenv("OPENAI_API_KEY", "").strip()
        )
        if not fast_model or not fast_base or not fast_key:
            raise ValueError(
                "Review fast-model configuration requires HARNESS_FAST_MODEL, "
                "HARNESS_FAST_MODEL_BASE_URL, and HARNESS_FAST_API_KEY; legacy "
                "HARNESS_MODEL/HARNESS_MODEL_BASE_URL/OPENAI_API_KEY are accepted"
            )
        _validate_profile(settings, fast_model, fast_base)
        fast = ModelProfile("fast", fast_model, fast_base, fast_key)

        reasoning_model = os.getenv("HARNESS_REASONING_MODEL", "").strip()
        reasoning_base = os.getenv("HARNESS_REASONING_MODEL_BASE_URL", "").strip()
        reasoning_key = os.getenv("HARNESS_REASONING_API_KEY", "").strip()
        reasoning_complete = bool(reasoning_model and reasoning_base and reasoning_key)
        reasoning_any = bool(reasoning_model or reasoning_base or reasoning_key)
        if reasoning_any and not reasoning_complete:
            raise ValueError(
                "HARNESS_REASONING_MODEL, HARNESS_REASONING_MODEL_BASE_URL, and "
                "HARNESS_REASONING_API_KEY must be configured together"
            )
        if not reasoning_complete:
            if require_reasoning and not allow_single_model_fallback:
                raise ValueError(
                    "This review profile requires a reasoning-model profile. "
                    "Configure HARNESS_REASONING_* or explicitly use "
                    "--allow-single-model-fallback"
                )
            reasoning = ModelProfile(
                "reasoning", fast.model, fast.base_url, fast.api_key
            )
            return cls(fast=fast, reasoning=reasoning, single_model_fallback=True)
        assert reasoning_model and reasoning_base and reasoning_key
        _validate_profile(settings, reasoning_model, reasoning_base)
        return cls(
            fast=fast,
            reasoning=ModelProfile(
                "reasoning", reasoning_model, reasoning_base, reasoning_key
            ),
        )


def _validate_profile(
    settings: HarnessSettings,
    model: str,
    base_url: str,
) -> None:
    settings.with_model(model, model_base_url=base_url)


def create_chat_model(settings: HarnessSettings) -> BaseChatModel:
    """Create one ChatOpenAI client without relying on provider inference."""

    settings.validate()
    model_name = settings.openai_model_name
    if model_name is None or settings.model_base_url is None:
        raise ValueError(
            "Configure HARNESS_MODEL=openai:<served-model-name> and "
            "HARNESS_MODEL_BASE_URL before creating a model client"
        )
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key.strip():
        raise ValueError(
            "OPENAI_API_KEY is required by the OpenAI-compatible model client"
        )
    timeout_seconds, max_retries = _model_runtime()
    return ChatOpenAI(
        model=model_name,
        base_url=settings.model_base_url,
        api_key=api_key,
        max_retries=max_retries,
        timeout=timeout_seconds,
    )


def create_profile_chat_model(profile: ModelProfile) -> BaseChatModel:
    """Create a deterministic client for one review task profile."""

    timeout_seconds, max_retries = _model_runtime(profile.role)
    return ChatOpenAI(
        model=profile.served_model_name,
        base_url=profile.base_url,
        api_key=profile.api_key,
        temperature=0,
        max_retries=max_retries,
        timeout=timeout_seconds,
    )


__all__ = [
    "ModelProfile",
    "ReviewModelBundle",
    "create_chat_model",
    "create_profile_chat_model",
]
