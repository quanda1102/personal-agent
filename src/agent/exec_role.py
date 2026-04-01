"""
Per-async-context execution role for tool commands.

Conversational agents run with ROLE_CONVERSATION so mutating `note` subcommands
are blocked; heartbeat and unattended jobs use ROLE_HEARTBEAT or ROLE_FULL.

Default is ROLE_FULL so direct CLI/tests stay unrestricted unless wrapped.
"""

from __future__ import annotations

import os
from contextvars import ContextVar

ROLE_FULL = "full"
ROLE_CONVERSATION = "conversation"
ROLE_HEARTBEAT = "heartbeat"

EXECUTION_ROLE: ContextVar[str] = ContextVar("execution_role", default=ROLE_FULL)


def get_execution_role() -> str:
    return EXECUTION_ROLE.get()


def note_mutation_blocked(sub: str) -> bool:
    """True if this `note` subcommand must be rejected in the current role."""
    if os.environ.get("HOMEAGENT_ALLOW_CHAT_VAULT_WRITE", "").lower() in (
        "1",
        "true",
        "yes",
    ):
        return False
    if get_execution_role() != ROLE_CONVERSATION:
        return False
    s = sub.lower()
    return s in ("new", "write", "mv", "move", "tag")
