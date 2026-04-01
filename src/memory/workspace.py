"""
openclawd.memory.workspace
───────────────────────────
WorkspaceContext — the agent's persistent identity layer.

Holds named text files (AGENTS.md, USER.md, SOUL.md, …) that are assembled
into the Layer 0 system prompt by PromptBuilder.

Two modes:
  In-memory (default) — files live in a dict.  Fast, no disk I/O.
                         Good for tests and single-session agents.
  Disk-backed          — pass a root_dir to read/write from the filesystem.
                         Use this for persistent multi-session agents.

Usage:
    # In-memory (test / ephemeral)
    ws = WorkspaceContext()
    ws.write("AGENTS.md", "You are Clawd …")
    ws.write("USER.md",   "User: Hung …")
    prompt = ws.build_system_prompt()

    # Disk-backed (persistent)
    ws = WorkspaceContext(root_dir=Path("~/.home-agent/workspace"))
    ws.write("AGENTS.md", "…")       # writes to disk
    prompt = ws.build_system_prompt() # reads from disk
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


# Canonical file order for system prompt assembly.
# Files listed here appear first, in order.  Any other files follow.
_PROMPT_ORDER = [
    "AGENTS.md",
    "SOUL.md",
    "USER.md",
    "BOOTSTRAP.md",
    "TOOLS.md",
]


class WorkspaceContext:
    """
    Named text file store that builds the workspace section of the system prompt.

    Files are lightweight — typically a few hundred bytes each.  The combined
    system prompt block is usually under 2 KB.
    """

    def __init__(self, root_dir: Path | str | None = None):
        self._root: Path | None = Path(root_dir) if root_dir else None
        self._mem:  dict[str, str] = {}   # in-memory store (or cache)

        if self._root:
            self._root.mkdir(parents=True, exist_ok=True)

    # ── Write ──────────────────────────────────────────────────────────────────

    def write(self, filename: str, content: str) -> None:
        """Store a named file.  Overwrites if it already exists."""
        self._mem[filename] = content
        if self._root:
            (self._root / filename).write_text(content, encoding="utf-8")

    # ── Read ───────────────────────────────────────────────────────────────────

    def read(self, filename: str) -> str | None:
        """Return file content, or None if it doesn't exist."""
        if filename in self._mem:
            return self._mem[filename]
        if self._root:
            path = self._root / filename
            if path.exists():
                content = path.read_text(encoding="utf-8")
                self._mem[filename] = content
                return content
        return None

    def files(self) -> list[str]:
        """List all stored filenames."""
        if self._root:
            return [f.name for f in self._root.iterdir() if f.is_file()]
        return list(self._mem)

    # ── System prompt assembly ─────────────────────────────────────────────────

    def build_system_prompt(self) -> str:
        """
        Assemble all workspace files into a single system prompt block.

        Files in _PROMPT_ORDER appear first (in that order).
        Remaining files are appended in arbitrary order.
        Empty files are skipped.
        """
        ordered: list[str] = []

        # Gather all filenames
        known = set(self.files())

        # First: files in canonical order
        for filename in _PROMPT_ORDER:
            if filename in known:
                content = self.read(filename)
                if content and content.strip():
                    ordered.append(content.strip())

        # Then: any remaining files not in the canonical list
        for filename in sorted(known - set(_PROMPT_ORDER)):
            content = self.read(filename)
            if content and content.strip():
                ordered.append(content.strip())

        return "\n\n".join(ordered)

    def __repr__(self) -> str:
        root = str(self._root) if self._root else "in-memory"
        return f"WorkspaceContext({root}, files={self.files()})"
