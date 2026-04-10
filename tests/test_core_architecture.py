from __future__ import annotations

import asyncio
from dataclasses import dataclass

from src.agent.capabilities import dispatch_act, make_restricted_policy
from src.agent.events import TextDelta, ToolUse, UsageDelta
from src.agent.executor import Executor
from src.agent.handler import SilentHandler
from src.agent.loop import RunContext, Runner
from src.agent.tools import make_default_registry
from src.api.session import SessionStore
from src.cli_handler.result import Result
from src.llm_provider.base import LLMProvider
from src.agent.usage import TurnUsage
from src.multi_agent.agent_executor import AgentScopedExecutor


class _ScriptedActProvider(LLMProvider):
    def __init__(self) -> None:
        self._seen_tools: list[dict] | None = None

    @property
    def model(self) -> str:
        return "scripted-act"

    @property
    def seen_tools(self) -> list[dict] | None:
        return self._seen_tools

    async def stream(
        self,
        messages: list[dict],
        system: str,
        on_event,
        turn_num: int = 1,
        tools: list[dict] | None = None,
    ) -> TurnUsage:
        self._seen_tools = tools
        if turn_num == 1:
            on_event(
                ToolUse(
                    tool_id="call-1",
                    name="act",
                    command="run_command('echo ok')",
                    turn=1,
                    input={"op": "run_command", "command": "echo ok"},
                )
            )
        else:
            on_event(TextDelta(text="done"))

        on_event(
            UsageDelta(
                input_tokens=1,
                output_tokens=1,
                cache_write_tokens=0,
                cache_read_tokens=0,
                turn=turn_num,
            )
        )
        return TurnUsage(turn=turn_num, input_tokens=1, output_tokens=1, model=self.model)

    def format_tool_result(self, tool_id: str, output: str, image: bytes | None = None) -> dict:
        return {"role": "tool", "tool_call_id": tool_id, "content": output}


class _CaptureExecutor(Executor):
    def __init__(self) -> None:
        self.commands: list[str] = []

    async def exec(self, command: str) -> Result:
        self.commands.append(command)
        return Result(stdout=f"executed:{command}", exit=0)

    @property
    def location(self) -> str:
        return "capture"


class _InnerExecutor(Executor):
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def exec(self, command: str) -> Result:
        self.calls.append(command)
        return Result(stdout="ok", exit=0)

    @property
    def location(self) -> str:
        return "inner"


@dataclass
class _DirectContext:
    executor: Executor


def test_runner_uses_single_act_tool_schema_and_executes_once():
    provider = _ScriptedActProvider()
    executor = _CaptureExecutor()
    handler = SilentHandler()
    runner = Runner(provider=provider, max_tool_calls=5)

    ctx = RunContext(
        user_message="test",
        system_prompt="test",
        tool_registry=make_default_registry(),
        executor=executor,
        handler=handler,
    )

    usage = asyncio.run(runner.run(ctx))

    assert provider.seen_tools is not None
    assert [tool["name"] for tool in provider.seen_tools] == ["act"]
    assert executor.commands == ["echo ok"]
    assert handler.text_output() == "done"
    assert usage.total_tool_calls == 1


def test_dispatch_act_restricted_policy_denies_run_command_op():
    result = asyncio.run(
        dispatch_act(
            {"op": "run_command", "command": "echo no"},
            _DirectContext(executor=_CaptureExecutor()),
            make_restricted_policy(allowed_commands=["cat"]),
        )
    )
    assert result.exit_code == 1
    assert "operation 'run_command' is not allowed" in result.output


def test_agent_scoped_executor_blocks_and_allows_expected_commands():
    inner = _InnerExecutor()
    scoped = AgentScopedExecutor(
        inner=inner,
        allowed_commands=["cat", "grep"],
        blocked_commands=["rm"],
        agent_id="worker-1",
    )

    blocked = asyncio.run(scoped.exec("rm -f out.txt"))
    allowed = asyncio.run(scoped.exec("cat README.md"))

    assert blocked.exit == 1
    assert "command 'rm' is blocked" in blocked.stdout
    assert allowed.exit == 0
    assert inner.calls == ["cat README.md"]


def test_session_store_keeps_sessions_isolated_and_clear_is_local():
    store = SessionStore()
    alpha = store.get_or_create("alpha")
    beta = store.get_or_create("beta")

    alpha.messages.append({"role": "user", "content": "hello"})
    beta.messages.append({"role": "user", "content": "world"})

    assert len(store.get("alpha").messages) == 1
    assert len(store.get("beta").messages) == 1

    assert store.clear_messages("alpha") is True
    assert store.get("alpha").messages == []
    assert store.get("beta").messages == [{"role": "user", "content": "world"}]
