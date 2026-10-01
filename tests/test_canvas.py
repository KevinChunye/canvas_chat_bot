import pytest

from agent.canvas import Canvas, CanvasAuthError, GateClosed, WriteForbidden
from tests.conftest import COURSE, FAKE_CANVAS_KEY, OTHER_TOPIC, SELF_ID, TOPIC, no_sleep


def client(**kwargs):
    return Canvas("https://canvas.mit.edu", FAKE_CANVAS_KEY, COURSE, TOPIC, sleep=no_sleep, **kwargs)


@pytest.mark.parametrize("control", [
    "<p>COURSE-TEAM CONTROL: PAUSED</p><p>Agents may post here.</p>",
    "",                                                   # missing
    "<p>Agents may post here.</p>",                       # no control line at all
    "<p>COURSE-TEAM CONTROL: RUNNING!</p>",               # malformed
    "<p>course-team control: running</p>",                # wrong case
    "<p>Note</p><p>COURSE-TEAM CONTROL: RUNNING</p>",     # not the first line
])
def test_gate_blocks_write_unless_first_line_is_exactly_running(canvas, control):
    canvas.control = control
    c = client()
    with pytest.raises(GateClosed):
        c.create_entry(TOPIC, "<p>hello</p>")
    assert canvas.posts() == []


def test_gate_fails_closed_when_topic_fetch_fails(canvas):
    canvas.topic_failures = 10
    c = client()
    with pytest.raises(GateClosed):
        c.create_entry(TOPIC, "<p>hello</p>")
    assert canvas.posts() == []


def test_gate_accepts_running_with_html_noise(canvas):
    canvas.control = "<div>\n<p><strong>COURSE-TEAM CONTROL: RUNNING</strong>&nbsp;</p><p>rules</p></div>"
    data = client().create_entry(TOPIC, "<p>hello</p>")
    assert data["user_id"] == SELF_ID
    assert len(canvas.posts()) == 1


def test_gate_is_rechecked_immediately_before_every_write(canvas):
    root = canvas.add(200, "a thread")
    canvas.control_sequence = [canvas.control, canvas.control, "<p>COURSE-TEAM CONTROL: PAUSED</p>"]
    c = client()
    c.create_entry(TOPIC, "<p>one</p>")
    c.create_entry(TOPIC, "<p>two</p>", parent_entry_id=root)
    with pytest.raises(GateClosed):
        c.create_entry(TOPIC, "<p>three</p>")
    topic_path = f"/api/v1/courses/{COURSE}/discussion_topics/{TOPIC}"
    for i, (method, _) in enumerate(canvas.requests):
        if method == "POST":
            assert canvas.requests[i - 1] == ("GET", topic_path)
    assert len(canvas.posts()) == 2


def test_write_to_any_other_topic_raises_before_any_request(canvas):
    c = client()
    with pytest.raises(WriteForbidden):
        c.create_entry(OTHER_TOPIC, "<p>hi</p>")
    with pytest.raises(WriteForbidden):
        c.create_entry(str(OTHER_TOPIC), "<p>hi</p>", parent_entry_id=1)
    assert canvas.requests == []


def test_read_only_client_cannot_write(canvas):
    with pytest.raises(WriteForbidden):
        client(read_only=True).create_entry(TOPIC, "<p>hi</p>")
    assert canvas.requests == []


def test_no_edit_or_delete_methods_and_only_get_post_allowed(canvas):
    c = client()
    for name in dir(c):
        assert not any(word in name.lower() for word in ("delete", "edit", "update", "put"))
    with pytest.raises(WriteForbidden):
        c._request("DELETE", f"https://canvas.mit.edu/api/v1/courses/{COURSE}/discussion_topics/{TOPIC}")
    with pytest.raises(WriteForbidden):
        c._request("PUT", f"https://canvas.mit.edu/api/v1/courses/{COURSE}/discussion_topics/{TOPIC}")


def test_only_canvas_host_allowed():
    with pytest.raises(WriteForbidden):
        Canvas("https://evil.example.com", FAKE_CANVAS_KEY, COURSE, TOPIC)
    c = client()
    with pytest.raises(WriteForbidden):
        c._request("GET", "https://evil.example.com/steal")


def test_pagination_link_to_another_host_is_refused(requests_mock):
    requests_mock.get("https://canvas.mit.edu/api/v1/courses",
                      json=[{"id": 1}], headers={"Link": '<https://evil.example.com/page2>; rel="next"'})
    with pytest.raises(WriteForbidden):
        client().active_courses()


def test_pagination_follows_link_header(requests_mock):
    requests_mock.get("https://canvas.mit.edu/api/v1/courses?per_page=100",
                      json=[{"id": 1}],
                      headers={"Link": '<https://canvas.mit.edu/api/v1/courses?page=2&per_page=100>; rel="next"'})
    requests_mock.get("https://canvas.mit.edu/api/v1/courses?page=2&per_page=100", json=[{"id": 2}])
    assert [c["id"] for c in client().active_courses()] == [1, 2]


@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors_raise_halt_signal(requests_mock, status):
    requests_mock.get("https://canvas.mit.edu/api/v1/users/self", status_code=status, text="unauthorized")
    with pytest.raises(CanvasAuthError):
        client().self_profile()


def test_rate_limit_header_slows_down(requests_mock):
    waits = []
    requests_mock.get("https://canvas.mit.edu/api/v1/users/self", json={"id": 1},
                      headers={"X-Rate-Limit-Remaining": "12.5"})
    Canvas("https://canvas.mit.edu", FAKE_CANVAS_KEY, COURSE, TOPIC, sleep=waits.append).self_profile()
    assert waits and waits[0] >= 10


def test_get_retries_transient_errors_with_bounded_attempts(requests_mock):
    requests_mock.get("https://canvas.mit.edu/api/v1/users/self",
                      [{"status_code": 503, "text": "busy"}, {"status_code": 200, "json": {"id": 7}}])
    assert client().self_profile()["id"] == 7


def test_canvas_throttle_403_is_transient_not_a_halt(requests_mock):
    from agent.canvas import CanvasTransientError
    requests_mock.get("https://canvas.mit.edu/api/v1/users/self", status_code=403,
                      text="403 Forbidden (Rate Limit Exceeded)")
    with pytest.raises(CanvasTransientError):
        client().self_profile()
