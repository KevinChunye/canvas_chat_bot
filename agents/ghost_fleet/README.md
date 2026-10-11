# Ghost Fleet

Co-op Battleship hosted in a Canvas forum thread. Players reply to the offer post with `FIRE` and an optional
`CALLSIGN`. Once an hour the house posts one TURN reply that resolves every new volley, and each game's hidden
layout is committed by SHA-256 up front, so every result can be rechecked at the reveal.

## Files

| File | What it is |
| --- | --- |
| `offer.txt` | The offer post, verbatim. Its numbers are tested against the engine constants. |
| `engine.py` | House logic: fleet placement, commitment, sonar, firing and scoring, intel, leaderboards, TURN post rendering. `python -m agents.ghost_fleet.engine` plays a seeded demo season and writes `state.json` and `turn_post.txt` to the current directory (both gitignored). |
| `spectator.html` | Read-only page for humans. It polls `state.json` next to it every minute and falls back to the demo data embedded in the page. |

Tests: `python -m pytest -q tests/ghost_fleet`. They check every promise the offer makes (straight non-overlapping
ships, the commitment, sonar bands with sunk ships counted, scoring, intel, first-open-cells firing, the cooldown),
replay the demo season against its reveals, and pin the demo to the published TURN 26 (commit `3bbcbd92…`).

## Status

The engine and the page are done. The live runner, which reads replies, resolves a turn and posts the TURN reply,
is the next piece. It should be built from `canvas_core`:

1. **Config:** `agents/ghost_fleet/config.toml` with the course and topic ids
   (`python scripts/discover.py "<topic title>" --config agents/ghost_fleet/config.toml`), the offer entry id, and
   its own data dir (`state/ghost_fleet`).
2. **Read:** `canvas_core.forum.fetch_forum` returns every entry in the topic. Keep the replies under the offer
   entry that are not ours and are newer than the last resolved one, in posting order.
3. **Parse:** only `FIRE` and `CALLSIGN` lines. Everything else is ignored, and nothing from a reply is ever
   quoted back except a valid callsign.
4. **Players:** key them by Canvas user id, not by callsign. The offer says a player is a Canvas account.
5. **Memory:** keep the fleet, salt, shots and last resolved entry in SQLite, and commit them before posting, so a
   restart never re-rolls a fleet or resolves a volley twice.
6. **Write:** `Canvas.create_entry(topic_id, html, parent_entry_id=offer_id)` already re-checks the control line
   before every POST. Reuse the fact-check agent's intent, POST, verify and reconcile pattern
   (`agents/factcheck/actions.py`) so a lost acknowledgement never double-posts a turn.
7. **Spectators:** rewrite `state.json` after each confirmed turn.
8. **Deploy:** add `agents/ghost_fleet/server.py` (same contract as the fact-check one). The root `Dockerfile` runs
   the fact-check agent today, so the second container needs its own entry point.

## Before going live

- **Its own post filter.** A TURN post carries a 64-character hex commitment, which the fact-check filters reject
  as a secret-shaped token, and which `canvas_core.text.scrub` redacts in logs. Ghost Fleet needs a filter of its
  own: secret fragments, `canvas_core.text.SELF_IDENTIFICATION`, and no reply text except valid callsigns.
- **Separate topic from the fact-check agent.** Both agents post as the same Canvas account. In the same topic,
  every hourly TURN post would count as the fact-checker's latest post and keep its 48-hour window shut.
- **Engine vs. offer:**
  - `render_turn_post` adds a `GAME:` line (top five scorers this game) that the sample TURN post does not show.
  - A volley whose listed cells are all taken is refunded. The offer only mentions refunds when the fleet sinks
    mid-turn (though "a collision never wastes your volley" covers it in spirit).
  - A turn is capped at 40 shots and the rest carry over to the next turn. The offer does not say so; it only
    matters past 20 volleys in one turn.
  - Times use a fixed UTC-4 offset, which is right through the season end on Oct 14.
