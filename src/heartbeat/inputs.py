"""Assemble Phase-1 digest: conversation tail, queue, vault index."""

from __future__ import annotations

import json
from pathlib import Path

from ..vault.paths import iter_markdown_files, to_rel_posix
from ..vault.schema import normalize_tags
from ..vault.writer import read_frontmatter_head
from .queue_store import QueueStore


def load_conversation_since(
    conv_path: Path,
    last_run_at: str,
    *,
    max_lines: int = 120,
) -> str:
    """Return recent JSONL lines; if last_run_at set, filter ts > last_run_at (string compare OK for ISO)."""
    if not conv_path.exists():
        return "(no conversation log yet)"
    lines_out: list[str] = []
    for line in conv_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            lines_out.append(line[:500])
            continue
        ts = row.get("ts") or ""
        if last_run_at and ts and ts <= last_run_at:
            continue
        lines_out.append(json.dumps(row, ensure_ascii=False)[:2000])
    tail = lines_out[-max_lines:]
    return "\n".join(tail) if tail else "(no new lines since last_run_at)"


def format_pending_queue(store: QueueStore, limit: int = 50) -> str:
    items = store.list_items(status="pending", limit=limit)
    if not items:
        return "(no pending queue items)"
    lines = []
    for it in items:
        lines.append(
            f"- id={it.id} source={it.source} needs_user={it.needs_user} "
            f"priority={it.priority} action={it.action[:300]!r}"
        )
    return "\n".join(lines)


def build_vault_index_digest(vault: Path, *, max_notes: int = 200) -> str:
    rows: list[str] = []
    for abs_path in iter_markdown_files(vault, include_heartbeat_ops=False)[:max_notes]:
        rel = to_rel_posix(vault, abs_path)
        fm, _ = read_frontmatter_head(abs_path)
        tags = normalize_tags((fm or {}).get("tags"))
        title = (fm or {}).get("title") or ""
        ver = (fm or {}).get("version", "")
        mtime = int(abs_path.stat().st_mtime)
        rows.append(
            f"{rel}\tver={ver}\tmtime={mtime}\ttitle={title!s}\ttags={','.join(tags)}"
        )
    if not rows:
        return "(no markdown notes outside .heartbeat/)"
    return "\n".join(sorted(rows))


def build_phase1_digest(
    vault: Path,
    store: QueueStore,
    hb_dir: Path,
    last_run_at: str,
) -> str:
    conv = hb_dir / "conversation.jsonl"
    parts = [
        "## Conversation log (new since last_run_at)",
        load_conversation_since(conv, last_run_at),
        "",
        "## Pending queue",
        format_pending_queue(store),
        "",
        "## Vault index (paths, version, mtime, title, tags)",
        build_vault_index_digest(vault),
    ]
    return "\n".join(parts)
