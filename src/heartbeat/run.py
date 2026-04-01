"""
Heartbeat entrypoint (separate process).

  python -m src.heartbeat.run --mode evening

Plan: digest (conversation / queue / vault index) → LLM markdown plan + optional queue_inserts.
Execute (unless --plan-only or --no-llm): Runner with run() tool (note / queue only).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def _iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")


def _iso_state() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _heartbeat_dir(vault: Path) -> Path:
    d = vault / ".heartbeat"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _stub_plan_body(mode: str, ts: str, last_run: str, pending: int, conv_tail: str) -> str:
    return f"""# Heartbeat plan ({mode}) {ts}

## Context
- last_run_at: {last_run or "(none)"}
- pending_queue_items: {pending}

## Conversation log (last lines)
```
{conv_tail[:12000]}
```

## Checklist (stub — use LLM when API is available)
- [ ] Remove --no-llm or set OPENAI_API_KEY / run Ollama for full plan
"""


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Heartbeat agent (scheduled)")
    p.add_argument("--mode", default="evening", choices=("evening", "morning", "on-demand"))
    p.add_argument("--check-depth", action="store_true", help="Print pending count and exit 1 if over threshold")
    p.add_argument("--no-llm", action="store_true", help="Write stub plan only; no API calls")
    p.add_argument("--plan-only", action="store_true", help="LLM plan + queue inserts; skip execute phase")
    args = p.parse_args(argv)

    try:
        from ..vault.config import require_vault_root
    except Exception:
        print("ERR\nmessage: vault not configured (HOMEAGENT_VAULT)", file=sys.stderr)
        return 1

    try:
        vault = require_vault_root()
    except Exception as e:
        print(f"ERR\nmessage: {e}", file=sys.stderr)
        return 1

    hb = _heartbeat_dir(vault)
    from .queue_store import get_queue_store

    store = get_queue_store()
    pending = store.count_pending()

    if args.check_depth:
        th = int(os.environ.get("HOMEAGENT_HEARTBEAT_QUEUE_THRESHOLD", "5"))
        print(f"pending={pending} threshold={th}")
        return 1 if pending >= th else 0

    ts = _iso()
    plans = hb / "plans"
    logs = hb / "logs"
    plans.mkdir(exist_ok=True)
    logs.mkdir(exist_ok=True)

    state_path = hb / "state.json"
    last_run = ""
    if state_path.exists():
        try:
            last_run = json.loads(state_path.read_text(encoding="utf-8")).get("last_run_at", "")
        except json.JSONDecodeError:
            pass

    conv = hb / "conversation.jsonl"
    conv_tail = ""
    if conv.exists():
        raw = conv.read_text(encoding="utf-8", errors="replace")
        lines = [ln for ln in raw.splitlines() if ln.strip()][-80:]
        conv_tail = "\n".join(lines)

    no_llm = args.no_llm or os.environ.get("HOMEAGENT_HEARTBEAT_NO_LLM", "").lower() in (
        "1",
        "true",
        "yes",
    )
    queue_ids: list[str] = []
    execute_summary = ""
    plan_status = "stub"
    execute_status = "skipped"

    if no_llm:
        plan_body = _stub_plan_body(args.mode, ts, last_run, pending, conv_tail)
    else:
        try:
            from .inputs import build_phase1_digest
            from .llm_plan import apply_queue_inserts, extract_queue_inserts, generate_plan_markdown

            digest = build_phase1_digest(vault, store, hb, last_run)
            payload = f"Run mode: {args.mode}\nPlan file timestamp: {ts}\n\n{digest}\n"
            plan_body = asyncio.run(generate_plan_markdown(payload))
            if not plan_body.strip():
                raise ValueError("empty plan from model")
            plan_status = "llm"
            inserts = extract_queue_inserts(plan_body)
            queue_ids = apply_queue_inserts(store, inserts)
        except Exception as e:
            print(f"WARN: heartbeat LLM plan failed: {e}", file=sys.stderr)
            plan_body = (
                f"# Heartbeat plan ({args.mode}) {ts} — LLM error\n\n"
                f"**Error:** `{e}`\n\n---\n\n"
                + _stub_plan_body(args.mode, ts, last_run, pending, conv_tail)
            )
            plan_status = "fallback_stub"

    plan_rel = f".heartbeat/plans/{ts}.md"
    (plans / f"{ts}.md").write_text(plan_body, encoding="utf-8")

    if plan_status == "llm" and not args.plan_only:
        try:
            from .llm_execute import execute_plan

            execute_summary, _usage = execute_plan(vault, store, hb, last_run, plan_body)
            execute_status = "ok" if execute_summary else "ok_empty_summary"
        except Exception as e:
            execute_summary = f"execute failed: {e}"
            execute_status = "error"
            print(f"WARN: heartbeat execute failed: {e}", file=sys.stderr)
    elif args.plan_only:
        execute_status = "skipped_plan_only"
    elif no_llm or plan_status != "llm":
        execute_status = "skipped_no_llm_or_failed_plan"

    log_body = f"""# Heartbeat run log {ts}

mode: {args.mode}
pending_queue_at_start: {pending}
plan_status: {plan_status}
execute_status: {execute_status}
queue_rows_inserted: {len(queue_ids)}
"""
    if queue_ids:
        log_body += "\nnew_queue_ids:\n" + "\n".join(f"- {q}" for q in queue_ids) + "\n"
    if execute_summary:
        log_body += "\n## Execute summary\n\n" + execute_summary + "\n"

    (logs / f"{ts}.md").write_text(log_body, encoding="utf-8")

    state_now = _iso_state()
    state_path.write_text(
        json.dumps(
            {
                "last_run_at": state_now,
                "mode": args.mode,
                "plan_file": plan_rel,
                "plan_status": plan_status,
                "execute_status": execute_status,
                "queue_inserts": len(queue_ids),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    vault_r = vault.resolve()
    plan_abs = vault_r / ".heartbeat" / "plans" / f"{ts}.md"
    log_abs = vault_r / ".heartbeat" / "logs" / f"{ts}.md"
    print(
        f"OK\n"
        f"vault_root: {vault_r}\n"
        f"plan (rel): {plan_rel}\n"
        f"plan (abs): {plan_abs}\n"
        f"log (rel):  .heartbeat/logs/{ts}.md\n"
        f"log (abs):  {log_abs}\n"
        f"state:      {state_path.resolve()}\n"
        f"plan_status={plan_status} execute_status={execute_status}"
    )
    return 0


def spawn_on_demand_if_needed() -> None:
    """If pending queue depth exceeds threshold, spawn heartbeat subprocess."""
    th = int(os.environ.get("HOMEAGENT_HEARTBEAT_QUEUE_THRESHOLD", "5"))
    try:
        from .queue_store import get_queue_store

        if get_queue_store().count_pending() < th:
            return
    except Exception:
        return
    root = Path(__file__).resolve().parents[2]
    subprocess.Popen(
        [sys.executable, "-m", "src.heartbeat.run", "--mode", "on-demand"],
        cwd=str(root),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


if __name__ == "__main__":
    raise SystemExit(main())
