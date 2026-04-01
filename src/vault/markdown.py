"""YAML frontmatter split (Obsidian-style --- ... ---)."""

from __future__ import annotations

from typing import Any

import yaml


def split_frontmatter(raw: str) -> tuple[dict[str, Any] | None, str]:
    """
    Split file into (frontmatter dict, body).

    If the file does not start with a YAML frontmatter block, returns (None, raw).
    """
    if not raw.startswith("---"):
        return None, raw

    lines = raw.splitlines(keepends=True)
    if not lines or not lines[0].startswith("---"):
        return None, raw

    end = -1
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == "---":
            end = i
            break

    if end < 0:
        return None, raw

    yaml_block = "".join(lines[1:end])
    body = "".join(lines[end + 1 :])

    try:
        data = yaml.safe_load(yaml_block)
    except yaml.YAMLError:
        return None, raw

    if not isinstance(data, dict):
        return None, raw

    return data, body


def dump_frontmatter(fm: dict[str, Any]) -> str:
    """Serialize frontmatter dict to YAML for Obsidian-style header."""
    # default_flow_style=False for readable tags lists
    text = yaml.safe_dump(
        fm,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    )
    return text.rstrip() + "\n"
