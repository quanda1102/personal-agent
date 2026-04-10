""" swapped
─────────────────────────────
Sub-agent spawning via CLI command.

Flow:
  1. LLM calls:  run(command="spawn researcher 'analyze auth vulnerabilities'")
  2. router.py parses → dispatch.py routes → dispatch_spawn(args)
  3. dispatch_spawn parses CLI args
  4. spawn_sub_agent():
     a. load_agent(role) → AgentDef from YAML (or None for ad-hoc agents)
     b. _build_agent_prompt() → assemble system prompt from AgentDef + skills
     c. AgentScopedExecutor(parent_executor, allowed, blocked)
        → runtime enforcement, sub-agent CANNOT run blocked commands
     d. RunContext with fresh messages, scoped executor, assembled prompt
     e. runner.run(child_context) → same loop, different context
     f. Extract final response → return to parent

  ┌─────────────────────────────────────────────────────────────┐
  │ Parent agent (leader)                                       │
  │                                                             │
  │  run(command="spawn researcher 'analyze auth'")             │
  │       │                                                     │
  │       ▼                                                     │
  │  ┌───────────────────────────────────────────────────┐      │
  │  │ spawn_sub_agent(role="researcher", task="...")    │      │
  │  │                                                   │      │
  │  │  1. load_agent("researcher") → AgentDef           │      │
  │  │     - system_prompt, allowed/blocked, skills      │      │
  │  │                                                   │      │
  │  │  2. _build_agent_prompt(agent_def)                │      │
  │  │     - agent's system_prompt                       │      │
  │  │     - + auto-loaded skills content                │      │
  │  │     - + allowed commands list (informational)     │      │
  │  │                                                   │      │
  │  │  3. AgentScopedExecutor(parent_executor)          │      │
  │  │     - runtime filter: reject blocked commands     │      │
  │  │     - LLM literally cannot run write/rm/etc       │      │
  │  │                                                   │      │
  │  │  4. RunContext(prompt, scoped_executor, ...)       │      │
  │  │  5. runner.run(child_context)                     │      │
  │  │  6. extract final response                        │      │
  │  └───────────────────────────────────────────────────┘      │
  │       │                                                     │
  │       ▼                                                     │
  │  Result(stdout="[spawn:researcher-a1b2] ...\n\nFindings:")  │
  └─────────────────────────────────────────────────────────────┘

CLI syntax:
  spawn <role> <task>                           — basic (looks up YAML)
  spawn <role> <task> --model=gpt-4o            — override model
  spawn <role> <task> --max-turns=20            — override turn limit
  spawn <role> <task> --max-tools=30            — override tool ceiling
  spawn <role> <task> --system="custom prompt"  — override system prompt
  spawn --list                                  — show available agents

  # Heredoc for long task descriptions:
  spawn researcher <<'EOF'
  Analyze the authentication system. Cover:
  1. Current architecture
  2. Known vulnerabilities
  3. Recommended fixes
  EOF

  # Ad-hoc agent (no YAML definition):
  spawn worker "fix the bug"
  → No YAML found for "worker" → uses parent's defaults, no command restrictions

TODO (Phase 3 — async + coordination):
  - spawn --background: don't wait, return agent_id immediately
  - spawn --team=<n>: register in team context
  - Mailbox: send/receive messages between agents
  - Task board: shared task list with claim/update
  - Abort: parent can kill sub-agent by agent_id
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from ..cli_handler.result import Result, ok, err, Timer

if TYPE_CHECKING:
    from ..agent.loop import Runner, RunContext
    from .agent_schema import AgentDef


# ── Singleton runner reference ─────────────────────────────────────────────────
# Set by server.py at startup. Spawn needs runner to call runner.run().
#
# Why module-level? dispatch handlers are (args: list[str]) -> Result.
# They don't receive runner/context. spawn bridges via setter.
#
# TODO: cleaner DI — SpawnContext object or service locator

_runner: Runner | None = None
_parent_context: RunContext | None = None


def set_runner(runner: "Runner", parent_context: "RunContext") -> None:
    """
    Called by server.py / CLI entrypoint before the agent loop starts.

    Usage in server.py:
        from src.coordination.spawn import set_runner
        set_runner(runner, ctx)  # before runner.run(ctx)
    """
    global _runner, _parent_context
    _runner = runner
    _parent_context = parent_context


# ── Core spawn logic ───────────────────────────────────────────────────────────

async def spawn_sub_agent(
    task: str,
    role: str = "worker",
    model: str | None = None,
    system_prompt: str | None = None,
    max_turns: int | None = None,
    max_tool_calls: int | None = None,
) -> Result:
    """
    Spawn a sub-agent that runs the same agentic loop with its own context.

    Resolution order for each config field:
      CLI flag (--model, --system, etc.)
        → AgentDef from YAML
          → parent context defaults

    Returns Result with the sub-agent's final text response as stdout.
    """
    if _runner is None or _parent_context is None:
        return err("spawn: runner not initialized. Call set_runner() at startup.")

    from ..agent.loop import RunContext
    from .agent_schema import load_agent
    from .agent_executor import AgentScopedExecutor

    agent_id = f"{role}-{uuid.uuid4().hex[:8]}"

    # ── 1. Load agent definition (None if no YAML exists) ─────────────
    agent_def = load_agent(role)

    # ── 2. Resolve config: CLI flag → AgentDef → parent defaults ──────
    resolved_prompt     = system_prompt or _build_agent_prompt(agent_def)
    resolved_max_tools  = max_tool_calls or (agent_def.max_tools if agent_def else None)
    # TODO: resolved_model — requires provider-per-agent or model field in RunContext
    # TODO: resolved_max_turns — requires max_turns field in RunContext

    # ── 3. Build scoped executor (runtime command enforcement) ─────────
    #
    # If agent_def has allowed/blocked commands → wrap parent's executor
    # with AgentScopedExecutor so sub-agent CANNOT run restricted commands.
    #
    # If no agent_def (ad-hoc spawn) → use parent's executor as-is.
    #
    # Stacking example:
    #   AgentScopedExecutor          ← checks allowed/blocked per agent
    #     → RoleScopedExecutor       ← sets execution role (from parent)
    #       → LocalExecutor          ← runs command via router.py
    #
    if agent_def and (agent_def.allowed_commands or agent_def.blocked_commands):
        scoped_executor = AgentScopedExecutor(
            inner=_parent_context.executor,
            allowed_commands=agent_def.allowed_commands,
            blocked_commands=agent_def.blocked_commands,
            agent_id=agent_id,
        )
    else:
        scoped_executor = _parent_context.executor

    # ── 4. Build child RunContext ──────────────────────────────────────
    child_context = RunContext(
        user_message     = task,
        system_prompt    = resolved_prompt,
        agent_id         = agent_id,
        agent_role       = role,
        session_id       = _parent_context.session_id,
        messages         = [],                             # fresh history
        tool_registry    = _parent_context.tool_registry,  # same tools
        executor         = scoped_executor,                # filtered executor
        handler          = _parent_context.handler,        # same event stream
        max_tool_calls   = resolved_max_tools,
        log_conversation = True,
        # TODO: abort_signal for parent to cancel child
        # TODO: parent_agent_id = _parent_context.agent_id
    )

    # ── 5. Run sub-agent ──────────────────────────────────────────────
    with Timer() as t:
        try:
            usage = await _runner.run(child_context)
        except Exception as exc:
            return Result(
                stdout=f"[error] spawn: sub-agent {agent_id} failed: {exc}",
                exit=1,
            )

    # ── 6. Extract final response ─────────────────────────────────────
    final_response = _extract_final_response(child_context.messages)

    if not final_response:
        return Result(
            stdout=f"[spawn:{agent_id}] (sub-agent produced no text response)\n{usage}",
            exit=1,
            elapsed_ms=t.elapsed_ms,
        )

    return Result(
        stdout=(
            f"[spawn:{agent_id} | {usage.total_tool_calls} tools | "
            f"{usage.total_input_tokens + usage.total_output_tokens} tokens | "
            f"{t.elapsed_ms:.0f}ms]\n\n"
            f"{final_response}"
        ),
        exit=0,
        elapsed_ms=t.elapsed_ms,
    )


# ── Prompt assembly ────────────────────────────────────────────────────────────

def _build_agent_prompt(agent_def: "AgentDef | None") -> str:
    """
    Assemble system prompt for sub-agent from AgentDef.

    Layers (appended in order):
      1. AgentDef.system_prompt (or parent's prompt if no agent_def)
      2. Auto-loaded skills content
      3. Allowed commands list (informational — runtime enforcement is separate)

    The command constraints section is informational only — it tells the LLM
    what it can/can't do to reduce wasted attempts on blocked commands.
    Real enforcement happens in AgentScopedExecutor.
    """
    if _parent_context is None:
        return ""

    parts: list[str] = []

    # ── Base prompt ───────────────────────────────────────────────────
    if agent_def and agent_def.system_prompt:
        parts.append(agent_def.system_prompt.strip())
    else:
        parts.append(_parent_context.system_prompt)

    # ── Auto-load skills ──────────────────────────────────────────────
    if agent_def and agent_def.skills:
        skills_content = _load_skills(agent_def.skills)
        if skills_content:
            parts.append(skills_content)

    # ── Command constraints (informational, for LLM awareness) ────────
    if agent_def:
        constraints = _format_command_constraints(agent_def)
        if constraints:
            parts.append(constraints)

    return "\n\n".join(parts)


def _load_skills(skill_names: list[str]) -> str:
    """Load skill content by name, returns formatted string or ""."""
    if not skill_names:
        return ""

    try:
        from ..skills.loader import SkillLoader
        loader = SkillLoader()
    except ImportError:
        return ""

    parts: list[str] = []
    for name in skill_names:
        skill = loader.load(name)
        if skill:
            parts.append(
                f"[SKILL ACTIVE: {skill.name}]\n"
                f"{skill.content.strip()}\n"
                f"[END SKILL: {skill.name}]"
            )

    return "\n\n".join(parts)


def _format_command_constraints(agent_def: "AgentDef") -> str:
    """
    Format allowed/blocked commands as informational prompt section.
    Tells LLM what it can/can't do. Runtime enforcement is separate.
    """
    lines: list[str] = []

    if agent_def.allowed_commands:
        lines.append("Available commands (you can ONLY use these):")
        lines.append("  " + ", ".join(agent_def.allowed_commands))

    if agent_def.blocked_commands:
        lines.append("Blocked commands (these will be rejected):")
        lines.append("  " + ", ".join(agent_def.blocked_commands))

    if not lines:
        return ""

    return "Command restrictions:\n" + "\n".join(lines)


# ── Response extraction ────────────────────────────────────────────────────────

def _extract_final_response(messages: list[dict]) -> str:
    """
    Walk messages backwards, find the last assistant text message.
    Skip tool_use blocks.
    """
    for msg in reversed(messages):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", "")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return ""


# ── CLI dispatch handler ──────────────────────────────────────────────────────
# Route: dispatch.py → DISPATCH_TABLE["spawn"] → dispatch_spawn(args)

def dispatch_spawn(args: list[str]) -> Result:
    """
    CLI handler for the spawn command.

    Registered in dispatch.py DISPATCH_TABLE as:
        "spawn": dispatch_spawn

    Usage:
      spawn <role> <task>
      spawn <role> <task> --model=gpt-4o --max-turns=20
      spawn --list
      spawn --help
    """
    _USAGE = (
        "spawn: spawn a sub-agent to handle a task\n\n"
        "  spawn <role> <task>                    — basic spawn\n"
        "  spawn worker 'fix the login bug'       — ad-hoc worker\n"
        "  spawn researcher 'analyze auth system'  — uses researcher.yaml\n"
        "  spawn --list                            — show available agents\n\n"
        "Flags:\n"
        "  --model=<n>          override LLM model\n"
        "  --max-turns=<n>      max turns for sub-agent\n"
        "  --max-tools=<n>      max tool calls for sub-agent\n"
        "  --system=\"<prompt>\"  override system prompt\n\n"
        "If a <role>.yaml exists in the agents directory, its config\n"
        "(system prompt, allowed commands, skills) is applied automatically.\n"
        "Otherwise the sub-agent inherits parent's config."
        # TODO: --background for async spawn
    )

    if not args or args[0] in ("--help", "-h"):
        return ok(_USAGE)

    # ── List available agents ─────────────────────────────────────────
    if args[0] == "--list":
        from .agent_schema import agent_list_prompt
        listing = agent_list_prompt()
        if not listing:
            return ok("(no agent definitions found)\ntip: create YAML files in agents/ directory")
        return ok(listing)

    # ── Parse args ────────────────────────────────────────────────────
    from ..cli_handler.dispatch import _parse_args
    positional, flags = _parse_args(args)

    if len(positional) < 2:
        return err(
            "spawn: expected <role> <task>\n"
            "  spawn worker 'fix the bug'\n"
            "  spawn --help for full usage"
        )

    role = positional[0]
    task = " ".join(positional[1:])

    model          = flags.get("model")
    system_prompt  = flags.get("system")
    max_turns      = _safe_int(flags.get("max-turns"))
    max_tool_calls = _safe_int(flags.get("max-tools"))

    # ── Run async spawn from sync dispatch context ────────────────────
    return _run_async_from_sync(
        spawn_sub_agent(
            task=task,
            role=role,
            model=model,
            system_prompt=system_prompt,
            max_turns=max_turns,
            max_tool_calls=max_tool_calls,
        )
    )


# ── Helpers ────────────────────────────────────────────────────────────────────

def _safe_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _run_async_from_sync(coro) -> Result:
    """
    Bridge async spawn into sync dispatch handler.
    TODO: if dispatch moves to async handlers, remove this wrapper.
    """
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(asyncio.run, coro)
        return future.result()