"""Configuration: this agent's committed config.toml for settings, environment for secrets."""

import os
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from canvas_core.env import CANVAS_HOST

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.toml"

# The course forum allows at most three write attempts per rolling hour per student, across all of the course's
# discussions, retries included. These are ceilings that config can lower but never raise; the defaults leave
# room for the student's own posts elsewhere in the course.
MAX_WRITES_PER_HOUR = 3
MAX_WRITES_PER_DAY = 48
DEFAULT_WRITES_PER_HOUR = 2
DEFAULT_WRITES_PER_DAY = 36
OFFER_PATH = Path(__file__).resolve().parent / "offer.txt"
SPECTATOR_PATH = Path(__file__).resolve().parent / "spectator.html"


@dataclass
class Config:
    canvas_base_url: str
    course_id: int | None
    forum_topic_id: int | None
    canvas_timeout: float

    offer_entry_id: int | None
    season_start: datetime
    season_end: datetime

    max_writes_per_hour: int
    max_writes_per_day: int
    pending_max_age_hours: float
    max_post_attempts: int

    hire_enabled: bool
    hire_deadline_hours: float
    hire_max_providers: int
    hire_min_score: int

    data_dir: Path
    config_path: Path = DEFAULT_CONFIG_PATH

    @property
    def db_path(self) -> Path:
        return self.data_dir / "ghost_fleet.db"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "turn.lock"

    @property
    def state_json_path(self) -> Path:
        """What the spectator page reads."""
        return self.data_dir / "state.json"


def _when(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f"{value!r} needs a UTC offset, e.g. -04:00")
    return dt


def _capped(section: dict, key: str, old_key: str, default: int, ceiling: int) -> int:
    """A limit from config (or its older name), never above the ceiling."""
    return min(int(section.get(key, section.get(old_key, default))), ceiling)


def load_config(path: Path | str | None = None) -> Config:
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    canvas = raw.get("canvas", {})
    game = raw.get("game", {})
    limits = raw.get("limits", {})
    hire = raw.get("hire", {})
    paths = raw.get("paths", {})

    data_dir = Path(os.environ.get("AGENT_DATA_DIR") or paths.get("data_dir", "state/ghost_fleet")).expanduser()
    if not data_dir.is_absolute():
        data_dir = REPO_ROOT / data_dir

    return Config(
        canvas_base_url=canvas.get("base_url", f"https://{CANVAS_HOST}").rstrip("/"),
        course_id=canvas.get("course_id") or None,
        forum_topic_id=canvas.get("forum_topic_id") or None,
        canvas_timeout=float(canvas.get("timeout_seconds", 20)),
        offer_entry_id=game.get("offer_entry_id") or None,
        season_start=_when(game.get("season_start", "2026-10-12T09:00:00-04:00")),
        season_end=_when(game.get("season_end", "2026-10-14T20:00:00-04:00")),
        max_writes_per_hour=_capped(limits, "max_writes_per_hour", "max_posts_per_hour", DEFAULT_WRITES_PER_HOUR,
                                    MAX_WRITES_PER_HOUR),
        max_writes_per_day=_capped(limits, "max_writes_per_day", "max_posts_per_day", DEFAULT_WRITES_PER_DAY,
                                   MAX_WRITES_PER_DAY),
        pending_max_age_hours=float(limits.get("pending_max_age_hours", 2)),
        max_post_attempts=int(limits.get("max_post_attempts", 3)),
        hire_enabled=bool(hire.get("enabled", True)),
        hire_deadline_hours=float(hire.get("deadline_hours", 6)),
        hire_max_providers=int(hire.get("max_providers_per_game", 2)),
        hire_min_score=int(hire.get("min_score", 2)),
        data_dir=data_dir,
        config_path=path,
    )
