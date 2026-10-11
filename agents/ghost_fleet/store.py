"""SQLite memory for Ghost Fleet. A game is stored as its secret seed plus its shots: replaying the shots
through engine.Game rebuilds the board, sonar and intel exactly, so nothing derived is ever stored twice.

Every write to Canvas (the offer, TURN posts, audit requests) is a row in `outbox`, committed before the POST
in the same transaction as whatever it reports, so a crash at any point resumes cleanly and nothing is posted
twice."""

import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from canvas_core.env import secret_values
from canvas_core.text import scrub
from canvas_core.times import iso, now, parse_time

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    id INTEGER PRIMARY KEY,
    seed TEXT,                 -- secret until the reveal; never logged or exported
    start_turn INTEGER,
    end_turn INTEGER,
    commit_hash TEXT,
    announced INTEGER DEFAULT 0  -- 1 once a TURN post carrying its commitment is committed
);
CREATE TABLE IF NOT EXISTS shots (
    game_id INTEGER,
    n INTEGER,
    turn INTEGER,
    player_id INTEGER,
    cell TEXT,
    PRIMARY KEY (game_id, n)
);
CREATE TABLE IF NOT EXISTS volleys (
    entry_id INTEGER PRIMARY KEY,  -- the reply on Canvas
    player_id INTEGER,
    created_at TEXT,
    cells TEXT,                -- JSON list, as listed
    status TEXT,               -- fired | skipped | refunded | carried | ignored
    turn INTEGER,
    game_id INTEGER
);
CREATE TABLE IF NOT EXISTS players (
    player_id INTEGER PRIMARY KEY,  -- Canvas user id: one player per account
    callsign TEXT,             -- NULL until a valid one is given, then fixed for the season
    default_name TEXT UNIQUE,
    first_turn INTEGER
);
CREATE TABLE IF NOT EXISTS turns (
    turn INTEGER PRIMARY KEY,
    game_id INTEGER,
    record TEXT                -- JSON, players by id; the post itself is outbox row 'turn:<turn>'
);
CREATE TABLE IF NOT EXISTS outbox (
    key TEXT PRIMARY KEY,      -- offer | turn:<n> | hire:<token> | verdict:<token>
    kind TEXT,                 -- offer | turn | hire | verdict
    parent_entry_id INTEGER,   -- the entry it replies to; NULL for a new thread
    header TEXT,               -- first line; finds the post again after a lost acknowledgement
    body TEXT,
    message_html TEXT,
    status TEXT,               -- pending | confirmed | abandoned
    canvas_entry_id INTEGER,
    attempts INTEGER DEFAULT 0,
    created_at TEXT,
    confirmed_at TEXT,
    note TEXT
);
CREATE TABLE IF NOT EXISTS hires (
    token TEXT PRIMARY KEY,    -- GF-AUDIT-G<game>-<offer entry id>
    game_id INTEGER,
    provider_entry_id INTEGER, -- the offer the request replies to
    provider_user_id INTEGER,
    score INTEGER,
    capabilities TEXT,         -- JSON: our capability labels the offer matched
    expected TEXT,             -- JSON: the answers the house computed itself
    status TEXT,               -- requested | accepted | rejected | declined | expired | unsent
    deadline TEXT,
    reply_entry_id INTEGER,
    checks TEXT,               -- JSON: [[check, passed], ...] for the reply that settled it
    created_at TEXT,
    settled_at TEXT,
    tip INTEGER                -- virtual coins given for an accepted audit, recorded once
);
CREATE TABLE IF NOT EXISTS discoveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    at TEXT,
    game_id INTEGER,
    offers_seen INTEGER,
    candidates TEXT,           -- JSON: [{entry_id, user_id, score, capabilities, suitable}]
    chosen_entry_id INTEGER
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT,              -- an outbox key, a hire token, or a run
    run_id TEXT,
    at TEXT,
    event TEXT,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    mode TEXT,                 -- live | dry_run
    started_at TEXT,
    ended_at TEXT,
    outcome TEXT,
    summary TEXT
);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

TURN_JOIN = ("SELECT t.turn, t.game_id, t.record, o.* FROM turns t JOIN outbox o ON o.key = 'turn:' || t.turn")


class Store:
    def __init__(self, path: Path | str, clock=now):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        if "tip" not in {r["name"] for r in self.db.execute("PRAGMA table_info(hires)")}:
            self.db.execute("ALTER TABLE hires ADD COLUMN tip INTEGER")     # databases from before tips
        self.db.commit()
        self.clock = clock

    def close(self):
        self.db.close()

    def now_iso(self) -> str:
        return iso(self.clock())

    def snapshot(self) -> "Store":
        """An in-memory copy for dry runs: the real logic runs, the real database is never touched."""
        copy = Store(":memory:", self.clock)
        self.db.backup(copy.db)
        return copy

    # ------------------------------------------------------------------ state, halting, runs

    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str | None, commit: bool = True) -> None:
        if value is None:
            self.db.execute("DELETE FROM state WHERE key = ?", (key,))
        else:
            self.db.execute("INSERT INTO state(key, value) VALUES (?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
        if commit:
            self.db.commit()

    def halt_reason(self) -> str | None:
        return self.get_state("halted")

    def set_halt(self, reason: str) -> None:
        self.set_state("halted", f"{reason} at {self.now_iso()}")

    def clear_halt(self) -> None:
        self.set_state("halted", None)
        self.set_state("consecutive_failures", "0")

    def consecutive_failures(self) -> int:
        return int(self.get_state("consecutive_failures", "0"))

    def record_failure(self) -> int:
        count = self.consecutive_failures() + 1
        self.set_state("consecutive_failures", str(count))
        return count

    def reset_failures(self) -> None:
        self.set_state("consecutive_failures", "0")

    def start_run(self, mode: str) -> str:
        run_id = self.clock().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
        self.db.execute("INSERT INTO runs(run_id, mode, started_at, outcome) VALUES (?, ?, ?, 'running')",
                        (run_id, mode, self.now_iso()))
        self.db.commit()
        return run_id

    def finish_run(self, run_id: str, outcome: str, summary: str) -> None:
        self.db.execute("UPDATE runs SET ended_at = ?, outcome = ?, summary = ? WHERE run_id = ?",
                        (self.now_iso(), outcome, scrub(summary, secret_values()), run_id))
        self.db.commit()

    def add_event(self, subject: str | None, run_id: str, event: str, detail: dict | None = None) -> None:
        self.db.execute("INSERT INTO events(subject, run_id, at, event, detail) VALUES (?, ?, ?, ?, ?)",
                        (subject, run_id, self.now_iso(), event, scrub(json.dumps(detail or {}), secret_values())))
        self.db.commit()

    # ------------------------------------------------------------------ games and shots

    def games(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM games ORDER BY id").fetchall()

    def shots(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM shots ORDER BY game_id, n").fetchall()

    def add_game(self, game_id: int, seed: int, start_turn: int, commit_hash: str) -> None:
        self.db.execute("INSERT INTO games(id, seed, start_turn, commit_hash) VALUES (?, ?, ?, ?)",
                        (game_id, str(seed), start_turn, commit_hash))

    def end_game(self, game_id: int, turn: int) -> None:
        self.db.execute("UPDATE games SET end_turn = ? WHERE id = ?", (turn, game_id))

    def announce(self, game_id: int) -> None:
        self.db.execute("UPDATE games SET announced = 1 WHERE id = ?", (game_id,))

    def add_shot(self, game_id: int, n: int, turn: int, player_id: int, cell: str) -> None:
        self.db.execute("INSERT INTO shots(game_id, n, turn, player_id, cell) VALUES (?, ?, ?, ?, ?)",
                        (game_id, n, turn, player_id, cell))

    # ------------------------------------------------------------------ volleys and players

    def known_entry_ids(self) -> set[int]:
        return {row[0] for row in self.db.execute("SELECT entry_id FROM volleys")}

    def carried_volleys(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM volleys WHERE status = 'carried' ORDER BY created_at, entry_id"
                               ).fetchall()

    def last_fired_turns(self) -> dict[int, int]:
        rows = self.db.execute("SELECT player_id, MAX(turn) FROM volleys WHERE status = 'fired' GROUP BY player_id")
        return {row[0]: row[1] for row in rows}

    def save_volley(self, entry_id: int, player_id: int, created_at: str, cells: list[str], status: str,
                    turn: int | None, game_id: int | None) -> None:
        self.db.execute(
            "INSERT INTO volleys(entry_id, player_id, created_at, cells, status, turn, game_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(entry_id) DO UPDATE SET status = excluded.status, "
            "turn = excluded.turn, game_id = excluded.game_id",
            (entry_id, player_id, created_at, json.dumps(cells), status, turn, game_id))

    def players(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM players").fetchall()

    def save_player(self, player_id: int, callsign: str | None, default_name: str, first_turn: int) -> None:
        self.db.execute(
            "INSERT INTO players(player_id, callsign, default_name, first_turn) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(player_id) DO UPDATE SET callsign = excluded.callsign",
            (player_id, callsign, default_name, first_turn))

    # ------------------------------------------------------------------ the outbox: every write to Canvas

    def add_post(self, key: str, kind: str, parent_entry_id: int | None, header: str, body: str,
                 message_html: str) -> None:
        self.db.execute(
            "INSERT INTO outbox(key, kind, parent_entry_id, header, body, message_html, status, attempts, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?)",
            (key, kind, parent_entry_id, header, body, message_html, self.now_iso()))

    def post(self, key: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM outbox WHERE key = ?", (key,)).fetchone()

    def posts(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM outbox ORDER BY created_at").fetchall()

    def pending_posts(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM outbox WHERE status = 'pending' ORDER BY created_at").fetchall()

    def bump_attempts(self, key: str) -> None:
        self.db.execute("UPDATE outbox SET attempts = attempts + 1 WHERE key = ?", (key,))
        self.db.commit()

    def set_canvas_entry(self, key: str, entry_id: int) -> None:
        self.db.execute("UPDATE outbox SET canvas_entry_id = ? WHERE key = ?", (entry_id, key))
        self.db.commit()

    def confirm_post(self, key: str, entry_id: int, note: str) -> None:
        self.db.execute("UPDATE outbox SET status = 'confirmed', canvas_entry_id = ?, confirmed_at = ?, note = ? "
                        "WHERE key = ?", (entry_id, self.now_iso(), note, key))
        self.db.commit()

    def abandon_post(self, key: str, note: str) -> None:
        self.db.execute("UPDATE outbox SET status = 'abandoned', note = ? WHERE key = ?", (note, key))
        self.db.commit()

    def post_age(self, row) -> timedelta:
        return self.clock() - parse_time(row["created_at"])

    def attempt_times(self, since: datetime) -> list[datetime]:
        """When each POST attempt after `since` was made, whatever its outcome: every attempt counts against the
        write limit, retries included."""
        rows = self.db.execute("SELECT at FROM events WHERE event = 'attempt' AND at > ?", (iso(since),))
        return [parse_time(r[0]) for r in rows]

    # ------------------------------------------------------------------ TURN posts

    def turn_row(self, turn: int) -> sqlite3.Row | None:
        return self.db.execute(TURN_JOIN + " WHERE t.turn = ?", (turn,)).fetchone()

    def turn_rows(self) -> list[sqlite3.Row]:
        return self.db.execute(TURN_JOIN + " WHERE o.status != 'abandoned' ORDER BY t.turn").fetchall()

    def add_turn(self, turn: int, game_id: int, record: dict, header: str, body: str, message_html: str,
                 parent_entry_id: int | None) -> None:
        self.db.execute("INSERT INTO turns(turn, game_id, record) VALUES (?, ?, ?)", (turn, game_id, json.dumps(record)))
        self.add_post(f"turn:{turn}", "turn", parent_entry_id, header, body, message_html)

    # ------------------------------------------------------------------ hiring an auditor

    def hires(self, game_id: int | None = None) -> list[sqlite3.Row]:
        if game_id is None:
            return self.db.execute("SELECT * FROM hires ORDER BY created_at").fetchall()
        return self.db.execute("SELECT * FROM hires WHERE game_id = ? ORDER BY created_at", (game_id,)).fetchall()

    def open_hires(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM hires WHERE status = 'requested' ORDER BY created_at").fetchall()

    def add_hire(self, token: str, game_id: int, provider_entry_id: int, provider_user_id: int, score: int,
                 capabilities: list[str], expected: dict, deadline: datetime) -> None:
        self.db.execute(
            "INSERT INTO hires(token, game_id, provider_entry_id, provider_user_id, score, capabilities, expected, "
            "status, deadline, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'requested', ?, ?)",
            (token, game_id, provider_entry_id, provider_user_id, score, json.dumps(capabilities),
             json.dumps(expected), iso(deadline), self.now_iso()))

    def settle_hire(self, token: str, status: str, reply_entry_id: int | None = None, checks: list | None = None,
                    tip: int | None = None, commit: bool = True) -> None:
        self.db.execute("UPDATE hires SET status = ?, reply_entry_id = ?, checks = ?, tip = ?, settled_at = ? "
                        "WHERE token = ?",
                        (status, reply_entry_id, json.dumps(checks) if checks is not None else None, tip,
                         self.now_iso(), token))
        if commit:
            self.db.commit()

    def add_discovery(self, run_id: str, game_id: int | None, offers_seen: int, candidates: list[dict],
                      chosen_entry_id: int | None) -> None:
        self.db.execute("INSERT INTO discoveries(run_id, at, game_id, offers_seen, candidates, chosen_entry_id) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (run_id, self.now_iso(), game_id, offers_seen, json.dumps(candidates), chosen_entry_id))
        self.db.commit()

    def discoveries(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM discoveries ORDER BY id").fetchall()

    def last_discovery(self, game_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM discoveries WHERE game_id = ? ORDER BY id DESC LIMIT 1",
                               (game_id,)).fetchone()

    def offer_id(self) -> int | None:
        value = self.get_state("offer_entry_id")
        return int(value) if value else None
