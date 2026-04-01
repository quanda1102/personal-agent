hello boy

## What this is

**home-agent** ties together:

- **Conversational agent** — voice (`main.py`) or text API / CLI (`server.py`): one tool, `run()`, driving memory, skills, **note** (vault), and **queue**.
- **Heartbeat** — scheduled job: digest → LLM plan → optional execute with full vault access (`python -m src.heartbeat.run`).
- **Coordinator** (optional) — background poll; can spawn heartbeat when the queue is deep (`HOMEAGENT_ENABLE_COORDINATOR`).

Architecture diagram and data flow: [docs/agent.md](docs/agent.md).

## Configuration

1. `cp .env.example .env`
2. Set **`HOMEAGENT_VAULT`** and LLM variables (Ollama and/or OpenAI).
3. Full variable reference: [docs/configuration.md](docs/configuration.md).

## Entry points (run from repo root)

| Command | Purpose |
|---------|---------|
| `uv run python main.py` | Voice stack: STT/TTS + agent; optional coordinator if env set. |
| `uv run server.py` | Text API + **WebSocket** `/ws/{session}`. Browser UI at **http://127.0.0.1:8000/** streams over WS (not `file://`). |
| `uv run server.py --cli` | Interactive terminal chat. |
| `uv run python -m src.heartbeat.run --mode evening` | Heartbeat plan (+ execute unless `--plan-only` / `--no-llm`). |
| `uv run python -m src.heartbeat.run --check-depth` | Exit 1 if pending queue ≥ threshold (for alerting). |

Internal routing for `run()` lives in `src/cli_handler/router.py` and `dispatch.py`.

## More docs

- [docs/heartbeat-scheduling.md](docs/heartbeat-scheduling.md) — cron, `--check-depth`, APScheduler
