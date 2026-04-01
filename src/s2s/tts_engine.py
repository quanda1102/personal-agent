"""
src.s2s.tts_engine
───────────────────
Thin async wrapper around sherpa-onnx OfflineTts (VITS/Piper).

Usage:
    config = Config(model_dir=Path("tts_model/vits-piper-vi_VN-vais1000-medium-int8"))
    engine = Engine(config)
    await engine.ensure_loaded()

    async for pcm_bytes in engine.synthesize("Xin chào"):
        # pcm_bytes: raw PCM-16-LE mono at engine.sample_rate
        ...

Architecture mirrors stt_engine.py:
  - Heavy ONNX graph loading happens once in a thread-pool worker.
  - A module-level singleton is shared across all sessions (OfflineTts is
    read-only after construction, so sharing is safe).
  - synthesize() streams the output in fixed-size chunks so downstream
    consumers (WebSocket sender) get data as soon as it is ready.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import _sherpa_deps

_sherpa_deps.ensure_onnxruntime_loaded()
import sherpa_onnx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_tts: sherpa_onnx.OfflineTts | None = None
_tts_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    global _tts_lock
    if _tts_lock is None:
        _tts_lock = asyncio.Lock()
    return _tts_lock


def _build_tts_sync(
    model: str,
    tokens: str,
    data_dir: str,
    num_threads: int,
    provider: str,
    noise_scale: float,
    noise_scale_w: float,
    length_scale: float,
) -> sherpa_onnx.OfflineTts:
    """Blocking constructor — run in a thread-pool worker."""
    vits = sherpa_onnx.OfflineTtsVitsModelConfig(
        model=model,
        tokens=tokens,
        data_dir=data_dir,
        noise_scale=noise_scale,
        noise_scale_w=noise_scale_w,
        length_scale=length_scale,
    )
    model_cfg = sherpa_onnx.OfflineTtsModelConfig(
        vits=vits,
        num_threads=num_threads,
        provider=provider,
    )
    cfg = sherpa_onnx.OfflineTtsConfig(model=model_cfg, max_num_sentences=1)
    if not cfg.validate():
        logger.warning("TTS config validation failed — check model paths.")
    return sherpa_onnx.OfflineTts(cfg)


async def preload_tts(config: Config) -> None:
    """
    Load the shared TTS engine in a thread-pool worker.
    Safe to call multiple times; subsequent calls return immediately once
    the engine is cached.
    """
    global _tts
    async with _get_lock():
        if _tts is not None:
            return
        model_dir = config.model_dir.resolve()
        model    = str(model_dir / config.model_file)
        tokens   = str(model_dir / config.tokens_file)
        data_dir = str(model_dir / config.data_dir)
        logger.info("Loading sherpa-onnx TTS from %s …", model_dir)
        _tts = await asyncio.to_thread(
            _build_tts_sync,
            model, tokens, data_dir,
            config.num_threads, config.provider,
            config.noise_scale, config.noise_scale_w, config.length_scale,
        )
        logger.info(
            "sherpa-onnx TTS ready — sample_rate=%d  speakers=%d",
            _tts.sample_rate,
            _tts.num_speakers,
        )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Config:
    model_dir:     Path  = Path("tts_model/vits-piper-vi_VN-vais1000-medium-int8")
    model_file:    Path  = Path("vi_VN-vais1000-medium.onnx")
    tokens_file:   Path  = Path("tokens.txt")
    data_dir:      Path  = Path("espeak-ng-data")

    speaker_id:    int   = 0
    speed:         float = 1.0
    num_threads:   int   = 2
    provider:      str   = "cpu"
    noise_scale:   float = 0.667
    noise_scale_w: float = 0.8
    length_scale:  float = 1.0

    # How many samples to pack per PCM-16-LE chunk sent downstream.
    # 22050 * 0.1 ≈ 2205 samples → ~100 ms chunks.
    chunk_samples: int   = 2205


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class Engine:
    """
    Async TTS engine backed by sherpa-onnx OfflineTts.

    __init__ is fast (path setup + validation).
    Call await engine.ensure_loaded() once before the first synthesize().
    """

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or Config()
        self._validate_files()
        self._tts: sherpa_onnx.OfflineTts | None = None

    @property
    def sample_rate(self) -> int:
        """Sample rate of the loaded model (0 if not yet loaded)."""
        return self._tts.sample_rate if self._tts else 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def ensure_loaded(self) -> None:
        global _tts
        if self._tts is None:
            await preload_tts(self.config)
            self._tts = _tts

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """
        Synthesize text and yield raw PCM-16-LE mono bytes in chunks.

        Each chunk is approximately Config.chunk_samples samples (~100 ms).
        Runs ONNX inference in a thread-pool worker.
        """
        if not text.strip():
            return

        samples: np.ndarray = await asyncio.to_thread(self._synthesize_sync, text)

        if samples.size == 0:
            logger.warning("TTS produced empty audio for text=%r", text)
            return

        logger.debug(
            "synthesize: text=%r  samples=%d  duration=%.2fs",
            text[:60],
            samples.size,
            samples.size / self._tts.sample_rate,
        )

        pcm = _float32_to_pcm16le(samples)

        # Yield in fixed-size byte chunks so the WebSocket sender can start
        # streaming before the full synthesis is done.
        step = self.config.chunk_samples * 2  # 2 bytes per int16 sample
        for i in range(0, len(pcm), step):
            yield pcm[i: i + step]

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _synthesize_sync(self, text: str) -> np.ndarray:
        if self._tts is None:
            raise RuntimeError(
                "TTS engine not loaded. Call await engine.ensure_loaded() first."
            )
        audio = self._tts.generate(
            text,
            sid=self.config.speaker_id,
            speed=self.config.speed,
        )
        return np.array(audio.samples, dtype=np.float32)

    def _validate_files(self) -> None:
        model_dir = self.config.model_dir.resolve()
        required = [
            model_dir / self.config.model_file,
            model_dir / self.config.tokens_file,
            model_dir / self.config.data_dir,
        ]
        missing = [str(p) for p in required if not p.exists()]
        if missing:
            raise FileNotFoundError(
                "Missing TTS model files:\n" + "\n".join(missing)
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _float32_to_pcm16le(samples: np.ndarray) -> bytes:
    """Convert float32 [-1, +1] samples to raw PCM-16-LE bytes."""
    clipped = np.clip(samples, -1.0, 1.0)
    pcm16   = (clipped * 32767).astype(np.int16)
    return pcm16.tobytes()
