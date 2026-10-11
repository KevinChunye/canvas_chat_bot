#!/usr/bin/env python3
"""Ghost Fleet: reference engine + demo-season generator.

Run:  python -m agents.ghost_fleet.engine   (from the repo root)
Writes state.json (what the spectator page reads) and turn_post.txt (an example
TURN post) into the current directory. The functions here -- place_fleet,
layout_string, sonar, Game.fire, leaderboards, render_turn_post -- are the house
logic the live runner should use.

Rules implemented
- 10x10 board, rows A-J, columns 1-10. Fleet: Carrier 5, Battleship 4, Cruiser 3,
  Submarine 3, Destroyer 2 (17 ship cells). Straight, no overlap, may touch.
- One TURN per hour. Volleys posted since the previous TURN resolve in posting order.
- Volley = FIRE + up to 4 cells in priority order; the first 2 still-open cells fire.
- Cooldown: one counted volley per player per 2 turns. Volleys inside it are skipped.
- Turn cap: 40 shots; volleys beyond it carry to the next turn.
- If the fleet sinks mid-turn, later volleys that turn are refunded.
- Miss -> SONAR, computed against the whole fleet (sunk ships included):
  HOT  = a ship cell is in the 3x3 box around the miss
  WARM = none in 3x3, but one in the 5x5 box
  COLD = no ship cell in the 5x5 box
- INTEL after every 8th miss: the number of ship cells in one row or column.
- Points: hit 1, hit that sinks a ship 3, hit that sinks the last ship 5.
- Titles: First Blood, Admiral (last ship), Sharpshooter (best hit rate, 3+ shots).
- Swarm medal: Gold <= 45 shots, Silver <= 60, Bronze <= 75 (random play ~95).
- Season boards: TOTAL points; AVG points per finished game (min 3 finished games).
"""
import hashlib
import json
import random
from datetime import datetime, timedelta, timezone

import pandas as pd

ROWS = "ABCDEFGHIJ"
SIZE = 10
FLEET = [("Carrier", 5), ("Battleship", 4), ("Cruiser", 3), ("Submarine", 3), ("Destroyer", 2)]
SHIP_CELLS = sum(length for _, length in FLEET)
RANDOM_BASELINE = round(SHIP_CELLS * (SIZE * SIZE + 1) / (SHIP_CELLS + 1), 1)
MEDALS = [("Gold", 45), ("Silver", 60), ("Bronze", 75)]
SHOTS_PER_VOLLEY = 2
MAX_CELLS_LISTED = 4
COOLDOWN_TURNS = 2
TURN_SHOT_CAP = 40
INTEL_EVERY = 8
MIN_GAMES_FOR_AVG = 3
POINTS = {"hit": 1, "sunk": 3, "last": 5}
ET = timezone(timedelta(hours=-4))
KEYCAPS = ["1\ufe0f\u20e3", "2\ufe0f\u20e3", "3\ufe0f\u20e3", "4\ufe0f\u20e3", "5\ufe0f\u20e3",
           "6\ufe0f\u20e3", "7\ufe0f\u20e3", "8\ufe0f\u20e3", "9\ufe0f\u20e3", "\U0001f51f"]
MISS_ICON = {"hot": "\U0001f534", "warm": "\U0001f7e1", "cold": "\U0001f535"}
OPEN, HIT, SUNK = "\U0001f30a", "\U0001f4a5", "\U0001f480"


def cell_name(cell):
    return f"{ROWS[cell[0]]}{cell[1] + 1}"


def parse_cell(name):
    return ROWS.index(name[0]), int(name[1:]) - 1


def place_fleet(rng):
    taken = set()
    fleet = {}
    for name, length in FLEET:
        while True:
            if rng.random() < 0.5:
                r, c = rng.randrange(SIZE), rng.randrange(SIZE - length + 1)
                cells = [(r, c + i) for i in range(length)]
            else:
                r, c = rng.randrange(SIZE - length + 1), rng.randrange(SIZE)
                cells = [(r + i, c) for i in range(length)]
            if not taken.intersection(cells):
                taken.update(cells)
                fleet[name] = cells
                break
    return fleet


def layout_string(game_id, fleet, salt):
    parts = ["GHOSTFLEET", f"G{game_id}"]
    for name, _ in FLEET:
        parts.append(name.upper() + ":" + ",".join(cell_name(c) for c in sorted(fleet[name])))
    parts.append(f"SALT:{salt}")
    return "|".join(parts)


def sonar(cell, ship_cells):
    d = min(max(abs(cell[0] - s[0]), abs(cell[1] - s[1])) for s in ship_cells)
    if d == 1:
        return "hot"
    if d == 2:
        return "warm"
    return "cold"


class Game:
    def __init__(self, game_id, rng, start_turn):
        self.id = game_id
        self.rng = rng
        self.fleet = place_fleet(rng)
        self.owner = {c: ship for ship, cells in self.fleet.items() for c in cells}
        salt = "".join(rng.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(12))
        self.reveal = layout_string(game_id, self.fleet, salt)
        self.commit = hashlib.sha256(self.reveal.encode()).hexdigest()
        self.start_turn = start_turn
        self.end_turn = None
        self.shots = []
        self.fired = set()
        self.hits_on = {ship: 0 for ship, _ in FLEET}
        self.sunk = []
        self.misses = 0
        self.intel = []
        self.used_lines = set()

    @property
    def over(self):
        return len(self.sunk) == len(FLEET)

    def fire(self, cell, by, turn):
        self.fired.add(cell)
        shot = {"n": len(self.shots) + 1, "turn": turn, "by": by, "cell": cell_name(cell),
                "result": "miss", "hint": None, "ship": None, "points": 0}
        ship = self.owner.get(cell)
        if ship:
            self.hits_on[ship] += 1
            shot["result"], shot["points"] = "hit", POINTS["hit"]
            if self.hits_on[ship] == len(self.fleet[ship]):
                self.sunk.append({"ship": ship, "turn": turn,
                                  "cells": [cell_name(c) for c in sorted(self.fleet[ship])]})
                shot["result"], shot["ship"] = "sunk", ship
                shot["points"] = POINTS["last"] if self.over else POINTS["sunk"]
        else:
            shot["hint"] = sonar(cell, self.owner)
            self.misses += 1
            if self.misses % INTEL_EVERY == 0:
                self.intel.append(self.make_intel(turn))
        self.shots.append(shot)
        return shot

    def make_intel(self, turn):
        lines = [("Row", i) for i in range(SIZE)] + [("Column", i) for i in range(SIZE)]
        kind, idx = self.rng.choice([x for x in lines if x not in self.used_lines])
        self.used_lines.add((kind, idx))
        axis = 0 if kind == "Row" else 1
        count = sum(1 for c in self.owner if c[axis] == idx)
        label = f"Row {ROWS[idx]}" if kind == "Row" else f"Column {idx + 1}"
        return {"turn": turn, "after_miss": self.misses, "line": label, "count": count}


# ---------------------------------------------------------------- demo players
# callsign, skill (0-1), check interval in turns, offset
PLAYERS = [
    ("Kraken", 0.9, 2, 0), ("Nautilus", 0.8, 3, 1), ("Polaris", 0.85, 3, 2),
    ("Moby", 0.5, 2, 1), ("Corsair", 0.7, 4, 0), ("Tidewalker", 0.6, 3, 0),
    ("Harpoon", 0.75, 2, 0), ("Scylla", 0.4, 4, 2), ("Leviathan", 0.65, 3, 2),
    ("Player-3F", 0.3, 4, 1), ("Barnacle", 0.55, 1, 0), ("Sonarbot", 0.95, 3, 1),
    ("Player-A7", 0.2, 4, 3), ("Bilgewater", 0.45, 3, 0),
]


def choose_cells(game, skill, rng):
    """A demo policy: skilled volleys follow hits and sonar, others shoot at random."""
    sunk_cells = {x for s in game.sunk for x in s["cells"]}
    live_hits = [parse_cell(s["cell"]) for s in game.shots
                 if s["result"] != "miss" and s["cell"] not in sunk_cells]
    misses = [(parse_cell(s["cell"]), s["hint"]) for s in game.shots if s["result"] == "miss"]
    smart = rng.random() < skill
    scores = {}
    for r in range(SIZE):
        for c in range(SIZE):
            if (r, c) in game.fired:
                continue
            score = rng.random()
            if smart:
                score += 6 * sum(1 for h in live_hits if abs(h[0] - r) + abs(h[1] - c) == 1)
                for m, hint in misses:
                    d = max(abs(m[0] - r), abs(m[1] - c))
                    if hint == "cold" and d <= 2:
                        score -= 8
                    elif hint == "hot" and d == 1:
                        score += 2
                    elif hint == "warm" and d == 1:
                        score -= 3
                    elif hint == "warm" and d == 2:
                        score += 1
            scores[cell_name((r, c))] = score
    return pd.Series(scores).nlargest(MAX_CELLS_LISTED).index.tolist()


def resolve_turn(game, queue, turn, last_counted):
    """Resolve one turn's volleys in posting order.

    queue: volley dicts {"by", "cells", "carried"}; "by" is also the cooldown key.
    last_counted: by -> last turn a volley of theirs fired; updated in place.
    Returns (record, carry). Each volley gets an "outcome": fired | skipped | refunded | carried.
    """
    record = {"volleys": [], "skipped": [], "refunded": [], "carried": []}
    carry = []
    shots_this_turn = 0
    for volley in queue:
        name = volley["by"]
        if game.over:
            record["refunded"].append(name)
            volley["outcome"] = "refunded"
            continue
        if not volley["carried"] and name in last_counted and turn - last_counted[name] < COOLDOWN_TURNS:
            record["skipped"].append(name)
            volley["outcome"] = "skipped"
            continue
        open_cells = [parse_cell(x) for x in volley["cells"] if parse_cell(x) not in game.fired]
        open_cells = open_cells[:SHOTS_PER_VOLLEY]
        if not open_cells:
            record["refunded"].append(name)
            volley["outcome"] = "refunded"
            continue
        if shots_this_turn + len(open_cells) > TURN_SHOT_CAP:
            carry.append({**volley, "carried": True})
            record["carried"].append(name)
            volley["outcome"] = "carried"
            continue
        fired = []
        for cell in open_cells:          # a shot after the last ship sinks would land in a finished game
            if not game.over:
                fired.append(game.fire(cell, name, turn))
        shots_this_turn += len(fired)
        last_counted[name] = turn
        volley["outcome"] = "fired"
        record["volleys"].append({"by": name, "listed": volley["cells"],
                                  "shots": [{"cell": s["cell"], "result": s["result"],
                                             "hint": s["hint"], "ship": s["ship"]} for s in fired],
                                  "points": sum(s["points"] for s in fired)})
    if game.over and game.end_turn is None:
        game.end_turn = turn
    return record, carry


def simulate(seed, finished_target=3, current_min_shots=26):
    rng = random.Random(seed)
    start = datetime(2026, 10, 12, 9, 0, tzinfo=ET)
    games = [Game(1, rng, 1)]
    last_counted = {}
    turns = []
    carry = []
    turn = 0
    while turn < 400:
        turn += 1
        game = games[-1]
        if game.over:
            game = Game(len(games) + 1, rng, turn)
            games.append(game)
        posted = [p for p in PLAYERS if (turn - p[3]) % p[2] == 0 and rng.random() < 0.85]
        rng.shuffle(posted)
        fresh = [{"by": p[0], "cells": choose_cells(game, p[1], rng), "carried": False} for p in posted]
        record = {"turn": turn, "time": (start + timedelta(hours=turn - 1)).isoformat(), "game": game.id}
        resolved, carry = resolve_turn(game, carry + fresh, turn, last_counted)
        record.update(resolved)
        turns.append(record)
        finished = [g for g in games if g.over]
        if len(finished) >= finished_target and not games[-1].over and len(games[-1].shots) >= current_min_shots:
            break
    return games, turns


# ---------------------------------------------------------------- summaries
def game_summary(game):
    if not game.shots:
        return {"first_blood": None, "admiral": None, "sharpshooter": None, "medal": None, "players": []}
    shots = pd.DataFrame(game.shots).assign(hit=lambda d: d["result"] != "miss")
    first_blood = shots.loc[shots["hit"], "by"].iloc[0] if shots["hit"].any() else None
    per = shots.groupby("by").agg(shots=("hit", "size"), hits=("hit", "sum"))
    eligible = per[per["shots"] >= 3].assign(rate=lambda d: d["hits"] / d["shots"])
    eligible = eligible.sort_values(["rate", "hits"], ascending=False)
    medal = None
    if game.over:
        medal = next((m for m, cap in MEDALS if len(game.shots) <= cap), None)
    return {
        "first_blood": first_blood,
        "admiral": game.shots[-1]["by"] if game.over else None,
        "sharpshooter": eligible.index[0] if game.over and len(eligible) else None,
        "medal": medal,
        "players": sorted(shots["by"].unique().tolist()),
    }


def leaderboards(games):
    rows = [{"game": g.id, "finished": g.over, "player": s["by"], "points": s["points"],
             "hit": s["result"] != "miss"} for g in games for s in g.shots]
    if not rows:
        return pd.DataFrame(columns=["player", "points", "shots", "hits", "games", "fin_points", "finished_games",
                                     "avg", "hit_rate"])
    df = pd.DataFrame(rows)
    per_game = df.groupby(["player", "game", "finished"], as_index=False).agg(
        points=("points", "sum"), shots=("hit", "size"), hits=("hit", "sum"))
    board = per_game.groupby("player", as_index=False).agg(
        points=("points", "sum"), shots=("shots", "sum"), hits=("hits", "sum"), games=("game", "nunique"))
    fin = per_game[per_game["finished"]].groupby("player", as_index=False).agg(
        fin_points=("points", "sum"), finished_games=("game", "nunique"))
    board = board.merge(fin, on="player", how="left").fillna({"fin_points": 0, "finished_games": 0})
    board["finished_games"] = board["finished_games"].astype(int)
    board["avg"] = (board["fin_points"] / board["finished_games"]).where(board["finished_games"] > 0)
    board["hit_rate"] = board["hits"] / board["shots"]
    return board


def board_lines(game):
    grid = [[OPEN] * SIZE for _ in range(SIZE)]
    sunk_cells = {x for s in game.sunk for x in s["cells"]}
    for s in game.shots:
        r, c = parse_cell(s["cell"])
        if s["result"] == "miss":
            grid[r][c] = MISS_ICON[s["hint"]]
        else:
            grid[r][c] = SUNK if s["cell"] in sunk_cells else HIT
    return ["   " + "".join(KEYCAPS)] + [f"{ROWS[r]}  " + "".join(grid[r]) for r in range(SIZE)]


def fmt_time(iso):
    t = datetime.fromisoformat(iso)
    return t.strftime("%a %b %d · %I:%M %p ET").replace(" 0", " ")


def render_turn_post(game, record, board, next_game=None, final=False, audits=None):
    """The TURN reply. When this turn sank the fleet, it also reveals the layout and, given next_game,
    opens the next game with its commitment. final=True is the season's last post: the unfinished game
    stops and its layout is revealed, so its commitment can be checked like every other game's. audits is the
    house's line of accepted independent audits, if any."""
    sunk_cells = {x for s in game.sunk for x in s["cells"]}
    if final:
        out = [f"GHOST FLEET · SEASON OVER · GAME {game.id} · {fmt_time(record['time'])}", "THIS TURN",
               f"The season has ended. Game {game.id} stops here, unfinished."]
    else:
        out = [f"GHOST FLEET · TURN {record['turn']} · GAME {game.id} · {fmt_time(record['time'])}", "THIS TURN"]
    for v in record["volleys"]:
        parts = []
        for s in v["shots"]:
            if s["result"] == "miss":
                parts.append(f"{s['cell']} MISS {s['hint'].upper()}")
            elif s["result"] == "sunk":
                parts.append(f"{s['cell']} SUNK {s['ship']}")
            else:
                parts.append(f"{s['cell']} HIT")
        out.append(f"{v['by']} (+{v['points']}): " + " · ".join(parts))
    if record["skipped"]:
        out.append("SKIPPED (cooldown): " + ", ".join(record["skipped"]))
    if record["refunded"]:
        out.append("REFUNDED: " + ", ".join(record["refunded"]))
    if not (final or record["volleys"] or record["skipped"] or record["refunded"] or record.get("carried")):
        out.append("No volleys this turn.")
    if record.get("carried"):
        out.append("CARRIED to next turn: " + ", ".join(record["carried"]))
    out.append("")
    out += board_lines(game)
    out.append("LEGEND: 💥 hit · 💀 sunk · 🔴 miss HOT · 🟡 miss WARM · 🔵 miss COLD · 🌊 open")
    out.append("")
    live = [s["cell"] for s in game.shots if s["result"] != "miss" and s["cell"] not in sunk_cells]
    out.append("HITS: " + (" ".join(live) or "none"))
    out.append("SUNK: " + (" · ".join(f"{s['ship'].upper()} {' '.join(s['cells'])}" for s in game.sunk) or "none"))
    for band in ["hot", "warm", "cold"]:
        cells = [s["cell"] for s in game.shots if s["hint"] == band]
        out.append(f"MISS {band.upper()}: " + (" ".join(cells) or "none"))
    out.append("INTEL: " + (" · ".join(f"{i['line']} holds {i['count']} ship cell{'' if i['count'] == 1 else 's'}"
                                         for i in game.intel) or "none yet"))
    sunk_names = {s["ship"] for s in game.sunk}
    out.append("AFLOAT: " + (" ".join(f"{n}({l})" for n, l in FLEET if n not in sunk_names) or "none"))
    found = sum(1 for s in game.shots if s["result"] != "miss")
    out.append(f"SWARM: {len(game.shots)} shots · {found}/{SHIP_CELLS} ship cells found · "
               f"Gold ≤45 · Silver ≤60 · Bronze ≤75 (random ≈{round(RANDOM_BASELINE)})")
    if game.shots:
        gp = pd.DataFrame(game.shots).groupby("by")["points"].sum().sort_values(ascending=False).head(5)
        out.append("GAME: " + " · ".join(f"{p} {int(v)}" for p, v in gp.items()))
    else:
        out.append("GAME: no shots yet")
    top_total = board.sort_values(["points", "hit_rate"], ascending=False).head(3)
    out.append("SEASON TOTAL: " + (" · ".join(f"{i + 1} {r.player} {int(r.points)}"
                                              for i, r in enumerate(top_total.itertuples())) or "none yet"))
    top_avg = board[board["finished_games"] >= MIN_GAMES_FOR_AVG].sort_values(["avg", "hit_rate"], ascending=False).head(3)
    out.append("SEASON AVG/GAME: " + (" · ".join(f"{i + 1} {r.player} {r.avg:.1f}"
                                                 for i, r in enumerate(top_avg.itertuples())) or "none yet"))
    out.append(f"COMMIT G{game.id}: {game.commit}")
    if audits:
        out.append(f"AUDITS: {audits}")
    if game.over:
        s = game_summary(game)
        medal = f"{s['medal']} medal" if s["medal"] else "no medal"
        out.append(f"GAME {game.id} OVER in {len(game.shots)} shots · {medal} · First Blood {s['first_blood']} · "
                   f"Sharpshooter {s['sharpshooter'] or 'none'} · Admiral {s['admiral']}")
        out.append(f"REVEAL G{game.id}: {game.reveal}")
    elif final:
        out.append(f"REVEAL G{game.id}: {game.reveal}")
    if next_game is not None:
        out.append(f"NEW GAME {next_game.id} · COMMIT G{next_game.id}: {next_game.commit}")
    if final:
        out.append("Every layout is now public: hash each REVEAL line and compare it with its COMMIT. "
                   "Thanks for playing.")
        return "\n".join(out)
    out.append("NEXT: reply to the offer with FIRE + up to 4 cells. Your first 2 open cells fire. One volley per 2 hours.")
    return "\n".join(out)


def sonar_mix(trials=3000, seed=1):
    rng = random.Random(seed)
    counts = {"hot": 0, "warm": 0, "cold": 0}
    for _ in range(trials):
        owner = {c for cells in place_fleet(rng).values() for c in cells}
        for r in range(SIZE):
            for c in range(SIZE):
                if (r, c) not in owner:
                    counts[sonar((r, c), owner)] += 1
    total = sum(counts.values())
    return {k: round(v / total, 3) for k, v in counts.items()}


def export(games, turns, demo=True, ends="2026-10-14T20:00:00-04:00", status="RUNNING"):
    board = leaderboards(games)
    live = [g for g in games if g.end_turn is None]
    current = live[-1] if live else None
    summaries = {g.id: game_summary(g) for g in games}
    medal_count = {}
    title_count = {}
    for g in games:
        s = summaries[g.id]
        if s["medal"]:
            for p in s["players"]:
                medal_count[p] = medal_count.get(p, 0) + 1
        for key in ["first_blood", "admiral", "sharpshooter"]:
            if g.over and s[key]:
                title_count[s[key]] = title_count.get(s[key], 0) + 1
    players = []
    for r in board.itertuples():
        players.append({"name": r.player, "points": int(r.points), "shots": int(r.shots), "hits": int(r.hits),
                        "games": int(r.games), "finished_games": int(r.finished_games),
                        "avg": None if pd.isna(r.avg) else round(float(r.avg), 2),
                        "medals": medal_count.get(r.player, 0), "titles": title_count.get(r.player, 0)})
    history = []
    for g in games:
        if g.end_turn is None:          # still live: its layout stays secret
            continue
        s = summaries[g.id]
        closed = {} if g.over else {"closed": True}   # stopped unfinished at season end
        history.append({**closed, "game": g.id, "commit": g.commit, "reveal": g.reveal, "start_turn": g.start_turn,
                        "end_turn": g.end_turn, "total_shots": len(g.shots), "medal": s["medal"],
                        "first_blood": s["first_blood"], "admiral": s["admiral"],
                        "sharpshooter": s["sharpshooter"], "shots": g.shots, "intel": g.intel})
    state = {
        "demo": demo,
        "updated": turns[-1]["time"],
        "season": {
            "name": "Season 1", "ends": ends, "status": status, "turn": turns[-1]["turn"],
            "rules": {"size": SIZE, "fleet": [{"ship": n, "len": l} for n, l in FLEET],
                      "medals": [{"medal": m, "max_shots": cap} for m, cap in MEDALS],
                      "random_baseline": RANDOM_BASELINE, "cooldown_turns": COOLDOWN_TURNS,
                      "shots_per_volley": SHOTS_PER_VOLLEY, "max_listed": MAX_CELLS_LISTED,
                      "intel_every": INTEL_EVERY, "min_games_for_avg": MIN_GAMES_FOR_AVG, "points": POINTS},
        },
        "current": None if current is None else {
            "game": current.id, "commit": current.commit, "start_turn": current.start_turn,
            "shots": current.shots, "sunk": current.sunk, "intel": current.intel,
            "first_blood": summaries[current.id]["first_blood"]},
        "history": history,
        "turns": turns,
        "players": players,
    }
    return state, board


if __name__ == "__main__":
    games, turns = simulate(seed=11)
    state, board = export(games, turns)
    with open("state.json", "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    post = render_turn_post(games[-1], turns[-1], board)
    with open("turn_post.txt", "w") as f:
        f.write(post + "\n")
    print(post)
    print()
    for h in state["history"]:
        print(f"G{h['game']}: {h['total_shots']} shots, turns {h['start_turn']}-{h['end_turn']}, medal {h['medal']}, "
              f"admiral {h['admiral']}, intel {len(h['intel'])}")
    print("current game shots:", len(games[-1].shots), "turns so far:", turns[-1]["turn"])
    print("random baseline:", RANDOM_BASELINE)
    print("sonar mix on random misses:", sonar_mix())
