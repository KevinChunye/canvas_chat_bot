"""Tiny HTTP shim for maritime.sh's custom-container contract (stdlib only), shared by every agent.

    GET  /health     -> 200
    GET  /schedules  -> the cron schedules Maritime registers as wake triggers
    POST /chat       -> a scheduled message {"source": "scheduled", "message": <prompt>} starts one run in a
                        background thread and replies at once (Maritime allows 30s)

Any other chat message gets a canned reply and changes nothing: chat text can never pick an action, a topic or
a command. An agent may also publish read-only pages at fixed paths.
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable


def background(run: Callable[[], None]) -> Callable[[], bool]:
    """A starter that runs `run` in a daemon thread, one at a time. It returns False while a run is going."""
    running = threading.Lock()

    def start() -> bool:
        if not running.acquire(blocking=False):
            return False

        def work():
            try:
                run()
            finally:
                running.release()

        threading.Thread(target=work, daemon=True).start()
        return True

    return start


def make_handler(start: Callable[[], bool], prompt: str, schedules: list, idle_reply: str, noun: str = "run",
                 pages: dict[str, Callable[[], tuple[str, bytes] | None]] | None = None):
    """pages: path -> callable returning (content type, body), or None for 404."""
    pages = pages or {}

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, payload) -> None:
            self._raw(status, "application/json", json.dumps(payload).encode("utf-8"))

        def _raw(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/health":
                self._send(200, {"ok": True})
            elif path == "/schedules":
                self._send(200, schedules)
            elif path in pages and (page := pages[path]()) is not None:
                self._raw(200, *page)
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
            if source == "scheduled" and message == prompt:
                started = start()
                self._send(200, {"response": f"{noun} started" if started else f"a {noun} is already running"})
            else:
                self._send(200, {"response": idle_reply})

        def log_message(self, fmt, *args):  # keep request lines out of stderr noise
            pass

    return Handler


def serve(handler) -> None:
    port = int(os.environ.get("PORT", "18789"))
    ThreadingHTTPServer(("0.0.0.0", port), handler).serve_forever()
