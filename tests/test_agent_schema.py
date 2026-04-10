from __future__ import annotations

from pathlib import Path

from src.multi_agent.agent_schema import agent_list_prompt, load_agent


def test_agent_list_prompt_discovers_dot_agents_dir(monkeypatch, tmp_path: Path):
    agents_dir = tmp_path / ".agents"
    agents_dir.mkdir()
    (agents_dir / "obsidian.yaml").write_text(
        "identifier: obsidian\ndescription: vault specialist\n",
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)

    listing = agent_list_prompt()
    assert "obsidian" in listing
    assert "vault specialist" in listing


def test_load_agent_matches_filename_case_insensitively(tmp_path: Path):
    agents_dir = tmp_path / ".agents"
    agents_dir.mkdir()
    (agents_dir / "Researcher.yaml").write_text(
        "identifier: researcher\ndescription: reads code\n",
        encoding="utf-8",
    )

    agent = load_agent("researcher", agents_dir=agents_dir)
    assert agent is not None
    assert agent.identifier == "researcher"
    assert agent.description == "reads code"


def test_obsidian_agent_prompt_encodes_note_edit_semantics():
    agent = load_agent("obsidian", agents_dir=Path(".agents"))
    assert agent is not None
    prompt = agent.system_prompt
    assert "note patch" in prompt
    assert "body text only" in prompt
    assert "YAML" in prompt
    assert "frontmatter" in prompt
    assert "Use `note tag`" in prompt
    assert "Use `note write" in prompt
