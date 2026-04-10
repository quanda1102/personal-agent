from __future__ import annotations

import asyncio

from src.agent.exec_role import ROLE_CONVERSATION, ROLE_VAULT_EDITOR, get_execution_role
from src.agent.executor import Executor, RoleScopedExecutor
from src.cli_handler.result import Result, ok
from src.multi_agent.agent_executor import AgentScopedExecutor


class _CaptureExec(Executor):
    def __init__(self) -> None:
        self.roles: list[str] = []
        self.commands: list[str] = []

    async def exec(self, command: str) -> Result:
        self.roles.append(get_execution_role())
        self.commands.append(command)
        return ok("ok")

    @property
    def location(self) -> str:
        return "capture"


def test_role_override_can_escape_parent_conversation_role():
    inner = _CaptureExec()
    parent = RoleScopedExecutor(ROLE_CONVERSATION, inner=inner)
    child_base = RoleScopedExecutor(ROLE_VAULT_EDITOR, inner=parent.inner)
    child = AgentScopedExecutor(
        inner=child_base,
        allowed_commands=["note"],
        blocked_commands=[],
        agent_id="obsidian-1",
    )

    asyncio.run(child.exec('note tag "x.md" --add agent'))

    assert inner.commands == ['note tag "x.md" --add agent']
    assert inner.roles == [ROLE_VAULT_EDITOR]
