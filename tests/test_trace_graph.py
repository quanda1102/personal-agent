from __future__ import annotations

import asyncio

from src.agent.events import TextDelta, UsageDelta
from src.agent.handler import SilentHandler
from src.agent.loop import RunContext, Runner
from src.agent.trace import get_trace_store, reset_trace_store
from src.agent.usage import TurnUsage
from src.llm_provider.base import LLMProvider


class _SingleTurnProvider(LLMProvider):
    def __init__(self, *, text: str, in_tokens: int, out_tokens: int) -> None:
        self._text = text
        self._in = in_tokens
        self._out = out_tokens

    @property
    def model(self) -> str:
        return "trace-test"

    async def stream(
        self,
        messages: list[dict],
        system: str,
        on_event,
        turn_num: int = 1,
        tools: list[dict] | None = None,
    ) -> TurnUsage:
        on_event(TextDelta(text=self._text))
        on_event(
            UsageDelta(
                input_tokens=self._in,
                output_tokens=self._out,
                cache_write_tokens=0,
                cache_read_tokens=0,
                turn=turn_num,
            )
        )
        return TurnUsage(turn=turn_num, input_tokens=self._in, output_tokens=self._out, model=self.model)

    def format_tool_result(self, tool_id: str, output: str, image: bytes | None = None) -> dict:
        return {"role": "tool", "tool_call_id": tool_id, "content": output}


def test_trace_store_keeps_local_usage_and_aggregates_subtree():
    reset_trace_store()

    parent_runner = Runner(_SingleTurnProvider(text="parent", in_tokens=10, out_tokens=3))
    child_runner = Runner(_SingleTurnProvider(text="child", in_tokens=7, out_tokens=2))

    parent_ctx = RunContext(
        user_message="parent task",
        system_prompt="test",
        handler=SilentHandler(),
    )
    child_ctx = RunContext(
        user_message="child task",
        system_prompt="test",
        parent_run_id=parent_ctx.run_id,
        handler=SilentHandler(),
    )

    parent_usage = asyncio.run(parent_runner.run(parent_ctx))
    child_usage = asyncio.run(child_runner.run(child_ctx))

    store = get_trace_store()
    parent_trace = store.get_run(parent_ctx.run_id)
    child_trace = store.get_run(child_ctx.run_id)
    parent_subtree = store.subtree_usage(parent_ctx.run_id)

    assert parent_usage.total_input_tokens == 10
    assert child_usage.total_input_tokens == 7

    assert parent_trace is not None
    assert child_trace is not None
    assert parent_trace.local_usage.input_tokens == 10
    assert child_trace.local_usage.input_tokens == 7
    assert child_trace.parent_run_id == parent_ctx.run_id

    assert parent_subtree.input_tokens == 17
    assert parent_subtree.output_tokens == 5
