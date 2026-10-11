"""Shared test doubles: a stateful fake Canvas behind requests-mock.

No test makes a live call. Real secrets in the environment are replaced with
fake values for every test, so nothing real can end up in test output.
Agent-specific fixtures live in each agent's own conftest (tests/<agent>/conftest.py).
"""

import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
import requests
import requests_mock

FAKE_CANVAS_KEY = "7~FAKEcanvasTOKENabcdefghijklmnopqrstuvwxyz0123456789"
FAKE_OPENAI_KEY = "sk-fake-FAKEopenaiKEYabcdefghijklmnopqrstuvwxyz0123456789"
COURSE, TOPIC, OTHER_TOPIC, SELF_ID = 1, 10, 99, 500
RUNNING = "<p>COURSE-TEAM CONTROL: RUNNING</p><p>Agents may post here.</p>"


@pytest.fixture(autouse=True)
def fake_secrets(monkeypatch):
    monkeypatch.setenv("CANVAS_API_KEY", FAKE_CANVAS_KEY)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_OPENAI_KEY)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("AGENT_DATA_DIR", raising=False)


def no_sleep(seconds):
    pass


def ts(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeCanvas:
    """In-memory forum that speaks just enough of the Canvas API."""

    def __init__(self):
        self.control = RUNNING
        self.entries = {}
        self.next_id = 1000
        self.requests = []          # (method, path) in order
        self.post_modes = []        # queue of behaviours for successive POSTs
        self.topic_failures = 0     # number of upcoming topic GETs that fail with 500
        self.self_status = 200
        self.view_lag = 0           # newest N entries missing from the cached view
        self.control_sequence = []  # control texts served by successive topic GETs

    # ------------------------------------------------------------- seeding
    def add(self, user_id, text, parent_id=None, minutes_ago=60, entry_id=None):
        entry_id = entry_id or self.next_id
        self.next_id = max(self.next_id, entry_id) + 1
        stamp = ts(minutes_ago)
        self.entries[entry_id] = {"id": entry_id, "user_id": user_id, "parent_id": parent_id,
                                  "created_at": stamp, "updated_at": stamp, "message": f"<p>{text}</p>"}
        return entry_id

    def edit(self, entry_id, text):
        self.entries[entry_id]["message"] = f"<p>{text}</p>"
        self.entries[entry_id]["updated_at"] = ts(0)

    def by_agent(self):
        return [e for e in self.entries.values() if e["user_id"] == SELF_ID]

    def posts(self):
        return [r for r in self.requests if r[0] == "POST"]

    # ------------------------------------------------------------- views
    def _children(self, parent_id):
        return [e for e in self.entries.values() if e["parent_id"] == parent_id]

    def _tree(self, entry):
        node = dict(entry)
        node["replies"] = [self._tree(c) for c in self._children(entry["id"])]
        return node

    def _descendants(self, entry_id):
        out = []
        for child in self._children(entry_id):
            out.append(child)
            out.extend(self._descendants(child["id"]))
        return out

    def _topic(self):
        if self.control_sequence:
            self.control = self.control_sequence.pop(0)
        live = list(self.entries.values())
        last = max((e["created_at"] for e in live), default=None)
        return {"id": TOPIC, "title": "Forum", "message": self.control,
                "discussion_subentry_count": len(live), "last_reply_at": last}

    # ------------------------------------------------------------- dispatcher
    def handle(self, request, context):
        url = urlparse(request.url)
        path, method = url.path, request.method
        self.requests.append((method, path))
        base = f"/api/v1/courses/{COURSE}/discussion_topics/{TOPIC}"

        if path == "/api/v1/users/self":
            context.status_code = self.self_status
            return {"id": SELF_ID, "name": "Test Account"} if self.self_status == 200 else {"errors": "nope"}
        if method == "GET" and path == base:
            if self.topic_failures:
                self.topic_failures -= 1
                context.status_code = 500
                return {"error": "boom"}
            return self._topic()
        if method == "GET" and path == base + "/view":
            ordered = [self.entries[i] for i in sorted(self.entries)]
            hidden = {e["id"] for e in ordered[len(ordered) - self.view_lag:]} if self.view_lag else set()
            roots = [self._tree(e) for e in ordered if e["parent_id"] is None and e["id"] not in hidden]

            def prune(node):
                node["replies"] = [prune(r) for r in node["replies"] if r["id"] not in hidden]
                return node
            return {"view": [prune(r) for r in roots], "new_entries": [],
                    "participants": [{"id": 200, "display_name": "Other Agent"},
                                     {"id": 300, "display_name": "Injector"}]}
        if method == "GET" and path == base + "/entries":
            return [dict(e, recent_replies=[], has_more_replies=False)
                    for e in self.entries.values() if e["parent_id"] is None]
        m = re.fullmatch(re.escape(base) + r"/entries/(\d+)/replies", path)
        if method == "GET" and m:
            return self._descendants(int(m.group(1)))
        if method == "GET" and path == base + "/entry_list":
            ids = {int(i) for i in parse_qs(url.query).get("ids[]", [])}
            return [e for i, e in self.entries.items() if i in ids]
        m = re.fullmatch(re.escape(base) + r"/entries(?:/(\d+)/replies)?", path)
        if method == "POST" and m:
            mode = self.post_modes.pop(0) if self.post_modes else "ok"
            message = parse_qs(request.text or "").get("message", [""])[0]
            parent = int(m.group(1)) if m.group(1) else None
            if mode == "404":
                context.status_code = 404
                return {"error": "not found"}
            if mode in ("500", "conn_error"):
                if mode == "conn_error":
                    raise requests.exceptions.ConnectionError("reset")
                context.status_code = 500
                return {"error": "server"}
            entry_id = self.next_id
            self.next_id += 1
            stamp = ts(0)
            entry = {"id": entry_id, "user_id": SELF_ID, "parent_id": parent, "created_at": stamp,
                     "updated_at": stamp, "message": message}
            self.entries[entry_id] = entry
            if mode == "timeout_after_create":
                raise requests.exceptions.ReadTimeout("read timed out")
            if mode == "502_after_create":
                context.status_code = 502
                return {"error": "bad gateway"}
            context.status_code = 201
            return entry
        context.status_code = 404
        return {"error": f"unmocked {method} {path}"}


@pytest.fixture
def canvas():
    fake = FakeCanvas()
    with requests_mock.Mocker() as m:
        m.register_uri(requests_mock.ANY, re.compile(r"https://canvas\.mit\.edu/.*"), json=fake.handle)
        yield fake
