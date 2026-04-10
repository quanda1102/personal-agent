"""
────────────────────────────
PromptBuilder — assembles the system prompt that RunContext.system_prompt receives.

This is the ONLY place that knows about:
  - workspace files  (AGENTS.md, SOUL.md, USER.md, etc.)
  - skill index      (names + descriptions, always lightweight)
  - skill content    (full SKILL.md, injected only for explicit active_skills lists)
  - available commands block (from cli_handler.dispatch)

The Runner (loop) knows none of this — it receives a finished string.

─────────────────────────────────────────────────────────────────────────────
Prompt structure (top → bottom)
─────────────────────────────────────────────────────────────────────────────

  [Layer 0 — Persona]
    Workspace files: AGENTS.md, SOUL.md, USER.md, BOOTSTRAP.md, …
    Fallback: DEFAULT_SYSTEM_PROMPT when workspace is empty.

  [Layer 1 — Skill index]
    Always lightweight — names + descriptions only (~100 tokens total).
    The agent calls  skills load <name>  to get full instructions on demand.
    Why cheap index instead of full dump:
      - Full dump for 10 skills ≈ 5,000–20,000 tokens per request
      - Index for 10 skills ≈ 200 tokens regardless of skill size
      - Agent self-selects what it needs → Anthropic "progressive disclosure"

    Exception: if active_skills=[...] is passed explicitly, those skills'
    full content IS injected — caller has decided they're always needed.

  [Layer 2 — CLI interface]
    The command list from command_list_prompt().  Always last.

─────────────────────────────────────────────────────────────────────────────
Skill injection modes
─────────────────────────────────────────────────────────────────────────────

  active_skills=None  (default)
    Auto-discover all skills. Inject INDEX only. Agent uses `skills load`
    to pull full content at inference time.

  active_skills=[]
    No skills. Just persona + commands. For tasks where skills are irrelevant.

  active_skills=["weather", "git"]
    Inject those skills' FULL content upfront. Use when you know the skill
    is always needed and want zero latency on the first turn.

─────────────────────────────────────────────────────────────────────────────
Usage
─────────────────────────────────────────────────────────────────────────────

    # Default: persona + skill index (lazy) + commands
    prompt = PromptBuilder().build()

    # With disk-backed workspace identity files
    ws = WorkspaceContext(root_dir=Path("~/.home-agent/workspace"))
    prompt = PromptBuilder(workspace=ws).build()

    # Pre-load specific skills (no lazy loading needed for these)
    prompt = PromptBuilder(active_skills=["weather", "reminders"]).build()

    # Wire into RunContext
    ctx = RunContext(
        user_message="what's the weather?",
        system_prompt=prompt,
        handler=CLIStreamHandler(),
    )
    await runner.run(ctx)
"""

from __future__ import annotations

from pathlib import Path

from ..memory.workspace import WorkspaceContext
from ..skills.loader import Skill, SkillLoader
from ..cli_handler.dispatch import command_list_prompt
from ..multi_agent.agent_schema import agent_list_prompt


# ── Default persona ─────────────────────────────────────────────────────────────
#
# Used when workspace has no identity files (AGENTS.md, SOUL.md, etc.).
# Override by writing to WorkspaceContext — this is just the fallback.

DEFAULT_SYSTEM_PROMPT = """\
You are a voice assistant. Respond in natural spoken language only. No bullet points, no markdown, no emojis. Use natural speech rhythms — short sentences, contractions, 
filler transitions like 'so', 'well', 'now'. Think how a person talks, not how they write.

You have one tool: act(...). It dispatches structured operations.

For CLI access, use:
  act(op="run_command", command="memory search preference")
  act(op="run_command", command="note read daily.md")

For direct file/capability access, use:
  act(op="read_file", path="README.md")
  act(op="search_files", query="Authorization", root="src")
  act(op="spawn_agent", role="researcher", task="audit auth flow")
  act(op="load_skill", name="weather")

Operating principles:
  - Check memory before answering questions about the user: act(op="run_command", command="memory search <query>")
  - Store anything worth remembering: act(op="run_command", command="memory store <text>")
  - If a task involves a domain you have a skill for, load it first:
    act(op="load_skill", name="<name>") — the skill gives you the right instructions and context.
  - Vault (Obsidian): in this session you may only note ls, note read, note find
    (read-only). You cannot note new, note write, note tag, or note mv — the tool
    will reject them. To change the vault or schedule multi-step work, use
    act(op="run_command", command="queue push --source conversation --action \"...\"")
    so the heartbeat agent can run later,
    or ask the user to edit notes themselves.
  - When a note needs a small targeted edit, prefer note patch over rewriting the
    whole file. Use exact replace or anchor-based insert when possible.
  - If the task is specifically about editing or organizing Obsidian vault notes,
    prefer delegating to the `obsidian` specialist via
    act(op="spawn_agent", role="obsidian", task="...") instead of using your own
    top-level tools.
  - When you delegate to a sub-agent via act(op="spawn_agent", ...), read its
    `[spawn_result]` block carefully.
    If `status: completed`, use the child result and do not repeat the same task
    with your own tools unless verification is explicitly needed.
    If `status: failed` or `status: partial`, recover intelligently based on
    `stop_reason` and `next_action`: choose a better specialist, narrow the task,
    continue locally, or ask the user. Do not blindly redo the same failed attempt.
  - Run commands to get real data — never guess or hallucinate file contents.
  - Errors are information — read them, adjust, try again.
  - Be concise. Show the result, not the scaffolding.
"""


# ── PromptBuilder ───────────────────────────────────────────────────────────────

class PromptBuilder:
    """
    Builds the system prompt from three layers:

    Layer 0 — Persona
      The agent's persistent identity: who it is, how it behaves.
      Source: WorkspaceContext files (AGENTS.md, SOUL.md, …).
      Fallback: DEFAULT_SYSTEM_PROMPT if workspace is empty.

    Layer 1 — Skills
      Index (names + descriptions) is ALWAYS injected when any skills exist.
      This is cheap (~100 tokens) and ensures the agent always knows what's
      available to load.

      active_skills controls whether full content is also pre-loaded:
        None  → index only. Agent lazy-loads via `skills load <name>`.
        [...] → index + full content for those named skills pre-loaded.
        []    → index only (same as None, no pre-loading).

    Layer 2 — CLI interface
      The command reference the LLM uses to write run() calls.
      Generated from the dispatch command table. Always appended last.
    """

    def __init__(
        self,
        workspace:     WorkspaceContext | None = None,
        active_skills: list[str] | None = None,
        skills_root:   Path | str | None = None,
    ):
        self.workspace     = workspace or WorkspaceContext()
        self.active_skills = active_skills   # None or [] = index only; [...] = also pre-load content
        self._loader       = SkillLoader(skills_root=skills_root)

    def build(self) -> str:
        """
        Assemble and return the full system prompt string.
        Call once per session start — or rebuild when active_skills changes.
        """
        parts: list[str] = []

        # ── Layer 0: persona ──────────────────────────────────────────────────
        workspace_block = self.workspace.build_system_prompt()
        if workspace_block.strip():
            parts.append(workspace_block.strip())
        else:
            parts.append(DEFAULT_SYSTEM_PROMPT.strip())

        # ── Layer 1: skills ───────────────────────────────────────────────────
        # Index (names + descriptions) is always injected — cheap, constant cost.
        # The agent always knows what skills exist and can call `skills load <name>`.
        all_skills = self._loader.discover()
        if all_skills:
            parts.append(self._build_skill_index(all_skills))

        # ── Layer 1.5: specialist agent index ───────────────────────────────
        agents_block = agent_list_prompt()
        if agents_block.strip():
            parts.append("## Specialist Agents\n" + agents_block)

        # Pre-load full content for explicitly requested skills.
        # Useful when you know a skill will definitely be needed on the first turn.
        if self.active_skills:
            preload_block = self._loader.build_skills_prompt(self.active_skills)
            if preload_block.strip():
                parts.append(preload_block.strip())

        # ── Layer 2: CLI commands (always present) ────────────────────────────
        parts.append("## CLI Interface\n" + command_list_prompt())

        return "\n\n".join(parts)

    # ── Layer 1 helpers ────────────────────────────────────────────────────────

    def _build_skill_index(self, skills: list[Skill]) -> str:
        """
        Build the lightweight skill index block.

        Only names + descriptions are emitted — full SKILL.md content stays
        on disk until the agent calls `skills load <name>`.
        Keeps the system prompt cost constant regardless of skill file sizes.
        """
        lines = [
            "## Skills",
            f"You have {len(skills)} skill(s) available.",
            "Load a skill's full instructions when needed:  skills load <name>",
            "",
        ]
        for s in skills:
            tag_str = f"  [{', '.join(s.tags)}]" if s.tags else ""
            lines.append(f"  {s.name:<20} — {s.description}{tag_str}")

        lines += [
            "",
            "Use `skills list` to see all skills with descriptions.",
            "Use `skills info <name>` to inspect a skill before loading.",
        ]
        return "\n".join(lines)

    # ── Builder helpers ────────────────────────────────────────────────────────

    def with_skills(self, *names: str) -> "PromptBuilder":
        """Return a new PromptBuilder with extra explicit skills pre-loaded."""
        current = list(self.active_skills) if self.active_skills is not None else []
        return PromptBuilder(
            workspace=self.workspace,
            active_skills=current + list(names),
            skills_root=self._loader._root,
        )

    def without_skills(self, *names: str) -> "PromptBuilder":
        """Return a new PromptBuilder with the named skills removed."""
        drop    = set(names)
        current = self.active_skills or []
        return PromptBuilder(
            workspace=self.workspace,
            active_skills=[s for s in current if s not in drop],
            skills_root=self._loader._root,
        )

    def __repr__(self) -> str:
        preload = repr(self.active_skills) if self.active_skills else "none"
        return f"PromptBuilder(preload={preload}, root={self._loader._root})"


# ── Standalone test / preview ───────────────────────────────────────────────────

if __name__ == "__main__":
    """
    Run with:
        python -m Home_agent.src.agent.prompt
        python prompt.py  (from the agent directory)

    Prints the assembled system prompt for visual inspection.
    Uses a temp directory so no real filesystem state is required.
    """
    import os
    import sys
    import tempfile
    from pathlib import Path

    SEP = "─" * 70

    def _section(title: str, text: str) -> None:
        print(f"\n{SEP}")
        print(f"  {title}")
        print(SEP)
        print(text)

    with tempfile.TemporaryDirectory() as _tmp:
        _skills_dir = Path(_tmp) / "skills"
        _ws_dir     = Path(_tmp) / "workspace"
        _skills_dir.mkdir()
        _ws_dir.mkdir()

        # Create two test skills
        for _name, _desc, _body in [
            ("weather",   "Weather forecasts and current conditions",
             "# Weather\nUse `curl wttr.in/City` to get weather.\n"),
            ("reminders", "Set and manage time-based reminders",
             "# Reminders\nUse `memory store 'remind: ...'` to set a reminder.\n"),
        ]:
            _d = _skills_dir / _name
            _d.mkdir()
            (_d / "SKILL.md").write_text(
                f"---\ndescription: {_desc}\ntags: {_name}\n---\n{_body}",
                encoding="utf-8",
            )

        os.environ["OPENCLAWD_SKILLS_ROOT"] = str(_skills_dir)

        ws = WorkspaceContext(root_dir=_ws_dir)
        ws.write("AGENTS.md", "You are Aria, a personal assistant for Hung.\n")
        ws.write("USER.md",   "User: Hung. Prefers concise answers. Located in Hanoi.\n")

        # ── Mode 1: default — index always injected ───────────────────────────
        prompt_default = PromptBuilder(workspace=ws, skills_root=_skills_dir).build()
        _section("Mode 1 — default (index always injected, no pre-load)", prompt_default)

        # ── Mode 2: pre-load one skill — index still present ──────────────────
        prompt_preload = PromptBuilder(
            workspace=ws,
            active_skills=["weather"],
            skills_root=_skills_dir,
        ).build()
        _section("Mode 2 — weather pre-loaded (index + full content)", prompt_preload)

        # ── Mode 3: no workspace, default persona ─────────────────────────────
        prompt_no_ws = PromptBuilder(skills_root=_skills_dir).build()
        _section("Mode 3 — no workspace (default persona fallback)", prompt_no_ws)

        # ── Token estimates ───────────────────────────────────────────────────
        print(f"\n{SEP}")
        print("  Token estimates (rough: chars / 4)")
        print(SEP)
        for label, p in [
            ("default (index only)", prompt_default),
            ("weather pre-loaded",   prompt_preload),
            ("no workspace",         prompt_no_ws),
        ]:
            print(f"  {label:<24} ≈ {len(p) // 4:>5} tokens  ({len(p):>6} chars)")

    sys.exit(0)
