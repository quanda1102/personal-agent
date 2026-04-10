"""
openclawd.coordination.agent_executor
──────────────────────────────────────
Runtime command enforcement for sub-agents.

AgentScopedExecutor wraps any Executor and filters commands
BEFORE they reach the inner executor. This is runtime-level
enforcement — the sub-agent literally cannot run blocked commands,
regardless of what the LLM outputs.

Two filter modes (both can be active simultaneously):
  allowed_commands: whitelist — ONLY these commands pass through.
                    Empty list = no restriction (all allowed).
  blocked_commands: blacklist — these commands are ALWAYS rejected.
                    Applied ON TOP of allowed_commands.

Evaluation order:
  1. Parse first token (command name) from command string
  2. If blocked_commands contains it → reject
  3. If allowed_commands is non-empty and doesn't contain it → reject
  4. Otherwise → pass to inner executor

Example:
  # Researcher: read-only, cannot write/delete
  executor = AgentScopedExecutor(
      inner=parent_executor,
      allowed_commands=["memory", "note", "cat", "grep", "find", "ls"],
      blocked_commands=["write", "append", "rm", "mv"],
  )

  # Coder: can write, but cannot install packages or delete
  executor = AgentScopedExecutor(
      inner=parent_executor,
      allowed_commands=["memory", "cat", "grep", "write", "python3", "git"],
      blocked_commands=["rm", "pip", "npm"],
  )

Stacking:
  AgentScopedExecutor wraps RoleScopedExecutor wraps LocalExecutor:

    AgentScopedExecutor          ← checks allowed/blocked
      → RoleScopedExecutor       ← sets execution role
        → LocalExecutor          ← runs command via router.py
          → dispatch.py          ← routes to handler

  The command must pass ALL layers to execute.

TODO (Phase 3+):
  - Per-command audit log (which agent ran what)
  - Rate limiting per agent (max N commands per minute)
  - Path scoping (agent can only access certain directories)
  - Cost budgeting (agent has token/dollar budget)
"""

from __future__ import annotations

import shlex

from ..agent.executor import Executor
from ..cli_handler.result import Result


class AgentScopedExecutor(Executor):
    """
    Executor wrapper that enforces per-agent command restrictions.

    Args:
        inner:            The actual executor to delegate to
        allowed_commands: Whitelist — only these commands can run.
                          Empty = no restriction.
        blocked_commands: Blacklist — these commands always rejected.
        agent_id:         For error messages and logging.
    """

    def __init__(
        self,
        inner: Executor,
        allowed_commands: list[str] | None = None,
        blocked_commands: list[str] | None = None,
        agent_id: str = "unknown",
    ) -> None:
        self._inner = inner
        self._allowed = frozenset(allowed_commands) if allowed_commands else frozenset()
        self._blocked = frozenset(blocked_commands) if blocked_commands else frozenset()
        self._agent_id = agent_id

    async def exec(self, command: str) -> Result:
        """
        Check command against allowed/blocked lists, then delegate.

        Parses the FIRST token of the command string as the command name.
        Handles chain operators by checking only the first command —
        router.py splits chains and calls exec() per segment, so each
        segment arrives here as a single command.

        NOTE: router.py calls engine_run() which calls dispatch() which
        calls exec() per chain segment. So by the time we see a command
        here, chains are already split. But heredocs and pipes may still
        contain the full string — we check the first token only.
        """
        cmd_name = _extract_command_name(command)

        if not cmd_name:
            return await self._inner.exec(command)

        # ── Blocked commands — absolute priority ──────────────────────────
        if cmd_name in self._blocked:
            return Result(
                stdout=(
                    f"[error] agent {self._agent_id}: command '{cmd_name}' is blocked.\n"
                    f"This agent does not have permission to run '{cmd_name}'.\n"
                    f"Allowed commands: {', '.join(sorted(self._allowed)) if self._allowed else '(all)'}"
                ),
                exit=1,
            )

        # ── Allowed commands — whitelist check ────────────────────────────
        if self._allowed and cmd_name not in self._allowed:
            return Result(
                stdout=(
                    f"[error] agent {self._agent_id}: command '{cmd_name}' is not allowed.\n"
                    f"Allowed commands: {', '.join(sorted(self._allowed))}"
                ),
                exit=1,
            )

        # ── Passed all checks — delegate to inner executor ────────────────
        return await self._inner.exec(command)

    @property
    def location(self) -> str:
        constraints = []
        if self._allowed:
            constraints.append(f"allow={len(self._allowed)}")
        if self._blocked:
            constraints.append(f"block={len(self._blocked)}")
        scope = ",".join(constraints) if constraints else "unrestricted"
        return f"{self._inner.location}[agent={self._agent_id},{scope}]"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _extract_command_name(command: str) -> str:
    """
    Extract the first token (command name) from a command string.

    Examples:
      "memory search hello"     → "memory"
      "cat /path/to/file"       → "cat"
      "spawn worker 'do stuff'" → "spawn"
      ""                        → ""

    Uses shlex for proper quote handling, falls back to split on failure.
    """
    command = command.strip()
    if not command:
        return ""
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    return tokens[0].lower() if tokens else ""