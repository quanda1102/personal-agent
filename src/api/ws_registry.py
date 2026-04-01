"""
Track active WebSocket StreamHandlers per session_id so background tasks can
push server→client messages (e.g. queue_task after heartbeat/cron).
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .ws_handler import WebSocketHandler

logger = logging.getLogger(__name__)


class WSSessionRegistry:
    """Multiple tabs may share the same session_id — broadcast to all handlers."""

    def __init__(self) -> None:
        self._by_session: dict[str, list[WebSocketHandler]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def register(self, session_id: str, handler: WebSocketHandler) -> None:
        async with self._lock:
            self._by_session[session_id].append(handler)

    async def unregister(self, session_id: str, handler: WebSocketHandler) -> None:
        async with self._lock:
            lst = self._by_session.get(session_id)
            if not lst:
                return
            try:
                lst.remove(handler)
            except ValueError:
                return
            if not lst:
                del self._by_session[session_id]

    async def broadcast_count(self, session_id: str, msg: dict) -> int:
        """
        Enqueue msg on every handler for this session (same event loop only).
        Returns how many handlers received the message.
        """
        async with self._lock:
            handlers = list(self._by_session.get(session_id, []))
        n = 0
        for h in handlers:
            try:
                h.send(msg)
                n += 1
            except Exception:
                logger.debug("ws registry: send failed for session %s", session_id, exc_info=True)
        return n


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def queue_ws_notify_loop(registry: WSSessionRegistry, stop: asyncio.Event) -> None:
    """
    Poll SQLite for pending queue rows with metadata.notify_session_id and no ws_notified;
    push {"type":"queue_task",...} to connected clients. Only sets ws_notified when ≥1 send.
    """
    interval = float(os.environ.get("HOMEAGENT_QUEUE_WS_POLL_SEC", "5"))
    interval = max(0.15, min(interval, 120.0))

    while not stop.is_set():
        try:
            from ..heartbeat.queue_store import get_queue_store

            store = get_queue_store()
            for it in store.list_pending_ws_delivery(limit=40):
                meta = it.metadata or {}
                sid = str(meta.get("notify_session_id") or "").strip()
                if not sid:
                    continue
                msg = {
                    "type":       "queue_task",
                    "session_id": sid,
                    "item": {
                        "id":          it.id,
                        "action":      it.action,
                        "source":      it.source,
                        "needs_user":  it.needs_user,
                        "priority":    it.priority,
                        "created_at":  it.created_at,
                        "status":      it.status,
                    },
                }
                n = await registry.broadcast_count(sid, msg)
                if n > 0:
                    store.patch_metadata(
                        it.id,
                        {"ws_notified": True, "ws_notified_at": _utc_iso()},
                    )
        except Exception:
            logger.exception("queue_ws_notify_loop tick failed")

        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass
