"""SQLite memory for Ghost Fleet. A game is stored as its secret seed plus its shots: replaying the shots
through engine.Game rebuilds the board, sonar and intel exactly, so nothing derived is ever stored twice.

Everything a turn decides (shots, volley outcomes, players, game end, the next game, the pending TURN post)
is committed in one transaction before the POST, so a crash at any point resumes cleanly."""

import json
import sqlite3
import uuid
from datetime import timedelta
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
    record TEXT,               -- JSON, players by id
    header TEXT,               -- first line of the post; finds it again after a lost acknowledgement
    body TEXT,
    message_html TEXT,
    status TEXT,               -- pending | confirmed | abandoned
    canvas_entry_id INTEGER,
    attempts INTEGER DEFAULT 0,
    created_at TEXT,
    confirmed_at TEXT,
    note TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    turn INTEGER,
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

    def add_event(self, turn: int | None, run_id: str, event: str, detail: dict | None = None) -> None:
        self.db.execute("INSERT INTO events(turn, run_id, at, event, detail) VALUES (?, ?, ?, ?, ?)",
                        (turn, run_id, self.now_iso(), event, json.dumps(detail or {})))
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

    # ------------------------------------------------------------------ TURN posts

    def turn_row(self, turn: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM turns WHERE turn = ?", (turn,)).fetchone()

    def turn_rows(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM turns WHERE status != 'abandoned' ORDER BY turn").fetchall()

    def pending_turns(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM turns WHERE status = 'pending' ORDER BY turn").fetchall()

    def add_turn(self, turn: int, game_id: int, record: dict, header: str, body: str, message_html: str) -> None:
        self.db.execute(
            "INSERT INTO turns(turn, game_id, record, header, body, message_html, status, attempts, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?)",
            (turn, game_id, json.dumps(record), header, body, message_html, self.now_iso()))

    def bump_attempts(self, turn: int) -> None:
        self.db.execute("UPDATE turns SET attempts = attempts + 1 WHERE turn = ?", (turn,))
        self.db.commit()

    def set_canvas_entry(self, turn: int, entry_id: int) -> None:
        self.db.execute("UPDATE turns SET canvas_entry_id = ? WHERE turn = ?", (entry_id, turn))
        self.db.commit()

    def confirm_turn(self, turn: int, entry_id: int, note: str) -> None:
        self.db.execute("UPDATE turns SET status = 'confirmed', canvas_entry_id = ?, confirmed_at = ?, note = ? "
                        "WHERE turn = ?", (entry_id, self.now_iso(), note, turn))
        self.db.commit()

    def abandon_turn(self, turn: int, note: str) -> None:
        self.db.execute("UPDATE turns SET status = 'abandoned', note = ? WHERE turn = ?", (note, turn))
        self.db.commit()

    def claimed_entry_ids(self) -> set[int]:
        rows = self.db.execute("SELECT canvas_entry_id FROM turns WHERE canvas_entry_id IS NOT NULL")
        return {row[0] for row in rows}

    def turn_age(self, row) -> timedelta:
        return self.clock() - parse_time(row["created_at"])

    def offer_id(self) -> int | None:
        value = self.get_state("offer_entry_id")
        return int(value) if value else None
