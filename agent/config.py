"""Configuration: committed config.toml for settings, environment for secrets.

Secrets (CANVAS_API_KEY, OPENAI_API_KEY) are only ever read from the
environment and are never stored on the Config object's repr.
"""

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.toml"

# Hard ceilings that config can lower but never raise.
LIFETIME_CAP_USD = 5.00
CONTROL_LINE_RUNNING = "COURSE-TEAM CONTROL: RUNNING"
CANVAS_HOST = "canvas.mit.edu"
OPENAI_BASE_URL = "https://api.openai.com/v1"


@dataclass
class Config:
    canvas_base_url: str
    course_id: int | None
    forum_topic_id: int | None
    canvas_timeout: float

    agent_name: str

    openai_model: str
    prices: dict  # model -> {"input_usd_per_mtok": float, "output_usd_per_mtok": float}
    max_output_tokens: int
    reasoning_effort: str
    openai_timeout: float
    carryover_spend_usd: float

    lifetime_cap_usd: float
    cycle_cap_usd: float
    day_cap_usd: float

    max_posts_per_cycle: int
    max_posts_per_hour: int
    max_new_threads_per_day: int
    max_exchanges_per_author: int
    wrong_confidence_threshold: float
    similarity_threshold: float
    min_words: int
    max_words: int
    max_candidates: int
    entry_char_limit: int
    pending_max_age_hours: float
    max_post_attempts: int

    data_dir: Path
    config_path: Path = field(default=DEFAULT_CONFIG_PATH)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "agent.db"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "cycle.lock"


def load_config(path: Path | str | None = None) -> Config:
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    canvas = raw.get("canvas", {})
    agent = raw.get("agent", {})
    openai_cfg = raw.get("openai", {})
    budget = raw.get("budget", {})
    limits = raw.get("limits", {})
    paths = raw.get("paths", {})

    data_dir = os.environ.get("AGENT_DATA_DIR") or paths.get("data_dir", "state")
    data_dir = Path(data_dir).expanduser()
    if not data_dir.is_absolute():
        data_dir = REPO_ROOT / data_dir

    return Config(
        canvas_base_url=canvas.get("base_url", f"https://{CANVAS_HOST}").rstrip("/"),
        course_id=canvas.get("course_id") or None,
        forum_topic_id=canvas.get("forum_topic_id") or None,
        canvas_timeout=float(canvas.get("timeout_seconds", 20)),
        agent_name=agent.get("name", "Footnote"),
        openai_model=os.environ.get("OPENAI_MODEL") or openai_cfg.get("model", "gpt-6-luna"),
        prices=openai_cfg.get("prices", {}),
        max_output_tokens=int(openai_cfg.get("max_output_tokens", 3000)),
        reasoning_effort=openai_cfg.get("reasoning_effort", "low"),
        openai_timeout=float(openai_cfg.get("timeout_seconds", 90)),
        carryover_spend_usd=float(budget.get("carryover_spend_usd", 0.0)),
        lifetime_cap_usd=min(float(budget.get("lifetime_cap_usd", LIFETIME_CAP_USD)), LIFETIME_CAP_USD),
        cycle_cap_usd=float(budget.get("cycle_cap_usd", 0.05)),
        day_cap_usd=float(budget.get("day_cap_usd", 0.50)),
        max_posts_per_cycle=min(int(limits.get("max_posts_per_cycle", 2)), 2),
        max_posts_per_hour=min(int(limits.get("max_posts_per_hour", 3)), 3),
        max_new_threads_per_day=min(int(limits.get("max_new_threads_per_day", 1)), 1),
        max_exchanges_per_author=min(int(limits.get("max_exchanges_per_author", 2)), 2),
        wrong_confidence_threshold=float(limits.get("wrong_confidence_threshold", 0.85)),
        similarity_threshold=float(limits.get("similarity_threshold", 0.6)),
        min_words=int(limits.get("min_words", 40)),
        max_words=int(limits.get("max_words", 160)),
        max_candidates=int(limits.get("max_candidates", 12)),
        entry_char_limit=int(limits.get("entry_char_limit", 1200)),
        pending_max_age_hours=float(limits.get("pending_max_age_hours", 24)),
        max_post_attempts=int(limits.get("max_post_attempts", 3)),
        data_dir=data_dir,
        config_path=path,
    )


def canvas_token() -> str:
    token = os.environ.get("CANVAS_API_KEY", "")
    if not token:
        raise RuntimeError("CANVAS_API_KEY is not set")
    return token


def openai_key() -> str:
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    return key


def secret_values() -> list[str]:
    """Current secret values, for scrubbing logs and filtering output."""
    return [v for v in (os.environ.get("CANVAS_API_KEY"), os.environ.get("OPENAI_API_KEY")) if v]
