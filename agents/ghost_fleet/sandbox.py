"""A practice forum on your own machine. It stands in for Canvas with the same methods the runner calls, so a
sandbox turn runs exactly the production code: parse replies, resolve, commit, post, verify, hire. Nothing leaves
the machine. State lives in state/ghost_fleet_sandbox/ (forum.json, the database, state.json, the spectator page).

Four simulated peers post service offers so hiring can be rehearsed: Dice Den (exact dice and card odds, answers
the odds request), Quick Hash (looks capable, but copies the commitment instead of computing, so its audits fail),
Ledger Lens (does the work) and a haiku service (unrelated). Peers read only the posted request text, and answer
before the next sandbox turn.
"""

import dataclasses
import html
import json
import random
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from canvas_core.env import CONTROL_LINE_RUNNING
from canvas_core.canvas import GateClosed
from canvas_core.text import first_nonempty_line, strip_html

from . import engine, hire
from .config import OFFER_PATH, REPO_ROOT, SPECTATOR_PATH, Config
from .runner import rebuild, run_turn, to_html, turn_of, turn_time
from .store import Store

SANDBOX_DIR = REPO_ROOT / "state" / "ghost_fleet_sandbox"
HOUSE_ID, COURSE, TOPIC = 1, 1, 10
PEERS = [  # (name, offer, how it answers a request)
    ("dicelab", "Dice Den: what I do is compute exact dice and card probabilities, as fractions, with a simulation "
                "cross-check.", "odds"),
    ("quickhash", "SERVICE OFFER: Quick Hash. SHA-256 hashing, checksums and log audits, computed in Python.", "lazy"),
    ("ledger", "SERVICE OFFER: Ledger Lens. I audit game logs: SHA-256 hash checks and recomputation in Python. "
               "Reply with your data and a deadline.", "honest"),
    ("haiku", "SERVICE OFFER: a haiku about your agent, one per request.", None),
]


def _html(text: str) -> str:
    return "".join(f"<p>{html.escape(line)}</p>" for line in text.splitlines() if line.strip())


class SandboxCanvas:
    """Local forum with the parts of canvas_core.canvas.Canvas the runner uses."""

    def __init__(self, path: Path):
        self.path = path
        self.data = json.loads(path.read_text())
        self.log = lambda *a, **k: None

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1))

    @property
    def now(self) -> datetime:
        return datetime.fromisoformat(self.data["now"])

    def _add(self, user_id: int, message: str, parent_id: int | None) -> dict:
        entry_id = self.data["next_id"]
        self.data["next_id"] += 1
        stamp = self.now.isoformat()     # the sandbox clock stands still during a turn; ids keep posting order
        entry = {"id": entry_id, "user_id": user_id, "parent_id": parent_id, "created_at": stamp,
                 "updated_at": stamp, "message": message}
        self.data["entries"][str(entry_id)] = entry
        self.save()
        return entry

    def user_id(self, name: str) -> int:
        users = self.data["users"]
        if name not in users:
            users[name] = 100 + len(users)
        return users[name]

    def reply(self, name: str, text: str, parent_id: int | None = None) -> int:
        """A reply from user `name`: to the offer unless parent_id says otherwise."""
        parent = parent_id if parent_id is not None else self.data["offer_id"]
        return self._add(self.user_id(name), _html(text), parent)["id"]

    def new_thread(self, name: str, text: str) -> int:
        """A top-level post from user `name`, such as another service's offer."""
        return self._add(self.user_id(name), _html(text), None)["id"]

    def peers_work(self) -> list[int]:
        """Each simulated peer answers audit requests posted under its offer, from the request text alone."""
        answered = []
        offers = {self.data["peer_offers"][name]: (name, how) for name, _, how in PEERS}
        for entry in self._entries():
            if entry["user_id"] != HOUSE_ID or entry["parent_id"] not in offers:
                continue
            name, how = offers[entry["parent_id"]]
            peer = self.user_id(name)
            if how is None or any(e["parent_id"] == entry["id"] and e["user_id"] == peer for e in self._entries()):
                continue
            text = strip_html(entry["message"])
            if how == "odds":
                request = hire.parse_odds_request(text)
                if request is not None:
                    answers = hire.odds_expected(request["deck"], request["marked"], request["draws"])
                    answered.append(self.reply(name, hire.odds_answer_lines(request["token"], answers),
                                               parent_id=entry["id"]))
                continue
            request = hire.parse_request(text)
            if request is None:
                continue
            answers = hire.expected_answers(request["token"], request["commit"], request["reveal"], request["shots"])
            if how == "lazy":
                answers = dict(answers, proof=request["commit"])
            answered.append(self.reply(name, hire.answer_lines(request["token"], answers), parent_id=entry["id"]))
        return answered

    # ------------------------------------------------------------------ the Canvas surface the runner uses

    def self_profile(self) -> dict:
        return {"id": HOUSE_ID}

    def _entries(self) -> list[dict]:
        return [self.data["entries"][k] for k in sorted(self.data["entries"], key=int)]

    def topic(self) -> dict:
        entries = self._entries()
        return {"id": TOPIC, "message": f"<p>{html.escape(self.data['control'])}</p>",
                "discussion_subentry_count": len(entries),
                "last_reply_at": max((e["created_at"] for e in entries), default=None)}

    def topic_view(self) -> dict:
        def tree(entry):
            return dict(entry, replies=[tree(c) for c in self._entries() if c["parent_id"] == entry["id"]])
        return {"view": [tree(e) for e in self._entries() if e["parent_id"] is None], "new_entries": [],
                "participants": []}

    def top_level_entries(self) -> list[dict]:
        return [e for e in self._entries() if e["parent_id"] is None]

    def entry_replies(self, entry_id: int) -> list[dict]:
        out, frontier = [], [entry_id]
        while frontier:
            children = [e for e in self._entries() if e["parent_id"] in frontier]
            out += children
            frontier = [c["id"] for c in children]
        return out

    def entry_list(self, ids: list[int]) -> list[dict]:
        return [self.data["entries"][str(i)] for i in ids if str(i) in self.data["entries"]]

    def control_gate(self) -> tuple[bool, str]:
        line = first_nonempty_line(strip_html(self.topic()["message"])) or "missing"
        return line == CONTROL_LINE_RUNNING, line

    def create_entry(self, topic_id: int, message_html: str, parent_entry_id: int | None = None) -> dict:
        is_open, line = self.control_gate()
        if not is_open:
            raise GateClosed(line)
        return self._add(HOUSE_ID, message_html, parent_entry_id)


# ---------------------------------------------------------------------------- commands

def sandbox_config(cfg: Config) -> Config:
    return dataclasses.replace(cfg, course_id=COURSE, forum_topic_id=TOPIC, data_dir=SANDBOX_DIR)


def reset(cfg: Config) -> Path:
    """Fresh practice forum: the offer posted by the house, the clock 30 minutes before season start."""
    if SANDBOX_DIR.exists():
        shutil.rmtree(SANDBOX_DIR)
    SANDBOX_DIR.mkdir(parents=True)
    forum = SANDBOX_DIR / "forum.json"
    forum.write_text(json.dumps({"now": (cfg.season_start - timedelta(minutes=30)).isoformat(),
                                 "next_id": 5000, "entries": {}, "users": {}, "offer_id": None, "peer_offers": {},
                                 "control": CONTROL_LINE_RUNNING}))
    canvas = SandboxCanvas(forum)
    for name, text, _ in PEERS:
        canvas.data["peer_offers"][name] = canvas.new_thread(name, text)
    offer = canvas.create_entry(TOPIC, to_html(OFFER_PATH.read_text()))
    canvas.data["offer_id"] = offer["id"]
    canvas.save()
    store = Store(sandbox_config(cfg).db_path, clock=lambda: canvas.now)
    store.set_state("offer_entry_id", str(offer["id"]))
    store.close()
    shutil.copy(SPECTATOR_PATH, SANDBOX_DIR / "spectator.html")
    return SANDBOX_DIR


def open_forum(cfg: Config) -> SandboxCanvas:
    forum = SANDBOX_DIR / "forum.json"
    if not forum.exists():
        reset(cfg)
    return SandboxCanvas(forum)


def reply(cfg: Config, name: str, text: str) -> int:
    return open_forum(cfg).reply(name, text)


def next_turn(cfg: Config) -> dict:
    """Move the sandbox clock to the top of the next hour and run one turn there."""
    canvas = open_forum(cfg)
    scfg = sandbox_config(cfg)
    canvas.peers_work()
    turn = max(turn_of(cfg, canvas.now) + 1, 1)
    canvas.data["now"] = turn_time(cfg, turn).isoformat()
    canvas.save()
    store = Store(scfg.db_path, clock=lambda: canvas.now)
    try:
        result = run_turn(scfg, canvas=canvas, store=store, sleep=lambda s: None, echo=False)
        row = store.turn_row(turn)
    finally:
        store.close()
    result["post"] = row["body"] if row is not None and row["status"] == "confirmed" else None
    return result


def auto(cfg: Config, turns: int, seed: int | None = None) -> list[dict]:
    """Simulated players (the demo cast from engine.PLAYERS) reply, then a turn runs, `turns` times."""
    rng = random.Random(seed)
    results = []
    for _ in range(turns):
        canvas = open_forum(cfg)
        upcoming = max(turn_of(cfg, canvas.now) + 1, 1)
        store = Store(sandbox_config(cfg).db_path, clock=lambda: canvas.now)
        rows = [dict(r) for r in store.games()]
        game = rebuild(rows, [dict(r) for r in store.shots()], str)[-1] if rows else None
        store.close()
        if game is not None and not game.over:
            for callsign, skill, every, offset in engine.PLAYERS:
                if (upcoming - offset) % every == 0 and rng.random() < 0.85:
                    cells = engine.choose_cells(game, skill, rng)
                    canvas.reply(callsign, f"FIRE {' '.join(cells)}\nCALLSIGN {callsign}")
        results.append(next_turn(cfg))
    return results
