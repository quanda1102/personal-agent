# Heartbeat scheduling

Environment variables for the vault and LLM: [configuration.md](configuration.md) and [.env.example](../.env.example).

The heartbeat process talks to your vault (`HOMEAGENT_VAULT`), the SQLite queue, and optionally an LLM. Run it from the **project root** (where `pyproject.toml` lives) so `python -m src.heartbeat.run` resolves.

## Commands

| Command | Purpose |
|--------|---------|
| `python -m src.heartbeat.run --mode evening` | Normal plan (+ execute unless `--plan-only` / `--no-llm`). |
| `python -m src.heartbeat.run --check-depth` | Print `pending=N threshold=M`. **Exit 1** if `pending >= threshold` (for alerting). |
| `python -m src.heartbeat.run --plan-only` | Plan + queue inserts only; no executor phase. |

Environment:

- `HOMEAGENT_HEARTBEAT_QUEUE_THRESHOLD` — threshold for `--check-depth` and for coordinator-style spawn hooks (default `5`).
- `HOMEAGENT_HEARTBEAT_NO_LLM=1` — stub plan, no API calls (same idea as `--no-llm`).

Load `.env` yourself in cron (e.g. `set -a; source /path/to/home_agent/.env; set +a`) or export `HOMEAGENT_VAULT` in the crontab line.

## Local / CI without cron

- **Stub heartbeat (no LLM):** `uv run pytest tests/test_agent_flow_client.py -k heartbeat` — exercises in-process `--no-llm` and a subprocess `--check-depth` run (watchdog-style), no cron needed.

## crontab examples

Evening run once a day at 22:00 (user’s timezone is whatever the cron daemon uses):

```cron
0 22 * * * cd /path/to/home_agent && /path/to/uv run python -m src.heartbeat.run --mode evening >> /tmp/heartbeat.log 2>&1
```

Morning digest at 07:00:

```cron
0 7 * * * cd /path/to/home_agent && /path/to/uv run python -m src.heartbeat.run --mode morning >> /tmp/heartbeat.log 2>&1
```

Alert-only: exit 1 when the pending queue is deep (e.g. for a wrapper that emails or pings):

```cron
*/15 * * * * cd /path/to/home_agent && /path/to/uv run python -m src.heartbeat.run --check-depth || /usr/local/bin/notify "heartbeat queue deep"
```

Use a dedicated venv/uv project path; replace `/path/to/home_agent` and `/path/to/uv` with real paths (`which uv`).

## Optional: APScheduler inside a long-lived process

If you prefer not to use cron, a small Python entrypoint can schedule the same subprocess:

```python
from apscheduler.schedulers.blocking import BlockingScheduler
import subprocess
import sys

REPO = "/path/to/home_agent"
PY = sys.executable

def run_heartbeat():
    subprocess.run(
        [PY, "-m", "src.heartbeat.run", "--mode", "evening"],
        cwd=REPO,
        check=False,
    )

def check_depth():
    subprocess.run(
        [PY, "-m", "src.heartbeat.run", "--check-depth"],
        cwd=REPO,
        check=False,
    )

sched = BlockingScheduler()
sched.add_job(run_heartbeat, "cron", hour=22, minute=0)
sched.add_job(check_depth, "interval", minutes=15)
sched.start()
```

Add `apscheduler` to your environment if you use this pattern; it is not required for the core agent.
