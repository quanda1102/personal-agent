from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from src.s2s.events import (
    AudioChunkIn,
    Error,
    Interrupt,
    Ping,
    SessionStart,
    TurnComplete,
    Pong,
)
from src.s2s.pipeline import VoicePipeline
from src.s2s.ws_parser import parse_client_event
from src.s2s.sender import send_event

logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/ws/voice/{session_id}")
async def voice_ws(websocket: WebSocket, session_id: str) -> None:
    await websocket.accept()

    pipeline: VoicePipeline | None = None
    session_started = False

    try:
        pipeline = VoicePipeline(session_id=session_id)
        await pipeline.open()

        while True:
            logger.debug("session=%s waiting for message…", session_id)
            try:
                raw_message = await websocket.receive_json()
            except WebSocketDisconnect:
                raise
            except (RuntimeError, KeyError, ValueError) as exc:
                # RuntimeError  – uvicorn "WebSocket is not connected" on abrupt close
                # KeyError      – client sent a binary frame; starlette can't find "text" key
                # ValueError    – malformed JSON in the frame
                logger.warning(
                    "Receive failed for session=%s (%s): %s",
                    session_id, type(exc).__name__, exc,
                )
                break

            try:
                event = parse_client_event(raw_message, session_id=session_id)
            except ValueError as exc:
                logger.warning("Invalid client event for session=%s: %s", session_id, exc)
                await send_event(
                    websocket,
                    Error(
                        message=str(exc),
                        code="INVALID_EVENT",
                    ),
                )
                continue

            logger.info("session=%s recv event=%s", session_id, type(event).__name__)

            if isinstance(event, Ping):
                await send_event(
                    websocket,
                    Pong(timestamp_ms=event.timestamp_ms),
                )
                continue

            if isinstance(event, SessionStart):
                session_started = True

            elif not session_started:
                await send_event(
                    websocket,
                    Error(
                        message="SessionStart is required before other events",
                        code="SESSION_NOT_STARTED",
                    ),
                )
                continue

            try:
                async for out_event in _dispatch_event(pipeline, event):
                    logger.info("session=%s send event=%s", session_id, type(out_event).__name__)
                    await send_event(websocket, out_event)
            except Exception as exc:
                logger.exception("Pipeline error in session=%s", session_id)
                await send_event(
                    websocket,
                    Error(
                        message="Pipeline processing failed",
                        code="PIPELINE_ERROR",
                        detail={"reason": str(exc)},
                    ),
                )

    except WebSocketDisconnect as exc:
        logger.info(
            "Client disconnected: session=%s  code=%s reason=%r",
            session_id, exc.code, exc.reason or "",
        )

    except Exception as exc:
        logger.exception("Unhandled websocket error in session=%s", session_id)
        try:
            await send_event(
                websocket,
                Error(
                    message="Unhandled server error",
                    code="UNHANDLED_ERROR",
                    detail={"reason": str(exc)},
                ),
            )
        except Exception:
            pass

    finally:
        if pipeline is not None:
            try:
                await pipeline.close()
            except Exception:
                logger.exception("Failed to close pipeline cleanly for session=%s", session_id)

        try:
            await websocket.close()
        except Exception:
            pass

async def _dispatch_event(
    pipeline: VoicePipeline,
    event: SessionStart | AudioChunkIn | TurnComplete | Interrupt,
) -> AsyncIterator:
    if isinstance(event, SessionStart):
        async for out_event in pipeline.on_session_start(event):
            yield out_event
        return

    if isinstance(event, AudioChunkIn):
        async for out_event in pipeline.on_audio_chunk(event):
            yield out_event
        return

    if isinstance(event, TurnComplete):
        async for out_event in pipeline.on_turn_complete(event):
            yield out_event
        return

    if isinstance(event, Interrupt):
        async for out_event in pipeline.on_interrupt(event):
            yield out_event
        return

    raise ValueError(f"Unsupported event type: {type(event).__name__}")