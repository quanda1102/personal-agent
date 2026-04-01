"""Obsidian [[wikilinks]]: extract and replace when a note is moved."""

from __future__ import annotations

import re
from pathlib import Path

WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def iter_wikilink_inners(text: str) -> list[str]:
    """Return inner strings inside [[...]] (includes alias form 'target|alias')."""
    return [m.group(1) for m in WIKILINK_RE.finditer(text)]


def _split_inner(inner: str) -> tuple[str, str | None]:
    if "|" in inner:
        a, b = inner.split("|", 1)
        return a.strip(), b.strip()
    return inner.strip(), None


def patch_text_for_move(
    text: str,
    old_rel_no_ext: str,
    new_rel_no_ext: str,
    old_stem: str,
    new_stem: str,
) -> tuple[str, int]:
    """
    Replace wikilink targets that referred to the moved note.

    v1 rules:
    - Full path (no .md, forward slashes): old_rel_no_ext -> new_rel_no_ext
    - Basename-only: old_stem -> new_stem (only when stems differ)
    """
    total = 0

    def repl(m: re.Match[str]) -> str:
        nonlocal total
        inner = m.group(1)
        link, alias = _split_inner(inner)
        if link == old_rel_no_ext:
            new_inner = f"{new_rel_no_ext}|{alias}" if alias is not None else new_rel_no_ext
            total += 1
            return f"[[{new_inner}]]"
        if link == old_stem and old_stem != new_stem:
            new_inner = f"{new_stem}|{alias}" if alias is not None else new_stem
            total += 1
            return f"[[{new_inner}]]"
        return m.group(0)

    out = WIKILINK_RE.sub(repl, text)
    return out, total


def move_targets_for_path(vault_root: Path, old_abs: Path, new_abs: Path) -> tuple[str, str, str, str]:
    """Compute old/new rel (no .md) and stems for patching."""
    old_rel = old_abs.resolve().relative_to(vault_root.resolve()).as_posix()
    new_rel = new_abs.resolve().relative_to(vault_root.resolve()).as_posix()
    if old_rel.lower().endswith(".md"):
        old_rel = old_rel[:-3]
    if new_rel.lower().endswith(".md"):
        new_rel = new_rel[:-3]
    return old_rel, new_rel, old_abs.stem, new_abs.stem
