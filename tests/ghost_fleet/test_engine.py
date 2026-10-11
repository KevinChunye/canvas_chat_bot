"""Ghost Fleet house logic: every promise the offer post makes holds in the engine."""

import hashlib
import random
import re
from pathlib import Path

import pytest

from agents.ghost_fleet.engine import (COOLDOWN_TURNS, FLEET, INTEL_EVERY, MAX_CELLS_LISTED, MEDALS, POINTS,
                                       RANDOM_BASELINE, ROWS, SHIP_CELLS, SHOTS_PER_VOLLEY, SIZE, Game, export,
                                       parse_cell, place_fleet, render_turn_post, simulate, sonar)
from canvas_core.text import SELF_IDENTIFICATION

OFFER = (Path(__file__).resolve().parents[2] / "agents" / "ghost_fleet" / "offer.txt").read_text()


def ships_from_reveal(reveal: str) -> dict[str, list[tuple[int, int]]]:
    """Parse a revealed layout string the way a player checking it would."""
    return {name.title(): [parse_cell(c) for c in cells.split(",")]
            for name, cells in (part.split(":") for part in reveal.split("|")[2:-1])}


@pytest.fixture(scope="module")
def season():
    games, turns = simulate(seed=11)
    state, board = export(games, turns)
    return games, turns, state, board


# --------------------------------------------------------------------------- layout and commitment

def test_fleet_is_straight_inside_the_board_and_never_overlaps():
    for seed in range(300):
        fleet = place_fleet(random.Random(seed))
        cells = [c for ship in fleet.values() for c in ship]
        assert len(cells) == len(set(cells)) == SHIP_CELLS
        for name, length in FLEET:
            ship = fleet[name]
            assert len(ship) == length and all(0 <= r < SIZE and 0 <= c < SIZE for r, c in ship)
            rows, cols = {r for r, _ in ship}, {c for _, c in ship}
            span = cols if len(rows) == 1 else rows
            assert (len(rows) == 1 or len(cols) == 1) and max(span) - min(span) == length - 1


def test_commitment_is_the_sha256_of_the_revealed_layout():
    game = Game(4, random.Random(7), 1)
    assert game.commit == hashlib.sha256(game.reveal.encode()).hexdigest()
    parts = game.reveal.split("|")
    assert parts[:2] == ["GHOSTFLEET", "G4"] and re.fullmatch(r"SALT:\w{12}", parts[-1])
    assert ships_from_reveal(game.reveal) == {name: sorted(cells) for name, cells in game.fleet.items()}


# --------------------------------------------------------------------------- sonar, scoring, intel

@pytest.mark.parametrize("cell, band", [
    ("E6", "hot"), ("F6", "hot"), ("D4", "hot"),        # touching, diagonals included
    ("E7", "warm"), ("G7", "warm"), ("C3", "warm"),     # nearest ship cell 2 away
    ("E8", "cold"), ("A1", "cold"),                     # nothing within 2
])
def test_sonar_bands(cell, band):
    assert sonar(parse_cell(cell), {parse_cell("E5")}) == band


def test_sonar_counts_sunk_ships_so_a_reading_never_changes():
    game = Game(1, random.Random(2), 1)
    destroyer = sorted(game.fleet["Destroyer"])
    near = [(r, c) for r in range(SIZE) for c in range(SIZE)
            if (r, c) not in game.owner and max(abs(r - destroyer[0][0]), abs(c - destroyer[0][1])) == 1]
    before = game.fire(near[0], "p", 1)
    for cell in destroyer:
        game.fire(cell, "p", 1)
    after = game.fire(near[1], "p", 1)
    assert "Destroyer" in [s["ship"] for s in game.sunk]
    assert before["hint"] == after["hint"] == "hot"


def test_points_for_hit_sink_and_last_sink():
    game = Game(1, random.Random(3), 1)
    shots = [game.fire(cell, "p", 1) for name, _ in FLEET for cell in sorted(game.fleet[name])]
    assert game.over
    assert [s["points"] for s in shots].count(POINTS["sunk"]) == len(FLEET) - 1
    assert shots[-1]["result"] == "sunk" and shots[-1]["points"] == POINTS["last"]
    expected = (SHIP_CELLS - len(FLEET)) * POINTS["hit"] + (len(FLEET) - 1) * POINTS["sunk"] + POINTS["last"]
    assert sum(s["points"] for s in shots) == expected


def test_intel_after_every_eighth_miss_counts_one_line_and_never_repeats_it():
    game = Game(1, random.Random(5), 1)
    misses = [(r, c) for r in range(SIZE) for c in range(SIZE) if (r, c) not in game.owner][:2 * INTEL_EVERY]
    for cell in misses:
        game.fire(cell, "p", 1)
    assert [i["after_miss"] for i in game.intel] == [INTEL_EVERY, 2 * INTEL_EVERY]
    assert len({i["line"] for i in game.intel}) == 2
    for i in game.intel:
        kind, label = i["line"].split(" ")
        axis, idx = (0, ROWS.index(label)) if kind == "Row" else (1, int(label) - 1)
        assert i["count"] == sum(1 for cell in game.owner if cell[axis] == idx)


# --------------------------------------------------------------------------- a whole season

def test_demo_season_reproduces_the_published_turn_26(season):
    games, turns, _, board = season
    post = render_turn_post(games[-1], turns[-1], board)
    assert post.splitlines()[0] == "GHOST FLEET · TURN 26 · GAME 4 · Tue Oct 13 · 10:00 AM ET"
    assert "Scylla (+1): D8 MISS WARM · B6 HIT" in post
    assert "COMMIT G4: 3bbcbd9250625a17a707c5e324759fb15f8e07441ddacdf3c642de2f7bf2c0a7" in post


def test_every_finished_game_checks_out_against_its_reveal(season):
    _, _, state, _ = season
    assert len(state["history"]) == 3
    for game in state["history"]:
        assert hashlib.sha256(game["reveal"].encode()).hexdigest() == game["commit"]
        owner = {cell for cells in ships_from_reveal(game["reveal"]).values() for cell in cells}
        for shot in game["shots"]:
            cell = parse_cell(shot["cell"])
            assert (shot["result"] != "miss") == (cell in owner)
            if shot["result"] == "miss":
                assert shot["hint"] == sonar(cell, owner)
        assert game["shots"][-1]["by"] == game["admiral"]


def test_volleys_fire_the_first_open_cells_and_respect_the_cooldown(season):
    _, turns, _, _ = season
    fired: dict[int, set] = {}
    last_volley: dict[str, int] = {}
    for turn in turns:
        taken = fired.setdefault(turn["game"], set())
        for volley in turn["volleys"]:
            assert len(volley["listed"]) <= MAX_CELLS_LISTED
            expected = [c for c in volley["listed"] if c not in taken][:SHOTS_PER_VOLLEY]
            assert [s["cell"] for s in volley["shots"]] == expected
            taken.update(expected)
            if volley["by"] in last_volley:
                assert turn["turn"] - last_volley[volley["by"]] >= COOLDOWN_TURNS
            last_volley[volley["by"]] = turn["turn"]


# --------------------------------------------------------------------------- what gets posted

def test_turn_posts_and_the_offer_never_say_what_wrote_them(season):
    games, turns, _, board = season
    assert not SELF_IDENTIFICATION.search(render_turn_post(games[-1], turns[-1], board))
    assert not SELF_IDENTIFICATION.search(OFFER)


def test_offer_numbers_match_the_engine():
    assert f"FIRE + up to {MAX_CELLS_LISTED} cells" in OFFER
    assert f"I fire your first {SHOTS_PER_VOLLEY} cells that are still open" in OFFER
    assert f"One volley per player every {COOLDOWN_TURNS} hours" in OFFER
    assert f"After every {INTEL_EVERY}th miss" in OFFER
    assert ", ".join(f"{n} {length}" for n, length in FLEET) in " ".join(OFFER.split())
    assert f"Hit {POINTS['hit']} · hit that sinks a ship {POINTS['sunk']} · hit that sinks the last ship " \
           f"{POINTS['last']}" in OFFER
    (_, gold), (_, silver), (_, bronze) = MEDALS
    assert f"Gold if sunk in {gold} shots or fewer, Silver {silver}," in OFFER and f"Bronze {bronze}." in OFFER
    assert f"Random firing needs about {round(RANDOM_BASELINE)}" in OFFER
