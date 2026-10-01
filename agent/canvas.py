"""Canvas HTTP client.

Safety properties enforced here, not in any prompt:
  * every request goes to the configured Canvas host only (pagination links included);
  * the only write is create_entry, and it refuses any topic other than FORUM_TOPIC_ID;
  * create_entry re-checks the control line immediately before each POST;
  * 401/403 raise CanvasAuthError, which halts the agent.
There is deliberately no edit or delete method.
"""

import random
import time
from typing import Callable
from urllib.parse import urlparse

import requests

from .config import CANVAS_HOST, CONTROL_LINE_RUNNING
from .text import first_nonempty_line, strip_html


class CanvasAuthError(Exception):
    """401/403: token revoked or permissions changed. Halt."""


class CanvasTransientError(Exception):
    """Timeout, connection error, 5xx, 429, throttling. Outcome of a write is unknown."""


class CanvasError(Exception):
    """Other non-success responses."""


class MalformedResponse(CanvasTransientError):
    """A 2xx whose body we could not parse. Outcome of a write is unknown."""


class LostAck(Exception):
    """Injected fault (drop_ack_once): the POST went through but the response was thrown away.

    Deliberately not a CanvasTransientError: it propagates and ends the cycle with the
    intent still pending, like a process that died before reading the reply. The next
    cycle's reconcile has to find the entry."""


class WriteForbidden(Exception):
    """Attempted write outside the allowed topic, or from a read-only client."""


class GateClosed(Exception):
    """The control line does not say RUNNING (or could not be read)."""


def _noop_log(event, **fields):
    pass


class Canvas:
    def __init__(self, base_url: str, token: str, course_id: int | None, forum_topic_id: int | None,
                 timeout: float = 20, session: requests.Session | None = None,
                 sleep: Callable[[float], None] = time.sleep, log=_noop_log,
                 read_only: bool = False, fault: Callable[[], bool] | None = None,
                 get_attempts: int = 3):
        host = urlparse(base_url).hostname
        if host != CANVAS_HOST:
            raise WriteForbidden(f"Canvas host must be {CANVAS_HOST}, got {host}")
        self.base_url = base_url.rstrip("/")
        self.course_id = int(course_id) if course_id is not None else None
        self.forum_topic_id = int(forum_topic_id) if forum_topic_id is not None else None
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"
        self.sleep = sleep
        self.log = log
        self.read_only = read_only
        self.fault = fault
        self.get_attempts = get_attempts

    # ------------------------------------------------------------------ http

    def _url(self, path: str) -> str:
        return path if path.startswith("http") else f"{self.base_url}{path}"

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        if method not in ("GET", "POST"):
            raise WriteForbidden(f"method {method} is not allowed")
        if urlparse(url).hostname != CANVAS_HOST:
            raise WriteForbidden(f"refusing request to non-Canvas host {urlparse(url).hostname}")
        try:
            resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
        except requests.Timeout as e:
            raise CanvasTransientError(f"{method} timeout") from e
        except requests.ConnectionError as e:
            raise CanvasTransientError(f"{method} connection error") from e

        self._respect_rate_limit(resp)
        status = resp.status_code
        if status == 403 and "rate limit exceeded" in resp.text.lower():
            raise CanvasTransientError("403 throttled (Rate Limit Exceeded)")
        if status in (401, 403):
            raise CanvasAuthError(f"{method} {urlparse(url).path} -> {status}")
        if status == 429 or status >= 500:
            raise CanvasTransientError(f"{method} {urlparse(url).path} -> {status}")
        if status >= 400:
            raise CanvasError(f"{method} {urlparse(url).path} -> {status}")
        return resp

    def _respect_rate_limit(self, resp: requests.Response) -> None:
        remaining = resp.headers.get("X-Rate-Limit-Remaining")
        if remaining is None:
            return
        try:
            remaining = float(remaining)
        except ValueError:
            return
        if remaining < 50:
            self.log("rate_limit_wait", remaining=remaining, seconds=20)
            self.sleep(20)
        elif remaining < 200:
            self.log("rate_limit_wait", remaining=remaining, seconds=3)
            self.sleep(3)

    def _get(self, url: str, params=None) -> requests.Response:
        """GET with bounded retries (exponential backoff + jitter) on transient errors."""
        for attempt in range(self.get_attempts):
            try:
                return self._request("GET", url, params=params)
            except CanvasTransientError as e:
                if attempt == self.get_attempts - 1:
                    raise
                delay = 2 ** attempt + random.uniform(0, 1)
                self.log("get_retry", path=urlparse(url).path, attempt=attempt + 1, error=str(e), delay=round(delay, 2))
                self.sleep(delay)
        raise AssertionError("unreachable")

    def get_json(self, path: str, params=None):
        resp = self._get(self._url(path), params=params)
        try:
            return resp.json()
        except ValueError as e:
            raise MalformedResponse(f"GET {path}: invalid JSON") from e

    def get_paginated(self, path: str, params=None) -> list:
        params = dict(params or {})
        params["per_page"] = 100
        url = self._url(path)
        items = []
        while url:
            resp = self._get(url, params=params)
            try:
                page = resp.json()
            except ValueError as e:
                raise MalformedResponse(f"GET {path}: invalid JSON") from e
            if not isinstance(page, list):
                raise MalformedResponse(f"GET {path}: expected a list")
            items.extend(page)
            url = resp.links.get("next", {}).get("url")
            params = None  # the next link already carries the query string
        return items

    # ------------------------------------------------------------------ reads

    def self_profile(self) -> dict:
        return self.get_json("/api/v1/users/self")

    def active_courses(self) -> list:
        return self.get_paginated("/api/v1/courses", {"enrollment_state": "active"})

    def course_topics(self, course_id: int) -> list:
        return self.get_paginated(f"/api/v1/courses/{int(course_id)}/discussion_topics")

    def _topic_path(self, topic_id: int | None = None) -> str:
        topic_id = self.forum_topic_id if topic_id is None else int(topic_id)
        return f"/api/v1/courses/{self.course_id}/discussion_topics/{topic_id}"

    def topic(self, topic_id: int | None = None) -> dict:
        return self.get_json(self._topic_path(topic_id))

    def topic_view(self, topic_id: int | None = None) -> dict:
        return self.get_json(self._topic_path(topic_id) + "/view", {"include_new_entries": 1})

    def top_level_entries(self, topic_id: int | None = None) -> list:
        return self.get_paginated(self._topic_path(topic_id) + "/entries")

    def entry_replies(self, entry_id: int, topic_id: int | None = None) -> list:
        return self.get_paginated(self._topic_path(topic_id) + f"/entries/{int(entry_id)}/replies")

    def entry_list(self, ids: list[int], topic_id: int | None = None) -> list:
        data = self.get_json(self._topic_path(topic_id) + "/entry_list", {"ids[]": [int(i) for i in ids]})
        if not isinstance(data, list):
            raise MalformedResponse("entry_list: expected a list")
        return data

    # ------------------------------------------------------------------ gate

    def control_gate(self) -> tuple[bool, str]:
        """Fail closed: only an exact RUNNING first line opens the gate."""
        try:
            topic = self.topic(self.forum_topic_id)
        except CanvasAuthError:
            raise
        except Exception as e:  # any failed fetch keeps the gate shut
            self.log("gate", open=False, reason=f"fetch failed: {type(e).__name__}")
            return False, "fetch failed"
        line = first_nonempty_line(strip_html(topic.get("message") if isinstance(topic, dict) else None) or "")
        is_open = line == CONTROL_LINE_RUNNING
        self.log("gate", open=is_open, first_line=(line or "")[:80])
        return is_open, line or "missing"

    # ------------------------------------------------------------------ write

    def create_entry(self, topic_id: int, message_html: str, parent_entry_id: int | None = None) -> dict:
        """POST a new top-level entry or a reply. The one and only write in the codebase."""
        if self.read_only:
            raise WriteForbidden("client is read-only (dry run)")
        if self.forum_topic_id is None or self.course_id is None or int(topic_id) != self.forum_topic_id:
            raise WriteForbidden(f"writes are only allowed to topic {self.forum_topic_id}, not {topic_id}")

        is_open, line = self.control_gate()
        if not is_open:
            raise GateClosed(line)

        path = self._topic_path(self.forum_topic_id) + "/entries"
        if parent_entry_id is not None:
            path += f"/{int(parent_entry_id)}/replies"
        resp = self._request("POST", self._url(path), data={"message": message_html})

        if self.fault is not None and self.fault():
            self.log("fault_drop_ack_fired", path=path, status=resp.status_code,
                     note="POST went through; discarding the response to simulate a lost acknowledgement")
            raise LostAck("drop_ack_once: response discarded")

        try:
            data = resp.json()
            int(data["id"])
        except (ValueError, KeyError, TypeError) as e:
            raise MalformedResponse("POST: response missing entry id") from e
        return data
