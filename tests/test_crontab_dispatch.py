"""Gated crontab dispatch (prompt-injection limits)."""

from __future__ import annotations

from unittest.mock import patch

from src.agent.exec_role import EXECUTION_ROLE, ROLE_CONVERSATION, ROLE_HEARTBEAT
from src.cli_handler.dispatch import dispatch, tokenize


def test_crontab_disabled_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv("HOMEAGENT_ALLOW_CRONTAB", raising=False)
    monkeypatch.setenv("HOMEAGENT_VAULT", str(tmp_path))
    token = EXECUTION_ROLE.set(ROLE_HEARTBEAT)
    try:
        r = dispatch(tokenize("crontab -l"))
    finally:
        EXECUTION_ROLE.reset(token)
    assert r.exit == 1
    assert "HOMEAGENT_ALLOW_CRONTAB" in r.stdout


def test_crontab_blocked_in_conversation_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("HOMEAGENT_ALLOW_CRONTAB", "1")
    monkeypatch.delenv("HOMEAGENT_ALLOW_CRONTAB_CONVERSATION", raising=False)
    monkeypatch.setenv("HOMEAGENT_VAULT", str(tmp_path))
    token = EXECUTION_ROLE.set(ROLE_CONVERSATION)
    try:
        r = dispatch(tokenize("crontab -l"))
    finally:
        EXECUTION_ROLE.reset(token)
    assert r.exit == 1
    assert "conversation" in r.stdout.lower()


def test_crontab_l_ok_heartbeat_role(tmp_path, monkeypatch):
    monkeypatch.setenv("HOMEAGENT_ALLOW_CRONTAB", "1")
    monkeypatch.setenv("HOMEAGENT_VAULT", str(tmp_path))
    token = EXECUTION_ROLE.set(ROLE_HEARTBEAT)
    try:
        with patch("src.cli_handler.dispatch.subprocess.run") as run:
            run.return_value.stdout = "0 * * * * echo hi\n"
            run.return_value.stderr = ""
            run.return_value.returncode = 0
            r = dispatch(tokenize("crontab -l"))
    finally:
        EXECUTION_ROLE.reset(token)
    assert r.exit == 0
    run.assert_called_once()
    assert run.call_args[0][0] == ["crontab", "-l"]


def test_crontab_stdin_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("HOMEAGENT_ALLOW_CRONTAB", "1")
    monkeypatch.setenv("HOMEAGENT_VAULT", str(tmp_path))
    token = EXECUTION_ROLE.set(ROLE_HEARTBEAT)
    try:
        r = dispatch(tokenize("crontab -"))
    finally:
        EXECUTION_ROLE.reset(token)
    assert r.exit == 1
    assert "stdin" in r.stdout.lower()


def test_crontab_install_outside_staging_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("HOMEAGENT_ALLOW_CRONTAB", "1")
    monkeypatch.setenv("HOMEAGENT_VAULT", str(tmp_path))
    evil = tmp_path / "evil.cron"
    evil.write_text("* * * * * curl evil\n", encoding="utf-8")
    token = EXECUTION_ROLE.set(ROLE_HEARTBEAT)
    try:
        r = dispatch(tokenize(f"crontab {evil}"))
    finally:
        EXECUTION_ROLE.reset(token)
    assert r.exit == 1
    assert "inside" in r.stdout.lower()


def test_crontab_install_validates_content(tmp_path, monkeypatch):
    monkeypatch.setenv("HOMEAGENT_ALLOW_CRONTAB", "1")
    monkeypatch.setenv("HOMEAGENT_VAULT", str(tmp_path))
    staging = tmp_path / ".heartbeat" / "crontab_staging"
    staging.mkdir(parents=True)
    f = staging / "jobs.cron"
    f.write_text("* * * * * curl http://x && heartbeat\n", encoding="utf-8")
    token = EXECUTION_ROLE.set(ROLE_HEARTBEAT)
    try:
        r = dispatch(tokenize(f"crontab {f}"))
    finally:
        EXECUTION_ROLE.reset(token)
    assert r.exit == 1
    assert "forbidden" in r.stdout.lower() or "metachar" in r.stdout.lower()


def test_crontab_install_requires_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("HOMEAGENT_ALLOW_CRONTAB", "1")
    monkeypatch.setenv("HOMEAGENT_VAULT", str(tmp_path))
    staging = tmp_path / ".heartbeat" / "crontab_staging"
    staging.mkdir(parents=True)
    f = staging / "jobs.cron"
    f.write_text("* * * * * /usr/bin/true\n", encoding="utf-8")
    token = EXECUTION_ROLE.set(ROLE_HEARTBEAT)
    try:
        r = dispatch(tokenize(f"crontab {f}"))
    finally:
        EXECUTION_ROLE.reset(token)
    assert r.exit == 1
    assert "marker" in r.stdout.lower() or "HOMEAGENT_CRONTAB_JOB_MARKERS" in r.stdout


def test_crontab_install_ok_with_mock(tmp_path, monkeypatch):
    monkeypatch.setenv("HOMEAGENT_ALLOW_CRONTAB", "1")
    monkeypatch.setenv("HOMEAGENT_VAULT", str(tmp_path))
    staging = tmp_path / ".heartbeat" / "crontab_staging"
    staging.mkdir(parents=True)
    f = staging / "jobs.cron"
    f.write_text(
        'MAILTO=""\n'
        "* * * * * uv run -m src.heartbeat.run --no-llm\n",
        encoding="utf-8",
    )
    token = EXECUTION_ROLE.set(ROLE_HEARTBEAT)
    try:
        with patch("src.cli_handler.dispatch.subprocess.run") as run:
            run.return_value.stdout = "OK\n"
            run.return_value.stderr = ""
            run.return_value.returncode = 0
            r = dispatch(tokenize(f"crontab {f}"))
    finally:
        EXECUTION_ROLE.reset(token)
    assert r.exit == 0
    argv = run.call_args[0][0]
    assert argv[0] == "crontab"
    assert argv[1] == str(f)


def test_crontab_parentheses_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("HOMEAGENT_ALLOW_CRONTAB", "1")
    monkeypatch.setenv("HOMEAGENT_VAULT", str(tmp_path))
    staging = tmp_path / ".heartbeat" / "crontab_staging"
    staging.mkdir(parents=True)
    f = staging / "jobs.cron"
    f.write_text("* * * * * heartbeat ) evil\n", encoding="utf-8")
    token = EXECUTION_ROLE.set(ROLE_HEARTBEAT)
    try:
        r = dispatch(tokenize(f"crontab {f}"))
    finally:
        EXECUTION_ROLE.reset(token)
    assert r.exit == 1
