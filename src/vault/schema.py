"""Frontmatter keys: immutable vs writable (enforced in writer)."""

from __future__ import annotations

from typing import Any

# Never overwritten on update — restored from existing file before save.
LOCKED_KEYS: frozenset[str] = frozenset({"id", "created"})

# Always set by the tool on write (model cannot suppress).
SYSTEM_KEYS: frozenset[str] = frozenset({"modified"})

# Typical writable metadata (body is separate from frontmatter dict).
KNOWN_META_KEYS: frozenset[str] = frozenset(
    {"title", "tags", "summary", "id", "created", "modified"}
)


def normalize_tags(value: Any) -> list[str]:
    """Coerce YAML tags to a list of strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return [t.strip() for t in value.split(",") if t.strip()]
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    return [str(value).strip()]
