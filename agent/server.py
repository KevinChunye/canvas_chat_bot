"""Tiny HTTP shim for maritime.sh's custom-container contract (stdlib only).

    GET  /health     -> 200
    GET  /schedules  -> the cron schedule Maritime registers as a wake trigger
    POST /chat       -> a scheduled message {"source": "scheduled", "message": "run-cycle"} starts one
                        cycle in a background thread and replies at once (Maritime allows 30s)

Any other chat message gets a canned reply and changes nothing: chat text can never pick
an action, a topic or a command. The cycle's own file lock prevents overlapping runs.
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import load_config, secret_values
from .cycle import run_cycle
from .text import scrub

SCHEDULE_PROMPT = "run-cycle"
SCHEDULES = [{"id": "footnote-cycle", "cron": "0 */3 * * *", "tz": "UTC", "prompt": SCHEDULE_PROMPT,
              "enabled": True}]

_running = threading.Lock()


def _cycle_in_background() -> bool:
    if not _running.acquire(blocking=False):
        return False

    def work():
        try:
            run_cycle(load_config())
        except Exception as e:  # run_cycle records its own failures; this catches setup errors
            print(scrub(f"cycle crashed: {type(e).__name__}: {e}", secret_values()), file=sys.stderr)
        finally:
            _running.release()

    threading.Thread(target=work, daemon=True).start()
    return True


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True})
        elif self.path == "/schedules":
            self._send(200, SCHEDULES)
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/chat":
            self._send(404, {"error": "not found"})
            return
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 65536)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            payload = {}
        message = str(payload.get("message", "")).strip() if isinstance(payload, dict) else ""
        source = str(payload.get("source", "")) if isinstance(payload, dict) else ""
        if source == "scheduled" and message == SCHEDULE_PROMPT:
            started = _cycle_in_background()
            self._send(200, {"response": "cycle started" if started else "a cycle is already running"})
        else:
            self._send(200, {"response": "Footnote runs on its schedule only; chat messages do nothing."})

    def log_message(self, fmt, *args):  # keep request lines out of stderr noise
        pass


def main() -> None:
    port = int(os.environ.get("PORT", "18789"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
