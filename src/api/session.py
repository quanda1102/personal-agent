"""
src.api.session
────────────────
In-memory session store.

A session is a named conversation.  It holds the message history list that
the Runner mutates in-place across turns.  Persisting the list between
WebSocket connections gives the agent multi-turn memory within a session.

Design:
  - Thread-safe dict protected by threading.Lock (FastAPI runs in a thread
    pool for sync routes; async routes share the same event loop but the
    lock is cheap).
  - No size limit by default — prune with delete() or clear() as needed.
  - Session IDs are arbitrary strings; callers pick them (UUID, username, etc.)
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime


# ── Session data ───────────────────────────────────────────────────────────────

@dataclass
class Session:
    session_id:  str
    messages:    list[dict]  = field(default_factory=list)
    created_at:  str         = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    last_active: str         = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def touch(self) -> None:
        self.last_active = datetime.now().isoformat(timespec="seconds")

    def info(self) -> dict:
        return {
            "session_id":    self.session_id,
            "message_count": len(self.messages),
            "turn_count":    sum(1 for m in self.messages if m.get("role") == "user"),
            "created_at":    self.created_at,
            "last_active":   self.last_active,
        }


# ── Store ──────────────────────────────────────────────────────────────────────

class SessionStore:
    """
    Thread-safe in-memory store for conversation sessions.

    Each session holds a messages list that Runner mutates in-place.
    The store owns the list — callers get a reference, not a copy.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    # ── Read / create ──────────────────────────────────────────────────────────

    def get_or_create(self, session_id: str) -> Session:
        """Return the session, creating it if it doesn't exist."""
        with self._lock:
            if session_id not in self._sessions:
                self._sessions[session_id] = Session(session_id=session_id)
            session = self._sessions[session_id]
            session.touch()
            return session

    def get(self, session_id: str) -> Session | None:
        """Return the session or None if it doesn't exist."""
        with self._lock:
            return self._sessions.get(session_id)

    # ── List ───────────────────────────────────────────────────────────────────

    def list_all(self) -> list[dict]:
        """Return info dicts for all sessions, sorted by last_active desc."""
        with self._lock:
            sessions = list(self._sessions.values())
        return sorted(
            [s.info() for s in sessions],
            key=lambda x: x["last_active"],
            reverse=True,
        )

    # ── Mutate ─────────────────────────────────────────────────────────────────

    def delete(self, session_id: str) -> bool:
        """Delete a session.  Returns True if it existed."""
        with self._lock:
            if session_id in self._sessions:
                del self._sessions[session_id]
                return True
            return False

    def clear_messages(self, session_id: str) -> bool:
        """Clear conversation history but keep the session.  Returns True if it existed."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return False
            session.messages.clear()
            session.touch()
            return True

    def new_session_id(self) -> str:
        """Generate a new unique session ID."""
        return str(uuid.uuid4())[:8]


# ── Singleton ──────────────────────────────────────────────────────────────────

_store: SessionStore | None = None


def get_session_store() -> SessionStore:
    global _store
    if _store is None:
        _store = SessionStore()
    return _store
