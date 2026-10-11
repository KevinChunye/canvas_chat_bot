"""Fixed Canvas settings and secrets. Secrets are only ever read from the environment."""

import os

CANVAS_HOST = "canvas.mit.edu"
CONTROL_LINE_RUNNING = "COURSE-TEAM CONTROL: RUNNING"


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
