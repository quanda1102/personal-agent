"""Non-tool LLM call to produce plan markdown + optional queue_inserts JSON."""

from __future__ import annotations

import json
import re
from typing import Any

from .llm_client import heartbeat_model_and_client
from .prompts import HEARTBEAT_PLAN_SYSTEM


async def generate_plan_markdown(user_payload: str) -> str:
    model, client = heartbeat_model_and_client()
    from ..llm_provider.openai import _max_tokens_param

    resp = await client.chat.completions.create(
        model=model,
        **_max_tokens_param(model, 8192),
        messages=[
            {"role": "system", "content": HEARTBEAT_PLAN_SYSTEM},
            {"role": "user", "content": user_payload},
        ],
        stream=False,
    )
    choice = resp.choices[0].message
    return (choice.content or "").strip()


def extract_queue_inserts(plan_text: str) -> list[dict[str, Any]]:
    """Parse last JSON fence containing queue_inserts."""
    for m in re.finditer(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", plan_text):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        raw = data.get("queue_inserts")
        if isinstance(raw, list):
            return [x for x in raw if isinstance(x, dict)]
    return []


def apply_queue_inserts(store: Any, inserts: list[dict[str, Any]]) -> list[str]:
    """Push rows with source=heartbeat. Returns new ids."""
    ids: list[str] = []
    for row in inserts:
        action = str(row.get("action") or "").strip()
        if not action:
            continue
        needs_user = bool(row.get("needs_user", False))
        priority = str(row.get("priority") or "routine")
        if priority not in ("routine", "elevated", "urgent"):
            priority = "routine"
        exp = row.get("expires_at")
        expires_at = str(exp) if exp else None
        tp = row.get("target_path")
        target_path = str(tp) if tp else None
        md: dict[str, Any] = {}
        raw_meta = row.get("metadata")
        if isinstance(raw_meta, dict):
            md.update(raw_meta)
        ns = row.get("notify_session_id")
        if ns is not None and str(ns).strip():
            md["notify_session_id"] = str(ns).strip()
        qid = store.push(
            "heartbeat",
            action,
            needs_user=needs_user,
            priority=priority,
            expires_at=expires_at,
            target_path=target_path,
            metadata=md if md else None,
            default_expiry_hours=0,
        )
        ids.append(qid)
    return ids
