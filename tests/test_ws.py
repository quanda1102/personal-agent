"""WebSocket end-to-end voice pipeline test.

Decodes an audio file with ffmpeg, streams it to the voice pipeline as
100 ms audio.chunk messages, sends turn.complete, then prints every server
event.  Received TTS audio chunks are reassembled and saved as a WAV file.

Usage (server must be running on localhost:8000):
    uv run tests/test_ws.py [path/to/audio.m4a] [--session SESSION_ID] [--url WS_URL]

Defaults:
    audio   → tests/Đường Tam Trinh 12.m4a
    session → test-001
    url     → ws://localhost:8000/ws/voice/{session}
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import struct
import subprocess
import sys
import time
from pathlib import Path

import soundfile as sf
import numpy as np
import websockets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("test_ws")

DEFAULT_AUDIO   = Path(__file__).parent / "test_audio.m4a"
DEFAULT_SESSION = "test-001"
DEFAULT_URL     = "ws://localhost:8000/ws/voice/{session}"

SAMPLE_RATE  = 16_000
CHUNK_MS     = 100                          # ms per audio.chunk message
CHUNK_BYTES  = SAMPLE_RATE * CHUNK_MS // 1000 * 2   # 16-bit PCM = 2 bytes/sample


def decode_to_pcm(audio_path: Path, sample_rate: int = SAMPLE_RATE) -> bytes:
    logger.info("Decoding %s → raw PCM …", audio_path.name)
    result = subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", str(audio_path),
            "-ar", str(sample_rate),
            "-ac", "1",
            "-f", "s16le",
            "pipe:1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    pcm = result.stdout
    duration = len(pcm) / 2 / sample_rate
    logger.info("Decoded: %d bytes  (%.2f s)  →  %d chunks of %d ms",
                len(pcm), duration, -(-len(pcm) // CHUNK_BYTES), CHUNK_MS)
    return pcm


def _send(type_: str, **kwargs) -> str:
    return json.dumps({"type": type_, **kwargs})


async def run(audio_path: Path, session_id: str, url: str) -> None:
    pcm = decode_to_pcm(audio_path)

    # Collect TTS PCM chunks for saving to WAV at the end.
    tts_chunks:   list[bytes] = []
    tts_rate:     int         = 0

    async with websockets.connect(url, max_size=64 * 1024 * 1024) as ws:
        logger.info("Connected → %s", url)

        # 1. session.start
        await ws.send(_send("session.start", sample_rate=SAMPLE_RATE))
        logger.info("→ session.start")

        # 2. audio.chunk  (stream in CHUNK_MS slices)
        total_chunks = 0
        for i in range(0, len(pcm), CHUNK_BYTES):
            chunk = pcm[i: i + CHUNK_BYTES]
            b64   = base64.b64encode(chunk).decode()
            await ws.send(_send("audio.chunk",
                                sample_rate=SAMPLE_RATE,
                                seq=total_chunks,
                                data=b64))
            total_chunks += 1

        logger.info("→ %d audio.chunk messages sent", total_chunks)

        # 3. turn.complete
        await ws.send(_send("turn.complete"))
        logger.info("→ turn.complete")

        # 4. Receive until session.end
        print("\n" + "=" * 60)
        t0 = time.monotonic()
        async for raw in ws:
            msg     = json.loads(raw)
            etype   = msg.get("type", "?")
            elapsed = time.monotonic() - t0

            if etype == "stt.result":
                print(f"\n[{elapsed:5.2f}s] STT  → {msg.get('text', '')!r}")

            elif etype == "agent.text_delta":
                print(msg.get("text", ""), end="", flush=True)

            elif etype == "audio.chunk":
                raw_data = base64.b64decode(msg.get("data", ""))
                tts_chunks.append(raw_data)
                tts_rate = msg.get("sample_rate", tts_rate)
                seq      = msg.get("seq", "?")
                print(f"\n[{elapsed:5.2f}s] AUDIO← seq={seq}  {len(raw_data)} bytes", end="")

            elif etype == "session.end":
                print(f"\n[{elapsed:5.2f}s] SESSION END")
                break

            elif etype == "error":
                print(f"\n[{elapsed:5.2f}s] ERROR  code={msg.get('code')}  {msg.get('message')}")
                detail = msg.get("detail")
                if detail:
                    print(f"         detail={detail}")

            else:
                print(f"\n[{elapsed:5.2f}s] {etype}  {msg}")

        print("\n" + "=" * 60)

    # Save TTS output to WAV so you can actually listen to it.
    if tts_chunks and tts_rate:
        out_wav = audio_path.parent / f"{session_id}-tts-reply.wav"
        _save_pcm16le_wav(b"".join(tts_chunks), tts_rate, out_wav)
        total_s = sum(len(c) for c in tts_chunks) / 2 / tts_rate
        print(f"\nTTS audio saved → {out_wav}  ({total_s:.2f}s)")
    else:
        print("\n(no TTS audio received)")


def _save_pcm16le_wav(pcm: bytes, sample_rate: int, path: Path) -> None:
    """Write raw PCM-16-LE bytes to a proper WAV file via soundfile."""
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    sf.write(str(path), samples, sample_rate, subtype="PCM_16")


def main() -> None:
    parser = argparse.ArgumentParser(description="WebSocket STT smoke test")
    parser.add_argument("audio",    nargs="?", type=Path, default=DEFAULT_AUDIO)
    parser.add_argument("--session", default=DEFAULT_SESSION)
    parser.add_argument("--url",     default=None,
                        help="Full WS URL (overrides --session)")
    args = parser.parse_args()

    url = args.url or DEFAULT_URL.format(session=args.session)

    if not args.audio.exists():
        logger.error("Audio file not found: %s", args.audio)
        sys.exit(1)

    asyncio.run(run(args.audio, args.session, url))


if __name__ == "__main__":
    main()
