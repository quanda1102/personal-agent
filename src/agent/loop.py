"""
─────────────────────────
Runner — the agentic loop engine.

Two classes:

  RunContext  — system envelope, carries everything the system needs.
                The LLM never sees this object or any of its fields.
                SSH credentials, executor choice, run metadata all live here.

  Runner      — stateless loop engine. Receives a RunContext per run.
                Knows about: messages, tools, events, usage, ceiling.
                Knows NOTHING about: Claude/OpenAI, terminals, SSH, WebSockets.

Why RunContext holds the executor:
  Later, when the LLM controls a remote terminal over SSH, the executor
  carries the SSH connection (host, user, key). The LLM composes commands
  as strings — it never knows if they run locally or on a remote machine.
  Credentials never appear in any event, any log, or any LLM message.

                    LLM: tool_name(params)
                              ↓
                    context.tool_registry.get(tool_name)
                              ↓
                    tool_fn(params, context)
                              ↓
                    ToolOutput(output="...", exit_code=0)
                              ↓
                    LLM sees plain text output only

Multi-agent readiness:
  Each sub-agent gets its own RunContext with:
    - unique agent_id
    - own message history
    - own tool_registry (base.merge(coordination_tools))
  Runner.run() is the same loop for leader and worker.

WebSocket readiness:
  RunContext.handler: StreamHandler — inject any handler here.
  CLIStreamHandler for terminal, WebSocketHandler for ws, both together
  via CompositeHandler. Runner never changes.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from .events import (
    Event, EventType, StreamStart, ToolUse, ToolResult, StreamEnd,
    StreamError, TurnStart, TurnEnd, RetryAttempt, RecoveryApplied,
)
from .executor import Executor, LocalExecutor
from .handler import StreamHandler, SilentHandler
from .tools import ToolRegistry, ToolOutput, make_default_registry
from .capabilities import summarize_action
from .trace import get_trace_store
from ..llm_provider.base import LLMProvider
from .usage import RunUsage

DEFAULT_MAX_TOOL_CALLS = 20


# ── RunContext — system envelope ───────────────────────────────────────────────

@dataclass
class RunContext:
    """
    Everything the Runner needs for one run.
    The LLM never sees this object or any of its fields directly.

    Fields:
      user_message    — user's message (becomes first user turn)
      system_prompt   — fully assembled system prompt, built by PromptBuilder
                        before calling runner.run(). Loop uses this verbatim.
      agent_id        — unique identity for this agent instance
      agent_role      — "leader" | "worker" | custom role name
      run_id          — unique trace ID (never sent to LLM)
      session_id      — session ID (never sent to LLM)
      messages        — conversation history; mutated in place during run,
                        carry forward into next RunContext for multi-turn
      tool_registry   — name → async callable; loop resolves tools from here
      executor        — WHERE commands run: LocalExecutor or SSHExecutor
                        credentials live entirely inside the executor
      handler         — WHERE events go: CLI, WebSocket, silent, composite
      max_tool_calls  — per-run ceiling override (None = use Runner default)
    """

    user_message:   str
    system_prompt:  str              = ""

    # ── Agent identity — needed for multi-agent routing ───────────────────────
    agent_id:       str              = "main"
    agent_role:     str              = "leader"

    # ── Tracing — system-only, never sent to LLM ─────────────────────────────
    run_id:         str              = field(default_factory=lambda: str(uuid.uuid4()))
    parent_run_id:  str | None       = None
    session_id:     str              = "default"

    # ── Conversation state — mutated by runner, carry forward for multi-turn ──
    messages:       list[dict]       = field(default_factory=list)

    # ── Tool layer — registry replaces hardcoded tool dispatch ────────────────
    tool_registry:  ToolRegistry     = None  # set in __post_init__

    # ── Execution layer — credentials hidden inside executor ─────────────────
    executor:       Executor         = field(default_factory=LocalExecutor)

    # ── Output layer ─────────────────────────────────────────────────────────
    handler:        StreamHandler    = field(default_factory=SilentHandler)

    # ── Per-run ceiling override ─────────────────────────────────────────────
    max_tool_calls: int | None       = None
    last_stop_reason: str            = ""

    # ── Append turns to {VAULT}/.heartbeat/conversation.jsonl when vault is set
    log_conversation: bool           = True

    # TODO: abort_signal: asyncio.Event for sub-agent cancellation
    # TODO: parent_agent_id: str | None for tracing spawn chains

    def __post_init__(self):
        if self.tool_registry is None:
            self.tool_registry = make_default_registry()


# ── Runner — stateless agentic loop ──────────────────────────────────────────

class Runner:
    def __init__(
        self,
        provider: LLMProvider,
        max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
        max_turns: int = 10,
        max_retries: int = 3,
    ):
        self.provider       = provider
        self.max_tool_calls = max_tool_calls
        self.max_turns      = max_turns
        self.max_retries    = max_retries

    def _should_stop(
        self,
        tool_call_count: int,
        ceiling: int,
        turn_num: int,
    ) -> tuple[bool, str]:
        if tool_call_count >= ceiling:
            return True, "tool_ceiling"
        if turn_num >= self.max_turns:
            return True, "max_turns"
        # TODO: check abort_signal from context
        # TODO: check timeout
        return False, ""

    def _emit_stop_reason(self, stop_reason: str, run_id: str, emit: callable) -> None:
        messages = {
            "tool_ceiling": f"Stopped: tool call limit ({self.max_tool_calls}) reached.",
            "max_turns":    f"Stopped: max turns ({self.max_turns}) reached.",
            # TODO: "aborted": "Stopped: agent was aborted by leader."
            # TODO: "timeout": "Stopped: execution timeout."
        }
        text = messages.get(stop_reason)
        if text:
            emit(StreamError(run_id=run_id, message=text, detail=stop_reason))

    def _should_retry(self, exc: Exception, attempt: int) -> bool:
        if attempt >= self.max_retries:
            return False
        # TODO: check rate_limit error type từ provider
        # TODO: check transient network errors
        # TODO: check server 5xx
        return False

    async def _recover(self, exc: Exception, context: RunContext) -> bool:
        # TODO: handle prompt_too_long → truncate context.messages
        # TODO: handle max_output_tokens → inject nudge message
        return False

    async def run(self, context: RunContext) -> RunUsage:
        ceiling     = context.max_tool_calls or self.max_tool_calls
        start_time  = time.perf_counter()
        usage       = RunUsage(model=self.provider.model)
        stop_reason = "end_turn"

        def emit(event: Event) -> None:
            context.handler.handle(event)

        get_trace_store().begin_run(
            run_id=context.run_id,
            parent_run_id=context.parent_run_id,
            session_id=context.session_id,
            agent_id=context.agent_id,
            agent_role=context.agent_role,
            model=self.provider.model,
        )

        emit(StreamStart(
            run_id=context.run_id,
            session_id=context.session_id,
            model=self.provider.model,
        ))

        log_slice_start = len(context.messages)
        context.messages.append({
            "role": "user",
            "content": context.user_message,
        })

        tool_call_count = 0
        turn_num        = 0

        try:
            while True:
                turn_num   += 1
                should_stop = False
                pending_tool_uses: list[ToolUse] = []
                text_chunks: list[str] = []

                emit(TurnStart(
                    run_id=context.run_id,
                    turn_num=turn_num,
                ))

                def on_event(event: Event) -> None:
                    if event.type == EventType.TOOL_USE:
                        pending_tool_uses.append(event)
                    elif event.type == EventType.TEXT_DELTA:
                        text_chunks.append(event.text)
                    emit(event)

                # ── Stream with retry ─────────────────────────────────────
                attempt = 0
                while True:
                    try:
                        turn_usage = await self.provider.stream(
                            messages=context.messages,
                            system=context.system_prompt,
                            on_event=on_event,
                            turn_num=turn_num,
                            tools=context.tool_registry.schemas,
                        )
                        break
                    except Exception as exc:
                        if self._should_retry(exc, attempt):
                            attempt += 1
                            emit(RetryAttempt(
                                run_id=context.run_id,
                                turn_num=turn_num,
                                attempt=attempt,
                                reason=str(exc),
                                error_type=type(exc).__name__,
                            ))
                            # TODO: exponential backoff sleep
                            continue

                        recovered = await self._recover(exc, context)
                        if recovered:
                            attempt = 0
                            emit(RecoveryApplied(
                                run_id=context.run_id,
                                turn_num=turn_num,
                                reason=str(exc),
                                error_type=type(exc).__name__,
                            ))
                            continue

                        raise

                usage.add_turn(turn_usage)

                emit(TurnEnd(
                    run_id=context.run_id,
                    turn_num=turn_num,
                    input_tokens=turn_usage.input_tokens,
                    output_tokens=turn_usage.output_tokens,
                    tool_call_count=len(pending_tool_uses),
                ))

                assistant_text = "".join(text_chunks)

                # ── No tool calls → done ──────────────────────────────────
                if not pending_tool_uses:
                    if assistant_text:
                        context.messages.append({
                            "role": "assistant",
                            "content": assistant_text,
                        })
                    stop_reason = "end_turn"
                    break

                # ── Has tool calls ────────────────────────────────────────
                if assistant_text:
                    context.messages.append({
                        "role": "assistant",
                        "content": assistant_text,
                    })
                context.messages.append({
                    "role": "assistant",
                    "content": _tool_use_blocks(pending_tool_uses),
                })

                tool_results = []
                for tool_event in pending_tool_uses:
                    tool_call_count += 1
                    usage.total_tool_calls += 1

                    tool_results.append(await _execute_tool(
                        tool_event=tool_event,
                        context=context,
                        emit=emit,
                        provider=self.provider,
                    ))

                    should_stop, stop_reason = self._should_stop(
                        tool_call_count, ceiling, turn_num,
                    )
                    if should_stop:
                        break

                context.messages.append({
                    "role": "user",
                    "content": tool_results,
                })

                if should_stop:
                    self._emit_stop_reason(stop_reason, context.run_id, emit)
                    break

                continue

        except Exception as exc:
            context.last_stop_reason = "error"
            get_trace_store().finish_run(
                run_id=context.run_id,
                usage=usage,
                stop_reason="error",
                status="error",
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            emit(StreamError(
                run_id=context.run_id,
                turn_num=turn_num,
                message=str(exc),
                detail=type(exc).__name__,
            ))
            return usage

        elapsed_ms = (time.perf_counter() - start_time) * 1000
        context.last_stop_reason = stop_reason
        get_trace_store().finish_run(
            run_id=context.run_id,
            usage=usage,
            stop_reason=stop_reason,
            status="completed" if stop_reason == "end_turn" else "stopped",
        )
        emit(StreamEnd(
            run_id=context.run_id,
            stop_reason=stop_reason,
            total_input_tokens=usage.total_input_tokens,
            total_output_tokens=usage.total_output_tokens,
            total_cache_write_tokens=usage.total_cache_write_tokens,
            total_cache_read_tokens=usage.total_cache_read_tokens,
            total_tool_calls=usage.total_tool_calls,
            estimated_cost_usd=usage.estimated_cost_usd,
            elapsed_ms=elapsed_ms,
        ))

        if context.log_conversation:
            try:
                from ..heartbeat.conversation_log import (
                    append_conversation_line,
                    flatten_messages_for_log,
                )
                for role, text in flatten_messages_for_log(
                    context.messages[log_slice_start:]
                ):
                    append_conversation_line(
                        session_id=context.session_id,
                        role=role,
                        text=text,
                        agent_id=context.agent_id,
                    )
            except Exception:
                pass

        return usage


# ── Pure helpers ──────────────────────────────────────────────────────────────

async def _execute_tool(
    tool_event: ToolUse,
    context:    RunContext,
    emit:       callable,
    provider:   LLMProvider,
) -> dict:
    """
    Dispatch a tool call through the registry.
    Runner doesn't know what tools exist — registry resolves everything.
    """
    tool_name = tool_event.name
    tool_input = tool_event.input
    tool_fn = context.tool_registry.get(tool_name)

    if tool_fn is None:
        output = ToolOutput(
            output=f"Error: unknown tool '{tool_name}'. Available: {context.tool_registry.names}",
            exit_code=1,
        )
    else:
        try:
            output = await tool_fn(tool_input, context)
        except Exception as exc:
            output = ToolOutput(
                output=f"Tool '{tool_name}' failed: {exc}",
                exit_code=1,
            )

    # ── Heartbeat hook (bash tools only) ──────────────────────────────────
    if tool_name == "act":
        try:
            from ..heartbeat.tool_hooks import maybe_enqueue_remediation
            rendered_command = summarize_action(tool_input)
            maybe_enqueue_remediation(
                command=rendered_command,
                rendered_output=output.output,
                exit_code=output.exit_code,
            )
        except Exception:
            pass

    emit(ToolResult(
        tool_id=tool_event.tool_id,
        command=summarize_action(tool_input) if tool_name == "act" else tool_name,
        output=output.output,
        exit_code=output.exit_code,
        elapsed_ms=output.elapsed_ms,
        has_image=output.image is not None,
    ))

    return provider.format_tool_result(
        tool_id=tool_event.tool_id,
        output=output.output,
        image=output.image,
    )


def _tool_use_blocks(tool_uses: list[ToolUse]) -> list[dict]:
    """Build tool_use content blocks for the message history."""
    return [
        {
            "type":  "tool_use",
            "id":    t.tool_id,
            "name":  t.name,
            "input": t.input,
        }
        for t in tool_uses
    ]
