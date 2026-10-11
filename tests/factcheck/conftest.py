"""Fact-check agent fixtures: a temp config and store, a fake LLM, and canned decisions."""

import json

import pytest

from agents.factcheck.config import load_config
from agents.factcheck.llm import LLMResult
from agents.factcheck.store import Store
from tests.conftest import COURSE, TOPIC

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


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(f"""
[canvas]
base_url = "https://canvas.mit.edu"
course_id = {COURSE}
forum_topic_id = {TOPIC}

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
