"""Single write path for vault notes: locked id/created, auto modified."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .markdown import dump_frontmatter, split_frontmatter
from .paths import resolve_safe
from .schema import LOCKED_KEYS, normalize_tags


class VersionConflictError(Exception):
    """Disk version does not match --base-version."""

    def __init__(self, current: int, expected: int) -> None:
        self.current = int(current)
        self.expected = int(expected)
        super().__init__(f"version conflict: disk={self.current} base={self.expected}")


def _fm_version(fm: dict[str, Any]) -> int:
    v = fm.get("version")
    if v is None:
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class ParsedNote:
    path:       Path
    rel_posix:  str
    fm:         dict[str, Any]
    body:       str


def read_parsed(vault_root: Path, rel: str) -> ParsedNote:
    path = resolve_safe(vault_root, rel)
    raw = path.read_text(encoding="utf-8")
    fm, body = split_frontmatter(raw)
    if fm is None:
        fm = {}
    rel_posix = path.relative_to(vault_root.resolve()).as_posix()
    return ParsedNote(path=path, rel_posix=rel_posix, fm=fm, body=body)


def _merge_locked(existing_fm: dict[str, Any], incoming_fm: dict[str, Any]) -> dict[str, Any]:
    """Start from existing frontmatter; overlay non-locked keys from incoming; locked always from disk."""
    out = dict(existing_fm)
    for k, v in incoming_fm.items():
        if k not in LOCKED_KEYS:
            out[k] = v
    for k in LOCKED_KEYS:
        if k in existing_fm:
            out[k] = existing_fm[k]
    return out


def write_new(
    vault_root: Path,
    rel: str,
    fm_in: dict[str, Any],
    body: str,
) -> ParsedNote:
    """Create a new note; generates id and created; sets modified."""
    path = resolve_safe(vault_root, rel)
    if path.exists():
        raise FileExistsError(str(path))

    fm: dict[str, Any] = {k: v for k, v in fm_in.items() if k not in LOCKED_KEYS}
    fm["id"] = str(uuid.uuid4())
    fm["created"] = _now_iso()
    fm["modified"] = _now_iso()

    if "tags" in fm:
        fm["tags"] = normalize_tags(fm["tags"])

    fm["version"] = 1

    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = "---\n" + dump_frontmatter(fm) + "---\n" + body
    path.write_text(serialized, encoding="utf-8")

    return read_parsed(vault_root, rel)


def write_full_replace(
    vault_root: Path,
    rel: str,
    fm_in: dict[str, Any],
    body: str,
    base_version: int | None = None,
) -> ParsedNote:
    """Replace entire note; preserves locked keys from disk; increments version."""
    path = resolve_safe(vault_root, rel)
    if not path.exists():
        raise FileNotFoundError(str(path))

    existing = read_parsed(vault_root, rel)
    disk_v = _fm_version(existing.fm)
    if base_version is not None and disk_v != base_version:
        raise VersionConflictError(disk_v, base_version)

    merged = _merge_locked(existing.fm, fm_in)
    merged["modified"] = _now_iso()
    merged["version"] = disk_v + 1

    if "tags" in merged:
        merged["tags"] = normalize_tags(merged["tags"])

    serialized = "---\n" + dump_frontmatter(merged) + "---\n" + body
    path.write_text(serialized, encoding="utf-8")
    return read_parsed(vault_root, rel)


def append_body(
    vault_root: Path,
    rel: str,
    chunk: str,
    base_version: int | None = None,
) -> ParsedNote:
    """Append to body only; bumps modified; preserves locked metadata."""
    note = read_parsed(vault_root, rel)
    sep = "" if (not note.body or note.body.endswith("\n")) else "\n"
    new_body = note.body + sep + chunk
    return write_full_replace(vault_root, rel, note.fm, new_body, base_version=base_version)


def replace_section_body(body: str, heading: str, replacement: str) -> str:
    """
    Replace content under the first ## heading matching `heading` (trimmed),
    from that line until the next same-or-higher-level heading or EOF.
    """
    lines = body.splitlines(keepends=True)
    target = heading.strip()
    start_idx = -1
    level = 0

    for i, line in enumerate(lines):
        m = re.match(r"^(#{1,6})\s+(.+?)\s*$", line.rstrip("\r\n"))
        if not m:
            continue
        lev = len(m.group(1))
        title = m.group(2).strip()
        if title == target:
            start_idx = i
            level = lev
            break

    if start_idx < 0:
        raise ValueError(f"section heading not found: {heading!r}")

    end_idx = len(lines)
    for j in range(start_idx + 1, len(lines)):
        m = re.match(r"^(#{1,6})\s+", lines[j])
        if m and len(m.group(1)) <= level:
            end_idx = j
            break

    before = "".join(lines[: start_idx + 1])
    after = "".join(lines[end_idx:])
    mid = replacement
    if mid and not mid.endswith("\n"):
        mid += "\n"
    return before + mid + after


def update_tags_only(
    vault_root: Path,
    rel: str,
    add: list[str],
    remove: list[str],
    base_version: int | None = None,
) -> ParsedNote:
    """Add/remove tags in frontmatter without changing body."""
    note = read_parsed(vault_root, rel)
    tags = set(normalize_tags(note.fm.get("tags")))
    for r in remove:
        tags.discard(r.strip())
    for a in add:
        if a.strip():
            tags.add(a.strip())
    note.fm["tags"] = sorted(tags)
    return write_full_replace(vault_root, rel, note.fm, note.body, base_version=base_version)


def read_frontmatter_head(path: Path) -> tuple[dict[str, Any] | None, str]:
    """Read file and split frontmatter + body (full read; sufficient for typical notes)."""
    raw = path.read_text(encoding="utf-8")
    return split_frontmatter(raw)
