"""Append-only JSONL conversation log under the vault `.heartbeat/` tree."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def conversation_log_path() -> Path | None:
    """Path to conversation.jsonl, or None if vault not configured."""
    try:
        from ..vault.config import get_vault_root

        root = get_vault_root()
        if root is None:
            return None
        p = root / ".heartbeat" / "conversation.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    except Exception:
        return None


def append_conversation_line(
    *,
    session_id: str,
    role: str,
    text: str,
    extra: dict[str, Any] | None = None,
) -> None:
    """Append one JSON line. Silently no-op if vault unset or write fails."""
    path = conversation_log_path()
    if path is None:
        return
    row = {
        "ts": _utc_ts(),
        "session_id": session_id,
        "role": role,
        "text": text[:200_000],
    }
    if extra:
        row["extra"] = extra
    line = json.dumps(row, ensure_ascii=False) + "\n"
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def flatten_messages_for_log(messages: list[dict]) -> list[tuple[str, str]]:
    """Extract (role, text) pairs for logging from OpenAI-style message list."""
    out: list[tuple[str, str]] = []
    for m in messages:
        role = m.get("role", "")
        content = m.get("content")
        if isinstance(content, str):
            if content.strip():
                out.append((role, content[:50_000]))
        elif isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append(str(block.get("text", "")))
                    elif block.get("type") == "tool_use":
                        parts.append(
                            f"tool_use:{block.get('name')}:{block.get('input')!s}"[:2000]
                        )
                    elif block.get("type") == "tool_result":
                        out_s = str(block.get("content", ""))[:2000]
                        parts.append(f"tool_result:{out_s}")
            joined = "\n".join(parts)
            if joined.strip():
                out.append((role, joined[:50_000]))
    return out
