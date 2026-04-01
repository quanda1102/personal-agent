"""
server.py — Home Agent unified entry point (conversational / text API)
──────────────────────────────────────────────────────────────────────
Loads `.env` via python-dotenv. See docs/configuration.md and .env.example.

Architecture (summary): conversational agent uses run() → note (read-only in chat),
queue, memory, skills. Heartbeat + coordinator are separate processes / main.py hooks;
see docs/agent.md.

  API mode (default)
  ──────────────────
    uv run server.py
    uv run server.py --port 8080
    uv run server.py --model qwen2.5:7b --reload

    FastAPI: REST + WebSocket + streaming chat. Session history in memory for
    the process lifetime.

  CLI mode
  ────────
    uv run server.py --cli
    uv run server.py --cli --model qwen2.5:7b
    uv run server.py --cli --debug          # show system prompt first

    Interactive terminal chat; history persists across turns.  exit / quit / Ctrl-C.

Common flags:
    --model  -m <name>   Override model (else OPENCLAWD_MODEL if OPENAI_API_KEY, else OLLAMA_MODEL)
    --debug              Print the assembled system prompt on startup
    --help   -h          Show this message

API server flags (ignored in CLI mode):
    --host  <addr>       Bind address (default: 0.0.0.0)
    --port  -p <n>       Port         (default: 8000)
    --reload             Hot-reload   (development only)

API endpoints:
    GET    /health
    GET    /sessions
    POST   /sessions
    GET    /sessions/{id}
    DELETE /sessions/{id}
    POST   /sessions/{id}/clear
    GET    /sessions/{id}/history
    POST   /chat                   ← sync REST chat (returns full response)
    WS     /ws/{session_id}        ← streaming WebSocket chat

Environment (see .env.example):
    HOMEAGENT_VAULT     Obsidian vault path (required for note/queue/heartbeat)
    OPENCLAWD_OLLAMA_HOST / OLLAMA_MODEL   Local LLM
    OPENAI_API_KEY      Cloud OpenAI (optional)
    HOMEAGENT_ENABLE_COORDINATOR   Not used here — see main.py for voice stack
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import uvicorn
from dotenv import load_dotenv

load_dotenv()

from src.agent.exec_role import ROLE_CONVERSATION
from src.agent.executor import RoleScopedExecutor
from src.agent.loop    import Runner, RunContext
from src.agent.prompt  import PromptBuilder
from src.agent.handler import CLIStreamHandler
from src.llm_provider.chat_provider import build_chat_provider
from src.api.server          import create_app

HOST = os.environ.get("OPENCLAWD_OLLAMA_HOST", "http://localhost:11434")
SEP = "─" * 60

SKILLS_ROOT = Path(__file__).parent / "skills"
os.environ.setdefault("OPENCLAWD_SKILLS_ROOT", str(SKILLS_ROOT))


def _make_runner(model: str | None) -> tuple[Runner, str]:
    """Build (runner, system_prompt). model None → env (OPENAI_API_KEY vs Ollama)."""
    provider      = build_chat_provider(model_override=model)
    runner        = Runner(provider=provider, max_tool_calls=15)
    system_prompt = PromptBuilder(skills_root=SKILLS_ROOT).build()
    return runner, system_prompt


# ── CLI chat mode ──────────────────────────────────────────────────────────────

async def cli_chat(runner: Runner, system_prompt: str, model: str | None, debug: bool) -> None:
    """
    Interactive terminal chat with persistent conversation history.

    Key improvement over the old main.py:
      main.py called asyncio.run(run(msg)) in a loop — each turn started a
      fresh RunContext with empty messages, so the agent had no memory of
      what was said 2 turns ago.

      Here, a single messages list is shared across all turns.  The agent
      accumulates real multi-turn context exactly like a WebSocket session.
    """
    messages:   list[dict] = []
    session_id: str        = f"cli-{uuid.uuid4().hex[:8]}"

    shown_model = model or runner.provider.model
    print(SEP)
    print(f"  model    {shown_model}  @  {HOST}")
    print(f"  session  {session_id}")
    print(f"  quit     exit / Ctrl-C")
    print(SEP)

    if debug:
        print("\nSYSTEM PROMPT:")
        print(SEP)
        print(system_prompt)
        print(SEP)

    print()

    while True:
        # ── Read user input ────────────────────────────────────────────────────
        try:
            msg = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[bye]")
            break

        if not msg:
            continue
        if msg.lower() in ("exit", "quit", ":q"):
            print("[bye]")
            break

        # ── Run one turn ───────────────────────────────────────────────────────
        ctx = RunContext(
            user_message  = msg,
            system_prompt = system_prompt,
            session_id    = session_id,
            messages      = messages,      # shared list — history persists
            handler       = CLIStreamHandler(),
            executor      = RoleScopedExecutor(ROLE_CONVERSATION),
        )

        try:
            usage = await runner.run(ctx)
        except KeyboardInterrupt:
            print("\n[interrupted — history kept]")
            # Pop the last user message so the incomplete turn isn't stuck in history
            if messages and messages[-1].get("role") == "user":
                messages.pop()
            continue
        except Exception as exc:
            print(f"\n[error] {type(exc).__name__}: {exc}")
            print(f"If using Ollama: ollama serve && ollama pull {shown_model}")
            if messages and messages[-1].get("role") == "user":
                messages.pop()
            continue

        print(f"\n{SEP}")
        print(f"  {usage}")
        print(f"{SEP}\n")


# ── CLI argument parser ─────────────────────────────────────────────────────────

def _parse() -> dict:
    args = sys.argv[1:]

    opts: dict = {
        "mode":      "serve",     # "serve" | "cli"
        "bind_host": "0.0.0.0",
        "port":      8000,
        "model":     None,        # None → OPENAI_API_KEY ? OPENCLAWD_MODEL : OLLAMA_MODEL
        "reload":    False,
        "debug":     False,
    }

    i = 0
    while i < len(args):
        a = args[i]
        if a == "--cli":
            opts["mode"] = "cli";            i += 1
        elif a in ("--host",) and i + 1 < len(args):
            opts["bind_host"] = args[i + 1]; i += 2
        elif a in ("--port", "-p") and i + 1 < len(args):
            opts["port"] = int(args[i + 1]); i += 2
        elif a in ("--model", "-m") and i + 1 < len(args):
            opts["model"] = args[i + 1];     i += 2
        elif a == "--reload":
            opts["reload"] = True;           i += 1
        elif a == "--debug":
            opts["debug"]  = True;           i += 1
        elif a in ("-h", "--help"):
            print(__doc__); sys.exit(0)
        else:
            i += 1

    return opts


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    opts = _parse()

    runner, system_prompt = _make_runner(opts["model"])

    # ── CLI mode ───────────────────────────────────────────────────────────────
    if opts["mode"] == "cli":
        asyncio.run(cli_chat(
            runner        = runner,
            system_prompt = system_prompt,
            model         = opts["model"],
            debug         = opts["debug"],
        ))

    # ── API server mode ────────────────────────────────────────────────────────
    else:
        app = create_app(runner=runner, system_prompt=system_prompt)

        if opts["debug"]:
            print("SYSTEM PROMPT:")
            print("─" * 60)
            print(system_prompt)
            print("─" * 60 + "\n")

        print("─" * 60)
        print(f"  model   {runner.provider.model}")
        print(f"  server  http://{opts['bind_host']}:{opts['port']}")
        print(f"  ws      ws://{opts['bind_host']}:{opts['port']}/ws/{{session_id}}")
        print(f"  chat    POST http://{opts['bind_host']}:{opts['port']}/chat")
        print(f"  browser http://{opts['bind_host']}:{opts['port']}/  (chat UI + Heartbeat test panel)")
        print(f"  docs    http://{opts['bind_host']}:{opts['port']}/docs")
        print("─" * 60 + "\n")

        uvicorn.run(
            app,
            host      = opts["bind_host"],
            port      = opts["port"],
            reload    = opts["reload"],
            log_level = "info",
        )
