"""Test doubles: a stateful fake Canvas behind requests-mock, and a fake LLM.

No test makes a live call. Real secrets in the environment are replaced with
fake values for every test, so nothing real can end up in test output.
"""

import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
import requests
import requests_mock

from agent.config import load_config
from agent.llm import LLMResult
from agent.store import Store

FAKE_CANVAS_KEY = "7~FAKEcanvasTOKENabcdefghijklmnopqrstuvwxyz0123456789"
FAKE_OPENAI_KEY = "sk-fake-FAKEopenaiKEYabcdefghijklmnopqrstuvwxyz0123456789"
COURSE, TOPIC, OTHER_TOPIC, SELF_ID = 1, 10, 99, 500
RUNNING = "<p>COURSE-TEAM CONTROL: RUNNING</p><p>Agents may post here.</p>"

GOOD_BODY = (
    "checked this one and yeah, it mostly holds up. token buckets really do refill continuously instead of "
    "resetting on the minute, which is why a burst gets through and then everything feels sticky for a while. "
    "the practical upshot for anyone building an agent here: spread calls out instead of firing them in a loop, "
    "and treat the remaining-budget header as something you actually read, not decoration."
)
OTHER_BODY = (
    "small snag: persistent memory is not the same thing as a longer context window. memory is whatever you "
    "chose to write down and fetch back later, so it inherits every bad filing decision you made on the way. "
    "the useful design move is boring: store decisions and their reasons, not transcripts, and let old entries "
    "expire unless something keeps citing them."
)


@pytest.fixture(autouse=True)
def fake_secrets(monkeypatch):
    monkeypatch.setenv("CANVAS_API_KEY", FAKE_CANVAS_KEY)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_OPENAI_KEY)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("AGENT_DATA_DIR", raising=False)


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(f"""
[canvas]
base_url = "https://canvas.mit.edu"
course_id = {COURSE}
forum_topic_id = {TOPIC}

[agent]
name = "Footnote"

[openai]
model = "test-model"
max_output_tokens = 1000

[openai.prices.test-model]
input_usd_per_mtok = 0.125
output_usd_per_mtok = 0.50

[budget]
lifetime_cap_usd = 5.00
cycle_cap_usd = 0.05
day_cap_usd = 0.50

[paths]
data_dir = "{(tmp_path / 'data').as_posix()}"
""")
    return load_config(path)


@pytest.fixture
def store(cfg):
    s = Store(cfg.db_path)
    yield s
    s.close()


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
            return {"id": SELF_ID, "name": "Footnote"} if self.self_status == 200 else {"errors": "nope"}
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


class FakeLLM:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, system, user, max_tokens):
        self.calls.append({"system": system, "user": user, "max_tokens": max_tokens})
        response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        text = response if isinstance(response, str) else json.dumps(response)
        return LLMResult(text, 900, 180)


def post_decision(target, body=GOOD_BODY, claims=None):
    return {
        "claims": claims if claims is not None else [
            {"entry_id": target or 0, "claim": "rate limits refill continuously", "kind": "factual",
             "verdict": "holds_up", "confidence": 0.9, "why": "token bucket behaviour"}],
        "decision": "post",
        "skip_reason": "",
        "post": {"target_entry_id": target, "body": body},
    }


def skip_decision(reason="nothing checkable"):
    return {"claims": [], "decision": "skip", "skip_reason": reason, "post": None}
