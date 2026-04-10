"""
openclawd.cli_handler.dispatch
────────────────────────────────
Layer 1: command routing.

  tokenize(command_str) → list[str]
  dispatch(tokens)      → Result
  command_list_prompt() → str   (injected into Level-0 system prompt)

Three routing paths:
  1. Custom commands   — memory, see, write, help, etc.
                         Registered in DISPATCH_TABLE, full progressive-help.
  2. Unix whitelist    — cat, grep, ls, … → subprocess passthrough
                         Only listed commands are allowed.
  3. Unknown / blocked — error message that steers the agent to the right
                         command, never a dead-end.

Adding a new command:
  1. Add entry to CUSTOM_COMMANDS dict (name → description)
  2. Write _dispatch_<name>(args: list[str]) -> Result
  3. Add entry to DISPATCH_TABLE (name → handler)
  That's it. dispatch() and command_list_prompt() pick it up automatically.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Callable

from ..cli_handler.result import Result, ok, err, Timer
from ..multi_agent.spawn import dispatch_spawn


# ── Unix command whitelist ─────────────────────────────────────────────────────

UNIX_WHITELIST: frozenset[str] = frozenset({
    # File reading (safe, read-only)
    "cat", "head", "tail", "less", "more",
    "tac", "rev", "nl",
    # Directory listing & navigation
    "ls", "ll", "la", "find", "tree",
    "stat", "du", "df", "file",
    "mount", "lsblk",
    # Text processing
    "grep", "egrep", "fgrep", "rg",
    "awk", "sed", "cut", "tr", "column",
    "sort", "uniq", "wc",
    "diff", "comm", "patch",
    "fold", "fmt", "expand", "unexpand",
    "strings",
    # String / data tools
    "echo", "printf", "tee",
    "base64", "xxd", "od", "hexdump",
    "jq", "yq",
    "xargs",
    # Boolean helpers (used in && / || chains)
    "true", "false",
    # File manipulation
    "mkdir", "touch", "cp", "mv", "rm", "rmdir", "ln",
    # Archive
    "tar", "gzip", "gunzip", "bzip2", "bunzip2",
    "zip", "unzip", "7z",
    # System info (read-only)
    "pwd", "date", "cal", "uptime",
    "whoami", "hostname", "uname", "id",
    "env", "printenv",
    "which", "type", "whereis",
    "ps", "pgrep", "top",
    # Network (read-only / fetch)
    "curl", "wget",
    "ping", "nslookup", "dig", "host",
    # Code / package management
    "python3", "python", "pip", "pip3", "uv",
    "node", "npm", "npx",
    "git",
    # macOS extras
    "open", "pbcopy", "pbpaste",
    "defaults", "plutil",
    "sw_vers", "system_profiler",
})

UNIX_BLOCKLIST: frozenset[str] = frozenset({
    "sudo", "su", "doas", "pkexec",
    "chmod", "chown", "chgrp",
    "kill", "killall", "pkill",
    "dd", "mkfs", "fdisk", "parted",
    "iptables", "ufw", "nft", "firewall-cmd",
    "passwd", "useradd", "userdel", "usermod", "groupadd",
    "at",
    "systemctl", "service", "launchctl",
    "nc", "ncat", "netcat",
    "bash", "sh", "zsh", "fish",
    "exec",
})

# ── Per-command timeout overrides ──────────────────────────────────────────────

DEFAULT_TIMEOUT = 30

_CMD_TIMEOUTS: dict[str, int] = {
    "curl": 60, "wget": 60,
    "git": 120,
    "python3": 60, "python": 60,
    "pip": 120, "pip3": 120, "uv": 120,
    "node": 60, "npm": 120, "npx": 60,
    "find": 60,
    # TODO: make configurable via env var OPENCLAWD_CMD_TIMEOUT_<NAME>
}


# ── Custom command table ───────────────────────────────────────────────────────

CUSTOM_COMMANDS: dict[str, str] = {
    "memory":         "Search, store, update, and retrieve memories",
    "see":            "View an image file (attaches to vision)",
    "write":          "Write (overwrite) text to a file",
    "append":         "Append a line to a file (creates if missing)",
    "help":           "Show available commands",
    "skills":         "List, load and manage skills",
    "note":           "Obsidian vault: list, read, create, update, find, move, tag",
    "queue":          "Job queue: push, list, count, get, status (heartbeat / coordinator)",
    "crontab":        "User crontab (gated): crontab -l | install from .heartbeat/crontab_staging/ only",
    "my_preferences": "User thích gì", # Note that language does matter of how LLM reasoning,
    "spawn":          "Spawn a sub-agent - Khởi tạo một agent để thực hiện task"
    # TODO: "spawn"   — multi-agent: spawn sub-agent with task"""
    # openclawd.coordination.agent_executor
    # ──────────────────────────────────────
    # Runtime command enforcement for sub-agents.
    #
    # AgentScopedExecutor wraps any Executor and filters commands
    # BEFORE they reach the inner executor. This is runtime-level
    # enforcement — the sub-agent literally cannot run blocked commands,
    # regardless of what the LLM outputs.
    #
    # Two filter modes (both can be active simultaneously):
    #   allowed_commands: whitelist — ONLY these commands pass through.
    #                     Empty list = no restriction (all allowed).
    #   blocked_commands: blacklist — these commands are ALWAYS rejected.
    #                     Applied ON TOP of allowed_commands.
    #
    # Evaluation order:
    #   1. Parse first token (command name) from command string
    #   2. If blocked_commands contains it → reject
    #   3. If allowed_commands is non-empty and doesn't contain it → reject
    #   4. Otherwise → pass to inner executor
    #
    # Example:
    #   # Researcher: read-only, cannot write/delete
    #   executor = AgentScopedExecutor(
    #       inner=parent_executor,
    #       allowed_commands=["memory", "note", "cat", "grep", "find", "ls"],
    #       blocked_commands=["write", "append", "rm", "mv"],
    #   )
    #
    #   # Coder: can write, but cannot install packages or delete
    #   executor = AgentScopedExecutor(
    #       inner=parent_executor,
    #       allowed_commands=["memory", "cat", "grep", "write", "python3", "git"],
    #       blocked_commands=["rm", "pip", "npm"],
    #   )
    #
    # Stacking:
    #   AgentScopedExecutor wraps RoleScopedExecutor wraps LocalExecutor:
    #
    #     AgentScopedExecutor          ← checks allowed/blocked
    #       → RoleScopedExecutor       ← sets execution role
    #         → LocalExecutor          ← runs command via router.py
    #           → dispatch.py          ← routes to handler
    #
    #   The command must pass ALL layers to execute.
    #
    # TODO (Phase 3+):
    #   - Per-command audit log (which agent ran what)
    #   - Rate limiting per agent (max N commands per minute)
    #   - Path scoping (agent can only access certain directories)
    #   - Cost budgeting (agent has token/dollar budget)
    # """
    #
    # from __future__ import annotations
    #
    # import shlex
    #
    # from ..agent.executor import Executor
    # from ..cli_handler.result import Result
    #
    #
    # class AgentScopedExecutor(Executor):
    #     """
    #     Executor wrapper that enforces per-agent command restrictions.
    #
    #     Args:
    #         inner:            The actual executor to delegate to
    #         allowed_commands: Whitelist — only these commands can run.
    #                           Empty = no restriction.
    #         blocked_commands: Blacklist — these commands always rejected.
    #         agent_id:         For error messages and logging.
    #     """
    #
    #     def __init__(
    #         self,
    #         inner: Executor,
    #         allowed_commands: list[str] | None = None,
    #         blocked_commands: list[str] | None = None,
    #         agent_id: str = "unknown",
    #     ) -> None:
    #         self._inner = inner
    #         self._allowed = frozenset(allowed_commands) if allowed_commands else frozenset()
    #         self._blocked = frozenset(blocked_commands) if blocked_commands else frozenset()
    #         self._agent_id = agent_id
    #
    #     async def exec(self, command: str) -> Result:
    #         """
    #         Check command against allowed/blocked lists, then delegate.
    #
    #         Parses the FIRST token of the command string as the command name.
    #         Handles chain operators by checking only the first command —
    #         router.py splits chains and calls exec() per segment, so each
    #         segment arrives here as a single command.
    #
    #         NOTE: router.py calls engine_run() which calls dispatch() which
    #         calls exec() per chain segment. So by the time we see a command
    #         here, chains are already split. But heredocs and pipes may still
    #         contain the full string — we check the first token only.
    #         """
    #         cmd_name = _extract_command_name(command)
    #
    #         if not cmd_name:
    #             return await self._inner.exec(command)
    #
    #         # ── Blocked commands — absolute priority ──────────────────────────
    #         if cmd_name in self._blocked:
    #             return Result(
    #                 stdout=(
    #                     f"[error] agent {self._agent_id}: command '{cmd_name}' is blocked.\n"
    #                     f"This agent does not have permission to run '{cmd_name}'.\n"
    #                     f"Allowed commands: {', '.join(sorted(self._allowed)) if self._allowed else '(all)'}"
    #                 ),
    #                 exit=1,
    #             )
    #
    #         # ── Allowed commands — whitelist check ────────────────────────────
    #         if self._allowed and cmd_name not in self._allowed:
    #             return Result(
    #                 stdout=(
    #                     f"[error] agent {self._agent_id}: command '{cmd_name}' is not allowed.\n"
    #                     f"Allowed commands: {', '.join(sorted(self._allowed))}"
    #                 ),
    #                 exit=1,
    #             )
    #
    #         # ── Passed all checks — delegate to inner executor ────────────────
    #         return await self._inner.exec(command)
    #
    #     @property
    #     def location(self) -> str:
    #         constraints = []
    #         if self._allowed:
    #             constraints.append(f"allow={len(self._allowed)}")
    #         if self._blocked:
    #             constraints.append(f"block={len(self._blocked)}")
    #         scope = ",".join(constraints) if constraints else "unrestricted"
    #         return f"{self._inner.location}[agent={self._agent_id},{scope}]"
    #
    #
    # # ── Helpers ────────────────────────────────────────────────────────────────────
    #
    # def _extract_command_name(command: str) -> str:
    #     """
    #     Extract the first token (command name) from a command string.
    #
    #     Examples:
    #       "memory search hello"     → "memory"
    #       "cat /path/to/file"       → "cat"
    #       "spawn worker 'do stuff'" → "spawn"
    #       ""                        → ""
    #
    #     Uses shlex for proper quote handling, falls back to split on failure.
    #     """
    #     command = command.strip()
    #     if not command:
    #         return ""
    #     try:
    #         tokens = shlex.split(command)
    #     except ValueError:
    #         tokens = command.split()
    #     return tokens[0].lower() if tokens else ""
    # TODO: "send"    — multi-agent: send message to teammate via mailbox
    # TODO: "task"    — multi-agent: create/claim/update/list shared tasks
}


# ── Dispatch table ─────────────────────────────────────────────────────────────
# name → handler(args: list[str]) -> Result
# All handlers MUST accept args: list[str] even if they ignore it.
# Aliases (e.g. "mem" → memory) go here too.

# Populated after handler definitions below. Forward-declared here so
# dispatch() can reference it.
DISPATCH_TABLE: dict[str, Callable[[list[str]], Result]] = {}



# ── Public API ─────────────────────────────────────────────────────────────────

def tokenize(command: str) -> list[str]:
    """
    Split a command string into tokens using shell-like quoting rules.
    Falls back to whitespace-split on unbalanced quotes.
    """
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def dispatch(tokens: list[str]) -> Result:
    """
    Route a token list to the right handler.

    Routing order:
      1. Blocklist check  — immediate rejection with explanation
      2. Dispatch table   — custom commands + aliases
      3. Unix whitelist   — subprocess passthrough
      4. Unknown          — navigational error with command list
    """
    if not tokens:
        return err("dispatch: empty command")

    cmd = tokens[0].lower()

    # 1. Blocklist
    if cmd in UNIX_BLOCKLIST:
        return Result(
            stdout=(
                f"[error] {cmd}: command blocked for safety.\n"
                f"If you genuinely need this, ask the user to run it directly."
            ),
            exit=1,
        )

    # 2. Dispatch table
    handler = DISPATCH_TABLE.get(cmd)
    if handler is not None:
        return handler(tokens[1:])

    # 3. Unix whitelist
    if cmd in UNIX_WHITELIST:
        return _dispatch_unix(tokens)

    # 4. Unknown
    custom   = ", ".join(sorted(CUSTOM_COMMANDS))
    unix_top = ", ".join(sorted(UNIX_WHITELIST)[:20])
    return Result(
        stdout=(
            f"[error] unknown command: {cmd!r}\n"
            f"Custom commands: {custom}\n"
            f"Unix commands:   {unix_top}, ...\n"
            f"Run 'help' to see the full list."
        ),
        exit=127,
    )


def command_list_prompt() -> str:
    """
    Generate the Level-0 command list injected into the system prompt.
    """
    lines = [
        'Use CLI commands via: act(op="run_command", command="...")',
        "",
        "Custom commands:",
    ]
    for name, desc in CUSTOM_COMMANDS.items():
        lines.append(f"  {name:<16} — {desc}")

    lines.append("")
    lines.append("Unix commands (passthrough via subprocess):")
    unix_sorted = sorted(UNIX_WHITELIST)
    lines.append("  " + "  ".join(unix_sorted))

    lines.append("")
    lines.append("Command chaining:  |  &&  ||  ;  (standard Unix operators)")
    lines.append("Progressive help:  run any command with no args to see its usage.")

    lines.append("")
    lines.append("Multi-line file writing — use ONE of these idioms:")
    lines.append('  write path "line1\\nline2\\nline3"          ← \\n becomes real newline')
    lines.append("  printf 'line1\\nline2\\n' | write path      ← pipe content into write")
    lines.append("  write path <<'EOF'\\nline1\\nline2\\nEOF    ← heredoc (also supported)")
    lines.append("  AVOID: echo text | tee -a file  (tee needs a real shell)")
    lines.append("  AVOID: cat > file <<EOF  without newlines — use write instead")

    return "\n".join(lines)


# ── Unix passthrough ───────────────────────────────────────────────────────────

def _extract_redirect(tokens: list[str]) -> tuple[list[str], tuple[str, str] | None]:
    """Detect and extract shell output-redirect tokens ( > and >> )."""
    for op, mode in ((">>", "a"), (">", "w")):
        if op in tokens:
            idx = tokens.index(op)
            if idx + 1 < len(tokens):
                return tokens[:idx], (mode, tokens[idx + 1])
    return tokens, None


def _dispatch_unix(tokens: list[str]) -> Result:
    """Execute a whitelisted Unix command via subprocess."""
    cmd = tokens[0]
    timeout = _CMD_TIMEOUTS.get(cmd, DEFAULT_TIMEOUT)

    # Strip shell tokens subprocess cannot handle
    tokens = [t for t in tokens if not t.startswith("2>")]
    cmd_tokens, redirect = _extract_redirect(tokens)

    with Timer() as t:
        try:
            proc = subprocess.run(
                cmd_tokens,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError:
            return Result(
                stdout=(
                    f"[error] {cmd}: binary not found — "
                    f"is it installed?  Try: which {cmd}"
                ),
                exit=127,
            )
        except subprocess.TimeoutExpired:
            return Result(
                stdout=f"[error] {cmd}: timed out after {timeout}s",
                exit=124,
                elapsed_ms=timeout * 1000.0,
            )

    result = Result(
        stdout=proc.stdout,
        stderr=proc.stderr,
        exit=proc.returncode,
        elapsed_ms=t.elapsed_ms,
    )

    if redirect is not None and result.exit == 0:
        mode, filepath = redirect
        try:
            with open(filepath, mode, encoding="utf-8") as fh:
                fh.write(result.stdout)
            result.stdout = ""
        except Exception as e:
            result.stdout = f"[error] redirect to {filepath!r}: {e}"
            result.exit   = 1

    return result


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _parse_args(args: list[str]) -> tuple[list[str], dict[str, str]]:
    """Separate positional arguments from flags."""
    flags: dict[str, str] = {}
    positional: list[str] = []
    for a in args:
        if a.startswith("--"):
            k, _, v = a[2:].partition("=")
            flags[k] = v or "true"
        elif a.startswith("-") and len(a) == 2:
            flags[a[1]] = "true"
        else:
            positional.append(a)
    return positional, flags


# ── queue ──────────────────────────────────────────────────────────────────────

def _dispatch_queue(args: list[str]) -> Result:
    from ..heartbeat.queue_commands import dispatch_queue
    return dispatch_queue(args)


# ── note ───────────────────────────────────────────────────────────────────────

def _dispatch_note(args: list[str]) -> Result:
    from ..vault.note_commands import dispatch_note
    return dispatch_note(args)


# ── memory ─────────────────────────────────────────────────────────────────────

def _dispatch_memory(args: list[str]) -> Result:
    _USAGE = (
        "memory: usage: memory store|recent|search|count|update|forget\n"
        "  memory store <text>       — save to memory\n"
        "  memory recent [n]         — last n entries  (default 10)\n"
        "  memory search <query>     — full-text search (BM25)\n"
        "  memory count              — total entries stored\n"
        "  memory update <id> <text> — replace content of an existing entry\n"
        "  memory forget <id>        — delete entry by id"
    )

    if not args or args[0] in ("--help", "-h"):
        return ok(_USAGE)

    from ..memory.store import get_store
    store = get_store()

    sub  = args[0].lower()
    rest = args[1:]

    if sub == "store":
        if not rest:
            return err("memory: usage: memory store <text>")
        text     = " ".join(rest)
        entry_id = store.store(text)
        preview  = text[:80] + ("…" if len(text) > 80 else "")
        return ok(f"stored #{entry_id}: {preview}")

    if sub in ("recent", "r"):
        n = 10
        if rest:
            try:
                n = int(rest[0])
            except ValueError:
                return Result(
                    stderr=f"memory recent: error: expected a number, got {rest[0]!r}\nusage: memory recent [n]",
                    exit=2,
                )
        entries = store.recent(n)
        if not entries:
            return ok("(no memories yet — use: memory store <text>)")
        lines = [f"#{e['id']:>4}  [{e['created_at'][:16]}]  {e['content']}" for e in entries]
        return ok("\n".join(lines))

    if sub == "search":
        if not rest:
            return err("memory: usage: memory search <query>\n  Searches long-term memory by relevance (BM25 full-text).")
        query   = " ".join(rest)
        entries = store.search(query)
        if not entries:
            return ok(f"(no results for {query!r})\nTip: memory recent 20  to browse by time instead")
        lines = [f"#{e['id']:>4}  [{e['created_at'][:16]}]  {e['content']}" for e in entries]
        return ok("\n".join(lines))

    if sub == "count":
        n = store.count()
        return ok(f"{n} {'memory' if n == 1 else 'memories'} stored")

    if sub in ("update", "edit"):
        if len(rest) < 2:
            return Result(stderr="memory update: error: expected id and new text\nusage: memory update <id> <new text>", exit=2)
        try:
            memory_id = int(rest[0])
        except ValueError:
            return Result(stderr=f"memory update: error: expected a numeric id, got {rest[0]!r}\ntip: memory recent 10  to list ids", exit=2)
        new_text = " ".join(rest[1:])
        updated  = store.update(memory_id, new_text)
        if updated:
            preview = new_text[:80] + ("…" if len(new_text) > 80 else "")
            return ok(f"updated #{memory_id}: {preview}")
        return Result(stderr=f"memory update: error: #{memory_id} not found\ntip: memory recent 10  to list existing ids", exit=1)

    if sub in ("forget", "delete", "del", "rm"):
        if not rest:
            return err("memory: usage: memory forget <id>")
        try:
            memory_id = int(rest[0])
        except ValueError:
            return Result(stderr=f"memory forget: error: expected a numeric id, got {rest[0]!r}\ntip: memory recent 10  to list ids", exit=2)
        deleted = store.forget(memory_id)
        if deleted:
            return ok(f"forgotten #{memory_id}")
        else:
            return err(f"memory #{memory_id} not found\nTip: memory recent 10  to list existing ids")

    return Result(stderr=f"memory: error: unknown subcommand {sub!r}\n{_USAGE}", exit=2)


# ── skills ─────────────────────────────────────────────────────────────────────

def _dispatch_skills(args: list[str]) -> Result:
    _USAGE = (
        "skills: usage: skills list|load|info\n"
        "  skills list              — show all available skills\n"
        "  skills load <name...>    — load skill(s) full instructions into context\n"
        "  skills info <name>       — show skill description and metadata"
    )

    if not args or args[0] in ("--help", "-h"):
        return ok(_USAGE)

    from ..skills.loader import SkillLoader
    loader = SkillLoader()

    sub  = args[0].lower()
    rest = args[1:]

    if sub == "list":
        skills = loader.discover()
        if not skills:
            return ok("(no skills found)\ntip: set OPENCLAWD_SKILLS_ROOT or add skills to ~/.home-agent/skills/")
        lines = []
        for s in skills:
            tag_str = f"  [{', '.join(s.tags)}]" if s.tags else ""
            lines.append(f"  {s.name:<20} {s.description}{tag_str}")
        return ok(f"{len(skills)} skill(s) available:\n" + "\n".join(lines))

    if sub == "load":
        positional, flags = _parse_args(rest)
        if not positional or "help" in flags or "h" in flags:
            return ok("skills load: usage: skills load <name...>\n  skills load weather\n  skills load weather reminders git")

        loaded:    list[str] = []
        not_found: list[str] = []
        parts:     list[str] = []

        for name in positional:
            skill = loader.load(name)
            if skill:
                loaded.append(name)
                parts.append(
                    f"[SKILL ACTIVE: {skill.name}]\n"
                    f"The following instructions are now active. "
                    f"Apply them to your response.\n\n"
                    f"{skill.content.strip()}\n\n"
                    f"[END SKILL: {skill.name}]"
                )
            else:
                not_found.append(name)

        summary_lines: list[str] = []
        if loaded:
            summary_lines.append(f"skill(s) active: {', '.join(loaded)}")
        if not_found:
            summary_lines.append(f"not found: {', '.join(not_found)}")
            available = [s.name for s in loader.discover()]
            if available:
                summary_lines.append(f"available: {', '.join(available)}")

        output_parts: list[str] = []
        if parts:
            output_parts.append("\n\n".join(parts))
        if summary_lines:
            output_parts.append("\n".join(summary_lines))

        exit_code = 0 if not not_found else 1
        return Result(stdout="\n\n".join(output_parts), exit=exit_code)

    if sub == "info":
        positional, flags = _parse_args(rest)
        if not positional or "help" in flags or "h" in flags:
            return ok("skills info: usage: skills info <name>\n  skills info weather")

        name  = positional[0]
        skill = loader.load(name)
        if not skill:
            available = [s.name for s in loader.discover()]
            avail_str = ", ".join(available) if available else "(none)"
            return Result(stderr=f"skills info: error: unknown skill {name!r}\navailable: {avail_str}\nrun 'skills list' for full details", exit=1)

        lines = [
            f"name:        {skill.name}",
            f"description: {skill.description}",
            f"version:     {skill.version}",
        ]
        if skill.tags:
            lines.append(f"tags:        {', '.join(skill.tags)}")
        preview = skill.content[:200].strip()
        if preview:
            suffix = "…" if len(skill.content) > 200 else ""
            lines.append(f"\ncontent preview:\n{preview}{suffix}")
        return ok("\n".join(lines))

    return Result(stderr=f"skills: error: unknown subcommand {sub!r}\n{_USAGE}", exit=2)


# ── see ────────────────────────────────────────────────────────────────────────

_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff"})


def _dispatch_see(args: list[str]) -> Result:
    if not args or args[0] in ("--help", "-h"):
        return ok("see: usage: see <image_path>\n  Supported: .png .jpg .jpeg .gif .webp .bmp .tiff\n  For text files use: cat <filename>")

    path = Path(args[0])

    if not path.exists():
        return Result(stdout=f"[error] see: file not found: {path}\nUse 'ls' or 'find . -name \"*.png\"' to locate image files.", exit=1)

    suffix = path.suffix.lower()
    if suffix not in _IMAGE_EXTENSIONS:
        return Result(stdout=f"[error] see: {path.name} is not an image file (extension: {suffix or 'none'})\nFor text files use: cat {path}", exit=1)

    with Timer() as t:
        try:
            image_bytes = path.read_bytes()
            return Result(
                stdout=f"[image: {path.name} ({len(image_bytes) / 1024:.0f}KB)]",
                exit=0,
                elapsed_ms=t.elapsed_ms,
                image=image_bytes,
            )
        except PermissionError:
            return Result(stdout=f"[error] see: permission denied: {path}", exit=1)
        except Exception as e:
            return Result(stdout=f"[error] see: {e}", exit=1)


# ── write ──────────────────────────────────────────────────────────────────────

def _dispatch_write(args: list[str]) -> Result:
    if not args or args[0] in ("--help", "-h"):
        return ok("write: usage: write <path> <content...>\n  write notes.md  My meeting notes from today\n  write data/config.json  {\"key\": \"value\"}")

    path    = Path(args[0])
    content = " ".join(args[1:]) if len(args) > 1 else ""
    content = content.replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "\r")

    with Timer() as t:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return ok(f"wrote {len(content)} chars → {path}", elapsed_ms=t.elapsed_ms)
        except PermissionError:
            return Result(stdout=f"[error] write: permission denied: {path}", exit=1, elapsed_ms=t.elapsed_ms)
        except Exception as e:
            return Result(stdout=f"[error] write: {e}", exit=1)


# ── append ─────────────────────────────────────────────────────────────────────

def _dispatch_append(args: list[str]) -> Result:
    if not args or args[0] in ("--help", "-h"):
        return ok("append: usage: append <path> <text...>\n  append notes.md  New line of text\n  append hello.txt you are the best assistant in the world")

    path    = Path(args[0])
    content = " ".join(args[1:]) if len(args) > 1 else ""
    content = content.replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "\r")

    with Timer() as t:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(content + "\n")
            return ok(f"appended {len(content)} chars → {path}", elapsed_ms=t.elapsed_ms)
        except PermissionError:
            return Result(stdout=f"[error] append: permission denied: {path}", exit=1, elapsed_ms=t.elapsed_ms)
        except Exception as e:
            return Result(stdout=f"[error] append: {e}", exit=1)


# ── crontab (gated) ───────────────────────────────────────────────────────────

_CRONTAB_JOB_FORBIDDEN = frozenset(";|&$`")
_ENV_LINE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _crontab_master_enabled() -> bool:
    return os.environ.get("HOMEAGENT_ALLOW_CRONTAB", "").lower() in ("1", "true", "yes")


def _crontab_conversation_enabled() -> bool:
    return os.environ.get("HOMEAGENT_ALLOW_CRONTAB_CONVERSATION", "").lower() in ("1", "true", "yes")


def _crontab_ok_for_current_role() -> bool:
    from ..agent.exec_role import ROLE_CONVERSATION, get_execution_role
    if get_execution_role() != ROLE_CONVERSATION:
        return True
    return _crontab_conversation_enabled()


def _crontab_staging_root() -> Path | None:
    from ..vault.config import get_vault_root
    root = get_vault_root()
    if root is None:
        return None
    return (root / ".heartbeat" / "crontab_staging").resolve()


def _validate_crontab_staging_file(path: Path) -> Result | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return Result(stdout=f"[error] crontab: cannot read {path}: {e}", exit=1)
    max_bytes = int(os.environ.get("HOMEAGENT_CRONTAB_MAX_BYTES", "24576"))
    if len(text.encode("utf-8")) > max_bytes:
        return Result(stdout=f"[error] crontab: file too large (max {max_bytes} bytes)", exit=1)
    raw_markers = os.environ.get("HOMEAGENT_CRONTAB_JOB_MARKERS", "heartbeat,home_agent,home-agent")
    markers = [m.strip().lower() for m in raw_markers.split(",") if m.strip()]
    if not markers:
        return Result(stdout="[error] crontab: HOMEAGENT_CRONTAB_JOB_MARKERS must list at least one substring", exit=1)
    for i, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if _ENV_LINE_RE.match(s):
            continue
        if len(s) > 1024:
            return Result(stdout=f"[error] crontab: line {i} too long", exit=1)
        if any(c in s for c in _CRONTAB_JOB_FORBIDDEN):
            return Result(stdout=f"[error] crontab: line {i}: forbidden metacharacters (; | & $ `) — use a single simple command", exit=1)
        if "(" in s or ")" in s:
            return Result(stdout=f"[error] crontab: line {i}: subshell / () not allowed", exit=1)
        low = s.lower()
        if not any(m in low for m in markers):
            return Result(stdout=f"[error] crontab: line {i}: job must contain one of {markers!r} (HOMEAGENT_CRONTAB_JOB_MARKERS)", exit=1)
    return None


def _run_crontab_argv(argv: list[str]) -> Result:
    with Timer() as t:
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        except FileNotFoundError:
            return Result(stdout="[error] crontab: binary not found — is cron installed?", exit=127)
        except subprocess.TimeoutExpired:
            return Result(stdout="[error] crontab: timed out after 30s", exit=124, elapsed_ms=30_000.0)
    return Result(stdout=proc.stdout, stderr=proc.stderr, exit=proc.returncode if proc.returncode is not None else 1, elapsed_ms=t.elapsed_ms)


def _dispatch_crontab(args: list[str]) -> Result:
    if not args or args[0] in ("--help", "-h"):
        return ok(
            "crontab: gated user crontab (hardened against prompt injection)\n\n"
            "Requires HOMEAGENT_ALLOW_CRONTAB=1.\n"
            "From chat (conversation role): also set HOMEAGENT_ALLOW_CRONTAB_CONVERSATION=1.\n\n"
            "  crontab -l              list current user's crontab\n"
            "  crontab /abs/path/file  install only if file resolves under\n"
            "                          $HOMEAGENT_VAULT/.heartbeat/crontab_staging/\n\n"
            "Not allowed: crontab - (stdin), -e, -r, -u."
        )

    if not _crontab_master_enabled():
        return Result(stdout="[error] crontab: disabled. Set HOMEAGENT_ALLOW_CRONTAB=1 to enable (see crontab --help).", exit=1)

    if not _crontab_ok_for_current_role():
        return Result(stdout="[error] crontab: blocked in conversation role. Set HOMEAGENT_ALLOW_CRONTAB_CONVERSATION=1 if you accept the risk.", exit=1)

    if "-u" in args or "--user" in args:
        return Result(stdout="[error] crontab: -u / --user not allowed", exit=1)
    if args == ["-l"]:
        return _run_crontab_argv(["crontab", "-l"])
    if args[0] == "-":
        return Result(stdout="[error] crontab: stdin install (-) not allowed", exit=1)
    if args[0] in ("-e", "--edit"):
        return Result(stdout="[error] crontab: -e / editor mode not allowed", exit=1)
    if args[0] in ("-r", "--remove"):
        return Result(stdout="[error] crontab: -r / remove-all not allowed", exit=1)
    if len(args) != 1:
        return Result(stdout="[error] crontab: only `crontab -l` or `crontab <staging-file>` is supported", exit=1)

    raw = args[0]
    if raw.startswith("-"):
        return Result(stdout=f"[error] crontab: unsupported flag: {raw!r}", exit=1)

    staging_root = _crontab_staging_root()
    if staging_root is None:
        return Result(stdout="[error] crontab: install requires HOMEAGENT_VAULT pointing at an existing directory", exit=1)

    path = Path(raw).expanduser().resolve()
    staging_root.mkdir(parents=True, exist_ok=True)
    try:
        path.relative_to(staging_root)
    except ValueError:
        return Result(stdout=f"[error] crontab: path must be inside {staging_root}", exit=1)

    if not path.is_file():
        return Result(stdout=f"[error] crontab: not a file: {path}", exit=1)

    bad = _validate_crontab_staging_file(path)
    if bad is not None:
        return bad

    return _run_crontab_argv(["crontab", str(path)])


# ── my_preferences ─────────────────────────────────────────────────────────────

def _dispatch_my_preferences(args: list[str]) -> Result:
    # TODO: load from vault config / user profile file instead of hardcode
    return ok("My favourite streamer is thebausffs")


# ── help ───────────────────────────────────────────────────────────────────────

def _dispatch_help(args: list[str]) -> Result:
    lines = [
        "Available commands  (run any with no args for detailed usage)",
        "",
        "Custom commands:",
    ]
    for name, desc in CUSTOM_COMMANDS.items():
        lines.append(f"  {name:<16} — {desc}")

    lines += [
        "",
        "Unix passthrough (subprocess):",
        "  " + "  ".join(sorted(UNIX_WHITELIST)),
        "",
        "Chaining operators:",
        "  |    pipe     stdout → stdin of next command",
        "  &&   and      run next only if previous exit:0",
        "  ||   or       run next only if previous exit non-0",
        "  ;    seq      run next regardless",
        "",
        "Examples:",
        "  memory search 'pho preference' | head 5",
        "  cat notes.md | grep TODO",
        "  mkdir data && write data/info.txt Hello world",
    ]
    return ok("\n".join(lines))


# ── Populate dispatch table ────────────────────────────────────────────────────
# Done after all handlers are defined to avoid forward-reference issues.

DISPATCH_TABLE.update({
    "memory":         _dispatch_memory,
    "mem":            _dispatch_memory,       # alias
    "skills":         _dispatch_skills,
    "note":           _dispatch_note,
    "queue":          _dispatch_queue,
    "crontab":        _dispatch_crontab,
    "see":            _dispatch_see,
    "write":          _dispatch_write,
    "append":         _dispatch_append,
    "help":           _dispatch_help,
    "my_preferences": _dispatch_my_preferences,
    "spawn":           dispatch_spawn
    # TODO: multi-agent commands
    # "spawn":        _dispatch_spawn,
    # "send":         _dispatch_send,
    # "task":         _dispatch_task,
})


# ── Standalone test harness ────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import tempfile

    _PASS = "\033[32mPASS\033[0m"
    _FAIL = "\033[31mFAIL\033[0m"

    def _run(cmd_str: str) -> Result:
        return dispatch(tokenize(cmd_str))

    def _check(label: str, cmd_str: str, want_exit: int) -> bool:
        r = _run(cmd_str)
        ok_ = r.exit == want_exit
        sym = _PASS if ok_ else _FAIL
        print(f"  {sym}  [exit:{r.exit}{'≠' + str(want_exit) if not ok_ else ''}]  {cmd_str!r}")
        if not ok_:
            if r.stdout:
                print(f"         stdout: {r.stdout[:120]!r}")
            if r.stderr:
                print(f"         stderr: {r.stderr[:120]!r}")
        return ok_

    passed = failed = 0

    with tempfile.TemporaryDirectory() as _skills_dir, \
         tempfile.TemporaryDirectory() as _mem_dir:

        _weather_dir = Path(_skills_dir) / "weather"
        _weather_dir.mkdir()
        (_weather_dir / "SKILL.md").write_text(
            "---\ndescription: Weather forecasts and current conditions\nversion: 1.0\ntags: weather, forecast\n---\n# Weather Skill\nUse `curl wttr.in/City` to get weather.\n",
            encoding="utf-8",
        )
        _notes_dir = Path(_skills_dir) / "notes"
        _notes_dir.mkdir()
        (_notes_dir / "SKILL.md").write_text("# Notes Skill\nManage plain-text notes.\n", encoding="utf-8")

        os.environ["OPENCLAWD_SKILLS_ROOT"] = _skills_dir
        os.environ["HOMEAGENT_MEMORY_DB"]   = str(Path(_mem_dir) / "test.db")

        tests: list[tuple[str, str, int]] = [
            ("help",                    "help",                    0),
            ("memory no-args",          "memory",                  0),
            ("memory --help",           "memory --help",           0),
            ("mem alias",               "mem",                     0),
            ("skills no-args",          "skills",                  0),
            ("skills list",             "skills list",             0),
            ("skills load weather",     "skills load weather",     0),
            ("skills load both",        "skills load weather notes", 0),
            ("skills load not-found",   "skills load nope",        1),
            ("skills info weather",     "skills info weather",     0),
            ("skills info not-found",   "skills info nope",        1),
            ("skills unknown sub",      "skills unknown",          2),
            ("memory store",            "memory store hello world", 0),
            ("memory recent",           "memory recent",           0),
            ("memory recent 3",         "memory recent 3",         0),
            ("memory recent bad n",     "memory recent abc",       2),
            ("memory search",           "memory search hello",     0),
            ("memory count",            "memory count",            0),
            ("memory forget bad id",    "memory forget abc",       2),
            ("memory unknown sub",      "memory unknownsub",       2),
            ("my_preferences",          "my_preferences",          0),
            ("sudo blocked",            "sudo ls",                 1),
            ("bash blocked",            "bash -c whoami",          1),
            ("unknown cmd",             "foobar",                  127),
        ]

        for label, cmd_str, want_exit in tests:
            if _check(label, cmd_str, want_exit):
                passed += 1
            else:
                failed += 1

        _r = _run("memory recent 1")
        import re as _re
        _m = _re.search(r"#\s*(\d+)", _r.stdout)
        if _m:
            _id = _m.group(1)
            if _check("memory forget valid", f"memory forget {_id}", 0):
                passed += 1
            else:
                failed += 1
            if _check("memory forget missing", "memory forget 99999", 1):
                passed += 1
            else:
                failed += 1

    print(f"\n{'─' * 40}")
    print(f"  {passed} passed  {failed} failed  ({passed + failed} total)")
    sys.exit(0 if failed == 0 else 1)
