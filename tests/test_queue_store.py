"""Queue store and dispatch."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from src.cli_handler.dispatch import dispatch, tokenize
from src.heartbeat.queue_store import reset_queue_store


@pytest.fixture
def isolated_queue(monkeypatch: pytest.MonkeyPatch):
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "q.db"
        monkeypatch.setenv("HOMEAGENT_QUEUE_DB", str(p))
        reset_queue_store(p)
        yield p


def test_queue_push_list(isolated_queue: Path):
    os.environ["HOMEAGENT_QUEUE_DB"] = str(isolated_queue)
    reset_queue_store(isolated_queue)
    r = dispatch(
        tokenize(
            'queue push --source conversation --action "do thing" --priority elevated'
        )
    )
    assert r.exit == 0
    assert "id:" in r.stdout
    r2 = dispatch(tokenize("queue list --status pending"))
    assert r2.exit == 0
    assert "do thing" in r2.stdout


def test_queue_count_pending(isolated_queue: Path):
    os.environ["HOMEAGENT_QUEUE_DB"] = str(isolated_queue)
    reset_queue_store(isolated_queue)
    dispatch(tokenize("queue push --source vault --action x"))
    r = dispatch(tokenize("queue count --pending-only"))
    assert r.exit == 0
    assert "count: 1" in r.stdout


def test_queue_list_recent_newest_first(isolated_queue: Path):
    os.environ["HOMEAGENT_QUEUE_DB"] = str(isolated_queue)
    store = reset_queue_store(isolated_queue)
    a = store.push("vault", "older")
    b = store.push("vault", "newer")
    recent = store.list_recent(limit=10)
    assert [x.id for x in recent[:2]] == [b, a]


def test_queue_stats_and_source_filter(isolated_queue: Path):
    os.environ["HOMEAGENT_QUEUE_DB"] = str(isolated_queue)
    store = reset_queue_store(isolated_queue)
    assert store.count_total() == 0
    assert store.count_by_status() == {}
    x = store.push("conversation", "a")
    y = store.push("heartbeat", "b")
    store.update_status(x, "done")
    assert store.count_total() == 2
    assert store.count_by_status() == {"done": 1, "pending": 1}
    assert store.count_pending() == 1
    assert store.count_pending_needs_user() == 0
    only_hb = store.list_items(source="heartbeat", limit=50)
    assert len(only_hb) == 1 and only_hb[0].id == y
    peek = store.peek_pending(limit=5)
    assert len(peek) == 1 and peek[0].id == y
    assert store.db_path.resolve() == isolated_queue.resolve()


def test_queue_pending_ws_delivery_and_patch(isolated_queue: Path):
    os.environ["HOMEAGENT_QUEUE_DB"] = str(isolated_queue)
    store = reset_queue_store(isolated_queue)
    a = store.push("heartbeat", "notify me", metadata={"notify_session_id": "tab-1"})
    b = store.push("heartbeat", "no ws", metadata={})
    rows = store.list_pending_ws_delivery(limit=10)
    assert len(rows) == 1 and rows[0].id == a
    assert store.patch_metadata(a, {"ws_notified": True})
    assert store.list_pending_ws_delivery(limit=10) == []


def test_queue_stats_cli(isolated_queue: Path):
    os.environ["HOMEAGENT_QUEUE_DB"] = str(isolated_queue)
    reset_queue_store(isolated_queue)
    from src.cli_handler.dispatch import dispatch, tokenize

    dispatch(tokenize('queue push --source vault --action "job"'))
    r = dispatch(tokenize("queue stats"))
    assert r.exit == 0
    assert "pending" in r.stdout
    assert "by_status" in r.stdout
    assert str(isolated_queue) in r.stdout or str(isolated_queue.resolve()) in r.stdout
