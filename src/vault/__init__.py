"""Obsidian-style Markdown vault: path-primary storage and tool-enforced metadata."""

from .config import VaultConfigError, get_vault_root, require_vault_root

__all__ = ["VaultConfigError", "get_vault_root", "require_vault_root"]
