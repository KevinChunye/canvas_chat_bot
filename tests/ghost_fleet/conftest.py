"""Ghost Fleet fixtures: a temp config with a season under way, a clock the test moves, and a posted offer."""

from datetime import datetime, timedelta, timezone

import pytest

from agents.ghost_fleet.config import load_config
from agents.ghost_fleet.runner import run_turn
from agents.ghost_fleet.store import Store
from tests.conftest import COURSE, SELF_ID, TOPIC, no_sleep

ET = timezone(timedelta(hours=-4))


class Clock:
    def __init__(self, at: datetime):
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def advance(self, hours: float = 1) -> None:
        self.at += timedelta(hours=hours)


@pytest.fixture
def season_start():
    return datetime.now(ET).replace(minute=0, second=0, microsecond=0) - timedelta(hours=5)


@pytest.fixture
def cfg(tmp_path, season_start):
    path = tmp_path / "config.toml"
    path.write_text(f"""
[canvas]
base_url = "https://canvas.mit.edu"
course_id = {COURSE}
forum_topic_id = {TOPIC}

[game]
season_start = "{season_start.isoformat()}"
season_end = "{(season_start + timedelta(days=3)).isoformat()}"

[paths]
data_dir = "{(tmp_path / 'data').as_posix()}"
""")
    return load_config(path)


@pytest.fixture
def clock(season_start):
    return Clock(season_start + timedelta(minutes=1))          # turn 1


@pytest.fixture
def store(cfg, clock):
    s = Store(cfg.db_path, clock=clock)
    yield s
    s.close()


@pytest.fixture
def offer(canvas, store):
    entry_id = canvas.add(SELF_ID, "SERVICE OFFER: Ghost Fleet", minutes_ago=600)
    store.set_state("offer_entry_id", str(entry_id))
    return entry_id


def turn(cfg, store, dry_run=False):
    return run_turn(cfg, dry_run=dry_run, store=store, sleep=no_sleep, echo=False)


def fire(canvas, offer, user_id, text):
    return canvas.add(user_id, text, parent_id=offer, minutes_ago=0)
