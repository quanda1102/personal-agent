"""
Client-level integration tests (no live LLM, no cron required).

- POST /chat with a scripted provider: Runner + tools + RoleScopedExecutor.
- Heartbeat: in-process stub run (simulates scheduled job) + subprocess --check-depth (simulates cron wrapper).

Run: uv run pytest tests/test_agent_flow_client.py -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from src.agent.events import TextDelta, ToolUse, UsageDelta
from src.agent.loop import Runner
from src.llm_provider.base import LLMProvider
from src.agent.usage import TurnUsage
from src.api.server import create_app
from src.heartbeat.queue_store import reset_queue_store


class ScriptedToolThenTextProvider(LLMProvider):
    """
    Turn 1: one tool call (memory count). Turn 2: short text.
    Matches OpenAI-style tool_result messages expected by the loop.
    """

    def __init__(self) -> None:
        self._last_stop = "end_turn"

    @property
    def model(self) -> str:
        return "scripted-test"

    def get_stop_reason(self) -> str:
        return self._last_stop

    async def stream(
        self,
        messages: list[dict],
        system: str,
        on_event,
        turn_num: int = 1,
        tools: list[dict] | None = None,
    ) -> TurnUsage:
        usage = TurnUsage(turn=turn_num, model=self.model)
        if turn_num == 1:
            on_event(
                ToolUse(
                    tool_id="scripted-1",
                    name="act",
                    command="run_command('memory count')",
                    turn=turn_num,
                    input={"op": "run_command", "command": "memory count"},
                )
            )
            self._last_stop = "tool_use"
        else:
            on_event(TextDelta(text="Tool finished."))
            self._last_stop = "end_turn"
        on_event(
            UsageDelta(
                input_tokens=1,
                output_tokens=1,
                cache_write_tokens=0,
                cache_read_tokens=0,
                turn=turn_num,
            )
        )
        return usage

    def format_tool_result(
        self,
        tool_id: str,
        output: str,
        image: bytes | None = None,
    ) -> dict:
        return {"role": "tool", "tool_call_id": tool_id, "content": output}


@pytest.fixture
def chat_app_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("HOMEAGENT_MEMORY_DB", str(tmp_path / "mem.db"))
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("HOMEAGENT_VAULT", str(vault))
    q = tmp_path / "queue.db"
    monkeypatch.setenv("HOMEAGENT_QUEUE_DB", str(q))
    reset_queue_store(q)
    runner = Runner(ScriptedToolThenTextProvider(), max_tool_calls=10)
    app = create_app(runner=runner, system_prompt="You are a test assistant.")
    return app


def test_chat_rest_full_agent_flow(chat_app_env):
    """POST /chat runs Runner, executes memory count, returns final text."""
    with TestClient(chat_app_env) as client:
        r = client.post("/chat", json={"content": "run a check", "session_id": "pytest-chat"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["session_id"] == "pytest-chat"
    assert body["tool_calls"] >= 1
    assert body["local_tool_calls"] == body["tool_calls"]
    assert body["subtree_tool_calls"] >= body["local_tool_calls"]
    assert body["local_in_tokens"] == body["in_tokens"]
    assert body["local_out_tokens"] == body["out_tokens"]
    assert body["subtree_in_tokens"] >= body["local_in_tokens"]
    assert body["subtree_out_tokens"] >= body["local_out_tokens"]
    assert "Tool finished." in body["text"]
    assert body["stop_reason"] == "end_turn"


def test_heartbeat_stub_run_in_process(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Fake cron: call heartbeat main() directly with --no-llm (no API)."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("---\ntitle: T\n---\n", encoding="utf-8")
    monkeypatch.setenv("HOMEAGENT_VAULT", str(vault))
    q = tmp_path / "queue.db"
    monkeypatch.setenv("HOMEAGENT_QUEUE_DB", str(q))
    reset_queue_store(q)

    from src.heartbeat.run import main as heartbeat_main

    code = heartbeat_main(["--no-llm", "--mode", "evening"])
    assert code == 0

    hb = vault / ".heartbeat"
    assert hb.is_dir()
    plans = list((hb / "plans").glob("*.md"))
    assert len(plans) == 1
    assert "Heartbeat plan" in plans[0].read_text(encoding="utf-8")
    state = json.loads((hb / "state.json").read_text(encoding="utf-8"))
    assert state.get("mode") == "evening"
    assert "plan_file" in state


def test_heartbeat_check_depth_subprocess(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """
    Fake cron / watchdog: separate process, same as:

        cd repo && HOMEAGENT_VAULT=... uv run python -m src.heartbeat.run --check-depth
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    q = tmp_path / "queue.db"
    monkeypatch.setenv("HOMEAGENT_VAULT", str(vault))
    monkeypatch.setenv("HOMEAGENT_QUEUE_DB", str(q))
    reset_queue_store(q)

    repo_root = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        "HOMEAGENT_VAULT": str(vault),
        "HOMEAGENT_QUEUE_DB": str(q),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "src.heartbeat.run", "--check-depth"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "pending=0" in proc.stdout
