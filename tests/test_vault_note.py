"""Tests for Obsidian-style `note` vault commands."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from src.cli_handler.dispatch import dispatch, tokenize
from src.vault.semantic import FindHit
from src.vault.writer import read_parsed, write_full_replace


@pytest.fixture
def vault_env(monkeypatch: pytest.MonkeyPatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("HOMEAGENT_VAULT", d)
        yield Path(d)


def test_dispatch_note_usage():
    r = dispatch(tokenize("note"))
    assert r.exit == 0
    assert "note ls" in r.stdout


def test_note_new_write_preserves_locked_metadata(vault_env: Path):
    os.environ["HOMEAGENT_VAULT"] = str(vault_env)
    r = dispatch(tokenize('note new p/a.md --title Hello --tags "t1,t2"'))
    assert r.exit == 0
    assert r.stdout.startswith("OK")

    n1 = read_parsed(vault_env, "p/a.md")
    orig_id = n1.fm["id"]
    orig_created = n1.fm["created"]

    # Direct writer call: model tries to overwrite id/created — tool restores from disk
    write_full_replace(
        vault_env,
        "p/a.md",
        {"id": "00000000-0000-0000-0000-000000000001", "created": "1999-01-01T00:00:00Z", "title": "Y"},
        "new body",
    )
    n2 = read_parsed(vault_env, "p/a.md")
    assert n2.fm["id"] == orig_id
    assert n2.fm["created"] == orig_created
    assert n2.fm["title"] == "Y"
    assert n2.body == "new body\n" or n2.body == "new body"

    r2 = dispatch(tokenize('note write p/a.md --title Z'))
    assert r2.exit == 0
    n3 = read_parsed(vault_env, "p/a.md")
    assert n3.fm["id"] == orig_id
    assert n3.fm["title"] == "Z"


def test_note_mv_patches_wikilinks(vault_env: Path):
    os.environ["HOMEAGENT_VAULT"] = str(vault_env)
    (vault_env / "sub").mkdir(parents=True)
    dispatch(tokenize("note new sub/foo.md --title Foo"))
    dispatch(
        tokenize(
            "note new link.md --body 'See [[sub/foo]] and [[sub/foo|alias]].'"
        )
    )

    r = dispatch(tokenize("note mv sub/foo.md bar.md"))
    assert r.exit == 0
    assert "backlinks_patched" in r.stdout
    text = (vault_env / "link.md").read_text(encoding="utf-8")
    assert "[[sub/foo]]" not in text
    assert "[[bar]]" in text or "bar" in text
    # Path form sub/foo -> bar (stem match)
    assert "[[bar|alias]]" in text


def test_note_find_mocked_semantic(vault_env: Path, monkeypatch: pytest.MonkeyPatch):
    os.environ["HOMEAGENT_VAULT"] = str(vault_env)
    os.environ["OPENAI_API_KEY"] = "test-key"

    def _fake_semantic_find(*_a, **_k):
        return [FindHit(path="x.md", score=0.95)]

    monkeypatch.setattr("src.vault.note_commands.semantic_find", _fake_semantic_find)

    dispatch(tokenize("note new x.md --body alpha"))
    r = dispatch(tokenize("note find something"))
    assert r.exit == 0
    assert r.stdout.startswith("OK")
    assert "x.md" in r.stdout
    assert "0.9500" in r.stdout or "0.95" in r.stdout


def test_note_tag(vault_env: Path):
    os.environ["HOMEAGENT_VAULT"] = str(vault_env)
    dispatch(tokenize("note new t.md --tags a,b"))
    r = dispatch(tokenize("note tag t.md --add c --remove a"))
    assert r.exit == 0
    assert "OK" in r.stdout
    n = read_parsed(vault_env, "t.md")
    tags = set(n.fm.get("tags", []))
    assert tags == {"b", "c"}


def test_note_patch_replace_once(vault_env: Path):
    os.environ["HOMEAGENT_VAULT"] = str(vault_env)
    dispatch(tokenize('note new patch.md --body "alpha\\nbeta\\ngamma"'))

    r = dispatch(
        tokenize('note patch patch.md --replace "beta" --with "beta updated"')
    )
    assert r.exit == 0
    assert "op: replace" in r.stdout

    note = read_parsed(vault_env, "patch.md")
    assert "beta updated" in note.body
    assert "beta\n" not in note.body


def test_note_patch_insert_after(vault_env: Path):
    os.environ["HOMEAGENT_VAULT"] = str(vault_env)
    dispatch(tokenize('note new insert.md --body "start\\nanchor\\nend"'))

    r = dispatch(
        tokenize('note patch insert.md --insert-after "anchor" --content "\\nnew line"')
    )
    assert r.exit == 0
    assert "op: insert_after" in r.stdout

    note = read_parsed(vault_env, "insert.md")
    assert "anchor\nnew line\nend" in note.body


def test_note_patch_fails_on_ambiguous_match(vault_env: Path):
    os.environ["HOMEAGENT_VAULT"] = str(vault_env)
    dispatch(tokenize('note new ambiguous.md --body "x\\nanchor\\ny\\nanchor\\nz"'))

    r = dispatch(
        tokenize('note patch ambiguous.md --insert-after "anchor" --content "\\nnew line"')
    )
    assert r.exit == 1
    assert "ambiguous" in r.stdout
