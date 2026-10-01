import pytest

from agent.text import content_hash, output_violations, scrub, strip_html, to_html
from tests.conftest import FAKE_CANVAS_KEY, FAKE_OPENAI_KEY, GOOD_BODY

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


def test_scrub_removes_secrets_and_token_shapes():
    text = f"failed with {FAKE_OPENAI_KEY} and {FAKE_CANVAS_KEY} and Bearer xyz and sk-zzzzzzzzzz"
    out = scrub(text, SECRETS)
    assert FAKE_OPENAI_KEY not in out and FAKE_CANVAS_KEY not in out
    assert "sk-zzzz" not in out and "Bearer" not in out


def test_strip_html_keeps_line_structure():
    html = "<p>COURSE-TEAM CONTROL: RUNNING</p><p>Second &amp; third</p><ul><li>x</li></ul>"
    assert strip_html(html).splitlines()[0] == "COURSE-TEAM CONTROL: RUNNING"
    assert "Second & third" in strip_html(html)


def test_content_hash_survives_canvas_reformatting():
    body = "it's \"fine\" & dandy\n\nsecond <para>\n\n— Footnote, an agent"
    ours = to_html(body)
    canvas_version = ours.replace("&amp;", "&#38;").replace("</p><p>", "</p>\n<p>").replace("—", "&mdash;")
    assert content_hash(ours) == content_hash(canvas_version)
    assert content_hash(ours) != content_hash(to_html(body + " extra"))


def test_coursework_framing_is_filtered_from_posts_and_logs():
    word = "grad" + "er"
    assert "framing" in output_violations(f"the {word} liked it", SECRETS)
    assert word not in scrub(f"the {word} liked it", SECRETS)
    assert "framing" not in output_violations("an upgrade with a gradient", SECRETS)
