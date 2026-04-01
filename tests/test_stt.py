"""Quick standalone STT test.

Usage (from the home_agent directory):
    uv run tests/test_stt.py path/to/audio.m4a
    uv run tests/test_stt.py          # defaults to the test m4a in tests/

ffmpeg is required to convert m4a → raw PCM.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from pathlib import Path

# Make `src` importable when running the script directly from any directory.
sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("test_stt")

DEFAULT_AUDIO = Path(__file__).parent / "Đường Tam Trinh 12.m4a"
MODEL_DIR = Path(__file__).parent.parent / "voice_model"


def decode_to_pcm(audio_path: Path, sample_rate: int = 16_000) -> bytes:
    """Use ffmpeg to decode any audio file → raw PCM-16-LE mono."""
    logger.info("Decoding %s with ffmpeg …", audio_path)
    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i", str(audio_path),
            "-ar", str(sample_rate),   # resample to target rate
            "-ac", "1",                # mono
            "-f", "s16le",             # raw PCM-16-LE, no container
            "pipe:1",                  # write to stdout
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    pcm = result.stdout
    duration = len(pcm) / 2 / sample_rate  # 2 bytes per sample
    logger.info("Decoded: %d bytes  (%.2f s)", len(pcm), duration)
    return pcm


async def main() -> None:
    audio_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_AUDIO
    if not audio_path.exists():
        logger.error("Audio file not found: %s", audio_path)
        sys.exit(1)

    # Import here so the script can be run from the home_agent root where
    # the src package is on the path.
    from src.s2s.stt_engine import Config, Engine

    config = Config(model_dir=MODEL_DIR)
    engine = Engine(config)

    logger.info("Loading STT model …")
    await engine.ensure_loaded()
    logger.info("Model ready.")

    pcm = decode_to_pcm(audio_path, sample_rate=config.sample_rate)

    logger.info("Running transcription …")
    text = await engine.transcribe(pcm, sample_rate=config.sample_rate)

    print("\n" + "=" * 60)
    print("TRANSCRIPT:", text or "(empty)")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
