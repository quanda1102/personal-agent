"""
openclawd.core.providers.ollama
────────────────────────────────
Ollama local inference provider.

Ollama exposes an OpenAI-compatible API at http://localhost:11434/v1,
so OllamaProvider subclasses OpenAIProvider and inherits:
  - tool_schema()         (same OpenAI function-calling format)
  - format_tool_result()  (same role:tool message format)
  - _prepare_messages()   (same system injection + tool result flattening)

What we override:

  __init__:
    - default base_url = http://localhost:11434/v1
    - no API key (uses dummy "ollama" to satisfy openai SDK requirement)
    - OPENCLAWD_OLLAMA_HOST env var for custom host
    - supports_tools flag — some Ollama models don't support function calling

  stream:
    - skip tools param entirely if supports_tools=False
    - stream_options usage support varies by Ollama version; fall back to 0
    - tool_choice="auto" omitted when tools disabled

  estimated_cost_usd:
    - always $0.00 — local inference, no billing

Ollama tool support by model (as of early 2026):
  Supports tools:   llama3.1, llama3.2, llama3.3, qwen2.5, qwen2.5-coder,
                    mistral-nemo, mistral-small, mixtral, firefunction-v2,
                    command-r, command-r-plus, nemotron-mini
  No tool support:  phi3, gemma2, deepseek-r1 (most reasoning models),
                    codellama, most older models

Usage:
    # Default — llama3.2, tools enabled
    provider = OllamaProvider()

    # Specific model
    provider = OllamaProvider(model="qwen2.5-coder:14b")

    # Model without tool support — still works, just can't use run()
    provider = OllamaProvider(model="deepseek-r1:8b", supports_tools=False)

    # Custom host (e.g. Ollama on another machine)
    provider = OllamaProvider(host="http://192.168.1.50:11434")
"""

from __future__ import annotations

import json
import os
from typing import Callable

from ..agent.events import Event, TextDelta, ToolUse, UsageDelta
from ..agent.usage import TurnUsage
from .openai import OpenAIProvider, _prepare_messages, _STOP_MAP


class OllamaProvider(OpenAIProvider):
    """
    Ollama local inference provider.

    Inherits all OpenAI-compatible logic (tool schema, tool result format,
    message preparation). Overrides connection setup and stream() to handle
    Ollama's local-specific behavior.

    Args:
        model:          Ollama model name. Defaults to OPENCLAWD_MODEL env var
                        or "llama3.2".
        host:           Ollama server URL. Defaults to OPENCLAWD_OLLAMA_HOST
                        env var or "http://localhost:11434".
        supports_tools: Whether this model supports function calling.
                        Set False for models like deepseek-r1, phi3, gemma2.
                        Default True — if tools fail, set this to False.
        max_tokens:     Max output tokens per turn.
    """

    DEFAULT_MODEL = "llama3.2"
    DEFAULT_HOST  = "http://localhost:11434"

    def __init__(
        self,
        model:          str | None = None,
        host:           str | None = None,
        supports_tools: bool = True,
        max_tokens:     int = 4096,
    ):
        resolved_host = (
            host
            or os.environ.get("OPENCLAWD_OLLAMA_HOST")
            or self.DEFAULT_HOST
        ).rstrip("/")

        # Ollama doesn't need a real API key, but the openai SDK requires
        # the field to be non-empty. "ollama" is the conventional dummy value.
        super().__init__(
            model=model or os.environ.get("OPENCLAWD_MODEL", self.DEFAULT_MODEL),
            api_key="ollama",
            base_url=f"{resolved_host}/v1",
            max_tokens=max_tokens,
        )

        self._host           = resolved_host
        self._supports_tools = supports_tools

    # ── Cost: always zero (local inference) ───────────────────────────────────

    @property
    def estimated_cost_usd(self) -> float:
        """Local inference — no billing."""
        return 0.0

    def __repr__(self) -> str:
        tools = "tools=yes" if self._supports_tools else "tools=no"
        return f"OllamaProvider(model={self._model!r}, host={self._host!r}, {tools})"

    # ── Stream — override to handle Ollama quirks ──────────────────────────────

    async def stream(
        self,
        messages:  list[dict],
        system:    str,
        on_event:  Callable[[Event], None],
        turn_num:  int = 1,
    ) -> TurnUsage:
        """
        Stream a single Ollama turn.

        Differences from OpenAIProvider.stream():
          1. If supports_tools=False, omits tools and tool_choice params
             entirely. The model responds with plain text only.
          2. stream_options usage support is spotty across Ollama versions.
             We request it but fall back to 0 if it's missing — cost is $0
             anyway, but token counts are still useful for display.
          3. Tool call IDs: Ollama generates them, but some older builds
             may omit the id field. We generate a fallback if missing.
        """
        import uuid

        usage = TurnUsage(turn=turn_num, model=self._model)
        self._last_stop_reason = "end_turn"

        prepared   = _prepare_messages(messages, system)
        _tool_calls: dict[int, dict] = {}

        # Build create() kwargs — conditionally include tools
        create_kwargs: dict = {
            "model":          self._model,
            "max_tokens":     self._max_tokens,
            "messages":       prepared,
            "stream":         True,
            "stream_options": {"include_usage": True},
        }
        if self._supports_tools:
            create_kwargs["tools"]       = self.tool_schema()
            create_kwargs["tool_choice"] = "auto"

        stream = await self._client.chat.completions.create(**create_kwargs)

        async for chunk in stream:
            # ── Usage (present if Ollama version supports stream_options) ─────
            if getattr(chunk, "usage", None):
                usage.input_tokens  = chunk.usage.prompt_tokens or 0
                usage.output_tokens = chunk.usage.completion_tokens or 0

            if not chunk.choices:
                continue

            choice = chunk.choices[0]
            delta  = choice.delta

            # ── Stop reason ───────────────────────────────────────────────────
            if choice.finish_reason:
                self._last_stop_reason = _STOP_MAP.get(
                    choice.finish_reason, "end_turn"
                )

            # ── Text delta ────────────────────────────────────────────────────
            if delta.content:
                on_event(TextDelta(text=delta.content))

            # ── Tool call deltas ──────────────────────────────────────────────
            if self._supports_tools and delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in _tool_calls:
                        _tool_calls[idx] = {
                            "id":       "",
                            "name":     "",
                            "args_buf": "",
                        }
                    # Ollama may omit id on some builds — generate fallback
                    if tc.id:
                        _tool_calls[idx]["id"] = tc.id
                    elif not _tool_calls[idx]["id"]:
                        _tool_calls[idx]["id"] = f"call_{uuid.uuid4().hex[:8]}"

                    if tc.function and tc.function.name:
                        _tool_calls[idx]["name"] = tc.function.name
                    if tc.function and tc.function.arguments:
                        _tool_calls[idx]["args_buf"] += tc.function.arguments

        # ── Emit completed tool use events ────────────────────────────────────
        for tc in _tool_calls.values():
            try:
                tool_input = json.loads(tc["args_buf"] or "{}")
            except json.JSONDecodeError:
                tool_input = {}

            on_event(ToolUse(
                tool_id=tc["id"],
                command=tool_input.get("command", ""),
                turn=turn_num,
            ))

        # ── Emit usage delta (tokens may be 0 on older Ollama builds) ─────────
        on_event(UsageDelta(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_write_tokens=0,
            cache_read_tokens=0,
            turn=turn_num,
        ))

        return usage