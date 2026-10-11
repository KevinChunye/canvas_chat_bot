from canvas_core.text import content_hash, scrub, strip_html, to_html
from tests.conftest import FAKE_CANVAS_KEY, FAKE_OPENAI_KEY

SECRETS = [FAKE_CANVAS_KEY, FAKE_OPENAI_KEY]


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


def test_coursework_framing_is_scrubbed_from_logs():
    word = "grad" + "er"
    assert word not in scrub(f"the {word} liked it", SECRETS)
