"""`queue` CLI for dispatch."""

from __future__ import annotations

import json

from cli_handler.result import Result, ok, Timer
from .queue_store import get_queue_store


QUEUE_USAGE = """queue: usage: queue push|list|count|stats|get|status
  queue push --source conversation|heartbeat|vault --action "text"
             [--needs-user] [--priority routine|elevated|urgent]
             [--expires ISO] [--target-path REL] [--batch-id UUID]
  queue list [--status STATUS] [--source SOURCE] [--needs-user] [--limit N]
  queue count [--pending-only]
  queue stats   (total, pending, by_status, pending_needs_user, db path)
  queue get <id>
  queue status <id> <pending|in_progress|awaiting_response|needs_verification|done|discarded>
             [--meta-json '{"k":"v"}']"""


def _parse_queue_args(args: list[str]) -> tuple[list[str], dict[str, str]]:
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
        else:
            positional.append(a)
        i += 1
    return positional, flags


def dispatch_queue(args: list[str]) -> Result:
    if not args or args[0] in ("--help", "-h"):
        return ok(QUEUE_USAGE)

    sub = args[0].lower()
    rest = args[1:]
    store = get_queue_store()

    if sub == "push":
        return _cmd_push(rest, store)
    if sub == "list":
        return _cmd_list(rest, store)
    if sub == "count":
        return _cmd_count(rest, store)
    if sub == "stats":
        return _cmd_stats(store)
    if sub == "get":
        return _cmd_get(rest, store)
    if sub == "status":
        return _cmd_status(rest, store)

    return Result(
        stdout=f"queue: unknown subcommand {sub!r}\n{QUEUE_USAGE}",
        exit=2,
    )


def _cmd_push(rest: list[str], store) -> Result:
    pos, flags = _parse_queue_args(rest)
    src = flags.get("source", "")
    action = flags.get("action", "")
    if not src or not action:
        return Result(
            stdout="queue push: require --source and --action\n" + QUEUE_USAGE,
            exit=2,
        )
    needs_user = flags.get("needs-user") == "true" or "needs-user" in flags
    priority = flags.get("priority", "routine")
    expires = flags.get("expires")
    target = flags.get("target-path")
    batch = flags.get("batch-id")
    meta = {}
    if flags.get("meta-json"):
        try:
            meta = json.loads(flags["meta-json"])
        except json.JSONDecodeError as e:
            return Result(stdout=f"queue push: bad --meta-json: {e}", exit=2)

    default_h = 0.0 if src != "conversation" else 24.0
    with Timer() as t:
        qid = store.push(
            src,
            action,
            needs_user=needs_user,
            priority=priority,
            expires_at=expires or None,
            target_path=target,
            batch_id=batch,
            metadata=meta,
            default_expiry_hours=default_h,
        )
    return ok(f"OK\nid: {qid}", elapsed_ms=t.elapsed_ms)


def _cmd_list(rest: list[str], store) -> Result:
    pos, flags = _parse_queue_args(rest)
    status = flags.get("status")
    src = flags.get("source")
    limit = int(flags.get("limit", "100"))
    nu = None
    if flags.get("needs-user") == "true" or "needs-user" in flags:
        nu = True
    items = store.list_items(status=status, source=src, needs_user=nu, limit=limit)
    lines = ["OK", f"count: {len(items)}"]
    for it in items:
        lines.append(
            json.dumps(it.to_dict(), ensure_ascii=False, separators=(",", ":"))
        )
    return ok("\n".join(lines))


def _cmd_count(rest: list[str], store) -> Result:
    _, flags = _parse_queue_args(rest)
    if flags.get("pending-only") == "true" or "pending-only" in flags:
        n = store.count_pending()
    else:
        n = store.count_total()
    return ok(f"OK\ncount: {n}")


def _cmd_stats(store) -> Result:
    by_st = store.count_by_status()
    body = {
        "total": store.count_total(),
        "pending": store.count_pending(),
        "pending_needs_user": store.count_pending_needs_user(),
        "by_status": by_st,
        "db_path": str(store.db_path),
    }
    return ok(json.dumps(body, ensure_ascii=False, indent=2))


def _cmd_get(rest: list[str], store) -> Result:
    pos, _ = _parse_queue_args(rest)
    if not pos:
        return Result(stdout="queue get: missing id", exit=2)
    it = store.get(pos[0])
    if not it:
        return Result(stdout=f"queue get: not found: {pos[0]}", exit=1)
    return ok(json.dumps(it.to_dict(), ensure_ascii=False, indent=2))


def _cmd_status(rest: list[str], store) -> Result:
    pos, flags = _parse_queue_args(rest)
    if len(pos) < 2:
        return Result(stdout="queue status: usage: queue status <id> <new_status>", exit=2)
    qid, new_st = pos[0], pos[1]
    patch = None
    if flags.get("meta-json"):
        try:
            patch = json.loads(flags["meta-json"])
        except json.JSONDecodeError as e:
            return Result(stdout=f"bad meta-json: {e}", exit=2)
    ok_ = store.update_status(qid, new_st, patch)
    if not ok_:
        return Result(stdout=f"queue status: not found: {qid}", exit=1)
    return ok(f"OK\nid: {qid}\nstatus: {new_st}")
