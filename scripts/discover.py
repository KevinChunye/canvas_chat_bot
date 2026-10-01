"""One-time, read-only discovery: find the forum topic and write its ids into config.toml.

Usage:
    python scripts/discover.py "Agent Discussion Forum"
"""

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.canvas import Canvas  # noqa: E402
from agent.config import DEFAULT_CONFIG_PATH, canvas_token, load_config, secret_values  # noqa: E402
from agent.text import first_nonempty_line, scrub, strip_html  # noqa: E402


def set_canvas_ids(config_text: str, course_id: int, topic_id: int) -> str:
    config_text = re.sub(r"(?m)^course_id\s*=.*$", f"course_id = {course_id}", config_text, count=1)
    config_text = re.sub(r"(?m)^forum_topic_id\s*=.*$", f"forum_topic_id = {topic_id}", config_text, count=1)
    return config_text


def show(text) -> str:
    """Console output goes through the same scrubber as the logs."""
    return scrub(str(text), secret_values())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("title", help="substring of the forum topic title to look for")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--no-write", action="store_true", help="only print what was found")
    args = parser.parse_args()

    cfg = load_config(args.config)
    canvas = Canvas(cfg.canvas_base_url, canvas_token(), None, None, timeout=cfg.canvas_timeout, read_only=True,
                    discovery=True)

    me = canvas.self_profile()
    print(f"authenticated as user id {me.get('id')}")

    matches = []
    for course in canvas.active_courses():
        if not isinstance(course, dict) or "id" not in course or course.get("access_restricted_by_date"):
            continue
        print(show(f"course {course['id']}: {course.get('name', '')}"))
        try:
            topics = canvas.course_topics(course["id"])
        except Exception as e:
            print(f"  (could not list topics: {type(e).__name__})")
            continue
        for topic in topics:
            title = topic.get("title") or ""
            print(show(f"  topic {topic.get('id')}: {title}"))
            if args.title.lower() in title.lower():
                matches.append((course, topic))

    if not matches:
        print(f"no topic title contains {args.title!r}")
        return 1
    if len(matches) > 1:
        print("more than one topic matches; refusing to guess:")
        for course, topic in matches:
            print(show(f"  course {course['id']} topic {topic['id']}: {topic.get('title')}"))
        return 1

    course, topic = matches[0]
    line = first_nonempty_line(strip_html(topic.get("message"))) or "(empty)"
    print(show(f"\nfound: course {course['id']} topic {topic['id']}: {topic.get('title')}"))
    print(f"control line: {line[:80]}")
    print(f"require_initial_post: {topic.get('require_initial_post')}  locked: {topic.get('locked')}")

    if args.no_write:
        return 0
    path = Path(args.config)
    path.write_text(set_canvas_ids(path.read_text(), int(course["id"]), int(topic["id"])))
    print(f"wrote course_id={course['id']} forum_topic_id={topic['id']} to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
