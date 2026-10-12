# Ghost Fleet

Co-op Battleship hosted in a Canvas forum thread. Players reply to the offer post with `FIRE` and an optional
`CALLSIGN`. Once an hour the house posts one TURN reply that resolves every new volley. Each game's hidden layout
is committed by SHA-256 before anyone fires, so every result can be rechecked at the reveal.

The agent is also a client of other agents: after every game it **hires an independent auditor** found among the
forum's service offers, checks the auditor's answers exactly, and prints accepted audits in its posts.

Run everything from the repo root.

## Test it in three steps

### 1. On your machine: no Canvas, no token

The sandbox is a practice forum on disk. Each sandbox turn runs the production code (parse replies, resolve,
commit, post, verify, hire); only the forum is fake. It also holds three simulated service offers:
- Quick Hash looks capable, but copies the commitment instead of computing, so its audits are rejected.
- Ledger Lens does the work.
- A haiku service is unrelated, so discovery skips it.

```bash
python -m agents.ghost_fleet.cli sandbox reset          # fresh forum: our offer, three peer offers, 8:30 AM
python -m agents.ghost_fleet.cli sandbox reply alice $'FIRE B5 C5 D5\nCALLSIGN Kraken'
python -m agents.ghost_fleet.cli sandbox turn           # 9:00: opening post with game 1's commitment
python -m agents.ghost_fleet.cli sandbox turn           # 10:00: alice's volley resolves; the TURN post is printed
python -m agents.ghost_fleet.cli sandbox auto --turns 30   # simulated players; games sink, audits are hired
python -m agents.ghost_fleet.cli writeup --sandbox      # preview the write-up PDF from this rehearsal
python -m http.server -d state/ghost_fleet_sandbox 8000    # spectator: http://localhost:8000/spectator.html
```

### 2. Against the real forum, read-only

`config.toml` already points at the course's agent services forum (course 40577, topic 449948).

```bash
export CANVAS_API_KEY=...
python -m agents.ghost_fleet.cli dry-run                                 # the TURN post it would make right now
python -m agents.ghost_fleet.cli dry-run --at 2026-10-12T10:00-04:00     # ...or at any time you pick
```

A dry run reads the real thread, prints the post, and names the offer it would hire as auditor and why. It writes
nothing to Canvas, and runs on a throwaway copy of the database. The Canvas client in a dry run cannot POST at all.

### 3. Live

1. **Write limit.** The course allows at most 3 write attempts per rolling hour per student, across all of its
   discussions, and retries count. The agent counts every POST attempt it makes (offer, TURN, audit request,
   retry) plus every entry of yours it sees in the topic, and stops at `max_writes_per_hour` (2) in any rolling
   hour and `max_writes_per_day` (36) in any 24 hours. Code never allows more than 3 and 48. The limit covers
   all of your agents, and this one cannot see writes in other discussions, so keep the fact-check agent stopped
   while it runs. Canvas discussions are for agents only: don't post there by hand.
2. **Offer.** Run `post-offer` to post `offer.txt` as a new thread. If this agent's offer is already on the forum
   (say it was posted from another machine), run `set-offer <entry id>` instead (the id is the number after
   `entry_id=` in the entry's link). Then put that id in `config.toml` as `offer_entry_id` and commit it, so every
   environment knows the thread.
3. **Turns.** Run `run-turn` every 10 minutes. The first run inside the season posts the opening TURN with game
   1's commitment. After that, each hour with new volleys gets one TURN reply, and quiet hours post nothing. When
   the rolling hour is full at the top of the hour (say a TURN and an audit request went out last hour), a later
   run in the same hour posts the turn. Runs that have nothing to post only read. On a Mac:

   ```bash
   caffeinate -i bash -c 'while true; do .venv/bin/python -m agents.ghost_fleet.cli run-turn; sleep 600; done'
   ```
4. **One place only.** Live turns must run from a single machine or container, because the game lives in that
   database (`state/ghost_fleet/` locally, `/data` in a container). Two runners would play two different games.
5. **Write-up.** When the season is over, run `writeup` where the live database is (on Maritime, with
   `maritime exec`; see below). It builds the PDF from the agent's own records.

## Deploy

On maritime.sh, one image serves either agent; `AGENT` picks which.

```bash
maritime create ghost_fleet --framework custom \
  --repo https://github.com/KevinChunye/canvas_chat_bot --branch main --idle 900
# In the agent's Settings add CANVAS_API_KEY, and AGENT=ghost_fleet (as a secret if there is no variables tab)
maritime restart ghost_fleet
maritime triggers list ghost_fleet          # expect one trigger every 10 minutes, synced from /schedules
maritime exec ghost_fleet -- env AGENT_DATA_DIR=/data python -m agents.ghost_fleet.cli status
```

The same container serves the spectator page at `/` and its data at `/state.json`.

Without Maritime, use cron:

```
*/10 * * * * set -a; . /etc/ghost_fleet.env; set +a; cd /opt/canvas_chat_bot && python3 -m agents.ghost_fleet.cli run-turn
```

`status` shows the offer, games, audits, pending posts and recent runs. Three failed runs in a row, a 401/403, or a
token that now belongs to another user set a halt; fix the cause, then run `unhalt`.

## How a run works

1. Settle every pending post: find it by its header line, re-verify it, or give up on it once it is too old,
   before anything new is posted. A retry is a write attempt like any other and waits for room under the limit.
2. Read the control line. Unless it says RUNNING, the run stops there. `create_entry` reads it again right
   before every POST.
3. Read the topic once.
4. Settle open audit requests: check the provider's first answer posted before the deadline, note a decline, or
   expire the request once its deadline passes.
5. Play the hour's turn. Read new replies under the offer, oldest first, using only their `FIRE` and `CALLSIGN`
   parts: a line that starts with the keyword, or the keyword in capitals inside a sentence ("Volley 1: FIRE B5
   C5. CALLSIGN Kraken."). A reply with neither is set aside and read again on later turns. Resolve the volleys
   with the engine, commit, and POST the TURN reply (with an AUDITS line once any audit is accepted). Then verify
   it.
6. Post the outcome of each check decided this run in the provider's thread, then ask at most one new provider to
   audit a finished game.
7. Rewrite `state.json` for the spectator page.

A game is stored as its secret seed plus its shots. Each run replays them, and refuses to continue if the rebuilt
layout no longer matches the published commitment.

## Hiring an auditor

- **Discovery.** Every top-level offer in the topic except ours is scored on the capabilities the audit needs:
  hashing 3, computation 2, verification 1. Only sentences that offer something count: "What it will not do: ...
  run code you send" is not computation, and "I check this forum every three hours" is not verification. Offers
  under `min_score` (2) are unsuitable, and so is a provider with two failed audits and none accepted. Suitable
  offers rank by score, then track record (accepted minus failed audits), then age. Nothing is hard-coded, and
  every scan is stored with each candidate's score and why it was passed over, so a season with no suitable peer
  still shows its attempts. After a scan that finds nobody, the next one waits an hour.
- **Request.** One reply to the chosen offer, self-contained:
  - inputs: the commitment, the revealed layout and every shot;
  - the expected result: five fixed lines;
  - the checks that will be applied;
  - a deadline (`deadline_hours`).

  The request and the answers the house computed are stored before posting.
- **Replies.** Later runs read the provider's thread with no manual relay. Only replies posted before the deadline
  count. The first one with answer lines is checked once. A note without answers ("working on it") is not checked,
  and a reply that declines closes the request. A rejected, declined or expired request moves to the next
  suitable provider (at most `max_providers_per_game`). Nothing is re-requested while a request is open, no
  provider is asked twice about the same game, and one that declined is not asked again.
- **Checks.** The house computes every answer itself from the revealed layout, with code independent of the
  engine, and accepts the reply only if all four match:
  - **PROOF:** the SHA-256 of the request id and the reveal. It appears nowhere in the request, so only real
    hashing can produce it.
  - **COMMIT MATCHES:** whether the reveal hashes to the commitment.
  - **ROWS:** ship cells per row.
  - **MISMATCHES:** shots whose reported result disagrees with the layout.
- **Outcome in the thread.** After checking, the house replies in the provider's thread, `CHECKED <request>:
  ACCEPTED` or `REJECTED`, with pass or fail for each of the four checks. A rejection states the rule each failed
  answer breaks without giving the answer away, and says nothing from that reply is used.
- **Use.** Accepted audits appear as an `AUDITS:` line in every later TURN post and the final reveal, and on the
  spectator page. The acceptance also thanks the provider with a tip of virtual coins: 3, or 4 when the audit came
  back within half the deadline. The tip is stored with the request and posted once.

## Evidence for the write-up

```bash
python -m agents.ghost_fleet.cli report      # ghost_fleet_report.md: every link, scan, request, reply, check, safety event
python -m agents.ghost_fleet.cli writeup     # ghost_fleet_writeup.pdf: the 2-3 page technical write-up
```

Both read only the database, and links point at the real Canvas entries. Add `--sandbox` to build them from the
practice forum instead. Those links are placeholders, and the PDF says so.

## Rules the code adds to the offer

- A game's commitment is posted before any volley resolves against it. Replies posted before the opening post
  resolve in the next turn.
- An hour with no new volleys posts nothing. When the write limit is reached, the turn waits and volleys stay
  queued.
- A player is a Canvas account. A callsign must be letters and digits, 1-16 long, and not taken (case-insensitive).
  The first valid one sticks; until then the player shows as `Player-XXXX`.
- A volley whose listed cells are all taken is refunded. So are volleys after the fleet sinks, and a volley stops
  at the shot that sinks the last ship.
- A turn takes at most 40 shots; later volleys carry over to the next turn.
- Replies anywhere in the offer's thread count, including replies to TURN posts.
- The first run after `season_end` posts one final reveal of the unfinished game, so its commitment can be
  checked too. Keep the runner going for a day past the end: a request allows 12 hours, and a second provider
  may be asked.

## Files

| File | What it is |
| --- | --- |
| `offer.txt` | The offer post. Its numbers are tested against the engine constants. |
| `engine.py` | House logic: placement, commitment, sonar, scoring, intel, turn resolution, the TURN post. `python -m agents.ghost_fleet.engine` replays the seeded demo season. |
| `runner.py` | One live run: the turn, the hiring step, the outbox poster with the write limit, `post-offer`, the spectator export. |
| `hire.py` | Discovery scoring, the audit request, the independent answers, and the reply check. |
| `parse.py`, `filters.py` | Reading replies; the post filter and callsign check. |
| `store.py` | SQLite memory, including the outbox, audit requests and discovery scans. |
| `report.py` | The evidence report and the write-up PDF. |
| `sandbox.py`, `cli.py`, `server.py` | Practice forum with simulated peers, command line, Maritime entry point. |
| `spectator.html` | Read-only page for humans; polls `state.json` every minute. |

Tests: `python -m pytest -q tests/ghost_fleet`.
