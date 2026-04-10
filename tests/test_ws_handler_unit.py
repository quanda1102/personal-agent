from __future__ import annotations

import asyncio

from src.agent.events import StreamEnd, StreamError, TextDelta, ToolResult, ToolUse
from src.agent.trace import get_trace_store, reset_trace_store
from src.agent.usage import RunUsage
from src.api.ws_handler import WebSocketHandler, event_to_dict


class _FakeWS:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send_json(self, msg: dict) -> None:
        self.messages.append(msg)


def test_event_to_dict_serializes_tool_and_end_events():
    reset_trace_store()
    usage = RunUsage(model="test")
    usage.total_input_tokens = 3
    usage.total_output_tokens = 4
    usage.total_tool_calls = 1
    get_trace_store().begin_run(
        run_id="r1",
        parent_run_id=None,
        session_id="s1",
        agent_id="main",
        agent_role="leader",
        model="test",
    )
    get_trace_store().finish_run(
        run_id="r1",
        usage=usage,
        stop_reason="end_turn",
        status="completed",
    )
    tool = event_to_dict(
        ToolUse(tool_id="t1", turn=2, command="read_file('README.md')")
    )
    done = event_to_dict(
        StreamEnd(
            run_id="r1",
            stop_reason="end_turn",
            total_input_tokens=3,
            total_output_tokens=4,
            total_tool_calls=1,
            estimated_cost_usd=0.1234567,
            elapsed_ms=19.8,
        )
    )
    err = event_to_dict(StreamError(run_id="r1", message="boom", detail="ValueError"))

    assert tool == {
        "type": "tool_use",
        "turn": 2,
        "tool_id": "t1",
        "command": "read_file('README.md')",
    }
    assert done["type"] == "stream_end"
    assert done["cost"] == 0.123457
    assert done["elapsed_ms"] == 20.0
    assert done["local_in_tokens"] == 3
    assert done["local_out_tokens"] == 4
    assert done["local_tool_calls"] == 1
    assert done["subtree_in_tokens"] == 3
    assert done["subtree_out_tokens"] == 4
    assert done["subtree_tool_calls"] == 1
    assert err["detail"] == "ValueError"


def test_websocket_handler_sender_drains_queue_until_close():
    async def _run() -> list[dict]:
        handler = WebSocketHandler()
        ws = _FakeWS()
        sender_task = asyncio.create_task(handler.sender(ws))

        handler.handle(TextDelta(text="hi"))
        handler.handle(ToolResult(tool_id="t1", command="act", output="ok", exit_code=0, elapsed_ms=1.2))
        handler.send({"type": "custom"})
        handler.close()

        await sender_task
        return ws.messages

    messages = asyncio.run(_run())
    assert messages[0] == {"type": "text_delta", "text": "hi"}
    assert messages[1]["type"] == "tool_result"
    assert messages[1]["elapsed_ms"] == 1.2
    assert messages[2] == {"type": "custom"}
