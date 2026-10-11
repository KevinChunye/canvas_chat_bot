# Canvas forum agents

Agents that read and post in a Canvas discussion forum. Each agent lives in its own package under `agents/`, and
they all reach Canvas through one shared core.

## Layout

| Path | What it is |
| --- | --- |
| `canvas_core/` | Shared plumbing: the Canvas client (host pinned, every path allowlisted, control line re-checked before each write), forum reading, text hashing, secret scrubbing, JSONL logs |
| `agents/factcheck/` | Fact-check agent: reads the forum every three hours and replies only when it has something to add ([README](agents/factcheck/README.md), [architecture](agents/factcheck/ARCHITECTURE.md)) |
| `agents/ghost_fleet/` | Ghost Fleet: co-op Battleship hosted in a forum thread, with a verifiable hidden layout. Engine, offer post and spectator page are in; the live runner is next ([README](agents/ghost_fleet/README.md)) |
| `scripts/discover.py` | One-time, read-only lookup of a forum's course and topic ids, written into an agent's config |
| `tests/` | Offline tests: `core/` for the shared package, one folder per agent |

## Adding an agent

1. Create `agents/<name>/` with its own `config.toml`, and keep all of its behaviour there.
2. Talk to Canvas only through `canvas_core.canvas.Canvas`. It is the only code that sends the token, and it refuses
   anything outside the configured topic before a request leaves the process.
3. Give it its own data directory (`state/<name>/` locally, `$AGENT_DATA_DIR` in a container) so databases and logs
   never mix.
4. Put its tests in `tests/<name>/`. `tests/conftest.py` already provides a fake Canvas and fake secrets.
5. Never sign a post or say what wrote it. Check outgoing text against
   `canvas_core.text.SELF_IDENTIFICATION`, as the fact-check agent's filters do.

## Quick start

Python 3.11+, from the repo root:

```bash
pip install -r requirements-dev.txt
python -m pytest -q          # every agent's tests, offline
```
