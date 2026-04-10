"""
─────────────────────────────────
Agent definition — what an agent IS before it runs.

An AgentDef describes an agent's identity, capabilities, and constraints.
It does NOT contain runtime state (messages, abort signals, etc.) —
that lives in RunContext.

Think of it as a job description:
  AgentDef  = "who you are, what you can do, how you behave"
  RunContext = "the actual work session"

Storage: YAML files in an agents/ directory.
Discovery: scan folder, parse YAML, validate, return list.
Spawn reads AgentDef → builds RunContext with the right config.

File format (agents/researcher.yaml):
─────────────────────────────────────
  identifier: researcher
  description: "Deep analysis and investigation agent"
  when_to_use: >
    Use when the task requires research, analysis, or investigation.
    Examples: codebase audit, security review, literature survey.
    (phân tích, nghiên cứu, điều tra)

  system_prompt: >
    You are an expert researcher. You analyze problems deeply,
    gather evidence systematically, and present findings clearly.
    Always cite sources and show your reasoning.

  allowed_commands:
    - memory
    - note
    - skills
    - cat
    - grep
    - find
    - curl

  blocked_commands:
    - write
    - append
    - rm
    - mv

  skills:
    - research
    - note-taking

  model: null          # null = same as parent
  max_turns: 20
  max_tools: 30
  background: false    # Phase 3: true = async spawn

Flow:
  1. Leader LLM sees agent list in system prompt (from agent_list_prompt())
  2. Leader calls: run(command="spawn researcher 'analyze auth system'")
  3. spawn.py: dispatch_spawn() → load_agent("researcher") → AgentDef
  4. spawn.py: build RunContext from AgentDef fields
  5. runner.run(child_context)

TODO (Phase 3+):
  - agent memory: per-agent persistent notes across spawns
  - agent templates: inherit from base agent, override fields
  - dynamic agents: leader creates YAML at runtime via write command
  - agent plugins: load from external packages
  - permission_mode: "auto" | "confirm" | "plan_first"
  - isolation: "shared" | "sandboxed" (separate executor)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..agent.exec_role import (
    ROLE_CONVERSATION,
    ROLE_FULL,
    ROLE_HEARTBEAT,
    ROLE_VAULT_EDITOR,
)


# ── Agent definition dataclass ─────────────────────────────────────────────────

@dataclass
class AgentDef:
    """
    Static definition of an agent — loaded from YAML, immutable at runtime.

    Fields:
      identifier        — unique name, used in spawn command: spawn <identifier> <task>
      description        — one-line description for agent list prompt
      when_to_use        — semantic hint for leader LLM (when to spawn this agent)
                           bilingual recommended (EN + VI) for better intent matching
      system_prompt      — full system prompt injected into sub-agent's RunContext
      allowed_commands   — whitelist of commands this agent can use
                           empty list = all commands allowed (no restriction)
      blocked_commands   — blacklist of commands this agent CANNOT use
                           applied ON TOP of allowed_commands
      skills             — skill names to auto-load into agent's system prompt
      model              — LLM model override (None = same as parent)
      max_turns          — max turns for this agent (None = runner default)
      max_tools          — max tool calls for this agent (None = runner default)
      background         — Phase 3: if True, spawn async (don't block parent)
    """
    identifier:       str
    description:      str              = ""
    when_to_use:      str              = ""
    system_prompt:    str              = ""
    allowed_commands: list[str]        = field(default_factory=list)
    blocked_commands: list[str]        = field(default_factory=list)
    skills:           list[str]        = field(default_factory=list)
    model:            str | None       = None
    max_turns:        int | None       = None
    max_tools:        int | None       = None
    background:       bool             = False
    execution_role:   str | None       = None
    # TODO: permission_mode: str = "auto"   # "auto" | "confirm" | "plan_first"
    # TODO: isolation: str = "shared"        # "shared" | "sandboxed"
    # TODO: memory_instructions: str = ""    # per-agent memory guidance
    # TODO: inherit_from: str | None = None  # template inheritance


def validate_agent_def(agent: AgentDef) -> list[str]:
    """
    Validate an AgentDef. Returns list of error messages (empty = valid).
    """
    errors: list[str] = []

    if not agent.identifier:
        errors.append("identifier is required")
    elif not all(c.isalnum() or c in "-_" for c in agent.identifier):
        errors.append(f"identifier '{agent.identifier}' must be alphanumeric with hyphens/underscores only")

    if agent.max_turns is not None and agent.max_turns < 1:
        errors.append(f"max_turns must be >= 1, got {agent.max_turns}")

    if agent.max_tools is not None and agent.max_tools < 1:
        errors.append(f"max_tools must be >= 1, got {agent.max_tools}")

    if agent.execution_role is not None and agent.execution_role not in {
        ROLE_FULL,
        ROLE_CONVERSATION,
        ROLE_HEARTBEAT,
        ROLE_VAULT_EDITOR,
    }:
        errors.append(f"unknown execution_role: {agent.execution_role!r}")

    # Check for conflicts: same command in both allowed and blocked
    if agent.allowed_commands and agent.blocked_commands:
        overlap = set(agent.allowed_commands) & set(agent.blocked_commands)
        if overlap:
            errors.append(f"commands in both allowed and blocked: {overlap}")

    return errors


# ── YAML loading ───────────────────────────────────────────────────────────────

def load_agent(identifier: str, agents_dir: Path | None = None) -> AgentDef | None:
    """
    Load a single agent definition by identifier.

    Search order:
      1. agents_dir / <identifier>.yaml
      2. agents_dir / <identifier>.yml
      3. Return None if not found

    Args:
        identifier:  agent name (e.g. "researcher")
        agents_dir:  path to agents directory. None = auto-discover.

    Returns:
        AgentDef if found and valid, None otherwise.
    """
    agents_dir = _resolve_agents_dir(agents_dir)
    if agents_dir is None:
        return None

    for ext in (".yaml", ".yml"):
        path = agents_dir / f"{identifier}{ext}"
        if path.is_file():
            return _parse_agent_file(path)

    ident_lower = identifier.lower()
    for path in agents_dir.iterdir():
        if not path.is_file():
            continue
        if path.suffix not in (".yaml", ".yml"):
            continue
        if path.stem.lower() == ident_lower:
            return _parse_agent_file(path)

    return None


def discover_agents(agents_dir: Path | None = None) -> list[AgentDef]:
    """
    Discover all agent definitions in the agents directory.

    Returns list of valid AgentDefs, sorted by identifier.
    Silently skips invalid files (logs warning to stderr).
    """
    agents_dir = _resolve_agents_dir(agents_dir)
    if agents_dir is None:
        return []

    agents: list[AgentDef] = []

    for path in sorted(agents_dir.iterdir()):
        if path.suffix not in (".yaml", ".yml"):
            continue
        if path.name.startswith(".") or path.name.startswith("_"):
            continue

        agent = _parse_agent_file(path)
        if agent is not None:
            agents.append(agent)

    return agents


def agent_list_prompt(agents_dir: Path | None = None) -> str:
    """
    Generate agent list for injection into leader's system prompt.
    Similar to command_list_prompt() in dispatch.py — gives leader
    enough context to know which agents exist and when to use them.

    Format:
      Available agents (use: spawn <identifier> <task>):
        researcher      — Deep analysis and investigation agent
                          Use when: research, analysis, investigation
        coder           — Code implementation agent
                          Use when: writing code, fixing bugs
    """
    agents = discover_agents(agents_dir)
    if not agents:
        return ""

    lines = [
        "Available agents (spawn with: spawn <identifier> <task description>):",
    ]

    for agent in agents:
        lines.append(f"  {agent.identifier:<16} — {agent.description}")
        if agent.when_to_use:
            # Indent when_to_use under the agent, truncate if very long
            hint = agent.when_to_use.strip().replace("\n", " ")
            if len(hint) > 120:
                hint = hint[:117] + "..."
            lines.append(f"  {'':<16}   When: {hint}")

    lines.append("")
    lines.append("Spawn flags:  --model=<n>  --max-turns=<n>  --max-tools=<n>")
    # TODO: --background flag documentation

    return "\n".join(lines)


# ── Internal helpers ───────────────────────────────────────────────────────────

def _resolve_agents_dir(agents_dir: Path | None) -> Path | None:
    """
    Resolve the agents directory path.

    Priority:
      1. Explicit agents_dir argument
      2. OPENCLAWD_AGENTS_DIR env var
      3. {HOMEAGENT_VAULT}/.agents/
      4. ./.agents/ relative to cwd
      5. ./agents/ relative to cwd
    """
    if agents_dir is not None:
        return agents_dir if agents_dir.is_dir() else None

    # Env var
    env_dir = os.environ.get("OPENCLAWD_AGENTS_DIR")
    if env_dir:
        p = Path(env_dir)
        if p.is_dir():
            return p

    # Vault-based
    vault = os.environ.get("HOMEAGENT_VAULT") or os.environ.get("OBSIDIAN_VAULT")
    if vault:
        p = Path(vault) / ".agents"
        if p.is_dir():
            return p

    # CWD fallback: dotdir first, then plain dir
    p = Path.cwd() / ".agents"
    if p.is_dir():
        return p

    p = Path.cwd() / "agents"
    if p.is_dir():
        return p

    return None


def _parse_agent_file(path: Path) -> AgentDef | None:
    """
    Parse a single YAML agent definition file.
    Returns AgentDef if valid, None if parse/validation fails.
    """
    try:
        text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
    except Exception as e:
        import sys
        print(f"[warn] agent_def: failed to parse {path}: {e}", file=sys.stderr)
        return None

    if not isinstance(data, dict):
        import sys
        print(f"[warn] agent_def: {path} is not a YAML mapping", file=sys.stderr)
        return None

    # ── Map YAML fields to AgentDef ───────────────────────────────────────
    agent = AgentDef(
        identifier       = data.get("identifier", path.stem),
        description      = data.get("description", ""),
        when_to_use      = data.get("when_to_use", ""),
        system_prompt    = data.get("system_prompt", ""),
        allowed_commands = data.get("allowed_commands", []),
        blocked_commands = data.get("blocked_commands", []),
        skills           = data.get("skills", []),
        model            = data.get("model"),
        max_turns        = data.get("max_turns"),
        max_tools        = data.get("max_tools"),
        background       = data.get("background", False),
        execution_role   = data.get("execution_role"),
    )

    # ── Validate ──────────────────────────────────────────────────────────
    errors = validate_agent_def(agent)
    if errors:
        import sys
        print(f"[warn] agent_def: {path}: {'; '.join(errors)}", file=sys.stderr)
        return None

    return agent
