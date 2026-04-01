"""Vault root from environment: HOMEAGENT_VAULT or OBSIDIAN_VAULT."""

from __future__ import annotations

import os
from pathlib import Path


class VaultConfigError(RuntimeError):
    """Vault path missing or invalid."""


def get_vault_root() -> Path | None:
    """
    Return resolved vault directory if set and exists, else None.
    Checks HOMEAGENT_VAULT first, then OBSIDIAN_VAULT.
    """
    for key in ("HOMEAGENT_VAULT", "OBSIDIAN_VAULT"):
        raw = os.environ.get(key)
        if not raw:
            continue
        p = Path(raw).expanduser().resolve()
        if p.is_dir():
            return p
    return None


def require_vault_root() -> Path:
    """Return vault root or raise VaultConfigError with a recoverable hint."""
    for key in ("HOMEAGENT_VAULT", "OBSIDIAN_VAULT"):
        raw = os.environ.get(key)
        if not raw:
            continue
        p = Path(raw).expanduser().resolve()
        if p.is_dir():
            return p
        raise VaultConfigError(f"{key} is not a directory: {raw!r}")
    raise VaultConfigError(
        "No vault configured. Set HOMEAGENT_VAULT (or OBSIDIAN_VAULT) "
        "to your Obsidian vault folder path."
    )
