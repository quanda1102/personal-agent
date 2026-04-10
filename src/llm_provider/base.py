"""
openclawd.core.loop.provider
─────────────────────────────
LLMProvider — the abstract contract every provider adapter must implement.

The loop knows NOTHING about Claude, OpenAI, or Gemini.
It only calls provider.stream() and receives normalized events + TurnUsage.

Adding a new provider:
  1. Create core/providers/gemini.py
  2. Subclass LLMProvider
  3. Implement stream()
  4. Register in core/providers/__init__.py
  That's it. Loop is untouched.

Tool definitions:
  Default: a single "run" tool for shell commands.
  Multi-agent: loop passes dynamic tool schemas from ToolRegistry.
  Provider translates whatever it receives into wire format.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable

from ..agent.events import Event
from ..agent.usage import TurnUsage


# ── Default tool definition (used when no tools passed to stream) ─────────────

RUN_TOOL = {
    "name": "act",
    "description": (
        "Execute one structured action.\n\n"
        "Examples:\n"
        "  act(op='run_command', command='memory search \"breakfast preference\"')\n"
        "  act(op='read_file', path='README.md')\n"
        "  act(op='search_files', query='Authorization', root='src')\n"
        "  act(op='load_skill', name='weather')\n"
        "  act(op='spawn_agent', role='researcher', task='audit auth flow')\n"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "op": {
                "type": "string",
                "description": "Which action to execute.",
            },
            "command": {
                "type": "string",
                "description": "CLI command string for run_command or run_allowed_command.",
            },
            "path": {"type": "string"},
            "content": {"type": "string"},
            "root": {"type": "string"},
            "query": {"type": "string"},
            "name": {"type": "string"},
            "role": {"type": "string"},
            "task": {"type": "string"},
        },
        "required": ["op"],
    },
}


# ── Abstract provider ──────────────────────────────────────────────────────────

class LLMProvider(ABC):
    """
    Abstract base for all LLM provider adapters.

    Responsibilities:
      - Accept messages + system prompt + tool definitions
      - Stream tokens and tool calls from the provider API
      - Emit normalized Event objects via on_event()
      - Return TurnUsage with provider-agnostic token counts
    """

    @property
    @abstractmethod
    def model(self) -> str:
        """The model identifier string (e.g. 'claude-sonnet-4-6')."""
        ...

    @abstractmethod
    async def stream(
        self,
        messages:  list[dict],
        system:    str,
        on_event:  Callable[[Event], None],
        turn_num:  int = 1,
        tools:     list[dict] | None = None,
    ) -> TurnUsage:
        """
        Stream a single LLM turn.

        Args:
            messages:  Full conversation history in [{role, content}] format.
                       Tool results are included as tool_result content blocks.
            system:    System prompt string (workspace + skills combined).
            on_event:  Callback — call this for every event as it happens.
            turn_num:  Which turn in the run (for UsageDelta labeling).
            tools:     Tool definitions from ToolRegistry. If None, provider
                       falls back to self.tool_schema() (backward-compat).

        Returns:
            TurnUsage with token counts for this single API call.

        The provider must emit:
          - TextDelta      for each streamed text chunk
          - ToolUse        when the LLM requests a tool call (before execution)
          - UsageDelta     once at the end of the turn
        """
        ...

    @abstractmethod
    def format_tool_result(
        self,
        tool_id:    str,
        output:     str,
        image:      bytes | None = None,
    ) -> dict:
        """
        Format a tool result for inclusion in the messages array.

        Different providers have different formats:
          Claude:  {"type": "tool_result", "tool_use_id": ..., "content": ...}
          OpenAI:  {"role": "tool", "tool_call_id": ..., "content": ...}

        Returns a content block dict to be appended to the messages array.
        """
        ...

    def tool_schema(self) -> list[dict]:
        """
        Return the default tool definition list.
        Used as fallback when stream() receives tools=None.
        Override if your provider needs a different default structure.
        """
        return [RUN_TOOL]
