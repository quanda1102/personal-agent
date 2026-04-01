from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from . import _sherpa_deps

_sherpa_deps.ensure_onnxruntime_loaded()
import sherpa_onnx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level recognizer singleton.
#
# OfflineRecognizer.from_transducer() triggers expensive ONNX graph
# optimization (10-30 s).  We load it once per process in a thread pool
# worker so the asyncio event loop is never blocked, then share the read-only
# recognizer object across all sessions.
#
# The recognizer is stateless with respect to audio: every recognition call
# creates an independent OfflineStream, so sharing is safe.
# ---------------------------------------------------------------------------

_recognizer: sherpa_onnx.OfflineRecognizer | None = None
_recognizer_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    global _recognizer_lock
    if _recognizer_lock is None:
        _recognizer_lock = asyncio.Lock()
    return _recognizer_lock


def _build_recognizer_sync(
    tokens: str,
    encoder: str,
    decoder: str,
    joiner: str,
    num_threads: int,
    sample_rate: int,
    feature_dim: int,
    provider: str,
    decoding_method: str,
) -> sherpa_onnx.OfflineRecognizer:
    """Blocking call — must be run in a thread pool worker."""
    return sherpa_onnx.OfflineRecognizer.from_transducer(
        tokens=tokens,
        encoder=encoder,
        decoder=decoder,
        joiner=joiner,
        num_threads=num_threads,
        sample_rate=sample_rate,
        feature_dim=feature_dim,
        provider=provider,
        decoding_method=decoding_method,
    )


async def preload_recognizer(config: Config) -> None:
    """
    Load the shared recognizer in a thread pool worker.
    Safe to call multiple times; subsequent calls return immediately once
    the recognizer is already cached.
    """
    global _recognizer
    async with _get_lock():
        if _recognizer is not None:
            return
        model_dir = config.model_dir.resolve()
        tokens  = str(model_dir / config.tokens_file)
        encoder = str(model_dir / config.encoder_file)
        decoder = str(model_dir / config.decoder_file)
        joiner  = str(model_dir / config.joiner_file)
        logger.info(
            "Loading sherpa-onnx recognizer from %s (this may take a moment)…",
            model_dir,
        )
        _recognizer = await asyncio.to_thread(
            _build_recognizer_sync,
            tokens, encoder, decoder, joiner,
            config.num_threads, config.sample_rate,
            config.feature_dim, config.provider, config.decoding_method,
        )
        logger.info("sherpa-onnx recognizer ready.")


@dataclass(slots=True)
class Config:
    model_dir: Path = Path("voice_model")
    tokens_file: Path = Path("tokens.txt")
    encoder_file: Path = Path("encoder.int8.onnx")
    decoder_file: Path = Path("decoder.onnx")
    joiner_file: Path = Path("joiner.int8.onnx")

    sample_rate: int = 16000
    feature_dim: int = 80
    num_threads: int = 4
    provider: str = "cpu"
    decoding_method: str = "greedy_search"


class Engine:
    """
    Thin async wrapper around sherpa-onnx OfflineRecognizer.

    OfflineRecognizer usage (per sherpa-onnx docs):
        stream = recognizer.create_stream()          # fresh per utterance
        stream.accept_waveform(sample_rate, samples) # float32 [-1, +1]
        recognizer.decode_stream(stream)             # run inference
        text = recognizer.get_result(stream).text    # get transcript

    There are NO streaming partials, IS_READY checks, or per-stream resets —
    those belong to OnlineRecognizer.  This engine is intentionally simple:
    call transcribe() with all audio for a turn; get back the transcript.

    __init__ is fast (path setup + file validation only).
    Call await engine.ensure_loaded() once before the first transcribe().
    """

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or Config()

        self.model_dir = self.config.model_dir.resolve()
        self._tokens  = str(self.model_dir / self.config.tokens_file)
        self._encoder = str(self.model_dir / self.config.encoder_file)
        self._decoder = str(self.model_dir / self.config.decoder_file)
        self._joiner  = str(self.model_dir / self.config.joiner_file)

        self._validate_files()

        # Set by ensure_loaded(); points at the module-level singleton.
        self._recognizer: sherpa_onnx.OfflineRecognizer | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def ensure_loaded(self) -> None:
        """Resolve the shared recognizer, loading it on first call."""
        global _recognizer
        if self._recognizer is None:
            await preload_recognizer(self.config)
            self._recognizer = _recognizer

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    async def transcribe(self, audio_bytes: bytes, sample_rate: int) -> str:
        """
        Transcribe PCM-16-LE mono audio and return the transcript.

        Runs the ONNX inference in a thread pool worker so the event loop
        is never blocked.

        Args:
            audio_bytes: raw PCM-16-LE bytes (mono, little-endian int16).
            sample_rate: must match Config.sample_rate (default 16 000 Hz).
        """
        if sample_rate != self.config.sample_rate:
            raise ValueError(
                f"Expected sample_rate={self.config.sample_rate}, got {sample_rate}"
            )
        return await asyncio.to_thread(self._transcribe_sync, audio_bytes)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _validate_files(self) -> None:
        missing = [
            p for p in (self._tokens, self._encoder, self._decoder, self._joiner)
            if not Path(p).exists()
        ]
        if missing:
            raise FileNotFoundError(
                "Missing sherpa-onnx model files:\n" + "\n".join(missing)
            )

    def _transcribe_sync(self, audio_bytes: bytes) -> str:
        """Blocking transcription — must be called via asyncio.to_thread."""
        if self._recognizer is None:
            raise RuntimeError(
                "STT engine not loaded. Call await engine.ensure_loaded() first."
            )

        samples = _pcm16le_to_float32(audio_bytes)
        duration = samples.size / self.config.sample_rate
        logger.debug(
            "transcribe: audio_bytes=%d  samples=%d  duration=%.2fs",
            len(audio_bytes),
            samples.size,
            duration,
        )

        if samples.size == 0:
            logger.debug("transcribe: empty audio, skipping inference")
            return ""

        if duration > 30:
            logger.warning(
                "transcribe: audio is %.1fs — offline transducer models work best "
                "on clips under ~30s. Consider splitting turns. "
                "Also ensure you are sending raw PCM bytes, not a WAV file with header.",
                duration,
            )

        # OfflineRecognizer API (offline — NOT the same as OnlineRecognizer):
        # 1. create_stream()        → fresh OfflineStream per utterance
        # 2. accept_waveform()      → feed float32 audio into the stream
        # 3. decode_stream(stream)  → run inference
        # 4. stream.result          → OfflineRecognizerResult (.text, .tokens…)
        #
        # Note: get_result() exists only on OnlineRecognizer, not Offline.
        stream = self._recognizer.create_stream()
        stream.accept_waveform(self.config.sample_rate, samples)
        self._recognizer.decode_stream(stream)

        raw_result = stream.result
        text = _extract_text(raw_result)

        logger.debug(
            "transcribe result | text=%r | tokens=%s | raw=%r",
            text,
            getattr(raw_result, "tokens", "n/a"),
            raw_result,
        )

        return text


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pcm16le_to_float32(audio_bytes: bytes) -> np.ndarray:
    """Convert raw PCM-16-LE bytes to float32 in [-1, +1].

    Handles two common mistakes:
    - WAV file sent instead of raw PCM: the RIFF header is stripped
      automatically so the model receives clean audio samples.
    - Odd byte count: the trailing incomplete sample is dropped (inaudible).
    """
    if not audio_bytes:
        return np.array([], dtype=np.float32)

    # Strip WAV/RIFF container if present (44-byte canonical header).
    # Clients should send raw PCM, but this saves a confusing silent failure.
    if audio_bytes[:4] == b"RIFF":
        logger.warning(
            "Received a WAV file instead of raw PCM bytes. "
            "Stripping the RIFF header automatically. "
            "Send only the PCM payload for best results."
        )
        # The data chunk starts after: RIFF(4) + size(4) + WAVE(4) + fmt (24) = 44 bytes.
        # Walk chunks properly in case fmt chunk is non-standard size.
        import struct
        offset = 12  # skip "RIFF" + file-size + "WAVE"
        while offset + 8 <= len(audio_bytes):
            chunk_id   = audio_bytes[offset:offset + 4]
            chunk_size = struct.unpack_from("<I", audio_bytes, offset + 4)[0]
            offset += 8
            if chunk_id == b"data":
                audio_bytes = audio_bytes[offset: offset + chunk_size]
                break
            offset += chunk_size

    # Ensure even length (2 bytes per int16 sample).
    if len(audio_bytes) % 2:
        audio_bytes = audio_bytes[:-1]

    pcm = np.frombuffer(audio_bytes, dtype=np.int16)
    return pcm.astype(np.float32) / 32768.0


def _extract_text(result: Any) -> str:
    """Tolerant text extractor for different sherpa-onnx result shapes."""
    if result is None:
        return ""
    if isinstance(result, str):
        return _normalize(result)
    text = getattr(result, "text", "")
    if isinstance(text, str):
        return _normalize(text)
    return _normalize(str(result))


def _normalize(text: str) -> str:
    return " ".join(text.strip().split())
