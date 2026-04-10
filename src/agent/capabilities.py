from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from ..cli_handler.dispatch import tokenize
from ..cli_handler.result import Result
from ..cli_handler.router import _split_chain
from ..multi_agent.spawn import spawn_sub_agent
from ..skills.loader import SkillLoader


@dataclass(frozen=True)
class CapabilityPolicy:
    allowed_ops: frozenset[str]
    allowed_commands: frozenset[str] = frozenset()
    blocked_commands: frozenset[str] = frozenset()


TOP_LEVEL_OPS = frozenset({
    "run_command",
    "read_file",
    "list_dir",
    "search_files",
    "write_file",
    "append_file",
    "load_skill",
    "spawn_agent",
})

RESTRICTED_OPS = frozenset({
    "run_allowed_command",
    "read_file",
    "list_dir",
    "search_files",
    "load_skill",
})


def make_top_level_policy() -> CapabilityPolicy:
    return CapabilityPolicy(allowed_ops=TOP_LEVEL_OPS)


def make_restricted_policy(
    *,
    allowed_commands: list[str] | None = None,
    blocked_commands: list[str] | None = None,
) -> CapabilityPolicy:
    return CapabilityPolicy(
        allowed_ops=RESTRICTED_OPS,
        allowed_commands=frozenset(c.lower() for c in (allowed_commands or [])),
        blocked_commands=frozenset(c.lower() for c in (blocked_commands or [])),
    )


def make_act_schema(policy: CapabilityPolicy) -> dict:
    descriptions = {
        "run_command": "Run a CLI command through the router. Only for trusted agents.",
        "run_allowed_command": "Run a CLI command only if every segment stays inside the allowed command set.",
        "read_file": "Read a text file from disk.",
        "list_dir": "List directory contents.",
        "search_files": "Search text files under a directory for a query string or regex.",
        "write_file": "Write text to a file.",
        "append_file": "Append text to a file.",
        "load_skill": "Load a skill's full instructions into the conversation.",
        "spawn_agent": "Spawn a nested agent to handle a task.",
    }
    allowed_ops = sorted(policy.allowed_ops)
    allowed_block = "\n".join(f"  - {op}: {descriptions[op]}" for op in allowed_ops)
    return {
        "description": (
            "Dispatch one structured capability.\n\n"
            "Available operations:\n"
            f"{allowed_block}"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "op": {
                    "type": "string",
                    "enum": allowed_ops,
                    "description": "Which structured capability to execute.",
                },
                "command": {"type": "string"},
                "path": {"type": "string"},
                "content": {"type": "string"},
                "root": {"type": "string"},
                "query": {"type": "string"},
                "glob": {"type": "string"},
                "name": {"type": "string"},
                "role": {"type": "string"},
                "task": {"type": "string"},
                "model": {"type": "string"},
                "system_prompt": {"type": "string"},
                "max_turns": {"type": "integer"},
                "max_tools": {"type": "integer"},
            },
            "required": ["op"],
        },
    }


async def dispatch_act(params: dict, context, policy: CapabilityPolicy):
    from .tools import ToolOutput

    op = str(params.get("op", "")).strip()
    if not op:
        return ToolOutput(output="act: missing required field 'op'", exit_code=1)
    if op not in policy.allowed_ops:
        return ToolOutput(
            output=f"act: operation '{op}' is not allowed. Allowed: {sorted(policy.allowed_ops)}",
            exit_code=1,
        )

    t0 = time.perf_counter()
    try:
        if op == "run_command":
            result = await context.executor.exec(str(params.get("command", "")))
            return ToolOutput(
                output=result.render(),
                exit_code=result.exit,
                image=getattr(result, "image", None),
                elapsed_ms=(time.perf_counter() - t0) * 1000,
            )

        if op == "run_allowed_command":
            command = str(params.get("command", ""))
            err = _validate_restricted_command(
                command,
                allowed=policy.allowed_commands,
                blocked=policy.blocked_commands,
            )
            if err is not None:
                return ToolOutput(
                    output=err.render(),
                    exit_code=err.exit,
                    elapsed_ms=(time.perf_counter() - t0) * 1000,
                )
            result = await context.executor.exec(command)
            return ToolOutput(
                output=result.render(),
                exit_code=result.exit,
                image=getattr(result, "image", None),
                elapsed_ms=(time.perf_counter() - t0) * 1000,
            )

        if op == "read_file":
            path = Path(str(params.get("path", ""))).expanduser()
            text = path.read_text(encoding="utf-8", errors="replace")
            return ToolOutput(output=text, elapsed_ms=(time.perf_counter() - t0) * 1000)

        if op == "list_dir":
            root = Path(str(params.get("path") or params.get("root") or ".")).expanduser()
            if not root.exists():
                return ToolOutput(output=f"list_dir: path not found: {root}", exit_code=1, elapsed_ms=(time.perf_counter() - t0) * 1000)
            if not root.is_dir():
                return ToolOutput(output=f"list_dir: not a directory: {root}", exit_code=1, elapsed_ms=(time.perf_counter() - t0) * 1000)
            lines = []
            for entry in sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                suffix = "/" if entry.is_dir() else ""
                lines.append(f"{entry.name}{suffix}")
            return ToolOutput(output="\n".join(lines), elapsed_ms=(time.perf_counter() - t0) * 1000)

        if op == "search_files":
            query = str(params.get("query", ""))
            root = Path(str(params.get("root", "."))).expanduser()
            glob = str(params.get("glob", "*"))
            if not query:
                return ToolOutput(output="search_files: missing query", exit_code=1, elapsed_ms=(time.perf_counter() - t0) * 1000)
            matches = _search_files(root=root, query=query, glob=glob)
            return ToolOutput(output=matches, elapsed_ms=(time.perf_counter() - t0) * 1000)

        if op == "write_file":
            path = Path(str(params.get("path", ""))).expanduser()
            content = str(params.get("content", ""))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return ToolOutput(output=f"wrote {len(content)} chars -> {path}", elapsed_ms=(time.perf_counter() - t0) * 1000)

        if op == "append_file":
            path = Path(str(params.get("path", ""))).expanduser()
            content = str(params.get("content", ""))
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(content)
            return ToolOutput(output=f"appended {len(content)} chars -> {path}", elapsed_ms=(time.perf_counter() - t0) * 1000)

        if op == "load_skill":
            name = str(params.get("name", "")).strip()
            if not name:
                return ToolOutput(output="load_skill: missing name", exit_code=1, elapsed_ms=(time.perf_counter() - t0) * 1000)
            skill = SkillLoader().load(name)
            if skill is None:
                return ToolOutput(output=f"load_skill: skill not found: {name}", exit_code=1, elapsed_ms=(time.perf_counter() - t0) * 1000)
            content = skill.content.strip()
            if not content:
                return ToolOutput(output=f"[SKILL ACTIVE: {skill.name}]\n(no body)\n[END SKILL: {skill.name}]", elapsed_ms=(time.perf_counter() - t0) * 1000)
            return ToolOutput(
                output=f"[SKILL ACTIVE: {skill.name}]\n{content}\n[END SKILL: {skill.name}]",
                elapsed_ms=(time.perf_counter() - t0) * 1000,
            )

        if op == "spawn_agent":
            role = str(params.get("role", "")).strip() or "worker"
            task = str(params.get("task", "")).strip()
            if not task:
                return ToolOutput(output="spawn_agent: missing task", exit_code=1, elapsed_ms=(time.perf_counter() - t0) * 1000)
            result = await spawn_sub_agent(
                task=task,
                role=role,
                model=_none_if_empty(params.get("model")),
                system_prompt=_none_if_empty(params.get("system_prompt")),
                max_turns=_maybe_int(params.get("max_turns")),
                max_tool_calls=_maybe_int(params.get("max_tools")),
            )
            return ToolOutput(
                output=result.render(),
                exit_code=result.exit,
                elapsed_ms=(time.perf_counter() - t0) * 1000,
            )

        return ToolOutput(output=f"act: unhandled operation '{op}'", exit_code=1, elapsed_ms=(time.perf_counter() - t0) * 1000)
    except Exception as exc:
        return ToolOutput(
            output=f"act {op}: {type(exc).__name__}: {exc}",
            exit_code=1,
            elapsed_ms=(time.perf_counter() - t0) * 1000,
        )


def summarize_action(params: dict) -> str:
    op = str(params.get("op", "")).strip() or "act"
    if op in {"run_command", "run_allowed_command"}:
        return f"{op}({params.get('command', '')!r})"
    if op in {"read_file", "write_file", "append_file", "list_dir"}:
        return f"{op}({params.get('path') or params.get('root')!r})"
    if op == "search_files":
        return f"search_files(query={params.get('query', '')!r}, root={params.get('root', '.')!r})"
    if op == "load_skill":
        return f"load_skill({params.get('name', '')!r})"
    if op == "spawn_agent":
        return f"spawn_agent(role={params.get('role', 'worker')!r}, task={params.get('task', '')!r})"
    return op


def _validate_restricted_command(
    command: str,
    *,
    allowed: frozenset[str],
    blocked: frozenset[str],
) -> Result | None:
    if not command.strip():
        return Result(stdout="run_allowed_command: command is empty", exit=1)
    if "<<" in command:
        return Result(stdout="run_allowed_command: heredoc is not allowed for restricted agents", exit=1)
    if re.search(r"(^|\\s)>>?(\\s|$)", command):
        return Result(stdout="run_allowed_command: output redirection is not allowed for restricted agents", exit=1)

    for _, segment in _split_chain(command):
        tokens = tokenize(segment)
        if not tokens:
            continue
        if any(tok in {">", ">>"} or tok.startswith("2>") for tok in tokens):
            return Result(stdout="run_allowed_command: output redirection is not allowed for restricted agents", exit=1)
        cmd_name = tokens[0].lower()
        if cmd_name in blocked:
            return Result(stdout=f"run_allowed_command: '{cmd_name}' is blocked", exit=1)
        if allowed and cmd_name not in allowed:
            return Result(
                stdout=f"run_allowed_command: '{cmd_name}' is not in the allowed command set: {sorted(allowed)}",
                exit=1,
            )
    return None


def _search_files(*, root: Path, query: str, glob: str) -> str:
    if not root.exists():
        return f"search_files: root not found: {root}"

    limit = 200
    hits: list[str] = []
    use_regex = any(ch in query for ch in ".^$*+?{}[]|()")
    pattern = re.compile(query, re.IGNORECASE) if use_regex else None

    for path in root.rglob(glob):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            matched = pattern.search(line) if pattern else query.lower() in line.lower()
            if not matched:
                continue
            hits.append(f"{path}:{lineno}: {line}")
            if len(hits) >= limit:
                hits.append(f"... truncated after {limit} matches")
                return "\n".join(hits)

    return "\n".join(hits) if hits else f"(no matches for {query!r} under {root})"


def _maybe_int(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _none_if_empty(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None
