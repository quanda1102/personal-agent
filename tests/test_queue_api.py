"""REST endpoints for inspecting the SQLite task queue."""

from __future__ import annotations

import os

import pytest
from starlette.testclient import TestClient

from src.agent.loop import Runner
from src.agent.provider import LLMProvider
from src.agent.usage import TurnUsage
from src.api.server import create_app
from src.heartbeat.queue_store import get_queue_store, reset_queue_store


class _NoopProvider(LLMProvider):
    @property
    def model(self) -> str:
        return "noop"

    def get_stop_reason(self) -> str:
        return "end_turn"

    async def stream(self, messages, system, on_event, turn_num: int = 1) -> TurnUsage:
        return TurnUsage(turn=turn_num, model=self.model)

    def format_tool_result(self, tool_id, output, image=None):
        return {"role": "tool", "tool_call_id": tool_id, "content": output}


@pytest.fixture
def queue_api_app(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("HOMEAGENT_ENABLE_HEARTBEAT_TEST_API", "1")
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("HOMEAGENT_VAULT", str(vault))
    q = tmp_path / "q.db"
    monkeypatch.setenv("HOMEAGENT_QUEUE_DB", str(q))
    reset_queue_store(q)
    app = create_app(Runner(_NoopProvider(), max_tool_calls=1), system_prompt="test")
    return app


def test_queue_stats_and_items_rest(queue_api_app, tmp_path):
    store = get_queue_store()
    qid = store.push("conversation", "do the thing", needs_user=True)

    with TestClient(queue_api_app) as client:
        r = client.get("/queue/stats")
        assert r.status_code == 200
        j = r.json()
        assert j["total"] == 1
        assert j["pending"] == 1
        assert j["pending_needs_user"] == 1
        assert j["by_status"]["pending"] == 1
        assert "q.db" in j["db_path"]

        r2 = client.get("/queue/items")
        assert r2.status_code == 200
        body = r2.json()
        assert body["count"] == 1
        assert body["items"][0]["id"] == qid
        assert body["items"][0]["action"] == "do the thing"
        assert body["items"][0]["needs_user"] is True

        r3 = client.get(f"/queue/items/{qid}")
        assert r3.status_code == 200
        assert r3.json()["source"] == "conversation"

        r4 = client.get("/queue/items/00000000-0000-0000-0000-000000000000")
        assert r4.status_code == 404


def test_queue_rest_gated_404(queue_api_app, monkeypatch):
    monkeypatch.setenv("HOMEAGENT_ENABLE_HEARTBEAT_TEST_API", "0")
    with TestClient(queue_api_app) as client:
        assert client.get("/queue/stats").status_code == 404
        assert client.get("/queue/items").status_code == 404


def test_queue_items_filter_source(queue_api_app):
    store = get_queue_store()
    store.push("vault", "v")
    store.push("heartbeat", "h")

    with TestClient(queue_api_app) as client:
        r = client.get("/queue/items", params={"source": "heartbeat"})
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) == 1
        assert items[0]["source"] == "heartbeat"
