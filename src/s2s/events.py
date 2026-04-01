"""
Events for the speech to speech (s2s) pipeline.
Input events:
- SessionStart
- AudioChunk
- TurnComplete
- Ping 
- Interrupt
Output events:
- SessionEnd
- AudioChunk
- STTResult
-AgentTextDelta
- Pong
Error events:
- Error
"""


from __future__ import annotations

from dataclasses import dataclass, field, asdict, is_dataclass
from typing import Any, Literal, TypeAlias
import base64


# =========================================================
# Base
# =========================================================

@dataclass(slots=True)
class EventBase:
    type: str


# =========================================================
# Input events (client -> server)
# =========================================================

@dataclass(slots=True)
class SessionStart(EventBase):
    session_id: str
    sample_rate: int = 16000
    metadata: dict[str, Any] = field(default_factory=dict)
    type: Literal["session.start"] = field(default="session.start", init=False)


@dataclass(slots=True)
class AudioChunkIn(EventBase):
    data: bytes
    sample_rate: int = 16000
    seq: int | None = None
    type: Literal["audio.chunk"] = field(default="audio.chunk", init=False)


@dataclass(slots=True)
class TurnComplete(EventBase):
    type: Literal["turn.complete"] = field(default="turn.complete", init=False)


@dataclass(slots=True)
class Ping(EventBase):
    timestamp_ms: int | None = None
    type: Literal["ping"] = field(default="ping", init=False)


@dataclass(slots=True)
class Interrupt(EventBase):
    reason: str | None = None
    type: Literal["interrupt"] = field(default="interrupt", init=False)


# =========================================================
# Output events (server -> client)
# =========================================================

@dataclass(slots=True)
class SessionEnd(EventBase):
    reason: str | None = None
    type: Literal["session.end"] = field(default="session.end", init=False)


@dataclass(slots=True)
class AudioChunkOut(EventBase):
    data: bytes
    sample_rate: int
    seq: int
    type: Literal["audio.chunk"] = field(default="audio.chunk", init=False)


@dataclass(slots=True)
class STTResult(EventBase):
    text: str
    is_final: bool = True
    language: str | None = None
    confidence: float | None = None
    type: Literal["stt.result"] = field(default="stt.result", init=False)


@dataclass(slots=True)
class AgentTextDelta(EventBase):
    text: str
    run_id: str | None = None
    type: Literal["agent.text_delta"] = field(default="agent.text_delta", init=False)


@dataclass(slots=True)
class Pong(EventBase):
    timestamp_ms: int | None = None
    type: Literal["pong"] = field(default="pong", init=False)


# =========================================================
# Error event
# =========================================================

@dataclass(slots=True)
class Error(EventBase):
    message: str
    code: str | None = None
    detail: dict[str, Any] | None = None
    type: Literal["error"] = field(default="error", init=False)


# =========================================================
# Union types
# =========================================================

ClientEvent: TypeAlias = SessionStart | AudioChunkIn | TurnComplete | Ping | Interrupt
ServerEvent: TypeAlias = SessionEnd | AudioChunkOut | STTResult | AgentTextDelta | Pong | Error
AnyEvent: TypeAlias = ClientEvent | ServerEvent


# =========================================================
# Serialization helpers
# =========================================================

def event_to_dict(event: AnyEvent) -> dict[str, Any]:
    """
    Convert dataclass event to wire dict.
    bytes payloads are base64-encoded for JSON transport.
    """
    if not is_dataclass(event):
        raise TypeError("event must be a dataclass instance")

    payload = asdict(event)

    if payload["type"] == "audio.chunk" and isinstance(payload.get("data"), (bytes, bytearray)):
        payload["data"] = base64.b64encode(payload["data"]).decode("utf-8")

    return payload


def event_to_jsonable(event: AnyEvent) -> dict[str, Any]:
    return event_to_dict(event)