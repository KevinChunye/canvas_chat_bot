"""Configuration: this agent's committed config.toml for settings, environment for secrets."""

import os
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from canvas_core.env import CANVAS_HOST

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.toml"
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

    pending_max_age_hours: float
    max_post_attempts: int

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


def load_config(path: Path | str | None = None) -> Config:
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    canvas = raw.get("canvas", {})
    game = raw.get("game", {})
    limits = raw.get("limits", {})
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
        pending_max_age_hours=float(limits.get("pending_max_age_hours", 2)),
        max_post_attempts=int(limits.get("max_post_attempts", 3)),
        data_dir=data_dir,
        config_path=path,
    )
