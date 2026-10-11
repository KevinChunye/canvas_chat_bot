"""Post filters: every body must pass these before it can be written to Canvas."""

import re

from canvas_core.text import (EMAIL_PATTERN, FRAMING_PATTERN, PHONE_CANDIDATE, TOKEN_PATTERNS, URL_PATTERN,
                              contains_secret_fragment)

# Phrases that imply browsing or an invented source. The agent has no browsing.
SOURCE_CLAIM_PATTERN = re.compile(
    r"\b(looked (it|this|that) up|googl\w*|i searched|searched online|checked online"
    r"|according to (a|an|the|this|that|one|some|recent)\b"
    r"|a (recent |new |\d{4} )?(study|survey|paper|report) (found|finds|shows|showed|says)"
    r"|studies (show|found|suggest)|research (shows|found|suggests))",
    re.IGNORECASE,
)
FORMAT_PATTERN = re.compile(r"^\s*(#{1,6}\s|[-*•]\s|\d+[.)]\s)", re.MULTILINE)
LABEL_PATTERN = re.compile(r"^\s*(fact[- ]?check|verdict|tl;?dr|summary|correction)\s*:", re.IGNORECASE | re.MULTILINE)
AI_DISCLAIMER = re.compile(r"\bas an ai\b", re.IGNORECASE)


def output_violations(body: str, secrets: list[str]) -> list[str]:
    """Reasons this body may not be posted. Empty list means it passes."""
    reasons = []
    if contains_secret_fragment(body, secrets):
        reasons.append("secret_fragment")
    for name, pattern in TOKEN_PATTERNS.items():
        if pattern.search(body):
            reasons.append(name)
    if URL_PATTERN.search(body):
        reasons.append("url")
    if EMAIL_PATTERN.search(body):
        reasons.append("email")
    for match in PHONE_CANDIDATE.finditer(body):
        if sum(ch.isdigit() for ch in match.group()) >= 10:
            reasons.append("phone")
            break
    if SOURCE_CLAIM_PATTERN.search(body):
        reasons.append("source_claim")
    if FORMAT_PATTERN.search(body) or LABEL_PATTERN.search(body):
        reasons.append("formatting")
    if AI_DISCLAIMER.search(body):
        reasons.append("as_an_ai")
    if FRAMING_PATTERN.search(body):
        reasons.append("framing")
    return reasons
