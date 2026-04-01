from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from src.s2s.stt_engine import (
    Config as STTConfig,
    Engine as STTEngine,
)
from src.s2s.tts_engine import (
    Config as TTSConfig,
    Engine as TTSEngine,
)
from src.s2s.events import (
    AgentTextDelta,
    AudioChunkIn,
    AudioChunkOut,
    Error,
    Interrupt,
    SessionEnd,
    SessionStart,
    STTResult,
    TurnComplete,
)
from src.agent.handler import StreamHandler
from src.agent.events import EventType
from src.agent.exec_role import ROLE_CONVERSATION
from src.agent.executor import RoleScopedExecutor
from src.agent.loop import Runner, RunContext

logger = logging.getLogger(__name__)

model_path     = Path(__file__).resolve().parents[2] / "voice_model"
tts_model_path = Path(__file__).resolve().parents[2] / "tts_model" / "vits-piper-vi_VN-vais1000-medium-int8"


# ---------------------------------------------------------------------------
# Module-level agent singleton.
#
# Set once at app startup via configure_agent().  VoicePipeline._run_agent
# uses it when available; falls back to a hard-coded mock otherwise so the
# server can start without a real LLM configured.
# ---------------------------------------------------------------------------

_agent_runner:        Runner | None = None
_agent_system_prompt: str           = ""


def configure_agent(runner: Runner, system_prompt: str) -> None:
    """Register the shared Runner + system-prompt used by all voice sessions."""
    global _agent_runner, _agent_system_prompt
    _agent_runner        = runner
    _agent_system_prompt = system_prompt
    logger.info("Voice pipeline agent configured (model=%s)", runner.provider.model)


# ---------------------------------------------------------------------------
# Internal handler: bridges Runner's sync handle() callbacks → asyncio.Queue
# ---------------------------------------------------------------------------

class _QueueStreamHandler(StreamHandler):
    """
    Collects TextDelta events from the Runner and makes them available as an
    async stream.  The Runner calls handle() synchronously from within the
    asyncio event loop, so put_nowait() is safe.
    """

    def __init__(self, queue: asyncio.Queue[str | None]) -> None:
        self._queue = queue

    def handle(self, event: Any) -> None:
        if event.type == EventType.TEXT_DELTA:
            self._queue.put_nowait(event.text)
        elif event.type in (EventType.STREAM_END, EventType.STREAM_ERROR):
            self._queue.put_nowait(None)  # sentinel — consumer exits loop


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

class PipelineState(str, Enum):
    IDLE         = "idle"
    READY        = "ready"
    LISTENING    = "listening"   # collecting audio chunks
    TRANSCRIBING = "transcribing"  # running STT (awaiting thread)
    RUNNING_AGENT = "running_agent"
    SYNTHESIZING = "synthesizing"
    INTERRUPTED  = "interrupted"
    CLOSED       = "closed"


# ---------------------------------------------------------------------------
# Config / runtime dataclasses
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class PipelineConfig:
    input_sample_rate: int  = 16000
    output_sample_rate: int = 24000
    max_buffer_bytes: int   = 10 * 1024 * 1024  # 10 MB
    emit_agent_text_delta: bool      = True
    emit_session_end_after_turn: bool = True


@dataclass(slots=True)
class PipelineRuntime:
    session_id: str
    state: PipelineState            = PipelineState.IDLE
    input_sample_rate: int          = 16000
    output_sample_rate: int         = 24000
    metadata: dict[str, Any]        = field(default_factory=dict)
    audio_buffer: bytearray         = field(default_factory=bytearray)
    final_transcript: str           = ""
    output_seq: int                 = 0
    interrupted: bool               = False
    opened: bool                    = False
    closed: bool                    = False
    # Conversation history shared across turns within the same voice session.
    messages: list[dict]            = field(default_factory=list)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class VoicePipeline:
    """
    Offline speech-to-speech pipeline backed by sherpa-onnx OfflineRecognizer.

    Flow per turn:
        SessionStart  → reset session state, mark READY
        AudioChunkIn  → buffer raw PCM bytes (no partial STT — offline model)
        TurnComplete  → run STT on buffered audio → Agent → TTS → SessionEnd
        Interrupt     → discard buffer, stop in-flight work

    The OfflineRecognizer does not support streaming partials: all audio is
    collected first; a single create_stream / accept_waveform / decode_stream
    call produces the transcript.
    """

    def __init__(self, session_id: str, config: PipelineConfig | None = None):
        self.config  = config or PipelineConfig()
        self.runtime = PipelineRuntime(
            session_id=session_id,
            input_sample_rate=self.config.input_sample_rate,
            output_sample_rate=self.config.output_sample_rate,
        )
        self._lock       = asyncio.Lock()
        self._stt_engine = STTEngine(STTConfig(model_dir=model_path))
        self._tts_engine = TTSEngine(TTSConfig(model_dir=tts_model_path))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def open(self) -> None:
        """
        Prepare the pipeline for a new WebSocket session.
        Resolves the shared recognizer singleton (fast after app startup
        pre-warm, otherwise loads in a thread on first call).
        """
        if self.runtime.opened:
            return
        await self._stt_engine.ensure_loaded()
        await self._tts_engine.ensure_loaded()
        self.runtime.output_sample_rate = self._tts_engine.sample_rate
        self.runtime.opened = True
        self.runtime.state  = PipelineState.IDLE

    async def close(self) -> None:
        if self.runtime.closed:
            return
        self.runtime.closed  = True
        self.runtime.state   = PipelineState.CLOSED
        self.runtime.audio_buffer.clear()
        self.runtime.final_transcript = ""

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    async def on_session_start(self, event: SessionStart) -> AsyncIterator[Error]:
        """Reset session state and mark the pipeline READY."""
        async with self._lock:
            if self.runtime.closed:
                error: Error | None = Error(
                    message="Pipeline is already closed",
                    code="PIPELINE_CLOSED",
                )
            else:
                error = None
                self.runtime.input_sample_rate = event.sample_rate
                self.runtime.metadata          = dict(event.metadata or {})
                self.runtime.state             = PipelineState.READY
                self.runtime.interrupted       = False
                self.runtime.final_transcript  = ""
                self.runtime.audio_buffer.clear()

        if error is not None:
            yield error
            return

        if False:  # makes this function an async generator
            yield

    async def on_audio_chunk(
        self,
        event: AudioChunkIn,
    ) -> AsyncIterator[Error]:
        """
        Buffer incoming audio for the current turn.

        No partial STT is emitted — OfflineRecognizer has no streaming
        partials.  Transcription happens only on TurnComplete.
        """
        early_error: Error | None = None

        async with self._lock:
            if self.runtime.closed:
                early_error = Error(
                    message="Cannot receive audio: pipeline closed",
                    code="PIPELINE_CLOSED",
                )
            else:
                # Accept audio from READY or LISTENING; reject from other states.
                if self.runtime.state in (
                    PipelineState.IDLE,
                    PipelineState.READY,
                    PipelineState.INTERRUPTED,
                ):
                    self.runtime.state       = PipelineState.LISTENING
                    self.runtime.interrupted = False

                if self.runtime.state != PipelineState.LISTENING:
                    early_error = Error(
                        message=f"AudioChunk not allowed in state {self.runtime.state}",
                        code="INVALID_STATE",
                        detail={"state": self.runtime.state},
                    )
                else:
                    next_size = len(self.runtime.audio_buffer) + len(event.data)
                    if next_size > self.config.max_buffer_bytes:
                        early_error = Error(
                            message="Audio buffer exceeded limit",
                            code="AUDIO_BUFFER_OVERFLOW",
                            detail={
                                "max_buffer_bytes": self.config.max_buffer_bytes,
                                "current_bytes":    len(self.runtime.audio_buffer),
                                "incoming_bytes":   len(event.data),
                            },
                        )
                    else:
                        self.runtime.audio_buffer.extend(event.data)

        if early_error is not None:
            yield early_error
            return

        # Interrupt may have arrived while we were outside the lock.
        if self.runtime.interrupted:
            self.runtime.state = PipelineState.INTERRUPTED

        if False:  # makes this function an async generator
            yield

    async def on_turn_complete(
        self,
        event: TurnComplete,
    ) -> AsyncIterator[STTResult | AgentTextDelta | AudioChunkOut | SessionEnd | Error]:
        """
        Run the full offline pipeline:
            1. Transcribe buffered audio  (OfflineRecognizer in thread pool)
            2. Run agent on transcript
            3. Synthesize TTS
            4. Emit SessionEnd
        """
        turn_error: Error | None = None
        audio_bytes: bytes = b""

        async with self._lock:
            if self.runtime.closed:
                turn_error = Error(
                    message="Cannot process turn: pipeline closed",
                    code="PIPELINE_CLOSED",
                )
            elif self.runtime.state != PipelineState.LISTENING:
                turn_error = Error(
                    message=f"TurnComplete not allowed in state {self.runtime.state}",
                    code="INVALID_STATE",
                    detail={"state": self.runtime.state},
                )
            elif not self.runtime.audio_buffer:
                turn_error = Error(
                    message="No audio buffered for TurnComplete",
                    code="EMPTY_AUDIO_BUFFER",
                )
            else:
                self.runtime.state       = PipelineState.TRANSCRIBING
                self.runtime.interrupted = False
                audio_bytes = bytes(self.runtime.audio_buffer)

        if turn_error is not None:
            yield turn_error
            return

        # ------ STT -------------------------------------------------------
        try:
            final_text = await self._stt_engine.transcribe(
                audio_bytes,
                self.runtime.input_sample_rate,
            )
        except Exception as exc:
            self.runtime.state = PipelineState.READY
            yield Error(
                message="STT transcription failed",
                code="STT_FAILED",
                detail={"reason": str(exc)},
            )
            return

        if self.runtime.interrupted:
            self.runtime.state = PipelineState.INTERRUPTED
            return

        self.runtime.final_transcript = final_text or ""

        yield STTResult(
            text=self.runtime.final_transcript,
            is_final=True,
            language="vi",
            confidence=1.0,
        )

        # ------ Agent -----------------------------------------------------
        self.runtime.state = PipelineState.RUNNING_AGENT
        accumulated: list[str] = []

        try:
            async for delta in self._run_agent(self.runtime.final_transcript):
                if self.runtime.interrupted:
                    self.runtime.state = PipelineState.INTERRUPTED
                    return
                accumulated.append(delta)
                if self.config.emit_agent_text_delta:
                    yield AgentTextDelta(text=delta, run_id=None)
        except Exception as exc:
            self.runtime.state = PipelineState.READY
            yield Error(
                message="Agent failed",
                code="AGENT_FAILED",
                detail={"reason": str(exc)},
            )
            return

        agent_reply = "".join(accumulated).strip()
        logger.info(
            "session=%s agent_reply=%r  (%d chars)",
            self.runtime.session_id,
            agent_reply[:200] + ("…" if len(agent_reply) > 200 else ""),
            len(agent_reply),
        )

        if self.runtime.interrupted:
            self.runtime.state = PipelineState.INTERRUPTED
            return

        # ------ TTS -------------------------------------------------------
        self.runtime.state = PipelineState.SYNTHESIZING

        try:
            async for chunk in self._run_tts(agent_reply):
                if self.runtime.interrupted:
                    self.runtime.state = PipelineState.INTERRUPTED
                    return
                self.runtime.output_seq += 1
                yield AudioChunkOut(
                    data=chunk,
                    sample_rate=self.runtime.output_sample_rate,
                    seq=self.runtime.output_seq,
                )
        except Exception as exc:
            self.runtime.state = PipelineState.READY
            yield Error(
                message="TTS failed",
                code="TTS_FAILED",
                detail={"reason": str(exc)},
            )
            return

        # ------ Reset for next turn ----------------------------------------
        async with self._lock:
            self.runtime.audio_buffer.clear()
            self.runtime.final_transcript = ""
            self.runtime.state = PipelineState.READY

        if self.config.emit_session_end_after_turn:
            yield SessionEnd(reason="turn.complete")

    async def on_interrupt(
        self,
        event: Interrupt,
    ) -> AsyncIterator[SessionEnd | Error]:
        interrupt_error: Error | None = None

        async with self._lock:
            if self.runtime.closed:
                interrupt_error = Error(
                    message="Cannot interrupt: pipeline closed",
                    code="PIPELINE_CLOSED",
                )
            else:
                self.runtime.interrupted    = True
                self.runtime.audio_buffer.clear()
                self.runtime.final_transcript = ""
                self.runtime.state          = PipelineState.INTERRUPTED

        if interrupt_error is not None:
            yield interrupt_error
            return

        yield SessionEnd(reason=event.reason or "interrupted")

    # ------------------------------------------------------------------
    # Stub implementations (replace with real agent / TTS)
    # ------------------------------------------------------------------

    async def _run_agent(self, transcript: str) -> AsyncIterator[str]:
        """Stream text deltas from the real agent, or fall back to a mock."""
        if _agent_runner is None:
            logger.warning(
                "No agent configured — using mock reply. "
                "Call configure_agent() at startup to use the real agent."
            )
            reply = f"Tôi nhận được: {transcript}"
            for piece in self._chunk_text(reply, chunk_size=14):
                await asyncio.sleep(0.02)
                yield piece
            return

        queue: asyncio.Queue[str | None] = asyncio.Queue()
        handler = _QueueStreamHandler(queue)

        ctx = RunContext(
            user_message  = transcript,
            system_prompt = _agent_system_prompt,
            session_id    = self.runtime.session_id,
            messages      = self.runtime.messages,   # persists across turns
            handler       = handler,
            executor      = RoleScopedExecutor(ROLE_CONVERSATION),
        )

        async def _do_run() -> None:
            try:
                await _agent_runner.run(ctx)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Agent run failed for session=%s", self.runtime.session_id)
            finally:
                # Always guarantee the consumer gets a sentinel so it can exit.
                # The handler already sends one on StreamEnd/StreamError, but
                # putting a second None is harmless — the consumer breaks on
                # the first and never sees the extra item.
                queue.put_nowait(None)

        run_task = asyncio.create_task(_do_run())

        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item
        finally:
            if not run_task.done():
                run_task.cancel()
            try:
                await run_task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run_tts(self, text: str) -> AsyncIterator[bytes]:
        if not text.strip():
            return
        async for chunk in self._tts_engine.synthesize(text):
            yield chunk

    @staticmethod
    def _chunk_text(text: str, chunk_size: int = 20) -> list[str]:
        return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]
