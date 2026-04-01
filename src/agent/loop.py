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

                    LLM: run(command)
                              ↓
                    context.executor.exec(command)
                              ↓
            local: engine_run()  OR  ssh: asyncssh.run()
                              ↓
                    Result(stdout="...", exit=0)
                              ↓
                    LLM sees plain text output only

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
    Event, EventType, StreamStart, ToolUse, ToolResult, StreamEnd, StreamError,
)
from .executor import Executor, LocalExecutor
from .handler import StreamHandler, SilentHandler
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
      run_id          — unique trace ID (never sent to LLM)
      session_id      — session ID (never sent to LLM)
      messages        — conversation history; mutated in place during run,
                        carry forward into next RunContext for multi-turn
      executor        — WHERE commands run: LocalExecutor or SSHExecutor
                        credentials live entirely inside the executor
      handler         — WHERE events go: CLI, WebSocket, silent, composite
      max_tool_calls  — per-run ceiling override (None = use Runner default)
    """
 
    user_message:   str
    system_prompt:  str              = ""
 
    # ── Tracing — system-only, never sent to LLM ──────────────────────────────
    run_id:         str              = field(default_factory=lambda: str(uuid.uuid4()))
    session_id:     str              = "default"
 
    # ── Conversation state — mutated by runner, carry forward for multi-turn ──
    messages:       list[dict]       = field(default_factory=list)
 
    # ── Execution layer — credentials hidden inside executor ──────────────────
    executor:       Executor         = field(default_factory=LocalExecutor)
 
    # ── Output layer ──────────────────────────────────────────────────────────
    handler:        StreamHandler    = field(default_factory=SilentHandler)
 
    # ── Per-run ceiling override ───────────────────────────────────────────────
    max_tool_calls: int | None       = None

    # ── Append turns to {VAULT}/.heartbeat/conversation.jsonl when vault is set
    log_conversation: bool           = True
 
 
# ── Runner — stateless agentic loop ───────────────────────────────────────────
 
class Runner:
    """
    Stateless agentic loop engine.
 
    Runner holds no state — all state lives in RunContext.
    One Runner instance can serve many concurrent sessions.
 
    Usage:
        runner = Runner(provider=ClaudeProvider())
 
        # Build prompt separately — loop doesn't care how
        prompt = PromptBuilder(workspace, active_skills=["weather"]).build()
 
        ctx = RunContext(
            user_message  = "what's the weather?",
            system_prompt = prompt,
            handler       = CLIStreamHandler(),
        )
        await runner.run(ctx)
 
        # Multi-turn: carry messages forward
        ctx2 = RunContext(
            user_message  = "what about tomorrow?",
            system_prompt = prompt,          # same prompt, or rebuild with new skills
            messages      = ctx.messages,    # history from previous run
            handler       = CLIStreamHandler(),
        )
        await runner.run(ctx2)
    """
 
    def __init__(
        self,
        provider:       LLMProvider,
        max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
    ):
        self.provider       = provider
        self.max_tool_calls = max_tool_calls
 
    async def run(self, context: RunContext) -> RunUsage:
        """
        Execute one user message through the agentic turn cycle.
 
        Mutates context.messages in place — caller carries it forward.
        Returns RunUsage. Events stream to context.handler throughout.
        """
        ceiling    = context.max_tool_calls or self.max_tool_calls
        start_time = time.perf_counter()
        usage      = RunUsage(model=self.provider.model)
 
        def emit(event: Event) -> None:
            context.handler.handle(event)
 
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
        stop_reason     = "end_turn"
 
        try:
            while True:
                turn_num += 1
                pending_tool_uses: list[ToolUse] = []
                text_chunks: list[str] = []

                def on_event(event: Event) -> None:
                    if event.type == EventType.TOOL_USE:
                        pending_tool_uses.append(event)
                    elif event.type == EventType.TEXT_DELTA:
                        text_chunks.append(event.text)
                    emit(event)

                turn_usage = await self.provider.stream(
                    messages=context.messages,
                    system=context.system_prompt,
                    on_event=on_event,
                    turn_num=turn_num,
                )
                usage.add_turn(turn_usage)
                stop_reason = self.provider.get_stop_reason()

                assistant_text = "".join(text_chunks)

                if pending_tool_uses:
                    # Store any text the assistant produced before tool calls
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
 
                        if tool_call_count >= ceiling:
                            stop_reason = "tool_ceiling"
                            break
 
                    context.messages.append({
                        "role": "user",
                        "content": tool_results,
                    })
 
                    if stop_reason == "tool_ceiling":
                        break
                    if stop_reason == "tool_use":
                        continue

                else:
                    # Pure text response — store the assistant's reply
                    if assistant_text:
                        context.messages.append({
                            "role": "assistant",
                            "content": assistant_text,
                        })

                break

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
                        )
                except Exception:
                    pass
 
        except Exception as exc:
            emit(StreamError(
                run_id=context.run_id,
                message=str(exc),
                detail=type(exc).__name__,
            ))
            return usage
 
        elapsed_ms = (time.perf_counter() - start_time) * 1000
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
 
        return usage
 
 
# ── Pure helpers ───────────────────────────────────────────────────────────────
 
async def _execute_tool(
    tool_event: ToolUse,
    context:    RunContext,
    emit:       callable,
    provider:   LLMProvider,
) -> dict:
    t0     = time.perf_counter()
    result = await context.executor.exec(tool_event.command)
    elapsed = (time.perf_counter() - t0) * 1000

    rendered = result.render()
    try:
        from ..heartbeat.tool_hooks import maybe_enqueue_remediation

        maybe_enqueue_remediation(
            command=tool_event.command,
            rendered_output=rendered,
            exit_code=result.exit,
        )
    except Exception:
        pass
 
    emit(ToolResult(
        tool_id=tool_event.tool_id,
        command=tool_event.command,
        output=rendered,
        exit_code=result.exit,
        elapsed_ms=elapsed,
        has_image=result.image is not None,
    ))
 
    return provider.format_tool_result(
        tool_id=tool_event.tool_id,
        output=rendered,
        image=result.image,
    )


def _tool_use_blocks(tool_uses: list[ToolUse]) -> list[dict]:
    return [
        {
            "type":  "tool_use",
            "id":    t.tool_id,
            "name":  "run",
            "input": {"command": t.command},
        }
        for t in tool_uses
    ]
 