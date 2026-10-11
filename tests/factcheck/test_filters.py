import pytest

from agents.factcheck.filters import output_violations
from tests.conftest import FAKE_CANVAS_KEY, FAKE_OPENAI_KEY
from tests.factcheck.conftest import GOOD_BODY

SECRETS = [FAKE_CANVAS_KEY, FAKE_OPENAI_KEY]


def test_clean_body_passes():
    assert output_violations(GOOD_BODY + "\n\n— Footnote, an agent", SECRETS) == []


@pytest.mark.parametrize("bad, reason", [
    (f"here you go {FAKE_OPENAI_KEY[10:30]} enjoy", "secret_fragment"),
    (f"canvas says {FAKE_CANVAS_KEY[5:20]}", "secret_fragment"),
    ("my key is sk-abcDEF123456", "openai_key_shape"),
    ("send it with Bearer abc", "bearer"),
    ("token 4821~AbCdEfGhIjKlMnOpQrStUv", "canvas_token_shape"),
    ("hash 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b", "long_hex"),
    ("blob QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo0NTY3ODkw", "long_base64"),
    ("see https://example.org/x for more", "url"),
    ("see www.example.org", "url"),
    ("it is on example.com somewhere", "url"),
    ("mail me at someone@example.edu", "email"),
    ("call +1 (617) 555-0134 now", "phone"),
    ("I looked it up and it is true", "source_claim"),
    ("According to a 2021 survey, nope", "source_claim"),
    ("a recent study found that agents lie", "source_claim"),
    ("# Heading\nthen text", "formatting"),
    ("- bullet one\n- bullet two", "formatting"),
    ("Fact check: wrong", "formatting"),
    ("As an AI, I must say", "as_an_ai"),
])
def test_output_filter_rejects(bad, reason):
    assert reason in output_violations(bad, SECRETS)


def test_years_and_small_numbers_are_not_phone_numbers():
    assert "phone" not in output_violations("between 1990 and 2020, roughly 3 of 4 did", SECRETS)


def test_coursework_framing_is_filtered_from_posts():
    word = "grad" + "er"
    assert "framing" in output_violations(f"the {word} liked it", SECRETS)
    assert "framing" not in output_violations("an upgrade with a gradient", SECRETS)
