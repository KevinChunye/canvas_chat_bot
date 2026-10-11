# Ghost Fleet

Co-op Battleship hosted in a Canvas forum thread. Players reply to the offer post with `FIRE` and an optional
`CALLSIGN`. Once an hour the house posts one TURN reply that resolves every new volley. Each game's hidden layout
is committed by SHA-256 before anyone fires, so every result can be rechecked at the reveal.

Run everything from the repo root.

## Test it in three steps

### 1. On your machine: no Canvas, no token

The sandbox is a practice forum on disk. Each sandbox turn runs the production code (parse replies, resolve,
commit, post, verify); only the forum is fake.

```bash
python -m agents.ghost_fleet.cli sandbox reset          # fresh forum with the offer posted, clock at 8:30 AM
python -m agents.ghost_fleet.cli sandbox reply alice "FIRE B5 C5 D5
CALLSIGN Kraken"
python -m agents.ghost_fleet.cli sandbox turn           # 9:00: opening post with game 1's commitment
python -m agents.ghost_fleet.cli sandbox reply bob "FIRE B5 B6"
python -m agents.ghost_fleet.cli sandbox turn           # 10:00: both volleys resolve; the TURN post is printed
python -m agents.ghost_fleet.cli sandbox auto --turns 20   # simulated players play 20 more hours
python -m http.server -d state/ghost_fleet_sandbox 8000    # spectator: http://localhost:8000/spectator.html
```

### 2. Against the real forum, read-only

```bash
export CANVAS_API_KEY=...
# Read-only lookup that writes the course and topic ids into this agent's config.
# Pick a topic the fact-check agent does not post in: both post as your account.
python scripts/discover.py "<forum topic title>" --config agents/ghost_fleet/config.toml
python -m agents.ghost_fleet.cli dry-run                                 # the TURN post it would make right now
python -m agents.ghost_fleet.cli dry-run --at 2026-10-12T10:00-04:00     # ...or at any time you pick
```

A dry run reads the real thread and prints the post. It writes nothing to Canvas, and runs on a throwaway copy of
the database.

### 3. Live

1. **Offer.** Run `post-offer` to post `offer.txt` as a new thread. If you already posted it by hand, run
   `set-offer <entry id>` instead (the id is the number after `entry_id=` in the entry's link). Then put that id
   in `config.toml` as `offer_entry_id` and commit it, so every environment knows the thread.
2. **Turns.** Run `run-turn` once an hour. The first run inside the season posts the opening TURN with game 1's
   commitment. After that, each hour with new volleys gets one TURN reply, and quiet hours post nothing.
3. **One place only.** Live turns must run from a single machine or container, because the game lives in that
   database (`state/ghost_fleet/` locally, `/data` in a container). Two runners would play two different games.

## Deploy

On maritime.sh, one image serves either agent; `AGENT` picks which.

```bash
maritime create ghost_fleet --framework custom \
  --repo https://github.com/KevinChunye/canvas_chat_bot --branch main --idle 900
# In the agent's Settings add CANVAS_API_KEY, and AGENT=ghost_fleet (as a secret if there is no variables tab)
maritime restart ghost_fleet
maritime triggers list ghost_fleet          # expect one hourly trigger, synced from /schedules
maritime exec ghost_fleet -- env AGENT_DATA_DIR=/data python -m agents.ghost_fleet.cli status
```

The same container serves the spectator page at `/` and its data at `/state.json`.

Without Maritime, use cron:

```
0 * * * * set -a; . /etc/ghost_fleet.env; set +a; cd /opt/canvas_chat_bot && python3 -m agents.ghost_fleet.cli run-turn
```

`status` shows the offer, games, pending posts and recent runs. Three failed runs in a row, a 401/403, or a token
that now belongs to another user set a halt; fix the cause, then run `unhalt`.

## How a turn works

1. Confirm any TURN post whose outcome was unknown: find it by its header line before ever posting again.
2. Check the season window, that this hour has no TURN yet, and that the control line says RUNNING.
3. Read replies in the offer's thread that haven't been seen yet, oldest first. Only `FIRE` and `CALLSIGN` lines
   count, and nothing else from a reply is used or quoted.
4. Resolve them with the engine. Commit the shots, outcomes, players and the pending post in one transaction.
5. Run the post filter, POST under the offer, re-fetch to verify, then rewrite `state.json` for the spectator page.

A game is stored as its secret seed plus its shots. Each run replays them, and refuses to continue if the rebuilt
layout no longer matches the published commitment.

## Rules the code adds to the offer

- A game's commitment is posted before any volley resolves against it. Replies posted before the opening post
  resolve in the next turn.
- An hour with no new volleys posts nothing.
- A player is a Canvas account. A callsign must be letters and digits, 1-16 long, and not taken (case-insensitive).
  The first valid one sticks; until then the player shows as `Player-XXXX`.
- A volley whose listed cells are all taken is refunded. So are volleys after the fleet sinks, and a volley stops
  at the shot that sinks the last ship.
- A turn takes at most 40 shots; later volleys carry over to the next turn.
- Replies anywhere in the offer's thread count, including replies to TURN posts.
- The first run after `season_end` posts one final reveal of the unfinished game, so its commitment can be
  checked too. Keep the runner going at least an hour past the end.

## Files

| File | What it is |
| --- | --- |
| `offer.txt` | The offer post. Its numbers are tested against the engine constants. |
| `engine.py` | House logic: placement, commitment, sonar, scoring, intel, turn resolution, the TURN post. `python -m agents.ghost_fleet.engine` replays the seeded demo season. |
| `runner.py` | One live turn, the idempotent poster, `post-offer`, and the spectator export. |
| `parse.py`, `filters.py` | Reading replies; the post filter and callsign check. |
| `store.py` | SQLite memory. |
| `sandbox.py`, `cli.py`, `server.py` | Practice forum, command line, Maritime entry point. |
| `spectator.html` | Read-only page for humans; polls `state.json` every minute. |

Tests: `python -m pytest -q tests/ghost_fleet`.
