"""
Rule-based enqueue of remediation jobs when vault/tool commands fail.

No LLM — maps stderr/stdout patterns to queue rows for the heartbeat.
"""

from __future__ import annotations

import re

from .queue_store import get_queue_store


def maybe_enqueue_remediation(
    *,
    command: str,
    rendered_output: str,
    exit_code: int,
) -> None:
    """
    If exit_code != 0 and output looks like a vault conflict, push a heartbeat job.
    Called from the agent loop after each tool execution.
    """
    if exit_code == 0:
        return
    text = rendered_output or ""
    action: str | None = None
    if "version_conflict" in text or "code: version_conflict" in text:
        m_cur = re.search(r"current[:\s]+(\d+)", text, re.I)
        m_yrs = re.search(r"base[:\s]+(\d+)|yours[:\s]+(\d+)", text, re.I)
        extra = f"command={command!r}\n{text[:4000]}"
        action = f"Remediation: version conflict after note write. {extra}"
    elif "ERR" in text and "note" in command.lower():
        action = f"Remediation: note command failed. command={command!r}\n{text[:4000]}"

    if not action:
        return
    try:
        get_queue_store().push(
            "conversation",
            action,
            needs_user=False,
            priority="routine",
            metadata={"hook": "tool_error", "command": command[:500], "exit": exit_code},
            default_expiry_hours=168.0,
        )
    except Exception:
        pass
