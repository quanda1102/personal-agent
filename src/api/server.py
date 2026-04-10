"""
src.api.server
───────────────
FastAPI application — REST + WebSocket interface for the agent.

REST endpoints:
  GET  /health                      — liveness check
  GET  /sessions                    — list all sessions
  POST /sessions                    — create a new session
  GET  /sessions/{id}               — session info
  DELETE /sessions/{id}             — delete session (clears history)
  POST /sessions/{id}/clear         — clear history, keep session
  GET  /sessions/{id}/history       — full conversation history
  POST /chat                        — sync chat: run agent, return full response
  POST /heartbeat/test              — run heartbeat CLI once (stub by default)
  GET  /heartbeat/workflow          — snapshot .heartbeat/ + queue rows (pending + recent)
  GET  /queue/stats                 — queue totals / by_status (same gate as heartbeat test API)
  GET  /queue/items                 — list rows (?status=&source=&needs_user=&limit=)
  GET  /queue/items/{id}            — one row

WebSocket:
  WS /ws/{session_id}               — streaming agentic loop

WebSocket protocol (client → server, JSON):

  {"type": "run",    "content": "write a haiku"}  ← start a run
  {"type": "cancel"}                               ← cancel active run
  {"type": "ping"}                                 ← keepalive

WebSocket protocol (server → client, JSON):

  {"type": "connected",    "session_id": "..."}
  {"type": "queue_task",   "session_id": "...", "item": {...}}   ← pending queue row with notify_session_id
  {"type": "stream_start", "run_id": "...", "model": "..."}
  {"type": "text_delta",   "text": "..."}
  {"type": "thinking",     "text": "..."}
  {"type": "tool_use",     "turn": 1, "command": "..."}
  {"type": "tool_result",  "exit_code": 0, "output": "...", "elapsed_ms": 12}
  {"type": "stream_end",   "stop_reason": "end_turn", "cost": 0.001, ...}
  {"type": "error",        "message": "...", "detail": null}
  {"type": "pong"}
  {"type": "busy",         "message": "A run is already in progress"}
  {"type": "cancelled"}
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..agent.exec_role import ROLE_CONVERSATION
from ..agent.executor import RoleScopedExecutor
from ..agent.loop    import Runner, RunContext
from ..agent.handler import SilentHandler
from ..agent.trace import UsageSnapshot, get_trace_store
from ..heartbeat.queue_store import QueueItem
from .session import SessionStore, get_session_store
from .ws_handler import WebSocketHandler
from .ws_registry import WSSessionRegistry, queue_ws_notify_loop
from ..multi_agent.spawn import set_runner


# ── REST chat models ───────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    """
    Request body for POST /chat.

    session_id is optional — omit it for stateless one-shot requests,
    or pass the same ID across requests to maintain conversation history.
    """
    content:    str
    session_id: str = "default"


class ChatResponse(BaseModel):
    """Full agent response, returned synchronously after the run completes."""
    text:        str           # assistant's final text output
    session_id:  str
    tool_calls:  int           # how many tool calls were made
    stop_reason: str           # "end_turn" | "tool_ceiling" | "max_tokens"
    in_tokens:   int
    out_tokens:  int
    cost:        float
    elapsed_ms:  float
    local_tool_calls: int
    local_in_tokens: int
    local_out_tokens: int
    local_cost: float
    subtree_tool_calls: int
    subtree_in_tokens: int
    subtree_out_tokens: int
    subtree_cost: float


class HeartbeatTestRequest(BaseModel):
    """Manual heartbeat trigger (same process env as the API server)."""

    no_llm:    bool = True
    plan_only: bool = False
    mode:      str = "on-demand"


class HeartbeatTestResponse(BaseModel):
    exit_code: int
    stdout:    str
    stderr:    str


class HeartbeatArtifactPreview(BaseModel):
    filename:       str
    path_relative:  str
    modified_iso:   str
    preview:        str


class QueueItemSnapshot(BaseModel):
    """One row from `queue_items` for the workflow UI (action may be truncated)."""

    id:          str
    created_at:  str
    source:      str
    action:      str
    needs_user:  bool
    priority:    str
    status:      str
    expires_at:  str | None = None


class QueueItemPublic(BaseModel):
    """Full `queue_items` row for REST (no truncation)."""

    id:           str
    created_at:   str
    source:       str
    action:       str
    needs_user:   bool
    priority:     str
    expires_at:   str | None
    status:       str
    target_path:  str | None = None
    batch_id:     str | None = None
    metadata:     dict[str, Any] = Field(default_factory=dict)


class QueueStatsResponse(BaseModel):
    total:                int
    pending:              int
    pending_needs_user:   int
    by_status:            dict[str, int]
    db_path:              str


class QueueItemsListResponse(BaseModel):
    items: list[QueueItemPublic]
    count: int


class HeartbeatWorkflowResponse(BaseModel):
    """Snapshot of `.heartbeat/` on disk for UI workflow view."""

    vault_root:           str
    heartbeat_path:       str
    heartbeat_exists:     bool
    state:                dict[str, Any] | None
    latest_plan:          HeartbeatArtifactPreview | None
    latest_log:           HeartbeatArtifactPreview | None
    queue_pending:        int
    queue_note:           str
    queue_pending_items:  list[QueueItemSnapshot]
    queue_recent_items:   list[QueueItemSnapshot]
    flow_steps:           list[str]


def _hb_gate_or_404() -> None:
    if os.environ.get("HOMEAGENT_ENABLE_HEARTBEAT_TEST_API", "1").lower() in (
        "0",
        "false",
        "no",
    ):
        raise HTTPException(
            status_code=404,
            detail="heartbeat / queue admin API disabled (HOMEAGENT_ENABLE_HEARTBEAT_TEST_API)",
        )


def _iso_mtime(p: Path) -> str:
    try:
        ts = p.stat().st_mtime
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except OSError:
        return ""


def _latest_md_preview(folder: Path, *, preview_chars: int = 14_000) -> HeartbeatArtifactPreview | None:
    if not folder.is_dir():
        return None
    mds = [p for p in folder.glob("*.md") if p.is_file()]
    if not mds:
        return None
    latest = max(mds, key=lambda p: p.stat().st_mtime)
    try:
        raw = latest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        raw = ""
    prev = raw if len(raw) <= preview_chars else raw[:preview_chars] + "\n… (truncated)"
    rel = latest.name
    return HeartbeatArtifactPreview(
        filename=rel,
        path_relative=f".heartbeat/{folder.name}/{rel}",
        modified_iso=_iso_mtime(latest),
        preview=prev,
    )


def _queue_item_snapshot(it: QueueItem, *, action_max: int = 480) -> QueueItemSnapshot:
    a = it.action or ""
    if len(a) > action_max:
        a = a[:action_max] + "…"
    return QueueItemSnapshot(
        id=it.id,
        created_at=it.created_at,
        source=it.source,
        action=a,
        needs_user=it.needs_user,
        priority=it.priority,
        status=it.status,
        expires_at=it.expires_at,
    )


def _queue_item_public(it: QueueItem) -> QueueItemPublic:
    return QueueItemPublic(
        id=it.id,
        created_at=it.created_at,
        source=it.source,
        action=it.action,
        needs_user=it.needs_user,
        priority=it.priority,
        expires_at=it.expires_at,
        status=it.status,
        target_path=it.target_path,
        batch_id=it.batch_id,
        metadata=dict(it.metadata or {}),
    )


def build_heartbeat_workflow_response() -> HeartbeatWorkflowResponse:
    from ..vault.config import get_vault_root
    from ..heartbeat.queue_store import get_queue_store

    root = get_vault_root()
    if root is None:
        raise HTTPException(
            status_code=400,
            detail="HOMEAGENT_VAULT is not set or is not an existing directory",
        )
    vault_r = root.resolve()
    hb = vault_r / ".heartbeat"
    state_obj: dict[str, Any] | None = None
    sp = hb / "state.json"
    if sp.is_file():
        try:
            state_obj = json.loads(sp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state_obj = None

    pending_items: list[QueueItemSnapshot] = []
    recent_items: list[QueueItemSnapshot] = []
    pending = -1
    try:
        store = get_queue_store()
        pending = store.count_pending()
        for it in store.list_items(status="pending", limit=50):
            pending_items.append(_queue_item_snapshot(it))
        for it in store.list_recent(limit=25):
            recent_items.append(_queue_item_snapshot(it))
    except Exception:
        pending = -1

    th = os.environ.get("HOMEAGENT_HEARTBEAT_QUEUE_THRESHOLD", "5")
    queue_note = (
        "Hàng đợi (queue) chỉ là bảng SQLite: không có worker nền tự “đẩy” từng task. "
        "Việc được xử lý chỉ khi **heartbeat** chạy (nó đọc digest rồi plan/execute). "
        "`uv run server.py` **không** bật coordinator — pending có thể đứng yên mãi nếu bạn không chạy heartbeat. "
        f"Coordinator (chỉ trong `main.py` voice, `HOMEAGENT_ENABLE_COORDINATOR=1`) gọi heartbeat khi pending ≥ {th} "
        "(HOMEAGENT_HEARTBEAT_QUEUE_THRESHOLD); với 1 task mà ngưỡng = 5 thì **sẽ không** tự spawn. "
        "Cách xử lý: hạ ngưỡng xuống 1, hoặc cron / nút «Chạy heartbeat», hoặc `uv run python -m src.heartbeat.run`. "
        "`expires_at` chỉ là metadata, không tự kích hoạt. "
        "Trên server chat: hàng đợi có `metadata.notify_session_id` = id tab WebSocket sẽ được "
        "gửi tin `queue_task` tới tab đó (vòng lặp HOMEAGENT_ENABLE_QUEUE_WS_NOTIFY). "
        "Quan trọng: trường `action` chỉ là **mô tả văn bản** — không có worker đọc và “chạy” nó (không hẹn 2 phút thật). "
        "`status: done` + `ws_notified: true` nghĩa là **đã đóng dòng DB** và (nếu có) **đã gửi một gói WS**; "
        "không chứng minh mọi ý trong `action` đã xảy ra ngoài đời."
    )

    flow_steps = [
        "1. Digest: conversation.jsonl (từ last_run), queue pending (danh sách id + action), index vault.",
        "2. Plan: LLM → file trong .heartbeat/plans/ (+ queue_inserts JSON nếu có).",
        "3. Execute: (nếu không --plan-only và plan LLM OK) Runner + run() chỉ note/queue.",
        "4. Ghi .heartbeat/logs/*.md và cập nhật state.json.",
        "5. Lưu ý: queue không chạy theo đồng hồ — chỉ được đọc/cập nhật khi heartbeat (hoặc agent `queue push`) chạy.",
        "6. `queue status … done` chỉ cập nhật SQLite; không có bot riêng thực thi nội dung `action` theo thời gian thực.",
    ]

    return HeartbeatWorkflowResponse(
        vault_root=str(vault_r),
        heartbeat_path=str(hb),
        heartbeat_exists=hb.is_dir(),
        state=state_obj,
        latest_plan=_latest_md_preview(hb / "plans"),
        latest_log=_latest_md_preview(hb / "logs"),
        queue_pending=pending,
        queue_note=queue_note,
        queue_pending_items=pending_items,
        queue_recent_items=recent_items,
        flow_steps=flow_steps,
    )


# ── WebSocket protocol schema ──────────────────────────────────────────────────
# Single source of truth — consumed by GET /schema and the help handler.
# Clients (web UI, CLI tools, integrations) can fetch this to know the protocol.

WS_PROTOCOL = {
    "endpoint": "ws://<host>/ws/{session_id}",
    "description": (
        "Streaming agentic loop.  Each session_id maps to an independent "
        "conversation.  Conversation history persists across reconnects."
    ),
    "client_messages": {
        "run": {
            "description": "Start an agent run.",
            "fields": {
                "type":    {"type": "string", "const": "run", "required": True},
                "content": {"type": "string", "required": True, "description": "Your message to the agent"},
            },
            "example": {"type": "run", "content": "list files in the current directory"},
        },
        "cancel": {
            "description": "Cancel the currently running agent turn.",
            "fields": {
                "type": {"type": "string", "const": "cancel", "required": True},
            },
            "example": {"type": "cancel"},
        },
        "ping": {
            "description": "Keepalive / latency check.",
            "fields": {
                "type": {"type": "string", "const": "ping", "required": True},
            },
            "example": {"type": "ping"},
        },
        "help": {
            "description": "Request this protocol schema over the socket.",
            "fields": {
                "type": {"type": "string", "const": "help", "required": True},
            },
            "example": {"type": "help"},
        },
    },
    "server_messages": {
        "connected":    {"description": "Sent once on connection, includes this hint.", "fields": {"type": "connected", "session_id": "str", "hint": "str"}},
        "stream_start": {"description": "Run has started.", "fields": {"type": "stream_start", "run_id": "str", "model": "str"}},
        "text_delta":   {"description": "Streaming text chunk from the LLM.", "fields": {"type": "text_delta", "text": "str"}},
        "thinking":     {"description": "LLM extended thinking block.", "fields": {"type": "thinking", "text": "str"}},
        "tool_use":     {"description": "Agent is calling a command.", "fields": {"type": "tool_use", "turn": "int", "command": "str"}},
        "tool_result":  {"description": "Command output.", "fields": {"type": "tool_result", "exit_code": "int", "output": "str", "elapsed_ms": "float"}},
        "stream_end":   {"description": "Run completed. Flat fields (`in_tokens`, `out_tokens`, `tool_calls`, `cost`) are the local run only. `subtree_*` fields include nested sub-agent runs.", "fields": {"type": "stream_end", "stop_reason": "str", "in_tokens": "int", "out_tokens": "int", "tool_calls": "int", "cost": "float", "local_in_tokens": "int", "local_out_tokens": "int", "local_tool_calls": "int", "local_cost": "float", "subtree_in_tokens": "int", "subtree_out_tokens": "int", "subtree_tool_calls": "int", "subtree_cost": "float", "elapsed_ms": "float"}},
        "error":        {"description": "Non-fatal error (connection stays open).", "fields": {"type": "error", "message": "str", "detail": "str|null"}},
        "busy":         {"description": "A run is already active on this session.", "fields": {"type": "busy", "message": "str"}},
        "cancelled":    {"description": "Active run was cancelled.", "fields": {"type": "cancelled"}},
        "pong":         {"description": "Reply to ping.", "fields": {"type": "pong"}},
        "schema":       {"description": "Reply to help — this protocol document.", "fields": {"type": "schema", "protocol": "object"}},
        "queue_task":   {"description": "Queue row for this session (heartbeat/cron); metadata.notify_session_id matched.", "fields": {"type": "queue_task", "session_id": "str", "item": "object"}},
    },
    "quick_start": [
        'Connect:  ws://<host>/ws/my-session',
        'Send:     {"type": "run", "content": "list files in current directory"}',
        'Receive:  stream_start → text_delta* → tool_use? → tool_result? → stream_end',
    ],
}


# ── App state ──────────────────────────────────────────────────────────────────

@dataclass
class AgentState:
    runner:        Runner
    system_prompt: str
    sessions:      SessionStore = field(default_factory=get_session_store)
    ws_registry:   WSSessionRegistry = field(default_factory=WSSessionRegistry)


# ── App factory ────────────────────────────────────────────────────────────────

def create_app(runner: Runner, system_prompt: str) -> FastAPI:
    """
    Build the FastAPI application.

    Separate from module-level instantiation so the app can be created with
    different runners in tests or multiple server instances.
    """
    state = AgentState(runner=runner, system_prompt=system_prompt)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stop = asyncio.Event()
        notify_task: asyncio.Task | None = None
        if os.environ.get("HOMEAGENT_ENABLE_QUEUE_WS_NOTIFY", "1").lower() not in (
            "0",
            "false",
            "no",
        ):
            notify_task = asyncio.create_task(
                queue_ws_notify_loop(state.ws_registry, stop),
                name="queue_ws_notify",
            )
        yield
        stop.set()
        if notify_task is not None:
            notify_task.cancel()
            try:
                await notify_task
            except asyncio.CancelledError:
                pass

    app = FastAPI(
        title="Home Agent API",
        version="0.1.0",
        description="Agentic loop accessible over REST and WebSocket.",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],       # tighten in production
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── REST ──────────────────────────────────────────────────────────────────

    @app.get("/health", tags=["meta"])
    async def health() -> dict:
        """Liveness check — also confirms which model is loaded."""
        return {
            "status": "ok",
            "model":  runner.provider.model,
        }

    @app.get("/schema", tags=["meta"])
    async def schema() -> dict:
        """
        WebSocket message protocol schema.

        Returns the complete client↔server message format so clients
        can discover the protocol without reading source code.

        Equivalent to sending  {"type": "help"}  over the WebSocket.
        """
        return WS_PROTOCOL

    @app.get("/sessions", tags=["sessions"])
    async def list_sessions() -> list[dict]:
        """List all sessions sorted by last activity."""
        return state.sessions.list_all()

    @app.post("/sessions", tags=["sessions"], status_code=201)
    async def create_session() -> dict:
        """Create a new empty session.  Returns the generated session_id."""
        sid = state.sessions.new_session_id()
        state.sessions.get_or_create(sid)
        return {"session_id": sid}

    @app.get("/sessions/{session_id}", tags=["sessions"])
    async def get_session(session_id: str) -> dict:
        session = state.sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
        return session.info()

    @app.delete("/sessions/{session_id}", tags=["sessions"])
    async def delete_session(session_id: str) -> dict:
        """Delete the session and its entire conversation history."""
        deleted = state.sessions.delete(session_id)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
        return {"deleted": session_id}

    @app.post("/sessions/{session_id}/clear", tags=["sessions"])
    async def clear_session(session_id: str) -> dict:
        """Clear conversation history but keep the session alive."""
        cleared = state.sessions.clear_messages(session_id)
        if not cleared:
            raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
        return {"cleared": session_id}

    @app.get("/sessions/{session_id}/history", tags=["sessions"])
    async def get_history(session_id: str) -> list[dict]:
        """Return the full message list for a session."""
        session = state.sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
        return session.messages

    # ── POST /chat — synchronous REST chat ───────────────────────────────────

    @app.post("/chat", response_model=ChatResponse, tags=["chat"])
    async def chat(body: ChatRequest) -> ChatResponse:
        """
        Synchronous chat endpoint.

        Runs the full agentic loop (including tool calls) and returns the
        complete text response once the run finishes.  Conversation history
        is persisted under `session_id` — repeat the same ID to continue a
        multi-turn conversation.

        Use the WebSocket endpoint (/ws/{session_id}) if you want streaming
        output as the agent thinks and acts.

        Example:
            curl -s -X POST http://localhost:8000/chat \\
              -H 'Content-Type: application/json' \\
              -d '{"content": "list files in current directory", "session_id": "my-session"}'
        """
        t0      = time.perf_counter()
        session = state.sessions.get_or_create(body.session_id)
        handler = SilentHandler()

        ctx = RunContext(
            user_message  = body.content,
            system_prompt = state.system_prompt,
            session_id    = body.session_id,
            messages      = session.messages,   # shared list — history persists
            handler       = handler,
            executor      = RoleScopedExecutor(ROLE_CONVERSATION),
        )

        try:
            set_runner(state.runner, ctx)
            usage = await state.runner.run(ctx)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

        elapsed_ms = (time.perf_counter() - t0) * 1000
        end_event  = handler.final_usage()
        trace = get_trace_store().get_run(ctx.run_id)
        trace_local = trace.local_usage if trace is not None else UsageSnapshot.from_run_usage(usage)
        local_usage = UsageSnapshot(
            input_tokens=trace_local.input_tokens or usage.total_input_tokens,
            output_tokens=trace_local.output_tokens or usage.total_output_tokens,
            cache_write_tokens=trace_local.cache_write_tokens or usage.total_cache_write_tokens,
            cache_read_tokens=trace_local.cache_read_tokens or usage.total_cache_read_tokens,
            tool_calls=trace_local.tool_calls or usage.total_tool_calls,
            estimated_cost_usd=trace_local.estimated_cost_usd or usage.estimated_cost_usd,
        )
        subtree_usage = get_trace_store().subtree_usage(ctx.run_id)
        if (
            subtree_usage.input_tokens == 0
            and subtree_usage.output_tokens == 0
            and subtree_usage.tool_calls == 0
            and subtree_usage.estimated_cost_usd == 0.0
        ):
            subtree_usage = UsageSnapshot.from_run_usage(usage)

        return ChatResponse(
            text        = handler.text_output(),
            session_id  = body.session_id,
            tool_calls  = local_usage.tool_calls,
            stop_reason = end_event.stop_reason if end_event else "unknown",
            in_tokens   = local_usage.input_tokens,
            out_tokens  = local_usage.output_tokens,
            cost        = round(local_usage.estimated_cost_usd, 6),
            elapsed_ms  = round(elapsed_ms, 1),
            local_tool_calls=local_usage.tool_calls,
            local_in_tokens=local_usage.input_tokens,
            local_out_tokens=local_usage.output_tokens,
            local_cost=round(local_usage.estimated_cost_usd, 6),
            subtree_tool_calls=subtree_usage.tool_calls,
            subtree_in_tokens=subtree_usage.input_tokens,
            subtree_out_tokens=subtree_usage.output_tokens,
            subtree_cost=round(subtree_usage.estimated_cost_usd, 6),
        )

    # ── POST /heartbeat/test — run heartbeat subprocess (dev / manual cron) ─

    @app.post(
        "/heartbeat/test",
        response_model=HeartbeatTestResponse,
        tags=["heartbeat"],
    )
    async def heartbeat_test(body: HeartbeatTestRequest = HeartbeatTestRequest()) -> HeartbeatTestResponse:
        """
        Run `python -m src.heartbeat.run` once with the same environment as this server.

        Default: `--no-llm` (stub plan, no API cost). Uncheck in UI / send false to use real LLM.

        Disable with `HOMEAGENT_ENABLE_HEARTBEAT_TEST_API=0` on shared hosts.
        """
        _hb_gate_or_404()

        repo_root = Path(__file__).resolve().parents[2]
        args = [
            sys.executable,
            "-m",
            "src.heartbeat.run",
            "--mode",
            (body.mode or "on-demand").strip() or "on-demand",
        ]
        if body.no_llm:
            args.append("--no-llm")
        if body.plan_only:
            args.append("--plan-only")

        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(repo_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
        )
        stdout_b, stderr_b = await proc.communicate()
        out = stdout_b.decode(errors="replace")
        err = stderr_b.decode(errors="replace")
        max_len = 48_000
        if len(out) > max_len:
            out = out[:max_len] + "\n… (stdout truncated)"
        if len(err) > max_len:
            err = err[:max_len] + "\n… (stderr truncated)"

        code = 0 if proc.returncode is None else int(proc.returncode)
        return HeartbeatTestResponse(exit_code=code, stdout=out, stderr=err)

    @app.get(
        "/heartbeat/workflow",
        response_model=HeartbeatWorkflowResponse,
        tags=["heartbeat"],
    )
    async def heartbeat_workflow() -> HeartbeatWorkflowResponse:
        """
        Đọc snapshot `.heartbeat/` trên vault: state.json, plan/log mới nhất, queue depth.

        Dùng cho UI “tái hiện workflow” sau khi chạy heartbeat (cron hoặc POST /heartbeat/test).
        """
        _hb_gate_or_404()
        return build_heartbeat_workflow_response()

    @app.get(
        "/queue/stats",
        response_model=QueueStatsResponse,
        tags=["queue"],
    )
    async def queue_stats() -> QueueStatsResponse:
        """
        Inspect SQLite task queue: totals and counts per status.

        Gated like `/heartbeat/test` via `HOMEAGENT_ENABLE_HEARTBEAT_TEST_API`.
        """
        _hb_gate_or_404()
        from ..heartbeat.queue_store import get_queue_store

        store = get_queue_store()
        return QueueStatsResponse(
            total=store.count_total(),
            pending=store.count_pending(),
            pending_needs_user=store.count_pending_needs_user(),
            by_status=store.count_by_status(),
            db_path=str(store.db_path),
        )

    @app.get(
        "/queue/items",
        response_model=QueueItemsListResponse,
        tags=["queue"],
    )
    async def queue_items_list(
        status: str | None = None,
        source: str | None = None,
        needs_user: bool | None = None,
        limit: int = 100,
    ) -> QueueItemsListResponse:
        """
        List queue rows (oldest first). `limit` capped at 500.

        Gated like `/heartbeat/test` via `HOMEAGENT_ENABLE_HEARTBEAT_TEST_API`.
        """
        _hb_gate_or_404()
        from ..heartbeat.queue_store import get_queue_store

        lim = max(1, min(int(limit), 500))
        store = get_queue_store()
        rows = store.list_items(
            status=status,
            source=source,
            needs_user=needs_user,
            limit=lim,
        )
        return QueueItemsListResponse(
            items=[_queue_item_public(it) for it in rows],
            count=len(rows),
        )

    @app.get(
        "/queue/items/{item_id}",
        response_model=QueueItemPublic,
        tags=["queue"],
    )
    async def queue_item_get(item_id: str) -> QueueItemPublic:
        """Return one queue row by id."""
        _hb_gate_or_404()
        from ..heartbeat.queue_store import get_queue_store

        it = get_queue_store().get(item_id)
        if it is None:
            raise HTTPException(status_code=404, detail="queue item not found")
        return _queue_item_public(it)

    # ── WebSocket ─────────────────────────────────────────────────────────────

    @app.websocket("/ws/{session_id}")
    async def ws_endpoint(websocket: WebSocket, session_id: str) -> None:
        """
        Main WebSocket endpoint.

        One connection = one conversational session.
        Multiple runs on the same connection share the same message history
        → the agent remembers prior context within the session.

        Concurrency: each connection gets its own asyncio task for the runner
        and a separate sender task for non-blocking WebSocket writes.
        """
        await websocket.accept()

        handler     = WebSocketHandler()
        sender_task = asyncio.create_task(handler.sender(websocket))
        run_task:   asyncio.Task | None = None

        # Ensure the session exists
        session = state.sessions.get_or_create(session_id)

        await state.ws_registry.register(session_id, handler)

        # Greet the client — include a one-line usage hint so any new client
        # immediately knows the correct message format without docs.
        handler.send({
            "type":       "connected",
            "session_id": session_id,
            "hint":       'Send {"type":"run","content":"your message"} to chat  |  {"type":"help"} for full protocol',
        })

        try:
            while True:
                # ── Receive — two steps so we distinguish errors ───────────────
                # Step 1: receive raw text.  A real disconnect raises
                #         WebSocketDisconnect here and we exit the loop cleanly.
                # Step 2: parse JSON.  A bad payload raises ValueError and we
                #         send an error back and keep the connection alive.
                try:
                    raw = await websocket.receive_text()
                except WebSocketDisconnect:
                    break
                except Exception:
                    break   # any other transport-level error → exit

                try:
                    data = json.loads(raw)
                    if not isinstance(data, dict):
                        raise ValueError("expected a JSON object {…}, not a bare value")
                except (json.JSONDecodeError, ValueError) as exc:
                    handler.send({
                        "type":   "error",
                        "message": (
                            f"Invalid message — expected JSON object, got: {raw[:60]!r}"
                            if len(raw) <= 60 else
                            f"Invalid message — expected JSON object"
                        ),
                        "detail": (
                            'Send: {"type": "run", "content": "your message"} '
                            f'| parse error: {exc}'
                        ),
                    })
                    continue

                msg_type = data.get("type", "")

                # ── run ───────────────────────────────────────────────────────
                if msg_type == "run":
                    if run_task and not run_task.done():
                        handler.send({
                            "type":    "busy",
                            "message": "A run is already in progress. Send {\"type\": \"cancel\"} to stop it.",
                        })
                        continue

                    content = str(data.get("content", "")).strip()
                    if not content:
                        handler.send({"type": "error", "message": "run: content is empty"})
                        continue

                    meta_ex = json.dumps({"notify_session_id": session_id}, ensure_ascii=False)
                    ws_queue_hint = (
                        "\n\n--- WebSocket tab (queue → user) ---\n"
                        f"This connection's session_id is {json.dumps(session_id)}.\n"
                        "For follow-ups this user should see on this tab after heartbeat/cron, "
                        "put that id in queue metadata, e.g. one run() with:\n"
                        f'  queue push --source conversation --action "…" --meta-json {json.dumps(meta_ex)}\n'
                    )
                    ctx = RunContext(
                        user_message  = content,
                        system_prompt = state.system_prompt + ws_queue_hint,
                        session_id    = session_id,
                        messages      = session.messages,   # shared list, mutated in-place
                        handler       = handler,
                        executor      = RoleScopedExecutor(ROLE_CONVERSATION),
                    )

                    async def _do_run(ctx: RunContext = ctx) -> None:
                        try:
                            set_runner(state.runner, ctx)
                            await state.runner.run(ctx)
                        except asyncio.CancelledError:
                            handler.send({"type": "cancelled"})
                        except Exception as exc:
                            handler.send({
                                "type":    "error",
                                "message": str(exc),
                                "detail":  type(exc).__name__,
                            })

                    run_task = asyncio.create_task(_do_run())

                # ── cancel ────────────────────────────────────────────────────
                elif msg_type == "cancel":
                    if run_task and not run_task.done():
                        run_task.cancel()
                    else:
                        handler.send({"type": "error", "message": "No active run to cancel"})

                # ── ping ──────────────────────────────────────────────────────
                elif msg_type == "ping":
                    handler.send({"type": "pong"})

                # ── help — return the full protocol schema ─────────────────────
                elif msg_type == "help":
                    handler.send({"type": "schema", "protocol": WS_PROTOCOL})

                else:
                    handler.send({
                        "type":    "error",
                        "message": f"Unknown message type: {msg_type!r}",
                        "detail":  'Supported types: run, cancel, ping, help  |  send {"type":"help"} for full schema',
                    })

        except WebSocketDisconnect:
            pass

        finally:
            await state.ws_registry.unregister(session_id, handler)

            # Cancel any active run
            if run_task and not run_task.done():
                run_task.cancel()
                try:
                    await run_task
                except (asyncio.CancelledError, Exception):
                    pass

            # Drain remaining events and close sender
            handler.close()
            try:
                await asyncio.wait_for(sender_task, timeout=3.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass

    # ── Browser chat UI (static, no extra deps) ───────────────────────────────
    _chat_ui_dir = Path(__file__).resolve().parents[2] / "static" / "chat"
    if _chat_ui_dir.is_dir():
        app.mount(
            "/chat-ui",
            StaticFiles(directory=str(_chat_ui_dir), html=True),
            name="chat_ui",
        )

        @app.get("/", include_in_schema=False)
        async def _root_redirect_chat_ui() -> RedirectResponse:
            return RedirectResponse(url="/chat-ui/")

    return app
