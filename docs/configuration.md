# Configuration guide

## Quick start

1. Copy the example environment file:

   ```bash
   cp .env.example .env
   ```

2. Set **`HOMEAGENT_VAULT`** to your Obsidian vault directory (must exist).

3. Configure an LLM (see below).

4. Start an entry point from the **project root** (directory containing `pyproject.toml`):

   ```bash
   uv run python main.py          # voice + STT/TTS (port 8000)
   uv run server.py               # text API + WebSocket + browser UI at http://127.0.0.1:8000/
   uv run server.py --cli         # terminal chat
   ```

   With `server.py` running, open **http://127.0.0.1:8000/** (redirects to `/chat-ui/`). The page uses **WebSocket** `ws://127.0.0.1:8000/ws/{session}` for streaming; do not open `static/chat/index.html` as a `file://` URL or the client cannot connect.

`server.py` and `main.py` call `load_dotenv()`, so variables in `.env` are picked up automatically. For **cron** or **systemd**, export the same variables or `set -a; source .env; set +a` before running commands.

---

## Architecture (what each part reads)

| Component | Role | Main env vars |
|-----------|------|----------------|
| **Conversational agent** | Voice (`main.py`) or text (`server.py`): `Runner` + `run()` tool | `OLLAMA_MODEL`, `OPENCLAWD_*`, `OPENAI_API_KEY`; vault via `note` / `queue` |
| **Execution role** | Chat runs as `conversation`: `note` is **read-only** (`ls`, `read`, `find`) | `HOMEAGENT_ALLOW_CHAT_VAULT_WRITE` to opt out |
| **Heartbeat** | Scheduled / on-demand: `python -m src.heartbeat.run` | Same LLM vars as above; `HEARTBEAT_MODEL` optional override |
| **Queue** | SQLite job queue under vault `.heartbeat/` | `HOMEAGENT_QUEUE_DB` optional |
| **Coordinator** | Optional loop in `main.py`: poll depth, spawn heartbeat | `HOMEAGENT_ENABLE_COORDINATOR`, `HOMEAGENT_HEARTBEAT_QUEUE_THRESHOLD` |
| **Memory** | `memory` CLI tool | `HOMEAGENT_MEMORY_DB` optional |
| **Semantic find** | `note find` embeddings | `OPENAI_API_KEY`, `HOMEAGENT_EMBED_MODEL` |

More detail: [docs/agent.md](agent.md).

---

## Vault

| Variable | Description |
|----------|-------------|
| `HOMEAGENT_VAULT` | Preferred: absolute path to vault root. |
| `OBSIDIAN_VAULT` | Fallback if `HOMEAGENT_VAULT` is unset. |

The vault holds markdown notes. Runtime data lives under **`.heartbeat/`** inside the vault (plans, logs, `conversation.jsonl`, default `queue.db`). That tree is excluded from normal `note ls` / `note find`.

---

## LLM backends

### OpenAI (cloud)

Set `OPENAI_API_KEY`. The SDK also reads **`OPENAI_BASE_URL`** if set (no need to duplicate in code). Use `OPENCLAWD_MODEL` for the chat model id **on that endpoint** — a `404 model not found` usually means the deployment name is wrong (Azure) or the id is not offered at that URL.

Used by: `server.py` / `main.py` via `build_chat_provider`, heartbeat when key is set, embeddings for `note find` when not using test JSON.

### Ollama (local)

When `OPENAI_API_KEY` is **unset** (or whitespace-only), `server.py` and `main.py` use **Ollama** only — put a **pulled** tag in `OLLAMA_MODEL`, not a random GPT name.

| Variable | Default |
|----------|---------|
| `OPENCLAWD_OLLAMA_HOST` | `http://localhost:11434` |
| `OLLAMA_MODEL` | `llama3.2` (via `build_chat_provider`) |
| `OPENCLAWD_MODEL` | Fallback name for Ollama if `OLLAMA_MODEL` unset |

**Heartbeat** model resolution: `HEARTBEAT_MODEL` → `OLLAMA_MODEL` → `OPENCLAWD_MODEL` → `llama3.2` (see `src/heartbeat/llm_client.py`).

---

## Heartbeat

| Variable | Default | Meaning |
|----------|---------|---------|
| `HOMEAGENT_HEARTBEAT_QUEUE_THRESHOLD` | `5` | `--check-depth` exit 1 when pending ≥ this; coordinator spawn threshold |
| `HOMEAGENT_HEARTBEAT_NO_LLM` | off | `1`/`true`: stub plan, no API (like `--no-llm`) |
| `HOMEAGENT_ENABLE_HEARTBEAT_TEST_API` | `1` | `0` disables `POST /heartbeat/test` (browser “Heartbeat test” panel) |

With **`server.py`** running, open the chat UI and expand **Heartbeat test** to run the same CLI as cron, or call `POST /heartbeat/test` with JSON `{"no_llm": true, "plan_only": false, "mode": "on-demand"}`.

CLI flags override or complement env: see [heartbeat-scheduling.md](heartbeat-scheduling.md).

---

## Queue and memory

| Variable | Default |
|----------|---------|
| `HOMEAGENT_QUEUE_DB` | `{VAULT}/.heartbeat/queue.db` |
| `HOMEAGENT_MEMORY_DB` | Project `data/` layout (see `memory/store.py`) |

---

## Safety and development

| Variable | Meaning |
|----------|---------|
| `HOMEAGENT_ALLOW_CHAT_VAULT_WRITE` | `1`/`true`: allow mutating `note` subcommands from conversational agents (not recommended). |
| `HOMEAGENT_TEST_EMBED_JSON` | Tests only: path to JSON fixture for embeddings. |

---

## Skills

| Variable | Meaning |
|----------|---------|
| `OPENCLAWD_SKILLS_ROOT` | Directory of skill folders (often set by `main.py` / `server.py`). |
| `OPENCLAWD_PERSISTENT_ROOT` | Optional persistent workspace root for skills loader. |

---

## Troubleshooting

**`libonnxruntime.*.dylib` / `ImportError` when importing `sherpa_onnx` (voice stack)**  
Ensure `onnxruntime` is installed (see `pyproject.toml`). The app preloads `onnxruntime/capi/libonnxruntime.*` before `sherpa_onnx` (`src/s2s/_sherpa_deps.py`). If it still fails, reinstall matching versions: `uv sync` or `pip install onnxruntime==1.23.2 sherpa-onnx`.

**Text API only**  
`uv run server.py` does not import the voice stack. Use that if you do not need STT/TTS.

---

## Checklist

- [ ] `HOMEAGENT_VAULT` points to a real directory  
- [ ] Ollama running + model pulled, **or** `OPENAI_API_KEY` set  
- [ ] For `note find` with live embeddings: `OPENAI_API_KEY` + `HOMEAGENT_EMBED_MODEL`  
- [ ] Cron/systemd: export vars or source `.env` before `python -m src.heartbeat.run`  
