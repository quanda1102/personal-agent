from __future__ import annotations

import asyncio

from src.agent.capabilities import _validate_restricted_command, make_top_level_policy
from src.agent.executor import LocalExecutor
from src.agent.tools import make_default_registry, make_restricted_registry


def test_default_registry_exposes_single_act_tool():
    registry = make_default_registry()
    assert registry.names == ["act"]


def test_restricted_registry_uses_single_act_tool():
    registry = make_restricted_registry(allowed_commands=["cat", "grep"], blocked_commands=["rm"])
    assert registry.names == ["act"]


def test_restricted_command_rejects_interpreter_not_in_allowlist():
    result = _validate_restricted_command(
        'python3 -c "open(\'x\', \'w\').write(\'nope\')"',
        allowed=frozenset({"cat", "grep"}),
        blocked=frozenset(),
    )
    assert result is not None
    assert "not in the allowed command set" in result.stdout


def test_restricted_command_rejects_redirection():
    result = _validate_restricted_command(
        "cat README.md > out.txt",
        allowed=frozenset({"cat"}),
        blocked=frozenset(),
    )
    assert result is not None
    assert "output redirection is not allowed" in result.stdout


def test_restricted_command_checks_all_chain_segments():
    result = _validate_restricted_command(
        "cat README.md | grep act && rm -f out.txt",
        allowed=frozenset({"cat", "grep"}),
        blocked=frozenset({"rm"}),
    )
    assert result is not None
    assert "'rm' is blocked" in result.stdout


def test_run_command_dispatches_queue_and_note_without_import_errors(monkeypatch, tmp_path):
    from src.agent.capabilities import dispatch_act

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "hello.md").write_text("---\ntitle: Hello\n---\nbody\n", encoding="utf-8")

    monkeypatch.setenv("HOMEAGENT_VAULT", str(vault))
    monkeypatch.setenv("HOMEAGENT_QUEUE_DB", str(tmp_path / "queue.db"))

    class _Ctx:
        executor = LocalExecutor()

    queue_result = asyncio.run(
        dispatch_act(
            {"op": "run_command", "command": "queue list"},
            _Ctx(),
            make_top_level_policy(),
        )
    )
    note_result = asyncio.run(
        dispatch_act(
            {"op": "run_command", "command": "note read hello.md"},
            _Ctx(),
            make_top_level_policy(),
        )
    )

    assert "ModuleNotFoundError" not in queue_result.output
    assert "ModuleNotFoundError" not in note_result.output
    assert queue_result.exit_code == 0
    assert note_result.exit_code == 0
