"""Background loop pushes queue_task to WebSocket when metadata.notify_session_id matches."""

from __future__ import annotations

import time

from starlette.testclient import TestClient

from src.agent.loop import Runner
from src.agent.provider import LLMProvider
from src.agent.usage import TurnUsage
from src.api.server import create_app
from src.heartbeat.queue_store import get_queue_store, reset_queue_store


class _Noop(LLMProvider):
    @property
    def model(self) -> str:
        return "noop"

    def get_stop_reason(self) -> str:
        return "end_turn"

    async def stream(self, messages, system, on_event, turn_num: int = 1) -> TurnUsage:
        return TurnUsage(turn=turn_num, model=self.model)

    def format_tool_result(self, tool_id, output, image=None):
        return {"role": "tool", "tool_call_id": tool_id, "content": output}


def test_queue_task_delivered_over_websocket(monkeypatch, tmp_path):
    monkeypatch.setenv("HOMEAGENT_ENABLE_QUEUE_WS_NOTIFY", "1")
    monkeypatch.setenv("HOMEAGENT_QUEUE_WS_POLL_SEC", "0.15")
    q = tmp_path / "q.db"
    monkeypatch.setenv("HOMEAGENT_QUEUE_DB", str(q))
    reset_queue_store(q)

    app = create_app(Runner(_Noop(), max_tool_calls=1), "test")
    with TestClient(app) as client:
        with client.websocket_connect("/ws/browser-ws-test") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "connected"

            get_queue_store().push(
                "heartbeat",
                "cron/heartbeat để lại việc cho bạn",
                metadata={"notify_session_id": "browser-ws-test"},
            )

            time.sleep(0.8)
            msg = ws.receive_json()
            assert msg["type"] == "queue_task"
            assert msg["session_id"] == "browser-ws-test"
            assert msg["item"]["action"] == "cron/heartbeat để lại việc cho bạn"

            st = get_queue_store().get(msg["item"]["id"])
            assert st is not None
            assert st.metadata.get("ws_notified") is True
