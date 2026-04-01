"""
Voice + STT/TTS FastAPI entry (main.py).

Loads `.env`; builds Ollama-backed Runner + PromptBuilder; mounts s2s WebSocket routes.
Optional coordinator background task when HOMEAGENT_ENABLE_COORDINATOR is set.

Text-only API: use server.py. Scheduled vault work: python -m src.heartbeat.run
See docs/agent.md, docs/configuration.md, .env.example.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.s2s.pipeline import model_path, tts_model_path, configure_agent
from src.s2s.stt_engine import Config as STTConfig, preload_recognizer
from src.s2s.tts_engine import Config as TTSConfig, preload_tts
from src.s2s.router import router as s2s_router
from src.agent.loop import Runner
from src.agent.prompt import PromptBuilder
from src.llm_provider.chat_provider import build_chat_provider
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

SKILLS_ROOT = Path(__file__).parent / "skills"
os.environ.setdefault("OPENCLAWD_SKILLS_ROOT", str(SKILLS_ROOT))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Load STT + TTS models in thread-pool workers — never blocks the event loop.
    await asyncio.gather(
        preload_recognizer(STTConfig(model_dir=model_path)),
        preload_tts(TTSConfig(model_dir=tts_model_path)),
    )

    # 2. Build the real agent runner and register it with the voice pipeline.
    provider      = build_chat_provider(model_override=None)
    runner        = Runner(provider=provider, max_tool_calls=15)
    system_prompt = PromptBuilder(skills_root=SKILLS_ROOT).build()
    configure_agent(runner, system_prompt)

    coord_task = None
    coord_stop = None
    if os.environ.get("HOMEAGENT_ENABLE_COORDINATOR", "").lower() in ("1", "true", "yes"):
        from src.coordinator.service import start_coordinator_background

        coord_task, coord_stop = start_coordinator_background()

    yield

    if coord_stop is not None:
        coord_stop.set()
    if coord_task is not None:
        coord_task.cancel()
        try:
            await coord_task
        except asyncio.CancelledError:
            pass


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(s2s_router)

@app.get("/health")
async def health():
    return {"status": "ok"}
    
if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        ws_max_size=64 * 1024 * 1024,  # 64 MB
    )