from __future__ import annotations

import base64
from typing import Any

from src.s2s.events import (
    AudioChunkIn,
    ClientEvent,
    Interrupt,
    Ping,
    SessionStart,
    TurnComplete,
)


def parse_client_event(message: dict[str, Any], session_id: str) -> ClientEvent:
    event_type = message.get("type")

    if event_type == "session.start":
        return SessionStart(
            session_id=session_id,
            sample_rate=int(message.get("sample_rate", 16000)),
            metadata=message.get("metadata", {}) or {},
        )

    if event_type == "audio.chunk":
        raw_data = message.get("data")
        if not raw_data:
            raise ValueError("Missing audio chunk data")

        # Guard against absurdly large single-chunk messages.
        # Real-time streaming should send 20-100 ms slices (~640-3200 bytes at 16 kHz PCM16).
        _MAX_CHUNK_BYTES = 10 * 1024 * 1024  # 10 MB
        if isinstance(raw_data, str) and len(raw_data) > _MAX_CHUNK_BYTES * 4 // 3:
            raise ValueError(
                f"Audio chunk too large ({len(raw_data):,} base64 chars). "
                "Stream audio in small slices (20-100 ms each), do not send entire files at once."
            )

        try:
            audio_bytes = base64.b64decode(raw_data)
        except Exception as exc:
            raise ValueError("Invalid base64 audio chunk") from exc

        return AudioChunkIn(
            data=audio_bytes,
            sample_rate=int(message.get("sample_rate", 16000)),
            seq=message.get("seq"),
        )

    if event_type == "turn.complete":
        return TurnComplete()

    if event_type == "ping":
        return Ping(timestamp_ms=message.get("timestamp_ms"))

    if event_type == "interrupt":
        return Interrupt(reason=message.get("reason"))

    raise ValueError(f"Unsupported event type: {event_type}")