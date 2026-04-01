"""Run plan via Runner + run() tool (note / queue only)."""

from __future__ import annotations

import asyncio
import os

from ..agent.exec_role import ROLE_HEARTBEAT
from ..agent.executor import RoleScopedExecutor
from ..agent.handler import SilentHandler
from ..agent.loop import RunContext, Runner
from ..llm_provider.ollama import OllamaProvider
from ..llm_provider.openai import OpenAIProvider as CloudOpenAIProvider
from .inputs import build_phase1_digest
from .prompts import HEARTBEAT_EXECUTE_SYSTEM
from .queue_store import QueueStore


def _heartbeat_runner_provider():
    """Same backend selection as llm_client; executor needs tools."""
    if os.environ.get("OPENAI_API_KEY"):
        return CloudOpenAIProvider(
            model=os.environ.get("HEARTBEAT_MODEL")
            or os.environ.get("OPENCLAWD_MODEL")
            or None,
        )
    return OllamaProvider(
        model=os.environ.get("HEARTBEAT_MODEL")
        or os.environ.get("OLLAMA_MODEL")
        or os.environ.get("OPENCLAWD_MODEL")
        or None,
        supports_tools=True,
    )


def _last_assistant_text(messages: list) -> str:
    for m in reversed(messages):
        if m.get("role") != "assistant":
            continue
        c = m.get("content")
        if isinstance(c, str) and c.strip():
            return c.strip()
    return ""


async def execute_plan_async(
    vault,
    store: QueueStore,
    hb_dir,
    last_run_at: str,
    plan_text: str,
    *,
    max_tool_calls: int = 32,
) -> tuple[str, object]:
    """
    Run executor LLM. Returns (summary_text, usage).
    """
    digest = build_phase1_digest(vault, store, hb_dir, last_run_at)
    user = (
        "## Phase-1 digest (reference)\n\n"
        + digest
        + "\n\n## Plan to execute\n\n"
        + plan_text
        + "\n\nExecute the **Direct actions** section using run() only. "
        "Skip items that need user input unless they are simple queue status updates."
    )
    provider = _heartbeat_runner_provider()
    runner = Runner(provider=provider, max_tool_calls=max_tool_calls)
    ctx = RunContext(
        user_message=user,
        system_prompt=HEARTBEAT_EXECUTE_SYSTEM,
        executor=RoleScopedExecutor(ROLE_HEARTBEAT),
        handler=SilentHandler(),
        session_id="heartbeat",
        log_conversation=False,
        max_tool_calls=max_tool_calls,
    )
    usage = await runner.run(ctx)
    return _last_assistant_text(ctx.messages), usage


def execute_plan(
    vault,
    store: QueueStore,
    hb_dir,
    last_run_at: str,
    plan_text: str,
    *,
    max_tool_calls: int = 32,
) -> tuple[str, object]:
    return asyncio.run(
        execute_plan_async(
            vault, store, hb_dir, last_run_at, plan_text, max_tool_calls=max_tool_calls
        )
    )
