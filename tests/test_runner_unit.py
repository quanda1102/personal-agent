from __future__ import annotations

import asyncio

from src.agent.events import ToolUse, UsageDelta
from src.agent.handler import SilentHandler
from src.agent.loop import RunContext, Runner
from src.agent.tools import ToolRegistry
from src.llm_provider.base import LLMProvider
from src.agent.usage import TurnUsage


class _UnknownToolProvider(LLMProvider):
    @property
    def model(self) -> str:
        return "unknown-tool"

    async def stream(
        self,
        messages: list[dict],
        system: str,
        on_event,
        turn_num: int = 1,
        tools: list[dict] | None = None,
    ) -> TurnUsage:
        if turn_num == 1:
            on_event(
                ToolUse(
                    tool_id="missing-1",
                    name="missing_tool",
                    command="missing_tool()",
                    turn=1,
                    input={"op": "nope"},
                )
            )
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


class _AlwaysToolProvider(LLMProvider):
    @property
    def model(self) -> str:
        return "always-tool"

    async def stream(
        self,
        messages: list[dict],
        system: str,
        on_event,
        turn_num: int = 1,
        tools: list[dict] | None = None,
    ) -> TurnUsage:
        on_event(
            ToolUse(
                tool_id=f"tool-{turn_num}",
                name="act",
                command="run_command('echo hi')",
                turn=turn_num,
                input={"op": "run_command", "command": "echo hi"},
            )
        )
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


async def _ok_tool(params: dict, context) -> object:
    from src.agent.tools import ToolOutput

    return ToolOutput(output="ok", exit_code=0)


def test_runner_unknown_tool_returns_error_tool_result():
    provider = _UnknownToolProvider()
    handler = SilentHandler()
    ctx = RunContext(
        user_message="test",
        system_prompt="test",
        tool_registry=ToolRegistry(),
        handler=handler,
    )

    usage = asyncio.run(Runner(provider, max_tool_calls=3).run(ctx))

    tool_results = [e for e in handler.events if getattr(e, "type", None).name == "TOOL_RESULT"]
    assert usage.total_tool_calls == 1
    assert len(tool_results) == 1
    assert "unknown tool 'missing_tool'" in tool_results[0].output
    assert tool_results[0].exit_code == 1


def test_runner_emits_tool_ceiling_stop_reason():
    provider = _AlwaysToolProvider()
    handler = SilentHandler()
    registry = ToolRegistry()
    registry.register("act", _ok_tool, {"description": "ok", "input_schema": {"type": "object"}})
    ctx = RunContext(
        user_message="test",
        system_prompt="test",
        tool_registry=registry,
        handler=handler,
        max_tool_calls=1,
    )

    usage = asyncio.run(Runner(provider, max_tool_calls=5).run(ctx))

    final = handler.final_usage()
    errors = [e for e in handler.events if getattr(e, "type", None).name == "STREAM_ERROR"]
    assert usage.total_tool_calls == 1
    assert final is not None
    assert final.stop_reason == "tool_ceiling"
    assert any("tool call limit" in e.message.lower() for e in errors)
