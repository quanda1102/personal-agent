"""
openclawd.core.providers.openai
────────────────────────────────
OpenAI provider adapter.

Maps OpenAI's streaming API onto our normalized event model.
This is the ONLY file in the codebase that imports `openai`.

OpenAI-specific things handled here (never leak to loop.py):

  Tool schema:
    OpenAI wraps tools as {"type": "function", "function": {...}}
    with "parameters" (not "input_schema" like Claude).
    When loop passes dynamic tool schemas from ToolRegistry, this provider
    translates them into OpenAI wire format automatically.

  Tool result format:
    OpenAI expects a separate message per tool result:
      {"role": "tool", "tool_call_id": ..., "content": "..."}
    Unlike Claude which uses a tool_result content block inside a user message.
    We still batch all results into one user message for the loop's convenience,
    but format_tool_result() returns the OpenAI-flavored dict.

  System prompt:
    OpenAI has no separate `system` param in the messages API.
    We inject it as the first message: {"role": "system", "content": "..."}.
    But only if it's not already the first message (multi-turn idempotency).

  Streaming:
    OpenAI streams via choices[0].delta.
    Tool calls stream as delta.tool_calls[index] with partial arguments JSON.
    We accumulate per-index just like Claude's per-index approach.
    Usage only arrives if stream_options={"include_usage": True} is set —
    we always enable this.

  Stop reason mapping:
    OpenAI "stop"       → our "end_turn"
    OpenAI "tool_calls" → our "tool_use"
    OpenAI "length"     → our "max_tokens"

  Images (vision):
    OpenAI uses data-URI format:
      {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
    We encode image bytes into this format in format_tool_result().
"""

from __future__ import annotations

import base64
import json
import os
from typing import Callable

import openai as _openai

from ..agent.events import Event, TextDelta, ToolUse, UsageDelta
from ..llm_provider.base import LLMProvider, RUN_TOOL
from ..agent.usage import TurnUsage


# ── Stop reason mapping ────────────────────────────────────────────────────────

_STOP_MAP = {
    "stop":        "end_turn",
    "tool_calls":  "tool_use",
    "length":      "max_tokens",
    "content_filter": "end_turn",  # treat filtered as done
}


class OpenAIProvider(LLMProvider):
    """
    OpenAI streaming provider.

    Supports any OpenAI-compatible model: gpt-4o, gpt-4o-mini, o1, o3, etc.
    Also works with Azure OpenAI and local servers (LM Studio, Ollama)
    by passing a custom base_url.

    Args:
        model:      OpenAI model string. Defaults to OPENCLAWD_MODEL env var
                    or "gpt-5.4-mini".
        api_key:    OpenAI API key. Defaults to OPENAI_API_KEY env var.
        base_url:   Override API base URL. Useful for Azure or local servers.
        max_tokens: Max output tokens per turn.
    """

    DEFAULT_MODEL = "gpt-5.4-mini"

    def __init__(
        self,
        model:      str | None = None,
        api_key:    str | None = None,
        base_url:   str | None = None,
        max_tokens: int = 4096,
    ):
        self._model      = model or os.environ.get("OPENCLAWD_MODEL", self.DEFAULT_MODEL)
        self._max_tokens = max_tokens

        kwargs: dict = {
            "api_key": api_key or os.environ.get("OPENAI_API_KEY"),
        }
        resolved_base = base_url or os.environ.get("OPENAI_BASE_URL")
        if resolved_base:
            kwargs["base_url"] = resolved_base.rstrip("/")

        self._client = _openai.AsyncOpenAI(**kwargs)
        self._last_stop_reason = "end_turn"

    @property
    def model(self) -> str:
        return self._model

    # ── Tool schema (OpenAI format) ───────────────────────────────────────────

    def tool_schema(self) -> list[dict]:
        """
        Default tool schema — single "run" tool in OpenAI wire format.
        Used as fallback when stream() receives tools=None.
        """
        return _to_openai_tools([RUN_TOOL])

    # ── Tool result format (OpenAI format) ───────────────────────────────────

    def format_tool_result(
        self,
        tool_id:  str,
        output:   str,
        image:    bytes | None = None,
    ) -> dict:
        """
        Format a tool result as an OpenAI tool message.

        OpenAI expects a flat message per result:
          {"role": "tool", "tool_call_id": ..., "content": "..."}

        For vision results (image bytes), we use a multipart content list
        with the text output plus an image_url block (data-URI format).
        """
        if image:
            mime = _sniff_mime(image)
            data_uri = f"data:{mime};base64,{base64.standard_b64encode(image).decode()}"
            content: list[dict] | str = [
                {"type": "text",      "text": output},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ]
        else:
            content = output   # plain string for non-vision results

        return {
            "role":         "tool",
            "tool_call_id": tool_id,
            "content":      content,
        }

    # ── Main streaming method ─────────────────────────────────────────────────

    async def stream(
        self,
        messages:  list[dict],
        system:    str,
        on_event:  Callable[[Event], None],
        turn_num:  int = 1,
        tools:     list[dict] | None = None,
    ) -> TurnUsage:
        """
        Stream a single OpenAI turn.

        Args:
            tools:  Dynamic tool schemas from ToolRegistry (provider-agnostic format).
                    Converted to OpenAI wire format here.
                    If None, falls back to self.tool_schema().
        """
        usage = TurnUsage(turn=turn_num, model=self._model)
        self._last_stop_reason = "end_turn"

        # ── Prepare messages ──────────────────────────────────────────────────
        prepared = _prepare_messages(messages, system)

        # ── Resolve tool definitions ──────────────────────────────────────────
        if tools is not None:
            openai_tools = _to_openai_tools(tools)
        else:
            openai_tools = self.tool_schema()

        # ── Tool call accumulator ─────────────────────────────────────────────
        _tool_calls: dict[int, dict] = {}

        stream = await self._client.chat.completions.create(
            model=self._model,
            **_max_tokens_param(self._model, self._max_tokens),
            messages=prepared,
            tools=openai_tools,
            tool_choice="auto",
            stream=True,
            stream_options={"include_usage": True},
        )

        async for chunk in stream:
            # ── Usage (arrives in final chunk) ────────────────────────────────
            if chunk.usage:
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
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in _tool_calls:
                        _tool_calls[idx] = {"id": "", "name": "", "args_buf": ""}

                    if tc.id:
                        _tool_calls[idx]["id"] = tc.id
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
                name=tc["name"],
                input=tool_input,
                command=tool_input.get("command", ""),  # backward-compat
                turn=turn_num,
            ))

        # ── Emit usage delta ──────────────────────────────────────────────────
        on_event(UsageDelta(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_write_tokens=0,
            cache_read_tokens=0,
            turn=turn_num,
        ))

        return usage

    def get_stop_reason(self) -> str:
        return self._last_stop_reason


# ── Helpers ────────────────────────────────────────────────────────────────────

def _to_openai_tools(tools: list[dict]) -> list[dict]:
    """
    Convert provider-agnostic tool schemas to OpenAI wire format.

    Input (from ToolRegistry):
      {"name": "run", "description": "...", "input_schema": {"type": "object", ...}}

    Output (OpenAI wire format):
      {"type": "function", "function": {"name": "run", "description": "...", "parameters": {...}}}

    Handles both ToolRegistry format (input_schema) and legacy format (parameters).
    Already-wrapped tools (with "type": "function") pass through unchanged.
    """
    result = []
    for tool in tools:
        # Already in OpenAI format — pass through
        if tool.get("type") == "function":
            result.append(tool)
            continue

        # Convert from provider-agnostic format
        params = tool.get("input_schema") or tool.get("parameters", {})
        result.append({
            "type": "function",
            "function": {
                "name":        tool["name"],
                "description": tool.get("description", ""),
                "parameters":  params,
            },
        })
    return result


def _max_tokens_param(model: str, value: int) -> dict:
    """
    Return the correct token-limit parameter for the given model.

    Older models  (gpt-4o, gpt-4-*, …)  use  max_tokens.
    Newer models  (gpt-5*, o1, o3, o4-*) use  max_completion_tokens.
    """
    m = model.lower()
    if m.startswith(("o1", "o3", "o4", "gpt-5")):
        return {"max_completion_tokens": value}
    return {"max_tokens": value}


def _prepare_messages(messages: list[dict], system: str) -> list[dict]:
    """
    Prepare messages for the OpenAI / Ollama API.

    Two conversions:
    1. Assistant tool-use blocks (Claude format → OpenAI format)
    2. Batched tool results (loop format → individual top-level messages)
    3. System prompt injection (idempotent)
    """
    result: list[dict] = []

    # ── System prompt injection ────────────────────────────────────────────────
    if system:
        already_has_system = (
            bool(messages) and
            isinstance(messages[0], dict) and
            messages[0].get("role") == "system"
        )
        if not already_has_system:
            result.append({"role": "system", "content": system})

    for msg in messages:
        role    = msg.get("role", "")
        content = msg.get("content")

        # ── 1. Assistant turn with Claude-style tool_use blocks ────────────────
        if (
            role == "assistant" and
            isinstance(content, list) and
            content and
            isinstance(content[0], dict) and
            content[0].get("type") == "tool_use"
        ):
            tool_calls = [
                {
                    "id":   block["id"],
                    "type": "function",
                    "function": {
                        "name":      block["name"],
                        "arguments": json.dumps(block.get("input", {})),
                    },
                }
                for block in content
            ]
            result.append({
                "role":       "assistant",
                "content":    None,
                "tool_calls": tool_calls,
            })

        # ── 2. Batched tool results — flatten into individual tool messages ────
        elif (
            role == "user" and
            isinstance(content, list) and
            content and
            isinstance(content[0], dict) and
            content[0].get("role") == "tool"
        ):
            for tool_result in content:
                result.append(tool_result)

        # ── 3. Everything else passes through unchanged ────────────────────────
        else:
            result.append(msg)

    return result


def _sniff_mime(data: bytes) -> str:
    """Guess image MIME type from magic bytes."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"