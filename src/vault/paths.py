"""Safe path resolution under vault root; enumerate Markdown files."""

from __future__ import annotations

from pathlib import Path


class UnsafePathError(ValueError):
    """User path escapes vault root."""


def resolve_safe(vault_root: Path, rel: str) -> Path:
    """
    Resolve a vault-relative path (POSIX-style, no leading slash).

    Rejects '..' segments and paths that resolve outside vault_root.
    """
    rel = rel.strip().replace("\\", "/").lstrip("/")
    if not rel:
        raise UnsafePathError("empty path")
    parts = Path(rel).parts
    if ".." in parts:
        raise UnsafePathError("path must not contain '..'")

    vault_root = vault_root.resolve()
    candidate = (vault_root / rel).resolve()
    try:
        candidate.relative_to(vault_root)
    except ValueError as e:
        raise UnsafePathError("path escapes vault root") from e
    return candidate


def is_dot_heartbeat_path(vault_root: Path, path: Path) -> bool:
    """True if path is under vault's `.heartbeat/` operational directory."""
    try:
        rel = path.resolve().relative_to(vault_root.resolve()).as_posix()
    except ValueError:
        return False
    return rel == ".heartbeat" or rel.startswith(".heartbeat/")


def iter_markdown_files(
    vault_root: Path,
    *,
    include_heartbeat_ops: bool = False,
) -> list[Path]:
    """
    All .md files under vault as absolute Paths, sorted.

    By default excludes `.heartbeat/` so `note ls` / `note find` do not surface
    operational logs and plans as normal notes.
    """
    vault_root = vault_root.resolve()
    out: list[Path] = []
    for p in sorted(vault_root.rglob("*.md")):
        if not p.is_file():
            continue
        if not include_heartbeat_ops and is_dot_heartbeat_path(vault_root, p):
            continue
        out.append(p)
    return out


def to_rel_posix(vault_root: Path, path: Path) -> str:
    """Relative path with forward slashes, no leading dot."""
    return path.resolve().relative_to(vault_root.resolve()).as_posix()


def rel_no_ext(vault_root: Path, path: Path) -> str:
    """Relative path without .md suffix (Obsidian link target style)."""
    rel = path.resolve().relative_to(vault_root.resolve())
    s = rel.as_posix()
    if s.lower().endswith(".md"):
        s = s[:-3]
    return s
