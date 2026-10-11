"""Maritime entry point for Ghost Fleet (see canvas_core/server.py for the contract).

A scheduled message {"source": "scheduled", "message": "run-turn"} every 10 minutes runs the turn logic: the first
run in an hour with room under the write limit posts that hour's turn, and the others only read and handle audits.
Any other chat message gets a canned reply and changes nothing. GET / serves the spectator page and GET /state.json
its data, both read-only.
"""

import sys

from canvas_core.env import secret_values
from canvas_core.server import background, make_handler, serve
from canvas_core.text import scrub

from .config import SPECTATOR_PATH, load_config
from .runner import run_turn

SCHEDULE_PROMPT = "run-turn"
SCHEDULES = [{"id": "ghost-fleet-turn", "cron": "*/10 * * * *", "tz": "UTC", "prompt": SCHEDULE_PROMPT,
              "enabled": True}]


def _turn() -> None:
    try:
        run_turn(load_config())
    except Exception as e:  # run_turn records its own failures; this catches setup errors
        print(scrub(f"turn crashed: {type(e).__name__}: {e}", secret_values()), file=sys.stderr)


def _spectator():
    return "text/html; charset=utf-8", SPECTATOR_PATH.read_bytes()


def _state():
    path = load_config().state_json_path
    return ("application/json", path.read_bytes()) if path.exists() else None


Handler = make_handler(background(_turn), SCHEDULE_PROMPT, SCHEDULES,
                       "Ghost Fleet runs on its schedule only; chat messages do nothing.", noun="turn",
                       pages={"/": _spectator, "/spectator.html": _spectator, "/state.json": _state})


def main() -> None:
    serve(Handler)


if __name__ == "__main__":
    main()
