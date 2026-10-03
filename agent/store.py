"""SQLite memory. One file, plain SQL, committed after every logical step so a
crash at any point leaves a consistent record to resume from."""

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import secret_values
from .text import scrub

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_entries (
    entry_id INTEGER PRIMARY KEY,
    parent_id INTEGER,
    thread_root_id INTEGER,
    author_id INTEGER,
    created_at TEXT,
    updated_at TEXT,
    text_hash TEXT,
    first_seen_cycle TEXT
);
CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id TEXT,
    entry_id INTEGER,
    claim_text TEXT,
    kind TEXT,
    verdict TEXT,
    confidence REAL,
    why TEXT,
    checked_at TEXT
);
CREATE TABLE IF NOT EXISTS actions (
    intent_id TEXT PRIMARY KEY,
    cycle_id TEXT,
    kind TEXT,                 -- reply | thread
    target_entry_id INTEGER,   -- NULL for a new thread
    thread_root_id INTEGER,
    content_hash TEXT,
    body TEXT,                 -- plain text, exactly as posted
    message_html TEXT,         -- exactly what is POSTed
    status TEXT,               -- pending | confirmed | abandoned
    canvas_entry_id INTEGER,
    attempts INTEGER DEFAULT 0,
    last_attempt_at TEXT,
    created_at TEXT,
    confirmed_at TEXT,
    note TEXT
);
CREATE TABLE IF NOT EXISTS action_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id TEXT,
    cycle_id TEXT,
    at TEXT,
    event TEXT,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS cycles (
    cycle_id TEXT PRIMARY KEY,
    mode TEXT,                 -- live | dry_run
    started_at TEXT,
    ended_at TEXT,
    outcome TEXT,
    posts_made INTEGER DEFAULT 0,
    decision_summary TEXT
);
CREATE TABLE IF NOT EXISTS spend (
    call_id TEXT PRIMARY KEY,
    cycle_id TEXT,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd REAL,
    status TEXT,               -- reserved | recorded
    at TEXT
);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class Store:
    def __init__(self, path: Path | str, clock=now):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()
        self.clock = clock

    def close(self):
        self.db.close()

    def now_iso(self) -> str:
        return iso(self.clock())

    # ------------------------------------------------------------------ state

    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str | None) -> None:
        if value is None:
            self.db.execute("DELETE FROM state WHERE key = ?", (key,))
        else:
            self.db.execute("INSERT INTO state(key, value) VALUES (?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
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

    def consume_fault(self, name: str) -> bool:
        """One-shot fault flags: True once, then the flag is gone."""
        if self.get_state(f"fault.{name}") == "1":
            self.set_state(f"fault.{name}", None)
            return True
        return False

    # ------------------------------------------------------------------ cycles

    def start_cycle(self, mode: str) -> str:
        cycle_id = self.clock().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
        self.db.execute("INSERT INTO cycles(cycle_id, mode, started_at, outcome) VALUES (?, ?, ?, 'running')",
                        (cycle_id, mode, self.now_iso()))
        self.db.commit()
        return cycle_id

    def finish_cycle(self, cycle_id: str, outcome: str, posts_made: int, summary: str) -> None:
        summary = scrub(summary, secret_values())
        self.db.execute("UPDATE cycles SET ended_at = ?, outcome = ?, posts_made = ?, decision_summary = ? "
                        "WHERE cycle_id = ?", (self.now_iso(), outcome, posts_made, summary, cycle_id))
        self.db.commit()

    # ------------------------------------------------------------------ seen entries

    def seen_map(self) -> dict[int, sqlite3.Row]:
        rows = self.db.execute("SELECT * FROM seen_entries").fetchall()
        return {row["entry_id"]: row for row in rows}

    def mark_seen(self, entries: list[dict], cycle_id: str, commit: bool = True) -> None:
        for e in entries:
            self.db.execute(
                "INSERT INTO seen_entries(entry_id, parent_id, thread_root_id, author_id, created_at, updated_at, "
                "text_hash, first_seen_cycle) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(entry_id) DO UPDATE SET updated_at = excluded.updated_at, text_hash = excluded.text_hash",
                (e["id"], e["parent_id"], e["root_id"], e["user_id"], e["created_at"], e["updated_at"],
                 e["text_hash"], cycle_id))
        if commit:
            self.db.commit()

    # ------------------------------------------------------------------ claims

    def add_claims(self, cycle_id: str, claims: list[dict], commit: bool = True) -> None:
        for c in claims:
            self.db.execute("INSERT INTO claims(cycle_id, entry_id, claim_text, kind, verdict, confidence, why, "
                            "checked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (cycle_id, c["entry_id"], c["claim"], c["kind"], c["verdict"], c["confidence"],
                             c["why"], self.now_iso()))
        if commit:
            self.db.commit()

    # ------------------------------------------------------------------ actions

    def add_action(self, cycle_id: str, kind: str, target_entry_id: int | None, thread_root_id: int | None,
                   body: str, message_html: str, content_hash: str, commit: bool = True) -> str:
        intent_id = uuid.uuid4().hex
        self.db.execute(
            "INSERT INTO actions(intent_id, cycle_id, kind, target_entry_id, thread_root_id, content_hash, body, "
            "message_html, status, attempts, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?)",
            (intent_id, cycle_id, kind, target_entry_id, thread_root_id, content_hash, body, message_html,
             self.now_iso()))
        self.add_action_event(intent_id, cycle_id, "intent_recorded", {"kind": kind, "target": target_entry_id},
                              commit=False)
        if commit:
            self.db.commit()
        return intent_id

    def get_action(self, intent_id: str) -> sqlite3.Row:
        return self.db.execute("SELECT * FROM actions WHERE intent_id = ?", (intent_id,)).fetchone()

    def pending_actions(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM actions WHERE status = 'pending' ORDER BY created_at").fetchall()

    def bump_attempts(self, intent_id: str) -> None:
        self.db.execute("UPDATE actions SET attempts = attempts + 1, last_attempt_at = ? WHERE intent_id = ?",
                        (self.now_iso(), intent_id))
        self.db.commit()

    def set_canvas_entry(self, intent_id: str, canvas_entry_id: int) -> None:
        self.db.execute("UPDATE actions SET canvas_entry_id = ? WHERE intent_id = ?", (canvas_entry_id, intent_id))
        self.db.commit()

    def confirm_action(self, intent_id: str, canvas_entry_id: int, note: str) -> None:
        self.db.execute("UPDATE actions SET status = 'confirmed', canvas_entry_id = ?, confirmed_at = ?, note = ? "
                        "WHERE intent_id = ?", (canvas_entry_id, self.now_iso(), note, intent_id))
        self.db.commit()

    def abandon_action(self, intent_id: str, note: str) -> None:
        self.db.execute("UPDATE actions SET status = 'abandoned', note = ? WHERE intent_id = ?", (note, intent_id))
        self.db.commit()

    def add_action_event(self, intent_id: str, cycle_id: str, event: str, detail: dict | None = None,
                         commit: bool = True) -> None:
        self.db.execute("INSERT INTO action_events(intent_id, cycle_id, at, event, detail) VALUES (?, ?, ?, ?, ?)",
                        (intent_id, cycle_id, self.now_iso(), event, json.dumps(detail or {})))
        if commit:
            self.db.commit()

    def claimed_canvas_ids(self) -> set[int]:
        rows = self.db.execute("SELECT canvas_entry_id FROM actions WHERE canvas_entry_id IS NOT NULL "
                               "AND status = 'confirmed'").fetchall()
        return {row["canvas_entry_id"] for row in rows}

    def replied_targets(self) -> set[int]:
        rows = self.db.execute("SELECT target_entry_id FROM actions WHERE target_entry_id IS NOT NULL "
                               "AND status IN ('pending', 'confirmed')").fetchall()
        return {row["target_entry_id"] for row in rows}

    def recent_posts(self, since: datetime, kind: str | None = None, exclude: str | None = None) -> int:
        """Pending + confirmed actions created or last attempted after `since`."""
        sql = ("SELECT COUNT(*) FROM actions WHERE status IN ('pending', 'confirmed') "
               "AND COALESCE(last_attempt_at, created_at) > ?")
        args = [iso(since)]
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        if exclude:
            sql += " AND intent_id != ?"
            args.append(exclude)
        return self.db.execute(sql, args).fetchone()[0]

    def own_bodies(self, limit: int = 50) -> list[str]:
        rows = self.db.execute("SELECT body FROM actions WHERE status IN ('pending', 'confirmed') "
                               "ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [row["body"] for row in rows]

    def last_hour_cutoff(self) -> datetime:
        return self.clock() - timedelta(minutes=60)

    def last_day_cutoff(self) -> datetime:
        return self.clock() - timedelta(hours=24)

    # ------------------------------------------------------------------ spend

    def reserve_spend(self, cycle_id: str, model: str, worst_case_usd: float, input_estimate: int,
                      max_output: int) -> str:
        call_id = uuid.uuid4().hex
        self.db.execute("INSERT INTO spend(call_id, cycle_id, model, input_tokens, output_tokens, cost_usd, status, at) "
                        "VALUES (?, ?, ?, ?, ?, ?, 'reserved', ?)",
                        (call_id, cycle_id, model, input_estimate, max_output, worst_case_usd, self.now_iso()))
        self.db.commit()
        return call_id

    def record_spend(self, call_id: str, input_tokens: int, output_tokens: int, cost_usd: float) -> None:
        self.db.execute("UPDATE spend SET input_tokens = ?, output_tokens = ?, cost_usd = ?, status = 'recorded' "
                        "WHERE call_id = ?", (input_tokens, output_tokens, cost_usd, call_id))
        self.db.commit()

    def spend_total(self, since: datetime | None = None, cycle_id: str | None = None) -> float:
        sql = "SELECT COALESCE(SUM(cost_usd), 0) FROM spend WHERE 1 = 1"
        args = []
        if since is not None:
            sql += " AND at >= ?"
            args.append(iso(since))
        if cycle_id is not None:
            sql += " AND cycle_id = ?"
            args.append(cycle_id)
        return float(self.db.execute(sql, args).fetchone()[0])
