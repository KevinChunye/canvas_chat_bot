"""Filter for everything Ghost Fleet posts. A TURN post quotes nothing from players except validated
callsigns, so a hit here means a bug or a bad callsign got through, and the post is not made."""

from canvas_core.text import (EMAIL_PATTERN, FRAMING_PATTERN, PHONE_CANDIDATE, SELF_IDENTIFICATION, TOKEN_PATTERNS,
                              URL_PATTERN, contains_secret_fragment)

from .parse import CALLSIGN


def post_violations(body: str, secrets: list[str], commitments: list[str] = ()) -> list[str]:
    """Reasons this body may not be posted. Game commitments (64 hex characters) are the one allowed token."""
    text = body
    for commit in commitments:
        text = text.replace(commit, " ")
    reasons = []
    if contains_secret_fragment(text, secrets):
        reasons.append("secret_fragment")
    reasons += [name for name, pattern in TOKEN_PATTERNS.items() if pattern.search(text)]
    if URL_PATTERN.search(text):
        reasons.append("url")
    if EMAIL_PATTERN.search(text):
        reasons.append("email")
    if any(sum(ch.isdigit() for ch in m.group()) >= 10 for m in PHONE_CANDIDATE.finditer(text)):
        reasons.append("phone")
    if SELF_IDENTIFICATION.search(text):
        reasons.append("self_identification")
    if FRAMING_PATTERN.search(text):
        reasons.append("framing")
    return reasons


def callsign_ok(callsign: str | None, secrets: list[str]) -> bool:
    """Letters and digits, 1-16 long, and nothing the post filter would refuse to print."""
    return bool(callsign) and CALLSIGN.fullmatch(callsign) is not None and not post_violations(callsign, secrets)
