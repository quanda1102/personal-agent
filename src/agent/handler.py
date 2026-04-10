"""
openclawd.core.loop.handlers
─────────────────────────────
Event handler interface and built-in implementations.

StreamHandler is the only contract between the loop and anything
that wants to observe or display a run's progress.

Built-in handlers:
  CLIStreamHandler    — prints streaming output to the terminal
  SilentHandler       — swallows all events (useful for testing)
  CompositeHandler    — fans out to multiple handlers simultaneously

WebSocket handler lives in a future module — it just implements StreamHandler.
"""

from __future__ import annotations

import sys
from abc import ABC, abstractmethod

from .events import (
    Event, EventType,
    StreamStart, TextDelta, Thinking, ToolUse, ToolResult,
    UsageDelta, StreamEnd, StreamError, TurnStart, TurnEnd, RetryAttempt, RecoveryApplied,
)


# ── Abstract interface ─────────────────────────────────────────────────────────

class StreamHandler(ABC):
    """
    Receives events from the agent loop.

    All methods are sync — the loop calls them from within async context
    but handlers themselves don't need to be async. If you need async I/O
    in a handler (e.g. WebSocket send), do it via asyncio.create_task()
    or make the handler async and override handle_async() instead.
    """

    @abstractmethod
    def handle(self, event: Event) -> None:
        """Process a single event."""
        ...

    # Optional lifecycle hooks
    def on_start(self) -> None: pass
    def on_end(self)   -> None: pass


# ── CLI handler ────────────────────────────────────────────────────────────────

class CLIStreamHandler(StreamHandler):
    """
    Prints streaming output to the terminal.

    Text streams as it arrives (no newline until LLM adds one).
    Tool calls and results are printed with visual separators.
    Usage summary printed at StreamEnd.
    """

    def __init__(self, show_thinking: bool = False, show_usage: bool = True):
        self.show_thinking = show_thinking
        self.show_usage    = show_usage
        self._in_text      = False   # track if we're mid-text-stream

    def handle(self, event: Event) -> None:
        t = event.type

        if t == EventType.STREAM_START:
            e: StreamStart = event
            print(f"\n[run:{e.run_id[:8]}] {e.model}", file=sys.stderr)

        elif t == EventType.TEXT_DELTA:
            e: TextDelta = event
            print(e.text, end="", flush=True)
            self._in_text = True

        elif t == EventType.THINKING:
            if self.show_thinking:
                e: Thinking = event
                print(f"\n[thinking] {e.text}", file=sys.stderr)

        elif t == EventType.TOOL_USE:
            e: ToolUse = event
            if self._in_text:
                print()   # newline after streaming text
                self._in_text = False
            print(f"\n  → [{e.turn}] {e.command}", file=sys.stderr)

        elif t == EventType.TOOL_RESULT:
            e: ToolResult = event
            # Show first 3 lines of output, truncate the rest
            lines = e.output.splitlines()
            preview = "\n       ".join(lines[:3])
            if len(lines) > 3:
                preview += f"\n       ... ({len(lines) - 3} more lines)"
            icon = "✓" if e.exit_code == 0 else "✗"
            print(
                f"  {icon} [{e.elapsed_ms:.0f}ms] {preview}",
                file=sys.stderr
            )

        elif t == EventType.USAGE_DELTA:
            pass   # accumulated into RunUsage, shown at StreamEnd

        elif t == EventType.STREAM_END:
            e: StreamEnd = event
            if self._in_text:
                print()
                self._in_text = False
            if self.show_usage:
                print(
                    f"\n[{e.stop_reason} | "
                    f"in={e.total_input_tokens} "
                    f"out={e.total_output_tokens} "
                    f"tools={e.total_tool_calls} "
                    f"cost=${e.estimated_cost_usd:.4f} "
                    f"time={e.elapsed_ms:.0f}ms]",
                    file=sys.stderr
                )

        elif t == EventType.STREAM_ERROR:
            e: StreamError = event
            if self._in_text:
                print()
                self._in_text = False
            print(f"\n[error] {e.message}", file=sys.stderr)
            if e.detail:
                print(f"  detail: {e.detail}", file=sys.stderr)

        elif t == EventType.TURN_START:
            e: TurnStart = event
            print(f"\n[turn {e.turn_num}]", file=sys.stderr)

        elif t == EventType.TURN_END:
            e: TurnEnd = event
            print(
                f"[turn {e.turn_num} done | "
                f"in={e.input_tokens} out={e.output_tokens} tools={e.tool_call_count}]",
                file=sys.stderr
            )

        elif t == EventType.RETRY_ATTEMPT:
            e: RetryAttempt = event
            print(f"\n[retry attempt={e.attempt}] {e.error_type}: {e.reason}", file=sys.stderr)

        elif t == EventType.RECOVERY_APPLIED:
            e: RecoveryApplied = event
            print(f"\n[recovery] {e.error_type}: {e.reason}", file=sys.stderr)

# ── Silent handler ─────────────────────────────────────────────────────────────

class SilentHandler(StreamHandler):
    """Swallows all events. Useful for testing or background runs."""

    def __init__(self):
        self.events: list[Event] = []

    def handle(self, event: Event) -> None:
        self.events.append(event)

    def text_output(self) -> str:
        """Reconstruct the full text output from captured events."""
        return "".join(
            e.text for e in self.events
            if e.type == EventType.TEXT_DELTA
        )

    def tool_calls(self) -> list[ToolUse]:
        return [e for e in self.events if e.type == EventType.TOOL_USE]

    def final_usage(self) -> StreamEnd | None:
        for e in reversed(self.events):
            if e.type == EventType.STREAM_END:
                return e
        return None


# ── Composite handler ──────────────────────────────────────────────────────────

class CompositeHandler(StreamHandler):
    """Fan out events to multiple handlers simultaneously."""

    def __init__(self, *handlers: StreamHandler):
        self._handlers = list(handlers)

    def add(self, handler: StreamHandler) -> None:
        self._handlers.append(handler)

    def handle(self, event: Event) -> None:
        for h in self._handlers:
            h.handle(event)

    def on_start(self) -> None:
        for h in self._handlers:
            h.on_start()

    def on_end(self) -> None:
        for h in self._handlers:
            h.on_end()
