"""
src.api.ws_handler
───────────────────
WebSocketHandler — bridges the agent loop's sync event stream to an
async WebSocket connection.

The challenge:
  Runner calls handler.handle(event) synchronously from inside an async
  coroutine.  We can't await websocket.send_json() from a sync function.

Solution — asyncio.Queue + background sender task:
  1. handle()  →  puts event dict into a Queue (non-blocking, sync-safe)
  2. sender()  →  async task that drains the queue and sends to WebSocket

This pattern lets the runner produce events at full speed without blocking
on network I/O.  The sender runs concurrently via asyncio, interleaving
with the runner's await points.

Wire-up in the WebSocket endpoint:

    handler     = WebSocketHandler()
    sender_task = asyncio.create_task(handler.sender(websocket))

    ctx = RunContext(handler=handler, ...)
    await runner.run(ctx)          # produces events → queue
    handler.close()                # push sentinel
    await sender_task              # drain queue, then exit

All outbound messages (events AND control messages) go through the same
queue to prevent concurrent WebSocket writes.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import WebSocket

from ..agent.events import Event, EventType, StreamEnd
from ..agent.handler import StreamHandler
from ..agent.trace import UsageSnapshot, get_trace_store


def _usage_payload(event: StreamEnd) -> tuple[dict[str, int | float], dict[str, int | float]]:
    trace = get_trace_store().get_run(event.run_id)
    trace_local = trace.local_usage if trace is not None else UsageSnapshot(
        input_tokens=event.total_input_tokens,
        output_tokens=event.total_output_tokens,
        tool_calls=event.total_tool_calls,
        estimated_cost_usd=event.estimated_cost_usd,
    )
    local = UsageSnapshot(
        input_tokens=trace_local.input_tokens or event.total_input_tokens,
        output_tokens=trace_local.output_tokens or event.total_output_tokens,
        tool_calls=trace_local.tool_calls or event.total_tool_calls,
        estimated_cost_usd=trace_local.estimated_cost_usd or event.estimated_cost_usd,
    )
    subtree = get_trace_store().subtree_usage(event.run_id)
    if (
        subtree.input_tokens == 0
        and subtree.output_tokens == 0
        and subtree.tool_calls == 0
        and subtree.estimated_cost_usd == 0.0
    ):
        subtree = UsageSnapshot(
            input_tokens=local.input_tokens,
            output_tokens=local.output_tokens,
            tool_calls=local.tool_calls,
            estimated_cost_usd=local.estimated_cost_usd,
        )
    return (
        {
            "in_tokens": local.input_tokens,
            "out_tokens": local.output_tokens,
            "tool_calls": local.tool_calls,
            "cost": round(local.estimated_cost_usd, 6),
        },
        {
            "subtree_in_tokens": subtree.input_tokens,
            "subtree_out_tokens": subtree.output_tokens,
            "subtree_tool_calls": subtree.tool_calls,
            "subtree_cost": round(subtree.estimated_cost_usd, 6),
        },
    )


# ── Event → JSON serialisation ─────────────────────────────────────────────────

def event_to_dict(event: Event) -> dict:
    """
    Convert a typed loop event to a JSON-serialisable dict for the wire.

    The wire format is intentionally flat and minimal — frontend consumers
    should not need to know about our internal event class hierarchy.
    """
    t = event.type

    if t == EventType.STREAM_START:
        return {
            "type":       "stream_start",
            "run_id":     event.run_id,
            "session_id": event.session_id,
            "model":      event.model,
        }

    if t == EventType.TEXT_DELTA:
        return {"type": "text_delta", "text": event.text}

    if t == EventType.THINKING:
        return {"type": "thinking", "text": event.text}

    if t == EventType.TOOL_USE:
        return {
            "type":    "tool_use",
            "turn":    event.turn,
            "tool_id": event.tool_id,
            "command": event.command,
        }

    if t == EventType.TOOL_RESULT:
        return {
            "type":       "tool_result",
            "tool_id":    event.tool_id,
            "command":    event.command,
            "output":     event.output,
            "exit_code":  event.exit_code,
            "elapsed_ms": round(event.elapsed_ms, 1),
        }

    if t == EventType.USAGE_DELTA:
        return {
            "type":  "usage_delta",
            "turn":  event.turn,
            "in":    event.input_tokens,
            "out":   event.output_tokens,
        }

    if t == EventType.STREAM_END:
        local_usage, subtree_usage = _usage_payload(event)
        return {
            "type":        "stream_end",
            "run_id":      event.run_id,
            "stop_reason": event.stop_reason,
            "in_tokens":   local_usage["in_tokens"],
            "out_tokens":  local_usage["out_tokens"],
            "tool_calls":  local_usage["tool_calls"],
            "cost":        local_usage["cost"],
            "local_in_tokens": local_usage["in_tokens"],
            "local_out_tokens": local_usage["out_tokens"],
            "local_tool_calls": local_usage["tool_calls"],
            "local_cost": local_usage["cost"],
            "subtree_in_tokens": subtree_usage["subtree_in_tokens"],
            "subtree_out_tokens": subtree_usage["subtree_out_tokens"],
            "subtree_tool_calls": subtree_usage["subtree_tool_calls"],
            "subtree_cost": subtree_usage["subtree_cost"],
            "elapsed_ms":  round(event.elapsed_ms, 0),
        }

    if t == EventType.STREAM_ERROR:
        return {
            "type":    "error",
            "run_id":  event.run_id,
            "message": event.message,
            "detail":  str(event.detail) if event.detail else None,
        }
    if t == EventType.TURN_START:
        return {
            "type": "turn_start",
            "run_id": event.run_id,
            "turn_num": event.turn_num,
        }

    if t == EventType.TURN_END:
        return {
            "type": "turn_end",
            "run_id": event.run_id,
            "turn_num": event.turn_num,
            "input_tokens": event.input_tokens,
            "output_tokens": event.output_tokens,
            "tool_call_count": event.tool_call_count,
        }

    if t == EventType.RETRY_ATTEMPT:
        return {
            "type": "retry_attempt",
            "run_id": event.run_id,
            "turn_num": event.turn_num,
            "attempt": event.attempt,
            "reason": event.reason,
            "error_type": event.error_type,
        }

    if t == EventType.RECOVERY_APPLIED:
        return {
            "type": "recovery_applied",
            "run_id": event.run_id,
            "turn_num": event.turn_num,
            "reason": event.reason,
            "error_type": event.error_type,
        }

    return {"type": "unknown"}


# ── Handler ─────────────────────────────────────────────────────────────────────

_SENTINEL: dict = {}   # unique object that signals the sender to stop


class WebSocketHandler(StreamHandler):
    """
    StreamHandler implementation for WebSocket connections.

    Thread-safe: handle() may be called from any thread (asyncio task or
    sync code).  It only calls Queue.put_nowait() which is safe from any
    context in the same event loop.

    Non-event messages (control, errors) can be pushed via send().
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Any] = asyncio.Queue()

    # ── StreamHandler interface ────────────────────────────────────────────────

    def handle(self, event: Event) -> None:
        """Sync — convert event to dict and enqueue for async send."""
        self._queue.put_nowait(event_to_dict(event))

    # ── Extra control methods ──────────────────────────────────────────────────

    def send(self, msg: dict) -> None:
        """Enqueue any arbitrary JSON message (not a loop event)."""
        self._queue.put_nowait(msg)

    def close(self) -> None:
        """Signal the sender task to stop after draining remaining messages."""
        self._queue.put_nowait(_SENTINEL)

    # ── Background sender task ─────────────────────────────────────────────────

    async def sender(self, ws: WebSocket) -> None:
        """
        Drain the queue and send each message to the WebSocket.

        Run as an asyncio task alongside runner.run():
            sender_task = asyncio.create_task(handler.sender(ws))
            await runner.run(ctx)
            handler.close()
            await sender_task

        Exits cleanly when it receives the sentinel (from close()) or
        when the WebSocket disconnects.
        """
        while True:
            msg = await self._queue.get()

            # Sentinel → drain done, exit
            if msg is _SENTINEL:
                break

            try:
                await ws.send_json(msg)
            except Exception:
                # WebSocket closed mid-send; stop silently
                break
