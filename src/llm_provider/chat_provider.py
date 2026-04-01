"""
Pick cloud OpenAI vs local Ollama for conversational runners (server, main, tests).

- If OPENAI_API_KEY is set → OpenAIProvider (api.openai.com or OPENAI_BASE_URL).
- Else → OllamaProvider at OPENCLAWD_OLLAMA_HOST.

Do not pass OpenAI model names to Ollama or you will get model not found (404-style).
"""

from __future__ import annotations

import os

from ..llm_provider.base import LLMProvider
from .ollama import OllamaProvider
from .openai import OpenAIProvider


def build_chat_provider(*, model_override: str | None = None) -> LLMProvider:
    """
    model_override: from CLI --model; wins over env when non-empty.
    """
    key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if model_override and model_override.strip():
        m = model_override.strip()
    elif key:
        m = (
            os.environ.get("OPENCLAWD_MODEL")
            or os.environ.get("OLLAMA_MODEL")
            or OpenAIProvider.DEFAULT_MODEL
        )
    else:
        m = (
            os.environ.get("OLLAMA_MODEL")
            or os.environ.get("OPENCLAWD_MODEL")
            or "llama3.2"
        )

    if key:
        return OpenAIProvider(model=m)
    return OllamaProvider(model=m, supports_tools=True)
