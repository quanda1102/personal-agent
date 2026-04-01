"""Optimistic version on vault notes."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from src.cli_handler.dispatch import dispatch, tokenize
from src.vault.writer import read_parsed, write_new, write_full_replace, VersionConflictError


@pytest.fixture
def vault_env(monkeypatch: pytest.MonkeyPatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("HOMEAGENT_VAULT", d)
        yield d


def test_note_version_increments(vault_env: str):
    os.environ["HOMEAGENT_VAULT"] = vault_env
    dispatch(tokenize("note new v.md --body hi"))
    n = read_parsed(Path(vault_env), "v.md")
    assert n.fm.get("version") == 1
    dispatch(tokenize("note write v.md more"))
    n2 = read_parsed(Path(vault_env), "v.md")
    assert n2.fm.get("version") == 2


def test_base_version_conflict(vault_env: str):
    os.environ["HOMEAGENT_VAULT"] = vault_env
    root = Path(vault_env)
    write_new(root, "x.md", {}, "a")
    with pytest.raises(VersionConflictError):
        write_full_replace(root, "x.md", {}, "b", base_version=0)


def test_base_version_ok(vault_env: str):
    os.environ["HOMEAGENT_VAULT"] = vault_env
    root = Path(vault_env)
    write_new(root, "y.md", {}, "a")
    write_full_replace(root, "y.md", {}, "b", base_version=1)
    n = read_parsed(root, "y.md")
    assert n.body.strip() == "b"
    assert n.fm.get("version") == 2
