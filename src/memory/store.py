"""
Two-tier memory: SQLite short-term + FTS5 long-term (RAG layer).

Architecture
────────────
  Short-term  — recency-ordered ring buffer.
                Answers: "what happened recently?"
                Query: ORDER BY id DESC LIMIT n
                Use case: memory recent 10

  Long-term   — FTS5 full-text search with BM25 ranking.
                Answers: "what do I know about X?"
                Query: FTS5 MATCH … ORDER BY BM25 score
                Use case: memory search "breakfast preference"
                This is the RAG layer — BM25 is a strong lexical retrieval
                baseline that requires zero extra dependencies.
                (Later: plug in a vector store for semantic search.)

Both tiers live in the same SQLite database — one table, two query paths.
SQLite FTS5 is part of Python's stdlib sqlite3 module (compiled in on
macOS/Linux by default; verify with: python3 -c "import sqlite3; sqlite3.connect(':memory:').execute('CREATE VIRTUAL TABLE t USING fts5(x)')")

Database layout
───────────────
  memories          — main table, all stored entries
  memories_fts      — FTS5 virtual table (content= mode, mirrors memories)
  Triggers keep FTS in sync automatically on INSERT/UPDATE/DELETE.

Singleton pattern
─────────────────
  get_store() returns the process-wide shared instance.
  DB path: ~/.home-agent/memory.db  (configurable via HOMEAGENT_MEMORY_DB)
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ── DB path ────────────────────────────────────────────────────────────────────

def _default_db_path() -> Path:
    override = os.environ.get("HOMEAGENT_MEMORY_DB")
    if override:
        return Path(override)
    # Default: data/memory.db relative to the project root (two levels up from src/memory/)
    project_root = Path(__file__).parent.parent.parent
    return project_root / "data" / "memory.db"


# ── Schema ─────────────────────────────────────────────────────────────────────

_DDL = """
-- Main memories table
CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    content     TEXT    NOT NULL,
    tags        TEXT    NOT NULL DEFAULT '',
    scope       TEXT    NOT NULL DEFAULT 'long',   -- 'long' | 'session'
    created_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- FTS5 virtual table in content= (external content) mode.
-- Mirrors the 'content' column of 'memories'.
-- BM25 ranking is built-in to FTS5 — this IS the long-term RAG layer.
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content,
    tags,
    content='memories',
    content_rowid='id'
);

-- Keep FTS in sync with the main table automatically.
CREATE TRIGGER IF NOT EXISTS memories_ai
AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content, tags)
    VALUES (new.id, new.content, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS memories_ad
AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, tags)
    VALUES ('delete', old.id, old.content, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS memories_au
AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, tags)
    VALUES ('delete', old.id, old.content, old.tags);
    INSERT INTO memories_fts(rowid, content, tags)
    VALUES (new.id, new.content, new.tags);
END;
"""


# ── Data types ─────────────────────────────────────────────────────────────────

@dataclass
class MemoryEntry:
    id:         int
    content:    str
    tags:       str
    scope:      str
    created_at: str
    score:      float = 0.0   # BM25 rank for search results (0 = not ranked)

    def to_dict(self) -> dict:
        return {
            "id":         self.id,
            "content":    self.content,
            "tags":       self.tags,
            "scope":      self.scope,
            "created_at": self.created_at,
            "score":      self.score,
        }


# ── Store ──────────────────────────────────────────────────────────────────────

class MemoryStore:
    """
    Unified memory store.  One SQLite database, two query modes.

    Short-term access → .recent(n)
      Returns last n entries ordered by insertion time.
      Fast O(1) index scan — no FTS involved.

    Long-term / RAG access → .search(query)
      Full-text search using SQLite FTS5 with BM25 ranking.
      Returns results ordered by relevance score (lower BM25 = more relevant).
      This is the retrieval half of RAG — retrieves before the LLM generates.

    All writes go to both tiers simultaneously — you store once, query either way.
    """

    def __init__(self, db_path: Path | None = None):
        self._db_path = db_path or _default_db_path()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ── Write ──────────────────────────────────────────────────────────────────

    def store(
        self,
        content: str,
        tags:    str = "",
        scope:   str = "long",
    ) -> int:
        """
        Persist a memory entry.

        Args:
            content:  The text to remember.
            tags:     Optional space-separated tags for filtering
                      (e.g. "preference food" or "fact location").
            scope:    'long' (persists forever) or 'session' (transient).
                      Both are stored in the same table — scope is just a label.
                      Use 'memory recent' for time-ordered access to either.

        Returns:
            The new entry's integer id.
        """
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO memories (content, tags, scope) VALUES (?, ?, ?)",
                (content.strip(), tags.strip(), scope),
            )
            return cur.lastrowid  # type: ignore[return-value]

    def update(self, memory_id: int, content: str) -> bool:
        """
        Replace the content of an existing entry in-place.

        Preserves the original id, created_at, tags, and scope.
        The FTS5 index is updated automatically via the memories_au trigger.
        Returns True if the entry existed and was updated.
        """
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE memories SET content = ? WHERE id = ?",
                (content.strip(), memory_id),
            )
            return cur.rowcount > 0

    def forget(self, memory_id: int) -> bool:
        """Delete a memory entry by id.  Returns True if it existed."""
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            return cur.rowcount > 0

    def forget_session(self) -> int:
        """Delete all session-scoped entries.  Returns number deleted."""
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM memories WHERE scope = 'session'")
            return cur.rowcount

    # ── Short-term: recency query ──────────────────────────────────────────────

    def recent(
        self,
        n:     int = 10,
        scope: str | None = None,
    ) -> list[dict]:
        """
        Short-term memory: last n entries ordered by insertion time (newest first).

        Args:
            n:     Maximum number of entries to return.
            scope: Optional filter — 'long', 'session', or None (all scopes).
        """
        with self._conn() as conn:
            if scope:
                rows = conn.execute(
                    "SELECT id, content, tags, scope, created_at "
                    "FROM memories WHERE scope = ? ORDER BY id DESC LIMIT ?",
                    (scope, n),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, content, tags, scope, created_at "
                    "FROM memories ORDER BY id DESC LIMIT ?",
                    (n,),
                ).fetchall()

        return [
            {"id": r[0], "content": r[1], "tags": r[2],
             "scope": r[3], "created_at": r[4]}
            for r in rows
        ]

    # ── Long-term: FTS5 / BM25 search (RAG retrieval layer) ───────────────────

    def search(
        self,
        query:  str,
        limit:  int = 10,
        scope:  str | None = None,
    ) -> list[dict]:
        """
        Long-term memory: BM25 full-text search (the RAG retrieval layer).

        SQLite FTS5 uses the Okapi BM25 algorithm internally.  It returns
        negative scores — more negative = more relevant.  Results are ordered
        best-first (most relevant first).

        FTS5 supports:
          Simple terms:  memory search pho
          Phrases:       memory search "breakfast preference"
          Boolean:       memory search "pho OR ramen"
          Prefix:        memory search deploy*

        Args:
            query:  FTS5 match expression.
            limit:  Maximum number of results.
            scope:  Optional scope filter.
        """
        with self._conn() as conn:
            try:
                if scope:
                    rows = conn.execute(
                        """
                        SELECT m.id, m.content, m.tags, m.scope, m.created_at,
                               bm25(memories_fts) AS rank
                        FROM   memories_fts
                        JOIN   memories m ON memories_fts.rowid = m.id
                        WHERE  memories_fts MATCH ?
                          AND  m.scope = ?
                        ORDER  BY rank
                        LIMIT  ?
                        """,
                        (query, scope, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT m.id, m.content, m.tags, m.scope, m.created_at,
                               bm25(memories_fts) AS rank
                        FROM   memories_fts
                        JOIN   memories m ON memories_fts.rowid = m.id
                        WHERE  memories_fts MATCH ?
                        ORDER  BY rank
                        LIMIT  ?
                        """,
                        (query, limit),
                    ).fetchall()
            except sqlite3.OperationalError as e:
                # FTS5 query syntax errors — surface them clearly
                return [{"id": -1, "content": f"[search error] {e}", "tags": "",
                         "scope": "", "created_at": "", "score": 0.0}]

        return [
            {"id": r[0], "content": r[1], "tags": r[2],
             "scope": r[3], "created_at": r[4], "score": r[5]}
            for r in rows
        ]

    # ── Meta ───────────────────────────────────────────────────────────────────

    def count(self, scope: str | None = None) -> int:
        """Total number of memories (optionally filtered by scope)."""
        with self._conn() as conn:
            if scope:
                return conn.execute(
                    "SELECT COUNT(*) FROM memories WHERE scope = ?", (scope,)
                ).fetchone()[0]
            return conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

    def fts5_available(self) -> bool:
        """Check if SQLite FTS5 is available in this environment."""
        try:
            with self._conn() as conn:
                conn.execute("CREATE VIRTUAL TABLE _fts5_check USING fts5(x)")
                conn.execute("DROP TABLE _fts5_check")
            return True
        except sqlite3.OperationalError:
            return False

    # ── Internal ───────────────────────────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path))
        conn.execute("PRAGMA journal_mode=WAL")   # safe concurrent access
        return conn

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(_DDL)

    def __repr__(self) -> str:
        return f"MemoryStore(db={self._db_path}, entries={self.count()})"


# ── Singleton ──────────────────────────────────────────────────────────────────

_store: MemoryStore | None = None


def get_store() -> MemoryStore:
    """
    Return the process-wide shared MemoryStore instance.
    Lazily initialized on first call.
    """
    global _store
    if _store is None:
        _store = MemoryStore()
    return _store


def reset_store(db_path: Path | None = None) -> MemoryStore:
    """
    Replace the singleton with a new store (useful for testing or custom paths).
    """
    global _store
    _store = MemoryStore(db_path)
    return _store
