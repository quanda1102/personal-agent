"""`note` subcommand implementations (return Result)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from cli_handler.result import Result, ok, Timer
from .config import VaultConfigError, require_vault_root
from .output_fmt import result_err, vault_ok
from .paths import UnsafePathError, iter_markdown_files, resolve_safe, to_rel_posix
from .schema import normalize_tags
from .semantic import FindHit, semantic_find
from .wikilinks import move_targets_for_path, patch_text_for_move
from .writer import (
    VersionConflictError,
    append_body,
    read_frontmatter_head,
    read_parsed,
    replace_section_body,
    update_tags_only,
    write_full_replace,
    write_new,
)


NOTE_USAGE = """note: usage: note ls|read|new|write|find|mv|tag
  note ls [dir] [--all] [--tag TAG]     — list notes (paths + meta; skips .heartbeat/)
  note read <path> [--max-bytes N]       — frontmatter + body
  note new <path> [--title T] [--tags a,b] [--body TEXT]
  note write <path> [--base-version N] [--append] [--section=H] [--title T] [--tags a,b] [--create] [body...]
  note find <query> [--limit N] [--tag T] [--recent-days D]  (skips .heartbeat/)
  note mv <from> <to>                    — move + patch wikilinks
  note tag <path> --add a,b [--remove x]"""


def _unescape(s: str) -> str:
    return s.replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "\r")


def _parse_args(args: list[str]) -> tuple[list[str], dict[str, str]]:
    """
    Split positional vs flags. Supports `--key=value` and `--key value` (value is
    the next token if it does not start with `-`).
    """
    flags: dict[str, str] = {}
    positional: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            k, _, v = a[2:].partition("=")
            if v:
                flags[k] = v
            elif i + 1 < len(args) and not args[i + 1].startswith("-"):
                flags[k] = args[i + 1]
                i += 1
            else:
                flags[k] = "true"
        elif a.startswith("-") and len(a) == 2:
            flags[a[1]] = "true"
        else:
            positional.append(a)
        i += 1
    return positional, flags


def dispatch_note(args: list[str]) -> Result:
    if not args or args[0] in ("--help", "-h"):
        return ok(NOTE_USAGE)

    sub = args[0].lower()
    rest = args[1:]

    from ..agent.exec_role import note_mutation_blocked

    if note_mutation_blocked(sub):
        return result_err(
            "vault_write_forbidden",
            f"note {sub}: mutating vault commands are disabled for conversational agents.",
            hint=(
                "Use note ls|read|find to inspect. To change the vault, ask the user to run "
                "note locally, or enqueue: queue push --source conversation --action \"…\" "
                "(heartbeat can run later)."
            ),
            exit_code=1,
        )

    try:
        if sub == "ls":
            return _cmd_ls(rest)
        if sub == "read":
            return _cmd_read(rest)
        if sub == "new":
            return _cmd_new(rest)
        if sub == "write":
            return _cmd_write(rest)
        if sub == "find":
            return _cmd_find(rest)
        if sub in ("mv", "move"):
            return _cmd_mv(rest)
        if sub == "tag":
            return _cmd_tag(rest)
    except VaultConfigError as e:
        return result_err("no_vault", str(e), hint="export HOMEAGENT_VAULT=/path/to/vault")
    except UnsafePathError as e:
        return result_err("unsafe_path", str(e), hint="use a path inside the vault")
    except FileExistsError as e:
        return result_err("exists", str(e), hint="note read first or use note write")
    except FileNotFoundError as e:
        return result_err("not_found", str(e), hint="note ls --all")
    except ValueError as e:
        return result_err("value_error", str(e), hint="check note write --section heading text")
    except VersionConflictError as e:
        return result_err(
            "version_conflict",
            str(e),
            hint=f"re-read note; current version on disk is {e.current}, you passed base-version {e.expected}",
        )

    return result_err("unknown_sub", f"unknown subcommand {sub!r}", NOTE_USAGE, exit_code=2)


def _cmd_ls(rest: list[str]) -> Result:
    vault = require_vault_root()
    positional, flags = _parse_args(rest)
    prefix = positional[0] if positional else ""
    tag_filter = flags.get("tag")
    show_all = flags.get("all") == "true" or "all" in flags

    rows: list[dict[str, Any]] = []
    for abs_path in iter_markdown_files(vault):
        rel = to_rel_posix(vault, abs_path)
        if prefix and not (rel == prefix or rel.startswith(prefix.rstrip("/") + "/")):
            continue
        fm, _ = read_frontmatter_head(abs_path)
        tags = normalize_tags((fm or {}).get("tags"))
        if tag_filter and tag_filter.strip() not in tags:
            continue
        title = (fm or {}).get("title") or ""
        nid = (fm or {}).get("id") or ""
        mtime = int(abs_path.stat().st_mtime)
        rows.append(
            {
                "path": rel,
                "title": title,
                "tags": tags,
                "mtime": mtime,
                "id": nid,
            }
        )

    if show_all:
        lines = ["OK", f"count: {len(rows)}"] + [r["path"] for r in sorted(rows, key=lambda x: x["path"])]
        return ok("\n".join(lines))

    parts = ["OK", f"count: {len(rows)}"]
    for r in sorted(rows, key=lambda x: x["path"]):
        parts.append(
            f"path: {r['path']}\t"
            f"title: {r['title']}\t"
            f"tags: {','.join(r['tags'])}\t"
            f"mtime: {r['mtime']}\t"
            f"id: {r['id']}"
        )
    return ok("\n".join(parts))


def _cmd_read(rest: list[str]) -> Result:
    vault = require_vault_root()
    positional, flags = _parse_args(rest)
    if not positional:
        return result_err("usage", "note read: missing path", NOTE_USAGE, exit_code=2)
    rel = positional[0]
    max_b = int(flags["max-bytes"]) if flags.get("max-bytes") else None

    note = read_parsed(vault, rel)
    body = note.body
    if max_b is not None and len(body.encode("utf-8")) > max_b:
        body = body.encode("utf-8")[:max_b].decode("utf-8", errors="replace") + "\n…(truncated)"

    fm_s = yaml.safe_dump(note.fm, default_flow_style=False, allow_unicode=True, sort_keys=False)
    out = "\n".join(
        [
            "OK",
            f"path: {note.rel_posix}",
            "---",
            fm_s.rstrip(),
            "---",
            body,
        ]
    )
    return ok(out)


def _cmd_new(rest: list[str]) -> Result:
    vault = require_vault_root()
    positional, flags = _parse_args(rest)
    if not positional:
        return result_err("usage", "note new: missing path", NOTE_USAGE, exit_code=2)
    rel = positional[0]
    fm: dict[str, Any] = {}
    if flags.get("title"):
        fm["title"] = flags["title"]
    if flags.get("tags"):
        fm["tags"] = flags["tags"]
    body = _unescape(flags.get("body", "") or "")
    if not body and len(positional) > 1:
        body = _unescape(" ".join(positional[1:]))

    with Timer() as t:
        note = write_new(vault, rel, fm, body)
    return ok(
        vault_ok(
            path=note.rel_posix,
            id=note.fm.get("id"),
            created=note.fm.get("created"),
            modified=note.fm.get("modified"),
            version=note.fm.get("version"),
        ),
        elapsed_ms=t.elapsed_ms,
    )


def _cmd_write(rest: list[str]) -> Result:
    vault = require_vault_root()
    positional, flags = _parse_args(rest)
    if not positional:
        return result_err("usage", "note write: missing path", NOTE_USAGE, exit_code=2)
    rel = positional[0]
    path = resolve_safe(vault, rel)
    create = flags.get("create") == "true" or "create" in flags

    body_in = _unescape(" ".join(positional[1:])) if len(positional) > 1 else ""

    fm_updates: dict[str, Any] = {}
    if flags.get("title"):
        fm_updates["title"] = flags["title"]
    if flags.get("tags"):
        fm_updates["tags"] = flags["tags"]
    base_v: int | None = None
    if flags.get("base-version") is not None:
        try:
            base_v = int(flags["base-version"])
        except ValueError:
            return result_err("usage", "note write: --base-version must be an integer", NOTE_USAGE, exit_code=2)

    def _wfr(fm: dict[str, Any], body: str) -> Any:
        return write_full_replace(vault, rel, fm, body, base_version=base_v)

    with Timer() as t:
        if not path.exists():
            if create:
                note = write_new(vault, rel, fm_updates, body_in)
            else:
                return result_err("not_found", str(path), "use --create or note new")
        elif flags.get("append") == "true" or "append" in flags:
            note = (
                append_body(vault, rel, body_in, base_version=base_v)
                if body_in
                else read_parsed(vault, rel)
            )
            if fm_updates:
                note = write_full_replace(
                    vault, rel, {**note.fm, **fm_updates}, note.body, base_version=None
                )
        elif flags.get("section"):
            note = read_parsed(vault, rel)
            new_body = replace_section_body(note.body, flags["section"], body_in)
            merged = {**note.fm, **fm_updates}
            note = _wfr(merged, new_body)
        else:
            note = read_parsed(vault, rel)
            merged = {**note.fm, **fm_updates}
            note = _wfr(merged, body_in)

    return ok(
        vault_ok(
            path=note.rel_posix,
            id=note.fm.get("id"),
            modified=note.fm.get("modified"),
            version=note.fm.get("version"),
        ),
        elapsed_ms=t.elapsed_ms,
    )


def _cmd_find(rest: list[str]) -> Result:
    vault = require_vault_root()
    positional, flags = _parse_args(rest)
    if not positional:
        return result_err("usage", "note find: missing query", NOTE_USAGE, exit_code=2)
    query = " ".join(positional)
    limit = int(flags.get("limit", "10"))
    tag = flags.get("tag")
    recent = int(flags["recent-days"]) if flags.get("recent-days") else None

    embed_fn = None
    if os.environ.get("HOMEAGENT_TEST_EMBED_JSON"):
        import json

        import numpy as np

        payload = json.loads(Path(os.environ["HOMEAGENT_TEST_EMBED_JSON"]).read_text(encoding="utf-8"))

        def _fake_embed(texts: list[str]) -> Any:
            return np.array([payload.get(t, [0.0, 0.0, 1.0]) for t in texts], dtype=np.float64)

        embed_fn = _fake_embed
    elif not os.environ.get("OPENAI_API_KEY"):
        return result_err(
            "no_api_key",
            "OPENAI_API_KEY not set",
            hint="set OPENAI_API_KEY for embeddings, or use note ls / grep for lexical search",
        )

    with Timer() as t:
        hits: list[FindHit] = semantic_find(
            vault,
            query,
            limit=limit,
            tag_filter=tag,
            recent_days=recent,
            embed_fn=embed_fn,
        )

    lines = ["OK", f"query: {query}", f"count: {len(hits)}"]
    for h in hits:
        lines.append(f"path: {h.path}\tscore: {h.score:.4f}")
    return ok("\n".join(lines), elapsed_ms=t.elapsed_ms)


def _cmd_mv(rest: list[str]) -> Result:
    vault = require_vault_root()
    positional, flags = _parse_args(rest)
    if len(positional) < 2:
        return result_err("usage", "note mv: need <from> <to>", NOTE_USAGE, exit_code=2)
    from_rel, to_rel = positional[0], positional[1]
    old_abs = resolve_safe(vault, from_rel)
    new_abs = resolve_safe(vault, to_rel)
    if not old_abs.exists():
        return result_err("not_found", str(old_abs))
    if new_abs.exists():
        return result_err("exists", str(new_abs), hint="remove destination first")

    old_rel, new_rel, old_stem, new_stem = move_targets_for_path(vault, old_abs, new_abs)
    new_abs.parent.mkdir(parents=True, exist_ok=True)
    old_abs.rename(new_abs)

    total = 0
    for md in iter_markdown_files(vault, include_heartbeat_ops=True):
        text = md.read_text(encoding="utf-8")
        new_text, n = patch_text_for_move(text, old_rel, new_rel, old_stem, new_stem)
        if n > 0:
            md.write_text(new_text, encoding="utf-8")
            total += n

    return ok(
        vault_ok(
            path=to_rel_posix(vault, new_abs),
            backlinks_patched=total,
            old_path=from_rel,
        )
    )


def _cmd_tag(rest: list[str]) -> Result:
    vault = require_vault_root()
    positional, flags = _parse_args(rest)
    if not positional:
        return result_err("usage", "note tag: missing path", NOTE_USAGE, exit_code=2)
    rel = positional[0]
    add = [x.strip() for x in (flags.get("add") or "").split(",") if x.strip()]
    remove = [x.strip() for x in (flags.get("remove") or "").split(",") if x.strip()]
    if not add and not remove:
        return result_err("usage", "note tag: need --add and/or --remove", NOTE_USAGE, exit_code=2)

    note = update_tags_only(vault, rel, add, remove)
    return ok(
        vault_ok(
            path=note.rel_posix,
            tags=",".join(normalize_tags(note.fm.get("tags"))),
            modified=note.fm.get("modified"),
        )
    )
