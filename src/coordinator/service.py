"""
Coordinator background task: poll queue depth, spawn on-demand heartbeat.

Session gating + WebSocket injection + TTL retries are TODO hooks (see plan).
"""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger(__name__)


async def coordinator_loop(stop: asyncio.Event, interval_sec: float = 10.0) -> None:
    """Poll queue; trigger heartbeat subprocess when depth exceeds threshold."""
    while not stop.is_set():
        try:
            from src.heartbeat.run import spawn_on_demand_if_needed

            spawn_on_demand_if_needed()
        except Exception:
            logger.debug("coordinator tick failed", exc_info=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_sec)
        except TimeoutError:
            pass


def start_coordinator_background() -> tuple[asyncio.Task[None], asyncio.Event]:
    """Start coordinator loop; caller must cancel task on shutdown."""
    stop = asyncio.Event()
    task = asyncio.create_task(coordinator_loop(stop), name="coordinator")
    return task, stop
