"""One run: halt check, lock, settle pending posts, read the forum, play this hour's turn, then hire.

The turn: read new replies under the offer, resolve them with the engine, commit, post one TURN reply under the
offer, and verify it. A turn is one hour slot of the season (turn 1 starts at season_start). At most one TURN post
per slot, and a slot with no new volleys posts nothing. A game's commitment is always posted before any volley
resolves against it, and the first run after season_end reveals the unfinished game.

The hire: settle open audit requests (check the provider's reply, or expire it), then ask at most one new provider
to audit a finished game (see hire.py).

Every post goes through the outbox: committed first, POSTed under the forum's write limit and the control line,
verified by re-fetching, and found again by its header after a lost acknowledgement instead of being re-posted.
"""

import fcntl
import html
import json
import random
import re
import secrets as pysecrets
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from canvas_core.canvas import Canvas, CanvasAuthError, CanvasError, CanvasTransientError, GateClosed, LostAck
from canvas_core.env import canvas_token, secret_values
from canvas_core.forum import fetch_forum
from canvas_core.log import CycleLog
from canvas_core.text import normalize, scrub
from canvas_core.times import parse_time

from . import engine, hire
from .config import OFFER_PATH, Config
from .filters import callsign_ok, post_violations
from .parse import default_name, parse_reply
from .store import Store

HALT_AFTER_FAILURES = 3
CLOCK_SKEW = timedelta(minutes=10)


def turn_of(cfg: Config, when: datetime) -> int:
    """Hour slot of the season: 1 during the first hour after season_start, 0 or less before it."""
    return int((when - cfg.season_start).total_seconds() // 3600) + 1


def turn_time(cfg: Config, turn: int) -> datetime:
    return cfg.season_start + timedelta(hours=turn - 1)


def to_html(body: str) -> str:
    """Plain text to Canvas HTML that keeps every line (the board needs them): one <p> per block of lines,
    <br> between lines, and runs of spaces kept so the grid stays aligned."""
    def line(text: str) -> str:
        return re.sub(r" {2,}", lambda m: "&nbsp;" * len(m.group()), html.escape(text.rstrip(), quote=False))

    blocks = [b for b in re.split(r"\n\s*\n", body.strip("\n")) if b.strip()]
    return "".join("<p>" + "<br>".join(line(x) for x in b.split("\n")) + "</p>" for b in blocks)


def rebuild(game_rows: list[dict], shot_rows: list[dict], name) -> list[engine.Game]:
    """Replay every game from its seed and shots, crediting shots to name(player_id). Refuses a game whose
    rebuilt layout no longer matches the commitment already published for it."""
    by_game: dict[int, list[dict]] = {}
    for s in shot_rows:
        by_game.setdefault(s["game_id"], []).append(s)
    games = []
    for row in game_rows:
        game = engine.Game(row["id"], random.Random(int(row["seed"])), row["start_turn"])
        if game.commit != row["commit_hash"]:
            raise RuntimeError(f"game {row['id']} no longer matches its published commitment; refusing to continue")
        for s in sorted(by_game.get(row["id"], []), key=lambda s: s["n"]):
            game.fire(engine.parse_cell(s["cell"]), name(s["player_id"]), s["turn"])
        game.end_turn = row["end_turn"]
        games.append(game)
    return games


def new_game_row(game_id: int, start_turn: int) -> dict:
    seed = pysecrets.randbits(128)
    game = engine.Game(game_id, random.Random(seed), start_turn)
    return {"id": game_id, "seed": str(seed), "start_turn": start_turn, "end_turn": None,
            "commit_hash": game.commit, "announced": 0}


def display_record(record: dict, name) -> dict:
    out = dict(record)
    out["volleys"] = [dict(v, by=name(v["by"])) for v in record["volleys"]]
    for key in ("skipped", "refunded", "carried"):
        out[key] = [name(p) for p in record[key]]
    return out


def write_block(cfg: Config, store: Store, forum_times: list[datetime], exclude: str | None = None) -> str | None:
    """The forum's write limit, counted over every kind of post: at most max_posts_per_hour in one hour slot (the
    slots turns use) and max_posts_per_day in any 24 hours. Our own records and our entries seen on the forum are
    both counted, so a wiped database cannot reset the limit. Forum timestamps get CLOCK_SKEW of slack, and
    anything further in our future is not trusted as a count."""
    now = store.clock()
    slot_start = turn_time(cfg, turn_of(cfg, now)) - timedelta(microseconds=1)
    for since, cap, label in ((slot_start, cfg.max_posts_per_hour, "this hour"),
                              (now - timedelta(days=1), cfg.max_posts_per_day, "the last 24 hours")):
        seen = sum(1 for t in forum_times if since < t <= now + CLOCK_SKEW)
        count = max(len(store.post_times(since, exclude)), seen)
        if count >= cap:
            return f"write limit: {count} posts in {label} (max {cap})"
    return None


@dataclass
class Plan:
    turn: int
    game_id: int
    record: dict                                   # players by id
    body: str = ""
    header: str = ""
    message_html: str = ""
    commitments: list = field(default_factory=list)
    game_rows: list = field(default_factory=list)  # every game, including a new one
    new_games: list = field(default_factory=list)
    ended_game: int | None = None
    new_shots: list = field(default_factory=list)
    volleys: list = field(default_factory=list)    # (entry_id, player_id, created_at, cells, status)
    ignored: list = field(default_factory=list)    # (entry_id, player_id, created_at)
    players: dict = field(default_factory=dict)    # player_id -> row, all players
    changed_players: set = field(default_factory=set)
    announce: list = field(default_factory=list)
    post: bool = True
    final: bool = False                            # the season's closing reveal


class TurnRun:
    def __init__(self, cfg: Config, store: Store, canvas, log: CycleLog, run_id: str, dry_run: bool, sleep):
        self.cfg = cfg
        self.store = store
        self.canvas = canvas
        self.log = log
        self.run_id = run_id
        self.dry_run = dry_run
        self.sleep = sleep
        self.entries: dict = {}
        self.poster: Poster | None = None
        self.result = {"run_id": run_id, "mode": "dry_run" if dry_run else "live"}

    # ------------------------------------------------------------------ bookkeeping

    def finish(self, outcome: str, summary: str, failed: bool = False) -> dict:
        self.store.finish_run(self.run_id, outcome, summary)
        if not self.dry_run:
            if failed:
                count = self.store.record_failure()
                self.log("failure_counted", consecutive_failures=count)
                if count >= HALT_AFTER_FAILURES:
                    self.store.set_halt(f"{count} consecutive failed runs")
                    self.log("halt", reason=f"{count} consecutive failed runs")
            elif outcome != "halted":
                self.store.reset_failures()
        self.log("run_end", outcome=outcome, summary=summary)
        self.result.update(outcome=outcome, summary=summary)
        return self.result

    def run(self) -> dict:
        try:
            return self._run()
        except CanvasAuthError as e:
            self.store.set_halt(f"canvas auth error: {e}")
            self.log("halt", reason="canvas 401/403", error=str(e))
            return self.finish("halted", f"canvas auth error: {e}")
        except LostAck:
            return self.finish("error", "lost acknowledgement; the post stays pending", failed=True)
        except Exception as e:
            message = scrub(f"{type(e).__name__}: {e}", secret_values())
            self.log("error", error=message)
            return self.finish("error", message, failed=True)

    # ------------------------------------------------------------------ the run

    def _run(self) -> dict:
        cfg, store, log = self.cfg, self.store, self.log
        if self.canvas is None:
            self.canvas = Canvas(cfg.canvas_base_url, canvas_token(), cfg.course_id, cfg.forum_topic_id,
                                 timeout=cfg.canvas_timeout, log=log, read_only=self.dry_run, sleep=self.sleep)
        else:
            self.canvas.log = log
        self_id = int(self.canvas.self_profile()["id"])
        known = store.get_state("self_id")
        if known is not None and int(known) != self_id:
            store.set_halt(f"token now belongs to user {self_id}, expected {known}")
            return self.finish("halted", "Canvas token user changed")
        if known is None and not self.dry_run:
            store.set_state("self_id", str(self_id))

        offer_id = cfg.offer_entry_id or store.offer_id()
        if offer_id is None and not self.dry_run:
            return self.finish("idle", "no offer yet: run post-offer, or set-offer with the id of one you posted")
        self.poster = Poster(self.canvas, store, cfg, self_id, self.run_id, log, self.sleep)

        if not self.dry_run:
            outcomes = self.poster.reconcile()
            if any(o in ("unverified", "failed") for o in outcomes):
                return self.finish("error", f"pending post unresolved: {outcomes}", failed=True)

        turn = turn_of(cfg, store.clock())
        self.result["turn"] = turn
        if turn < 1:
            return self.finish("idle", f"season starts {cfg.season_start.isoformat()}")
        gate_open, line = self.canvas.control_gate()
        if not gate_open:
            return self.finish("idle", f"control gate closed ({line[:60]}); volleys and audits wait")
        self.entries = fetch_forum(self.canvas, log)[0]
        self.poster.forum_times = [parse_time(e["created_at"]) for e in self.entries.values()
                                   if e["user_id"] == self_id and e["created_at"]]

        # Replies to audit requests are settled first (reads only), so this hour's TURN post already carries
        # any audit accepted now; new requests go out after the TURN post.
        settled = [n for n in (self.settle_hire(h) for h in store.open_hires()) if n] \
            if cfg.hire_enabled and not self.dry_run else []
        outcome, summary, failed = self.turn_step(turn, self_id, offer_id)
        if outcome == "error":
            return self.finish(outcome, "; ".join([summary] + settled), failed=failed)
        if self.dry_run:
            if cfg.hire_enabled:
                summary += "; " + self.discovery_preview(self_id)
            return self.finish(outcome, summary)
        if cfg.hire_enabled:
            note, hire_failed = self.hire_step(self_id)
            summary = "; ".join([summary] + settled + ([note] if note else []))
            failed = failed or hire_failed
        write_spectator_state(cfg, store)
        return self.finish("error" if failed else outcome, summary, failed=failed)

    def turn_step(self, turn: int, self_id: int, offer_id: int | None) -> tuple[str, str, bool]:
        cfg, store = self.cfg, self.store
        season_over = turn_time(cfg, turn) > cfg.season_end
        if season_over and (store.get_state("season_closed") or not store.games()):
            return "idle", "season is over", False
        if store.turn_row(turn) is not None:
            return "idle", f"turn {turn} already posted", False
        blocked = self.poster.blocked()
        if blocked:
            return "idle", f"{blocked}; volleys wait", False

        if season_over:
            plan = self.final_plan(turn)
        else:
            plan = self.plan(turn, self.entries if offer_id is not None else {}, self_id, offer_id)
        violations = post_violations(plan.body, secret_values(), plan.commitments) if plan.post else []
        self.result.update(post=plan.body if plan.post else None, record=plan.record)
        if violations:
            self.log("post_rejected", reasons=violations)
            return "error", f"TURN post failed the filter: {violations}; nothing committed", True

        what = f"the final reveal of game {plan.game_id}" if plan.final else f"turn {turn}"
        if self.dry_run:
            return "dry_run", f"would post {what}" if plan.post else "quiet hour: nothing to post", False

        self.commit(plan, offer_id)
        if not plan.post:
            return "quiet", f"turn {turn}: no new volleys", False
        outcome = self.poster.execute(f"turn:{turn}")
        if outcome == "confirmed":
            return "posted", f"{what} posted as entry {store.turn_row(turn)['canvas_entry_id']}", False
        if outcome in ("gate_closed", "capped"):
            return "idle", f"{what} stays pending ({outcome})", False
        return "error", f"{what}: {outcome}", True

    def plan(self, turn: int, entries: dict, self_id: int, offer_id: int | None) -> Plan:
        store, cfg = self.store, self.cfg
        secrets = secret_values()
        game_rows = [dict(r) for r in store.games()]
        shot_rows = [dict(r) for r in store.shots()]
        players = {r["player_id"]: dict(r) for r in store.players()}
        new_games = []
        if not game_rows or game_rows[-1]["end_turn"] is not None:
            new_games.append(new_game_row(len(game_rows) + 1, turn))
            game_rows += new_games
        current = game_rows[-1]
        plan = Plan(turn, current["id"], {}, game_rows=game_rows, new_games=new_games, players=players)

        if not current["announced"]:
            # Opening post: publish the commitment first. Replies already waiting resolve next turn.
            plan.record = {"turn": turn, "time": turn_time(cfg, turn).isoformat(), "game": current["id"],
                           "volleys": [], "skipped": [], "refunded": [], "carried": []}
            plan.announce = [current["id"]]
            return self.render(plan, shot_rows)

        known = store.known_entry_ids()
        fresh = sorted((e for e in entries.values()
                        if e["root_id"] == offer_id and e["id"] != offer_id and not e["deleted"]
                        and e["user_id"] not in (None, self_id) and e["id"] not in known),
                       key=lambda e: (e["created_at"] or "", e["id"]))
        queue = [{"by": str(r["player_id"]), "cells": json.loads(r["cells"]), "carried": True,
                  "entry": r["entry_id"], "created_at": r["created_at"]} for r in store.carried_volleys()]
        taken_callsigns = {p["callsign"].lower() for p in players.values() if p["callsign"]}
        taken_defaults = {p["default_name"] for p in players.values()}
        for e in fresh:
            cells, callsign = parse_reply(e["text"])
            if not cells:
                plan.ignored.append((e["id"], e["user_id"], e["created_at"]))
                continue
            player = players.get(e["user_id"])
            if player is None:
                player = {"player_id": e["user_id"], "callsign": None, "first_turn": turn,
                          "default_name": default_name(e["user_id"], taken_defaults)}
                players[e["user_id"]] = player
                taken_defaults.add(player["default_name"])
                plan.changed_players.add(e["user_id"])
            if player["callsign"] is None and callsign_ok(callsign, secrets) \
                    and callsign.lower() not in taken_callsigns:
                player["callsign"] = callsign
                taken_callsigns.add(callsign.lower())
                plan.changed_players.add(e["user_id"])
            queue.append({"by": str(e["user_id"]), "cells": cells, "carried": False, "entry": e["id"],
                          "created_at": e["created_at"]})
        if not queue:
            plan.post = False
            return plan

        game = rebuild(game_rows, shot_rows, str)[-1]
        before = len(game.shots)
        last = {str(k): v for k, v in store.last_fired_turns().items()}
        resolved, _ = engine.resolve_turn(game, queue, turn, last)
        plan.record = {"turn": turn, "time": turn_time(cfg, turn).isoformat(), "game": game.id, **resolved}
        plan.new_shots = [(game.id, s["n"], turn, int(s["by"]), s["cell"]) for s in game.shots[before:]]
        plan.volleys = [(v["entry"], int(v["by"]), v["created_at"], v["cells"], v["outcome"]) for v in queue]
        shot_rows += [{"game_id": g, "n": n, "turn": t, "player_id": p, "cell": c} for g, n, t, p, c in plan.new_shots]
        if game.over:
            current["end_turn"] = turn
            plan.ended_game = game.id
            nxt = new_game_row(game.id + 1, turn + 1)
            plan.new_games.append(nxt)
            game_rows.append(nxt)
            plan.announce = [nxt["id"]]
        return self.render(plan, shot_rows)

    def final_plan(self, turn: int) -> Plan:
        """The season's last post: the unfinished game stops and its layout is revealed, so its commitment
        can be checked like every other game's."""
        game_rows = [dict(r) for r in self.store.games()]
        current = game_rows[-1]
        current["end_turn"] = turn
        record = {"turn": turn, "time": turn_time(self.cfg, turn).isoformat(), "game": current["id"],
                  "volleys": [], "skipped": [], "refunded": [], "carried": [], "final": True}
        plan = Plan(turn, current["id"], record, game_rows=game_rows, ended_game=current["id"], final=True,
                    players={r["player_id"]: dict(r) for r in self.store.players()})
        return self.render(plan, [dict(r) for r in self.store.shots()])

    def render(self, plan: Plan, shot_rows: list[dict]) -> Plan:
        names = {pid: p["callsign"] or p["default_name"] for pid, p in plan.players.items()}

        def name(pid) -> str:
            return names.get(int(pid), "Player-?")

        games = rebuild(plan.game_rows, shot_rows, name)
        game = next(g for g in games if g.id == plan.game_id)
        nxt = games[-1] if games[-1].id != plan.game_id else None
        board = engine.leaderboards(games)
        plan.body = engine.render_turn_post(game, display_record(plan.record, name), board, next_game=nxt,
                                            final=plan.final, audits=hire.audit_summary(self.store.hires()))
        plan.header = plan.body.splitlines()[0]
        plan.message_html = to_html(plan.body)
        plan.commitments = [g.commit for g in games]
        return plan

    def commit(self, plan: Plan, offer_id: int | None) -> None:
        """Everything the turn decided, in one transaction, before any POST."""
        store = self.store
        try:
            for row in plan.new_games:
                store.add_game(row["id"], int(row["seed"]), row["start_turn"], row["commit_hash"])
            if plan.ended_game is not None:
                store.end_game(plan.ended_game, plan.turn)
            for shot in plan.new_shots:
                store.add_shot(*shot)
            for entry_id, player_id, created_at, cells, status in plan.volleys:
                store.save_volley(entry_id, player_id, created_at, cells, status, plan.turn, plan.game_id)
            for entry_id, player_id, created_at in plan.ignored:
                store.save_volley(entry_id, player_id, created_at, [], "ignored", plan.turn, None)
            for pid in plan.changed_players:
                p = plan.players[pid]
                store.save_player(pid, p["callsign"], p["default_name"], p["first_turn"])
            if plan.post:
                store.add_turn(plan.turn, plan.game_id, plan.record, plan.header, plan.body, plan.message_html,
                               offer_id)
                for game_id in plan.announce:
                    store.announce(game_id)
            if plan.final:
                store.set_state("season_closed", "1", commit=False)
            store.db.commit()
        except BaseException:
            store.db.rollback()
            raise

    # ------------------------------------------------------------------ hiring an auditor

    def hire_step(self, self_id: int) -> tuple[str | None, bool]:
        """Ask at most one new provider to audit a finished game. Returns (note, failed)."""
        game_id = self.game_needing_audit()
        if game_id is None:
            return None, False
        return self.request_audit(game_id, self_id)

    def settle_hire(self, h) -> str | None:
        store, token = self.store, h["token"]
        post = store.post(f"hire:{token}")
        if post["status"] == "abandoned":
            store.settle_hire(token, "unsent")
            store.add_event(token, self.run_id, "hire_unsent")
            return f"audit request {token} could not be posted"
        if post["status"] != "confirmed":
            return None
        request_id = post["canvas_entry_id"]
        replies = sorted((e for e in self.entries.values()
                          if e["user_id"] == h["provider_user_id"] and e["root_id"] == h["provider_entry_id"]
                          and e["id"] > request_id and not e["deleted"]
                          and (e["parent_id"] == request_id or token in (e["text"] or ""))),
                         key=lambda e: (e["created_at"] or "", e["id"]))
        if replies:
            accepted, checks = hire.check_reply(replies[0]["text"], json.loads(h["expected"]))
            status = "accepted" if accepted else "rejected"
            store.settle_hire(token, status, replies[0]["id"], checks)
            store.add_event(token, self.run_id, f"hire_{status}", {"reply": replies[0]["id"], "checks": checks})
            self.log(f"hire_{status}", token=token, reply=replies[0]["id"], checks=checks)
            return f"audit of game {h['game_id']} by offer {h['provider_entry_id']}: {status}"
        if store.clock() > parse_time(h["deadline"]):
            store.settle_hire(token, "expired")
            store.add_event(token, self.run_id, "hire_expired")
            return f"audit request {token} expired"
        return None

    def game_needing_audit(self) -> int | None:
        """The first finished game with no open or accepted audit, whose reveal is already on the forum."""
        for g in self.store.games():
            if g["end_turn"] is None:
                continue
            hires = self.store.hires(g["id"])
            if any(h["status"] in ("requested", "accepted") for h in hires) or len(hires) >= self.cfg.hire_max_providers:
                continue
            revealed = self.store.turn_row(g["end_turn"])
            if revealed is not None and revealed["status"] == "confirmed":
                return g["id"]
        return None

    def track_record(self) -> dict[int, int]:
        record: dict[int, int] = {}
        for h in self.store.hires():
            if h["status"] in ("accepted", "rejected", "expired"):
                record[h["provider_user_id"]] = record.get(h["provider_user_id"], 0) + \
                    (1 if h["status"] == "accepted" else -1)
        return record

    def request_audit(self, game_id: int, self_id: int) -> tuple[str, bool]:
        cfg, store = self.cfg, self.store
        blocked = self.poster.blocked()
        if blocked:
            return f"audit request for game {game_id} waits: {blocked}", False
        tried = {h["provider_user_id"] for h in store.hires(game_id)}
        offers_seen, candidates = hire.discover(self.entries, self_id, cfg.hire_min_score, tried, self.track_record())
        chosen = candidates[0] if candidates and candidates[0]["suitable"] else None
        store.add_discovery(self.run_id, game_id, offers_seen, candidates, chosen["entry_id"] if chosen else None)
        self.log("discovery", game=game_id, offers_seen=offers_seen, candidates=candidates[:5],
                 chosen=chosen["entry_id"] if chosen else None)
        if chosen is None:
            return f"no suitable auditor among {offers_seen} offers for game {game_id}", False

        game = rebuild([dict(r) for r in store.games()], [dict(r) for r in store.shots()], str)[game_id - 1]
        token = hire.token_for(game_id, chosen["entry_id"])
        shots = [{"n": s["n"], "cell": s["cell"], "result": s["result"], "hint": s["hint"]} for s in game.shots]
        expected = hire.expected_answers(token, game.commit, game.reveal, shots)
        deadline = store.clock() + timedelta(hours=cfg.hire_deadline_hours)
        due = engine.fmt_time(deadline.astimezone(cfg.season_start.tzinfo).isoformat())
        body = hire.request_body(token, game_id, game.commit, game.reveal, shots,
                                 f"{due} ({cfg.hire_deadline_hours:g} hours)")
        violations = post_violations(body, secret_values(), [game.commit])
        if violations:
            return f"audit request for game {game_id} failed the filter: {violations}", True
        key = f"hire:{token}"
        try:
            store.add_hire(token, game_id, chosen["entry_id"], chosen["user_id"], chosen["score"],
                           chosen["capabilities"], expected, deadline)
            store.add_post(key, "hire", chosen["entry_id"], body.splitlines()[0], body, to_html(body))
            store.db.commit()
        except BaseException:
            store.db.rollback()
            raise
        store.add_event(token, self.run_id, "hire_requested", {"offer": chosen["entry_id"], "score": chosen["score"]})
        outcome = self.poster.execute(key)
        return f"audit of game {game_id} requested from offer {chosen['entry_id']}: {outcome}", \
            outcome in ("unverified", "failed")

    def discovery_preview(self, self_id: int) -> str:
        """Dry runs: which offer would be hired, without writing anything."""
        offers_seen, candidates = hire.discover(self.entries, self_id, self.cfg.hire_min_score, set(),
                                                self.track_record())
        self.result["discovery"] = candidates
        best = candidates[0] if candidates and candidates[0]["suitable"] else None
        if best is None:
            return f"auditor discovery: {offers_seen} offers read, none suitable"
        return (f"auditor discovery: {offers_seen} offers read, best is entry {best['entry_id']} "
                f"(score {best['score']}: {', '.join(best['capabilities'])})")


class Poster:
    """POST a committed outbox row exactly once: check the write limit right before, verify by re-fetching, and
    after an unknown outcome look for the post (by its header line) before ever posting again."""

    def __init__(self, canvas, store: Store, cfg: Config, self_id: int, run_id: str, log, sleep=time.sleep):
        self.canvas = canvas
        self.store = store
        self.cfg = cfg
        self.self_id = self_id
        self.run_id = run_id
        self.log = log
        self.sleep = sleep
        self.forum_times: list[datetime] = []

    def blocked(self, exclude: str | None = None) -> str | None:
        return write_block(self.cfg, self.store, self.forum_times, exclude)

    def _event(self, key: str, event: str, **detail) -> None:
        self.store.add_event(key, self.run_id, event, detail)
        self.log(f"post_{event}", key=key, **detail)

    def _matches(self, post, raw) -> bool:
        if not isinstance(raw, dict) or raw.get("user_id") is None or int(raw["user_id"]) != self.self_id:
            return False
        parent = int(raw["parent_id"]) if raw.get("parent_id") else None
        return parent == post["parent_entry_id"] and normalize(f"<p>{html.escape(post['header'])}</p>") in \
            normalize(raw.get("message") or "")

    def lookup(self, post) -> int | None:
        parent = post["parent_entry_id"]
        found = self.canvas.entry_replies(parent) if parent is not None else self.canvas.top_level_entries()
        for raw in found:
            if self._matches(post, raw):
                return int(raw["id"])
        return None

    def verify(self, post, entry_id: int, attempts: int = 4) -> bool:
        for i in range(attempts):
            try:
                found = self.canvas.entry_list([entry_id])
            except CanvasTransientError as e:
                self._event(post["key"], "verify_fetch_error", error=str(e))
                found = []
            if any(int(raw.get("id", -1)) == entry_id and self._matches(post, raw) for raw in found):
                return True
            if i < attempts - 1:
                self.sleep(2 ** i + random.uniform(0, 1))
        return False

    def execute(self, key: str) -> str:
        """Returns confirmed | gate_closed | capped | rejected | unverified | failed."""
        post = self.store.post(key)
        for attempt in range(1, self.cfg.max_post_attempts + 1):
            blocked = self.blocked(exclude=key)
            if blocked:
                self._event(key, "capped", reason=blocked)
                return "capped"
            self.store.bump_attempts(key)
            self._event(key, "attempt", attempt=attempt)
            try:
                data = self.canvas.create_entry(self.cfg.forum_topic_id, post["message_html"], post["parent_entry_id"])
            except GateClosed as e:
                self._event(key, "gate_closed", line=str(e)[:80])
                return "gate_closed"
            except LostAck:
                self._event(key, "ack_lost")
                raise
            except CanvasError as e:
                self.store.abandon_post(key, f"rejected by Canvas: {e}")
                self._event(key, "abandoned", reason=str(e))
                return "rejected"
            except CanvasTransientError as e:
                self._event(key, "outcome_unknown", attempt=attempt, error=str(e))
                found = self.lookup(post)
                if found is not None:
                    self.store.confirm_post(key, found, "found after an unknown outcome")
                    self._event(key, "confirmed", canvas_entry_id=found)
                    return "confirmed"
                if attempt < self.cfg.max_post_attempts:
                    self.sleep(2 ** attempt + random.uniform(0, 1))
                continue
            entry_id = int(data["id"])
            self.store.set_canvas_entry(key, entry_id)
            if self.verify(post, entry_id):
                self.store.confirm_post(key, entry_id, "verified after post")
                self._event(key, "confirmed", canvas_entry_id=entry_id)
                return "confirmed"
            self._event(key, "verify_failed", canvas_entry_id=entry_id)
            return "unverified"
        self._event(key, "attempts_exhausted")
        return "failed"

    def settle(self, post) -> str:
        """Resolve one pending post: re-verify, find it, give up on it, or POST it."""
        key = post["key"]
        too_old = self.store.post_age(post) > timedelta(hours=self.cfg.pending_max_age_hours)
        if post["canvas_entry_id"] is not None:
            if self.verify(post, post["canvas_entry_id"]):
                self.store.confirm_post(key, post["canvas_entry_id"], "verified at reconcile")
                return "confirmed"
            if too_old:
                self.store.abandon_post(key, "acknowledged but never verified")
                return "abandoned"
            return "unverified"
        found = self.lookup(post)
        if found is not None:
            self.store.confirm_post(key, found, "found at reconcile")
            self._event(key, "confirmed", canvas_entry_id=found)
            return "confirmed"
        if too_old:
            self.store.abandon_post(key, "never appeared and too old to post late")
            self._event(key, "abandoned", reason="too old")
            return "abandoned"
        return self.execute(key)

    def reconcile(self) -> list[str]:
        """Settle every pending post before doing anything new."""
        return [self.settle(post) for post in self.store.pending_posts()]


# ---------------------------------------------------------------------------- spectator state

def spectator_state(cfg: Config, store: Store) -> dict | None:
    """What the spectator page reads. Holds commitments and finished games' reveals, never a live layout."""
    turns = [json.loads(r["record"]) for r in store.turn_rows()]
    if not turns:
        return None
    names = {r["player_id"]: r["callsign"] or r["default_name"] for r in store.players()}

    def name(pid) -> str:
        return names.get(int(pid), "Player-?")

    games = rebuild([dict(r) for r in store.games()], [dict(r) for r in store.shots()], name)
    state, _ = engine.export(games, [display_record(t, name) for t in turns], demo=False,
                             ends=cfg.season_end.isoformat(),
                             status="ENDED" if store.get_state("season_closed") else "RUNNING")
    state["audits"] = [{"game": h["game_id"], "status": h["status"], "offer": h["provider_entry_id"],
                        "deadline": h["deadline"], "checks": json.loads(h["checks"]) if h["checks"] else None,
                        "mismatches": len(json.loads(h["expected"])["mismatches"]),
                        "commit_matches": json.loads(h["expected"])["commit_matches"]} for h in store.hires()]
    return state


def write_spectator_state(cfg: Config, store: Store) -> None:
    state = spectator_state(cfg, store)
    if state is None:
        return
    tmp = cfg.state_json_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False))
    tmp.replace(cfg.state_json_path)


# ---------------------------------------------------------------------------- entry points

def run_turn(cfg: Config, dry_run: bool = False, *, canvas=None, store: Store | None = None, sleep=time.sleep,
             echo: bool = True) -> dict:
    own_store = store is None
    store = store or Store(cfg.db_path)
    try:
        reason = store.halt_reason()
        if reason:
            print(f"halted: {reason}. Clear with: python -m agents.ghost_fleet.cli unhalt", file=sys.stderr)
            return {"outcome": "halted", "summary": reason}
        if cfg.course_id is None or cfg.forum_topic_id is None:
            raise RuntimeError("course_id/forum_topic_id missing from config; run scripts/discover.py")
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        with open(cfg.lock_path, "w") as lock_file:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"outcome": "locked", "summary": "another turn is running"}
            work = store.snapshot() if dry_run else store
            run_id = work.start_run("dry_run" if dry_run else "live")
            log = CycleLog(None if dry_run else cfg.log_dir, run_id, echo=echo)
            log("run_start", mode="dry_run" if dry_run else "live")
            try:
                return TurnRun(cfg, work, canvas, log, run_id, dry_run, sleep).run()
            finally:
                if dry_run:
                    work.close()
    finally:
        if own_store:
            store.close()


def post_offer(cfg: Config, *, canvas=None, store: Store) -> int:
    """Post offer.txt as a new thread in the topic, once, through the outbox, and remember its id."""
    existing = cfg.offer_entry_id or store.offer_id()
    if existing:
        raise RuntimeError(f"an offer is already set (entry {existing}); not posting another")
    if cfg.course_id is None or cfg.forum_topic_id is None:
        raise RuntimeError("course_id/forum_topic_id missing from config; run scripts/discover.py")
    body = OFFER_PATH.read_text()
    violations = post_violations(body, secret_values())
    if violations:
        raise RuntimeError(f"offer.txt fails the post filter: {violations}")
    canvas = canvas or Canvas(cfg.canvas_base_url, canvas_token(), cfg.course_id, cfg.forum_topic_id,
                              timeout=cfg.canvas_timeout)
    poster = Poster(canvas, store, cfg, int(canvas.self_profile()["id"]), "post-offer", lambda *a, **k: None)
    if store.post("offer") is None:
        store.add_post("offer", "offer", None, body.splitlines()[0], body, to_html(body))
        store.db.commit()
    post = store.post("offer")
    outcome = "confirmed" if post["status"] == "confirmed" else poster.settle(post)
    if outcome != "confirmed":
        raise RuntimeError(f"offer not posted ({outcome}); run post-offer again to retry, it never posts twice")
    entry_id = int(store.post("offer")["canvas_entry_id"])
    store.set_state("offer_entry_id", str(entry_id))
    return entry_id
