"""
─────────────────────────
Tools — tool registry and execution types.

Separates tool logic from the agentic loop.
Runner only knows: registry.get(name) → call → ToolOutput.
Runner never knows what tools exist or how they work.

Multi-agent extension point:
  registry.register("send_message", send_message_tool)
  registry.register("task_claim",   task_claim_tool)
  registry.register("spawn_agent",  spawn_agent_tool)
  — loop.py stays untouched.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol


# ── Types ──────────────────────────────────────────────────────────────────────

class HasExecutor(Protocol):
    """Minimal interface tools.py needs from RunContext. No circular import."""
    executor: Any


ToolFunction = Callable[[dict, Any], Awaitable["ToolOutput"]]
# Signature: async def my_tool(params: dict, context: RunContext) -> ToolOutput
# 'Any' here avoids circular import. Concrete type is always RunContext at runtime.


@dataclass
class ToolOutput:
    """
    Normalised result from any tool — bash, coordination, or custom.
    Every tool returns this; Runner doesn't care what produced it.
    """
    output:     str
    exit_code:  int = 0
    image:      Any = None
    elapsed_ms: float = 0.0
    # TODO: metadata dict cho structured data (e.g. task_id từ task_claim)
    # TODO: side_effects list để track những gì tool đã thay đổi


# ── Registry ───────────────────────────────────────────────────────────────────

class ToolRegistry:
    """
    Name → async callable mapping.
    Runner calls registry.get(name), gets a function, calls it.
    Tools are added at setup time, not at loop time.

    Later phases:
      - agent-specific registries (leader gets spawn, workers don't)
      - permission checks per tool
      - tool schemas for LLM function calling
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolFunction] = {}
        self._schemas: dict[str, dict] = {}
        # TODO: per-tool permission level (auto / confirm / deny)
        # TODO: per-tool timeout override

    def register(self, name: str, fn: ToolFunction, schema: dict | None = None) -> None:
        """Register a tool. schema is the JSON schema the LLM sees for this tool."""
        self._tools[name] = fn
        if schema is not None:
            self._schemas[name] = schema

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)
        self._schemas.pop(name, None)

    def get(self, name: str) -> ToolFunction | None:
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        return name in self._tools

    @property
    def names(self) -> list[str]:
        return list(self._tools.keys())

    @property
    def schemas(self) -> list[dict]:
        """Tool definitions to send to LLM in the API call."""
        return [
            {"name": name, **self._schemas[name]}
            for name in self._tools
            if name in self._schemas
        ]

    def merge(self, other: "ToolRegistry") -> "ToolRegistry":
        """
        Return new registry = self + other (other wins on conflicts).
        Useful for: base registry + coordination tools for a specific agent.
        """
        merged = ToolRegistry()
        for name in self._tools:
            merged.register(name, self._tools[name], self._schemas.get(name))
        for name in other._tools:
            merged.register(name, other._tools[name], other._schemas.get(name))
        return merged
        # TODO: warn on conflict nếu cần debug


# ── Built-in tools ─────────────────────────────────────────────────────────────

RUN_TOOL_SCHEMA = {
    "description": "Run a shell command.",
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute.",
            },
        },
        "required": ["command"],
    },
}


async def _run_tool(params: dict, context: "RunContext") -> ToolOutput:
    """Execute a shell command via context.executor."""
    command = params.get("command", "")
    t0 = time.perf_counter()
    result = await context.executor.exec(command)
    elapsed = (time.perf_counter() - t0) * 1000
    return ToolOutput(
        output=result.render(),
        exit_code=result.exit,
        image=getattr(result, "image", None),
        elapsed_ms=elapsed,
    )


# ── Factory ────────────────────────────────────────────────────────────────────

def make_default_registry() -> ToolRegistry:
    """
    Base registry — bash only.
    Multi-agent: merge with coordination registry at spawn time.

    Example:
        base = make_default_registry()
        coord = make_coordination_registry(mailbox, task_board)
        agent_registry = base.merge(coord)
    """
    registry = ToolRegistry()
    registry.register("run", _run_tool, RUN_TOOL_SCHEMA)
    return registry