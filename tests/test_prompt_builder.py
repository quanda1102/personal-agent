from __future__ import annotations

from pathlib import Path

from src.agent.prompt import PromptBuilder
from src.multi_agent.spawn import _format_command_constraints
from src.multi_agent.agent_schema import AgentDef


def test_prompt_builder_includes_specialist_agents(monkeypatch, tmp_path: Path):
    agents_dir = tmp_path / ".agents"
    agents_dir.mkdir()
    (agents_dir / "obsidian.yaml").write_text(
        "\n".join(
            [
                "identifier: obsidian",
                'description: "vault specialist"',
                'when_to_use: "Use for editing Obsidian vault notes"',
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)

    prompt = PromptBuilder().build()
    assert "## Specialist Agents" in prompt
    assert "obsidian" in prompt
    assert "vault specialist" in prompt


def test_command_constraints_include_note_semantics():
    block = _format_command_constraints(
        AgentDef(
            identifier="obsidian",
            allowed_commands=["note", "skills"],
        )
    )
    assert "Note command semantics" in block
    assert "Use `note tag`" in block
    assert "Use `note patch` only for note body edits" in block
