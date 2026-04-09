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
    STREAM_START     = auto()
    TEXT_DELTA       = auto()
    TOOL_USE         = auto()
    TOOL_RESULT      = auto()
    USAGE_DELTA      = auto()
    THINKING         = auto()   # extended thinking (Claude-specific)
    STREAM_END       = auto()
    STREAM_ERROR     = auto()
    TURN_START       = auto()
    TURN_END         = auto()
    RETRY_ATTEMPT    = auto()
    RECOVERY_APPLIED = auto()


@dataclass
class Event:
    """Base class for all events."""
    pass

@dataclass
class StreamStart(Event):
    type: EventType = field(default=EventType.STREAM_START, init=False)
    run_id:     str = ""
    session_id: str = ""
    model:      str = ""

@dataclass
class TextDelta(Event):
    type: EventType = field(default=EventType.TEXT_DELTA, init=False)
    text: str = ""

@dataclass
class Thinking(Event):
    type: EventType = field(default=EventType.THINKING, init=False)
    text: str = ""

@dataclass
class ToolUse(Event):
    type:       EventType = field(default=EventType.TOOL_USE, init=False)
    tool_id:    str = ""
    name: str = "run"
    command:    str = ""
    turn:       int = 0
    input: dict = field(default_factory=dict)

@dataclass
class ToolResult(Event):
    type:       EventType = field(default=EventType.TOOL_RESULT, init=False)
    tool_id:    str = ""
    command:    str = ""
    output:     str = ""
    exit_code:  int = 0
    elapsed_ms: float = 0.0
    has_image:  bool = False

@dataclass
class UsageDelta(Event):
    type:               EventType = field(default=EventType.USAGE_DELTA, init=False)
    input_tokens:       int = 0
    output_tokens:      int = 0
    cache_write_tokens: int = 0
    cache_read_tokens:  int = 0
    turn:               int = 0

@dataclass
class StreamEnd(Event):
    type:        EventType = field(default=EventType.STREAM_END, init=False)
    run_id:      str = ""
    stop_reason: str = ""
    total_input_tokens:       int = 0
    total_output_tokens:      int = 0
    total_cache_write_tokens: int = 0
    total_cache_read_tokens:  int = 0
    total_tool_calls:         int = 0
    estimated_cost_usd:       float = 0.0
    elapsed_ms:               float = 0.0

@dataclass
class StreamError(Event):
    type:    EventType = field(default=EventType.STREAM_ERROR, init=False)
    run_id:  str = ""
    turn_num: int = 0
    message: str = ""
    detail:  Any = None

@dataclass
class TurnStart(Event):
    type:     EventType = field(default=EventType.TURN_START, init=False)
    run_id:   str = ""
    turn_num: int = 0

@dataclass
class TurnEnd(Event):
    type:            EventType = field(default=EventType.TURN_END, init=False)
    run_id:          str = ""
    turn_num:        int = 0
    input_tokens:    int = 0
    output_tokens:   int = 0
    tool_call_count: int = 0

@dataclass
class RetryAttempt(Event):
    type:       EventType = field(default=EventType.RETRY_ATTEMPT, init=False)
    run_id:     str = ""
    turn_num:   int = 0
    attempt:    int = 0
    reason:     str = ""
    error_type: str = ""

@dataclass
class RecoveryApplied(Event):
    type:       EventType = field(default=EventType.RECOVERY_APPLIED, init=False)
    run_id:     str = ""
    turn_num:   int = 0
    reason:     str = ""
    error_type: str = ""
