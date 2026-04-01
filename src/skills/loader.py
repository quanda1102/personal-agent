"""
openclawd.skills.loader
────────────────────────
SkillLoader — discovers and loads SKILL.md files from the skills directory.

A skill is a directory containing a SKILL.md file that describes:
  - What the skill does
  - What commands the agent can use for this skill
  - Any relevant context the agent needs

Skill injection is lazy — only active skills are read from disk.
Inactive skills cost zero tokens.

The agent can discover and request skills via:
  memory search "weather"    → finds skill references in memory
  (skills command TBD)

Directory layout:
  <skills_root>/
    weather/
      SKILL.md        ← injected into system prompt when "weather" is active
    git/
      SKILL.md
    coding/
      SKILL.md

Skills root resolves in order:
  1. OPENCLAWD_SKILLS_ROOT env var
  2. OPENCLAWD_PERSISTENT_ROOT/skills
  3. ~/.home-agent/skills
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ── Skill data type ────────────────────────────────────────────────────────────

@dataclass
class Skill:
    name:        str
    description: str       = ""
    version:     str       = "0.1"
    tags:        list[str] = field(default_factory=list)
    content:     str       = ""   # raw SKILL.md text


# ── Loader ─────────────────────────────────────────────────────────────────────

class SkillLoader:
    """
    Discovers and loads skills from the skills root directory.

    Usage:
        loader = SkillLoader()
        skills = loader.discover()                      # list all available
        prompt = loader.build_skills_prompt(["weather", "git"])   # inject active ones
    """

    def __init__(self, skills_root: Path | str | None = None):
        self._root = Path(skills_root) if skills_root else self._resolve_root()

    @staticmethod
    def _resolve_root() -> Path:
        if v := os.environ.get("OPENCLAWD_SKILLS_ROOT"):
            return Path(v)
        if v := os.environ.get("OPENCLAWD_PERSISTENT_ROOT"):
            return Path(v) / "skills"
        return Path.home() / ".home-agent" / "skills"

    # ── Discovery ──────────────────────────────────────────────────────────────

    def discover(self) -> list[Skill]:
        """
        List all skills found in the skills root directory.

        Supports two layouts:
          Flat:   <skills_root>/skill_name.md
          Nested: <skills_root>/skill_name/SKILL.md
        """
        if not self._root.exists():
            return []

        skills: list[Skill] = []
        seen: set[str] = set()

        for entry in sorted(self._root.iterdir()):
            if entry.is_dir():
                skill_md = entry / "SKILL.md"
                if skill_md.exists():
                    skills.append(self._load(entry.name, skill_md))
                    seen.add(entry.name)
            elif entry.is_file() and entry.suffix.lower() == ".md":
                name = entry.stem
                if name not in seen:
                    skills.append(self._load(name, entry))

        return skills

    def load(self, name: str) -> Skill | None:
        """Load a single skill by name.  Returns None if not found."""
        # Try nested layout first: <skills_root>/<name>/SKILL.md
        nested_md = self._root / name / "SKILL.md"
        if nested_md.exists():
            return self._load(name, nested_md)

        # Fall back to flat layout: <skills_root>/<name>.md
        flat_md = self._root / f"{name}.md"
        if flat_md.exists():
            return self._load(name, flat_md)

        return None

    # ── System prompt assembly ─────────────────────────────────────────────────

    def build_skills_prompt(self, active_skills: list[str]) -> str:
        """
        Load active skills and assemble their content into a prompt block.

        Only the listed skills are read from disk.  Everything else costs zero.

        Returns empty string if no active skills or none found on disk.
        """
        parts: list[str] = []

        for name in active_skills:
            skill = self.load(name)
            if skill and skill.content.strip():
                parts.append(f"## Skill: {skill.name}\n{skill.content.strip()}")

        return "\n\n".join(parts)

    # ── Internals ──────────────────────────────────────────────────────────────

    def _load(self, name: str, skill_md: Path) -> Skill:
        content = skill_md.read_text(encoding="utf-8")

        # Parse simple YAML-like front matter if present
        description = ""
        version     = "0.1"
        tags: list[str] = []

        lines = content.splitlines()
        if lines and lines[0].strip() == "---":
            # Front matter block
            end = next((i for i, l in enumerate(lines[1:], 1) if l.strip() == "---"), None)
            if end:
                for line in lines[1:end]:
                    if line.startswith("description:"):
                        description = line.split(":", 1)[1].strip()
                    elif line.startswith("version:"):
                        version = line.split(":", 1)[1].strip()
                    elif line.startswith("tags:"):
                        raw = line.split(":", 1)[1].strip()
                        tags = [t.strip() for t in raw.split(",") if t.strip()]
                content = "\n".join(lines[end + 1:]).strip()

        if not description:
            # Fall back: first non-empty line as description
            description = next(
                (l.lstrip("#").strip() for l in lines if l.strip() and not l.startswith("---")),
                name,
            )

        return Skill(
            name=name,
            description=description,
            version=version,
            tags=tags,
            content=content,
        )

    def __repr__(self) -> str:
        return f"SkillLoader(root={self._root})"
