"""
SQLite job queue for three-agent coordination.

Default path: {VAULT}/.heartbeat/queue.db or HOMEAGENT_QUEUE_DB.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_queue_db_path() -> Path:
    override = os.environ.get("HOMEAGENT_QUEUE_DB")
    if override:
        return Path(override).expanduser().resolve()
    try:
        from ..vault.config import get_vault_root

        root = get_vault_root()
        if root is not None:
            p = root / ".heartbeat" / "queue.db"
            p.parent.mkdir(parents=True, exist_ok=True)
            return p
    except Exception:
        pass
    base = Path(os.environ.get("HOME", ".")).expanduser() / ".home-agent"
    base.mkdir(parents=True, exist_ok=True)
    return (base / "queue.db").resolve()


_DDL = """
CREATE TABLE IF NOT EXISTS queue_items (
    id            TEXT PRIMARY KEY,
    created_at    TEXT NOT NULL,
    source        TEXT NOT NULL,
    action        TEXT NOT NULL,
    needs_user    INTEGER NOT NULL DEFAULT 0,
    priority      TEXT NOT NULL DEFAULT 'routine',
    expires_at    TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',
    target_path   TEXT,
    batch_id      TEXT,
    metadata      TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_queue_status ON queue_items(status);
CREATE INDEX IF NOT EXISTS idx_queue_needs_user ON queue_items(needs_user, status);
"""


@dataclass
class QueueItem:
    id:           str
    created_at:   str
    source:       str
    action:       str
    needs_user:   bool
    priority:     str
    expires_at:   str | None
    status:       str
    target_path:  str | None
    batch_id:     str | None
    metadata:     dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "source": self.source,
            "action": self.action,
            "needs_user": self.needs_user,
            "priority": self.priority,
            "expires_at": self.expires_at,
            "status": self.status,
            "target_path": self.target_path,
            "batch_id": self.batch_id,
            "metadata": self.metadata,
        }


class QueueStore:
    def __init__(self, db_path: Path | None = None) -> None:
        self._path = db_path or default_queue_db_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self._path))
        c.row_factory = sqlite3.Row
        return c

    def _init(self) -> None:
        with self._conn() as conn:
            conn.executescript(_DDL)

    def push(
        self,
        source: str,
        action: str,
        *,
        needs_user: bool = False,
        priority: str = "routine",
        expires_at: str | None = None,
        target_path: str | None = None,
        batch_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        default_expiry_hours: float = 24.0,
    ) -> str:
        if expires_at is None and default_expiry_hours > 0 and source == "conversation":
            exp = datetime.now(timezone.utc) + timedelta(hours=default_expiry_hours)
            expires_at = exp.strftime("%Y-%m-%dT%H:%M:%SZ")
        qid = str(uuid.uuid4())
        meta = json.dumps(metadata or {})
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO queue_items (
                    id, created_at, source, action, needs_user, priority,
                    expires_at, status, target_path, batch_id, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    qid,
                    _utc_now(),
                    source,
                    action,
                    1 if needs_user else 0,
                    priority,
                    expires_at,
                    target_path,
                    batch_id,
                    meta,
                ),
            )
        return qid

    def count_pending(self) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM queue_items WHERE status = 'pending'"
            ).fetchone()
            return int(row[0]) if row else 0

    def count_total(self) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT COUNT(*) FROM queue_items").fetchone()
            return int(row[0]) if row else 0

    def count_by_status(self) -> dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM queue_items GROUP BY status"
            ).fetchall()
        return {str(r["status"]): int(r["n"]) for r in rows}

    def count_pending_needs_user(self) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM queue_items WHERE status = 'pending' AND needs_user = 1"
            ).fetchone()
            return int(row[0]) if row else 0

    @property
    def db_path(self) -> Path:
        """Resolved SQLite path (for ops / GET /queue/stats)."""
        return self._path

    def list_items(
        self,
        *,
        status: str | None = None,
        source: str | None = None,
        needs_user: bool | None = None,
        limit: int = 100,
    ) -> list[QueueItem]:
        q = "SELECT * FROM queue_items WHERE 1=1"
        params: list[Any] = []
        if status:
            q += " AND status = ?"
            params.append(status)
        if source:
            q += " AND source = ?"
            params.append(source)
        if needs_user is not None:
            q += " AND needs_user = ?"
            params.append(1 if needs_user else 0)
        q += " ORDER BY created_at ASC LIMIT ?"
        lim = max(1, min(int(limit), 10_000))
        params.append(lim)
        with self._conn() as conn:
            rows = conn.execute(q, params).fetchall()
        return [self._row_to_item(r) for r in rows]

    def peek_pending(self, *, limit: int = 10) -> list[QueueItem]:
        """Oldest pending tasks first (FIFO). Same as list_items(status='pending')."""
        return self.list_items(status="pending", limit=limit)

    def list_recent(self, *, limit: int = 20) -> list[QueueItem]:
        """Newest rows first (any status) — for UI / audit."""
        lim = max(1, min(int(limit), 500))
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM queue_items ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (lim,),
            ).fetchall()
        return [self._row_to_item(r) for r in rows]

    def get(self, item_id: str) -> QueueItem | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM queue_items WHERE id = ?", (item_id,)
            ).fetchone()
        return self._row_to_item(row) if row else None

    def update_status(self, item_id: str, status: str, metadata_patch: dict[str, Any] | None = None) -> bool:
        with self._conn() as conn:
            if metadata_patch:
                row = conn.execute(
                    "SELECT metadata FROM queue_items WHERE id = ?", (item_id,)
                ).fetchone()
                if not row:
                    return False
                m = json.loads(row[0] or "{}")
                m.update(metadata_patch)
                cur = conn.execute(
                    "UPDATE queue_items SET status = ?, metadata = ? WHERE id = ?",
                    (status, json.dumps(m), item_id),
                )
                return cur.rowcount > 0
            cur = conn.execute(
                "UPDATE queue_items SET status = ? WHERE id = ?",
                (status, item_id),
            )
            return cur.rowcount > 0

    def patch_metadata(self, item_id: str, metadata_patch: dict[str, Any]) -> bool:
        """Merge into JSON metadata without changing status."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT metadata FROM queue_items WHERE id = ?", (item_id,)
            ).fetchone()
            if not row:
                return False
            m = json.loads(row[0] or "{}")
            m.update(metadata_patch)
            cur = conn.execute(
                "UPDATE queue_items SET metadata = ? WHERE id = ?",
                (json.dumps(m), item_id),
            )
            return cur.rowcount > 0

    def list_pending_ws_delivery(self, *, limit: int = 50) -> list[QueueItem]:
        """
        Pending rows that request WebSocket delivery: metadata.notify_session_id set
        and metadata.ws_notified is not true. Used by API background loop.
        """
        lim = max(1, min(int(limit), 200))
        candidates = self.list_items(status="pending", limit=min(lim * 8, 800))
        out: list[QueueItem] = []
        for it in candidates:
            meta = it.metadata or {}
            sid = meta.get("notify_session_id")
            if not sid or not str(sid).strip():
                continue
            if meta.get("ws_notified") is True:
                continue
            out.append(it)
            if len(out) >= lim:
                break
        return out

    def _row_to_item(self, row: sqlite3.Row) -> QueueItem:
        return QueueItem(
            id=row["id"],
            created_at=row["created_at"],
            source=row["source"],
            action=row["action"],
            needs_user=bool(row["needs_user"]),
            priority=row["priority"],
            expires_at=row["expires_at"],
            status=row["status"],
            target_path=row["target_path"],
            batch_id=row["batch_id"],
            metadata=json.loads(row["metadata"] or "{}"),
        )


_store: QueueStore | None = None


def get_queue_store() -> QueueStore:
    global _store
    if _store is None:
        _store = QueueStore()
    return _store


def reset_queue_store(db_path: Path | None = None) -> QueueStore:
    global _store
    _store = QueueStore(db_path)
    return _store
