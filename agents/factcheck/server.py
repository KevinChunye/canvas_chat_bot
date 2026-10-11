"""Maritime entry point for the fact-check agent (see canvas_core/server.py for the contract).

A scheduled message {"source": "scheduled", "message": "run-cycle"} starts one cycle; any other chat message
gets a canned reply and changes nothing. The cycle's own file lock prevents overlapping runs.
"""

import sys

from canvas_core.env import secret_values
from canvas_core.server import background, make_handler, serve
from canvas_core.text import scrub

from .config import load_config
from .cycle import run_cycle

SCHEDULE_PROMPT = "run-cycle"
SCHEDULES = [{"id": "factcheck-cycle", "cron": "0 */3 * * *", "tz": "UTC", "prompt": SCHEDULE_PROMPT,
              "enabled": True}]


def _cycle() -> None:
    try:
        run_cycle(load_config())
    except Exception as e:  # run_cycle records its own failures; this catches setup errors
        print(scrub(f"cycle crashed: {type(e).__name__}: {e}", secret_values()), file=sys.stderr)


Handler = make_handler(background(_cycle), SCHEDULE_PROMPT, SCHEDULES,
                       "This agent runs on its schedule only; chat messages do nothing.", noun="cycle")


def main() -> None:
    serve(Handler)


if __name__ == "__main__":
    main()
