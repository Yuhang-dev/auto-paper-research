"""Explicit OpenAI-compatible chat-model construction."""

from __future__ import annotations

import os

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from .config import HarnessSettings


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
    return ChatOpenAI(
        model=model_name,
        base_url=settings.model_base_url,
        api_key=api_key,
    )
