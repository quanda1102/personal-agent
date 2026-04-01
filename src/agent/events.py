"""
openclawd.core.loop.events
───────────────────────────
All event types emitted during an agentic run.

These are the ONLY things that cross the boundary between:
  - provider  (Claude/Gemini/OpenAI) → loop
  - loop                             → handlers (CLI/WebSocket/log)

Every consumer — whether a terminal printer, a WebSocket pusher,
or a log writer — receives the same typed events in the same order.
No provider-specific types ever leak past this layer.

Event flow for a single run:
  StreamStart
    (TextDelta | ToolUse → ToolResult)*
    UsageDelta           ← after each LLM turn
  StreamEnd | StreamError
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any


class EventType(Enum):
    STREAM_START  = auto()
    TEXT_DELTA    = auto()
    TOOL_USE      = auto()
    TOOL_RESULT   = auto()
    USAGE_DELTA   = auto()
    THINKING      = auto()   # extended thinking (Claude-specific)
    STREAM_END    = auto()
    STREAM_ERROR  = auto()


@dataclass
class StreamStart:
    type: EventType = field(default=EventType.STREAM_START, init=False)
    run_id:     str = ""
    session_id: str = ""
    model:      str = ""


@dataclass
class TextDelta:
    """A chunk of streamed text from the LLM."""
    type: EventType = field(default=EventType.TEXT_DELTA, init=False)
    text: str = ""


@dataclass
class Thinking:
    """Extended thinking block (Claude extended thinking)."""
    type: EventType = field(default=EventType.THINKING, init=False)
    text: str = ""


@dataclass
class ToolUse:
    """LLM is calling run(command=...). Emitted before execution."""
    type:       EventType = field(default=EventType.TOOL_USE, init=False)
    tool_id:    str = ""       
    command:    str = ""       
    turn:       int = 0       


@dataclass
class ToolResult:
    """Result of executing the command. Emitted after execution."""
    type:       EventType = field(default=EventType.TOOL_RESULT, init=False)
    tool_id:    str = ""
    command:    str = ""
    output:     str = ""       # stdout text
    exit_code:  int = 0
    elapsed_ms: float = 0.0
    has_image:  bool = False   # True if result carries vision bytes


@dataclass
class UsageDelta:
    """
    Token usage after a single LLM API call (one turn).
    Accumulated into RunUsage across the full run.
    """
    type:               EventType = field(default=EventType.USAGE_DELTA, init=False)
    input_tokens:       int = 0
    output_tokens:      int = 0
    cache_write_tokens: int = 0   # Claude prompt cache write
    cache_read_tokens:  int = 0   # Claude prompt cache read hit
    turn:               int = 0


@dataclass
class StreamEnd:
    """Run completed successfully."""
    type:        EventType = field(default=EventType.STREAM_END, init=False)
    run_id:      str = ""
    stop_reason: str = ""        # "end_turn" | "tool_ceiling" | "max_tokens"
    total_input_tokens:       int = 0
    total_output_tokens:      int = 0
    total_cache_write_tokens: int = 0
    total_cache_read_tokens:  int = 0
    total_tool_calls:         int = 0
    estimated_cost_usd:       float = 0.0
    elapsed_ms:               float = 0.0


@dataclass
class StreamError:
    """Run failed."""
    type:    EventType = field(default=EventType.STREAM_ERROR, init=False)
    run_id:  str = ""
    message: str = ""
    detail:  Any = None

Event = (
    StreamStart | TextDelta | Thinking | ToolUse | ToolResult
    | UsageDelta | StreamEnd | StreamError
)