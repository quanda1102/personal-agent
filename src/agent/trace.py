from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .usage import RunUsage


@dataclass
class UsageSnapshot:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    tool_calls: int = 0
    estimated_cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, other: "UsageSnapshot") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.tool_calls += other.tool_calls
        self.estimated_cost_usd += other.estimated_cost_usd

    @classmethod
    def from_run_usage(cls, usage: RunUsage) -> "UsageSnapshot":
        return cls(
            input_tokens=usage.total_input_tokens,
            output_tokens=usage.total_output_tokens,
            cache_write_tokens=usage.total_cache_write_tokens,
            cache_read_tokens=usage.total_cache_read_tokens,
            tool_calls=usage.total_tool_calls,
            estimated_cost_usd=usage.estimated_cost_usd,
        )


@dataclass
class RunTrace:
    run_id: str
    parent_run_id: str | None
    session_id: str
    agent_id: str
    agent_role: str
    model: str
    started_at: float
    finished_at: float | None = None
    stop_reason: str = ""
    status: str = "running"
    local_usage: UsageSnapshot = field(default_factory=UsageSnapshot)
    error_type: str = ""
    error_message: str = ""


class TraceStore:
    def __init__(self) -> None:
        self._runs: dict[str, RunTrace] = {}
        self._children: dict[str, list[str]] = {}
        self._lock = threading.Lock()

    def begin_run(
        self,
        *,
        run_id: str,
        parent_run_id: str | None,
        session_id: str,
        agent_id: str,
        agent_role: str,
        model: str,
    ) -> None:
        with self._lock:
            self._runs[run_id] = RunTrace(
                run_id=run_id,
                parent_run_id=parent_run_id,
                session_id=session_id,
                agent_id=agent_id,
                agent_role=agent_role,
                model=model,
                started_at=time.time(),
            )
            if parent_run_id:
                self._children.setdefault(parent_run_id, []).append(run_id)

    def finish_run(
        self,
        *,
        run_id: str,
        usage: RunUsage,
        stop_reason: str,
        status: str,
        error_type: str = "",
        error_message: str = "",
    ) -> None:
        with self._lock:
            trace = self._runs.get(run_id)
            if trace is None:
                return
            trace.finished_at = time.time()
            trace.stop_reason = stop_reason
            trace.status = status
            trace.local_usage = UsageSnapshot.from_run_usage(usage)
            trace.error_type = error_type
            trace.error_message = error_message

    def get_run(self, run_id: str) -> RunTrace | None:
        with self._lock:
            trace = self._runs.get(run_id)
            if trace is None:
                return None
            return RunTrace(
                run_id=trace.run_id,
                parent_run_id=trace.parent_run_id,
                session_id=trace.session_id,
                agent_id=trace.agent_id,
                agent_role=trace.agent_role,
                model=trace.model,
                started_at=trace.started_at,
                finished_at=trace.finished_at,
                stop_reason=trace.stop_reason,
                status=trace.status,
                local_usage=UsageSnapshot(
                    input_tokens=trace.local_usage.input_tokens,
                    output_tokens=trace.local_usage.output_tokens,
                    cache_write_tokens=trace.local_usage.cache_write_tokens,
                    cache_read_tokens=trace.local_usage.cache_read_tokens,
                    tool_calls=trace.local_usage.tool_calls,
                    estimated_cost_usd=trace.local_usage.estimated_cost_usd,
                ),
                error_type=trace.error_type,
                error_message=trace.error_message,
            )

    def subtree_usage(self, run_id: str) -> UsageSnapshot:
        with self._lock:
            return self._subtree_usage_locked(run_id)

    def _subtree_usage_locked(self, run_id: str) -> UsageSnapshot:
        trace = self._runs.get(run_id)
        total = UsageSnapshot()
        if trace is None:
            return total
        total.add(trace.local_usage)
        for child_id in self._children.get(run_id, []):
            total.add(self._subtree_usage_locked(child_id))
        return total

    def reset(self) -> None:
        with self._lock:
            self._runs.clear()
            self._children.clear()


_TRACE_STORE: TraceStore | None = None


def get_trace_store() -> TraceStore:
    global _TRACE_STORE
    if _TRACE_STORE is None:
        _TRACE_STORE = TraceStore()
    return _TRACE_STORE


def reset_trace_store() -> None:
    get_trace_store().reset()
