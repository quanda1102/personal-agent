"""Pick OpenAI cloud vs Ollama-compatible client for heartbeat (matches app conventions)."""

from __future__ import annotations

import os
from typing import Any

import openai


def heartbeat_model_and_client() -> tuple[str, openai.AsyncOpenAI]:
    """
    Returns (model, AsyncOpenAI).

    If OPENAI_API_KEY is set, use OpenAI (optional OPENAI_BASE_URL).
    Otherwise use Ollama at OPENCLAWD_OLLAMA_HOST/v1 with dummy api_key.
    """
    model = (
        os.environ.get("HEARTBEAT_MODEL")
        or os.environ.get("OLLAMA_MODEL")
        or os.environ.get("OPENCLAWD_MODEL")
        or "llama3.2"
    )
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        m = os.environ.get("HEARTBEAT_MODEL") or os.environ.get("OPENCLAWD_MODEL") or "gpt-5.4-mini"
        kwargs: dict[str, Any] = {"api_key": key}
        bu = os.environ.get("OPENAI_BASE_URL")
        if bu:
            kwargs["base_url"] = bu
        return m, openai.AsyncOpenAI(**kwargs)

    host = (os.environ.get("OPENCLAWD_OLLAMA_HOST") or "http://localhost:11434").rstrip("/")
    return model, openai.AsyncOpenAI(api_key="ollama", base_url=f"{host}/v1")
