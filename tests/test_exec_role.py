import asyncio

import pytest

from src.agent.exec_role import (
    EXECUTION_ROLE,
    ROLE_CONVERSATION,
    ROLE_FULL,
    get_execution_role,
    note_mutation_blocked,
)
from src.agent.executor import Executor, RoleScopedExecutor
from src.agent.output import Result, ok
from src.vault.note_commands import dispatch_note


@pytest.fixture(autouse=True)
def reset_role():
    token = EXECUTION_ROLE.set(ROLE_FULL)
    yield
    EXECUTION_ROLE.reset(token)


def test_default_role_full():
    assert get_execution_role() == ROLE_FULL
    assert note_mutation_blocked("write") is False


def test_conversation_blocks_mutations():
    token = EXECUTION_ROLE.set(ROLE_CONVERSATION)
    try:
        assert note_mutation_blocked("write") is True
        assert note_mutation_blocked("new") is True
        assert note_mutation_blocked("mv") is True
        assert note_mutation_blocked("tag") is True
        assert note_mutation_blocked("read") is False
        assert note_mutation_blocked("ls") is False
        assert note_mutation_blocked("find") is False
    finally:
        EXECUTION_ROLE.reset(token)


def test_role_scoped_executor_sets_context():
    inner_calls: list[str] = []

    class CaptureExec(Executor):
        async def exec(self, command: str) -> Result:
            inner_calls.append(get_execution_role())
            return ok("ok")

        @property
        def location(self) -> str:
            return "capture"

    async def _run() -> None:
        ex = RoleScopedExecutor(ROLE_CONVERSATION, inner=CaptureExec())
        assert get_execution_role() == ROLE_FULL
        await ex.exec("note ls")
        assert get_execution_role() == ROLE_FULL

    asyncio.run(_run())
    assert inner_calls == [ROLE_CONVERSATION]


def test_dispatch_note_respects_conversation_role(monkeypatch, tmp_path):
    monkeypatch.setenv("HOMEAGENT_VAULT", str(tmp_path))
    (tmp_path / "x.md").write_text("---\n---\n", encoding="utf-8")
    token = EXECUTION_ROLE.set(ROLE_CONVERSATION)
    try:
        r = dispatch_note(["write", "x.md", "hello"])
        assert r.exit != 0
        assert "vault_write_forbidden" in (r.stdout or "")
    finally:
        EXECUTION_ROLE.reset(token)


def test_allow_chat_vault_write_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HOMEAGENT_VAULT", str(tmp_path))
    monkeypatch.setenv("HOMEAGENT_ALLOW_CHAT_VAULT_WRITE", "1")
    (tmp_path / "x.md").write_text("---\n---\n", encoding="utf-8")
    token = EXECUTION_ROLE.set(ROLE_CONVERSATION)
    try:
        r = dispatch_note(["write", "x.md", "hello"])
        # Should proceed past gate (may succeed or fail on write semantics)
        assert "vault_write_forbidden" not in (r.stdout or "")
    finally:
        EXECUTION_ROLE.reset(token)
        monkeypatch.delenv("HOMEAGENT_ALLOW_CHAT_VAULT_WRITE", raising=False)
