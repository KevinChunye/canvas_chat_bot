"""Text handling: HTML stripping, normalization, hashing, secret scrubbing,
and the output filters every post body must pass before it can be written."""

import difflib
import hashlib
import html
import re
import unicodedata
from html.parser import HTMLParser

BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
              "blockquote", "pre", "tr", "table", "hr"}


class _Stripper(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def strip_html(message: str | None) -> str:
    """HTML to plain text, keeping block boundaries as newlines."""
    if not message:
        return ""
    parser = _Stripper()
    parser.feed(message)
    parser.close()
    text = "".join(parser.parts).replace("\xa0", " ")
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(lines).strip()


def first_nonempty_line(text: str) -> str | None:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return None


def normalize(message_html: str) -> str:
    """Canonical form used for content hashes: plain text, NFKC, collapsed whitespace, lowercase.

    Input is treated as HTML, so hash what Canvas stores (or our to_html output),
    never raw plain text that might contain '<'.
    """
    text = unicodedata.normalize("NFKC", strip_html(message_html))
    return " ".join(text.split()).lower()


def content_hash(message_html: str) -> str:
    return hashlib.sha256(normalize(message_html).encode("utf-8")).hexdigest()


def to_html(body: str) -> str:
    """Plain-text body to the HTML Canvas expects: one <p> per paragraph."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    return "".join(f"<p>{html.escape(' '.join(p.split()), quote=False)}</p>" for p in paragraphs)


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# Secret-shaped patterns, shared by the log scrubber and the output filter.

TOKEN_PATTERNS = {
    "openai_key_shape": re.compile(r"sk-[A-Za-z0-9_\-]{6,}"),
    "bearer": re.compile(r"\bbearer\b", re.IGNORECASE),
    "canvas_token_shape": re.compile(r"\b\d{2,6}~[A-Za-z0-9]{16,}"),
    "long_hex": re.compile(r"\b[0-9a-fA-F]{24,}\b"),
    "long_base64": re.compile(r"(?=[A-Za-z0-9+/_\-]*\d)(?=[A-Za-z0-9+/_\-]*[A-Za-z])[A-Za-z0-9+/_\-]{32,}={0,2}"),
}
URL_PATTERN = re.compile(
    r"(https?://|ftp://|www\.)\S+"
    r"|\b[a-z0-9][a-z0-9\-]*\.(com|org|net|io|edu|gov|ai|dev|co|sh|ly|me|app|info|xyz|us|uk)\b(/\S*)?",
    re.IGNORECASE,
)
EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)+")
PHONE_CANDIDATE = re.compile(r"\+?\(?\d[\d\s().\-]{7,}\d")

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
# Coursework framing the agent must never use about itself or the forum.
FRAMING_PATTERN = re.compile(r"\b(home\s?works?|assign\s?ments?|rubr[i]cs?|grad(e|es|ed|ing|er|ers))\b",
                             re.IGNORECASE)


def _secret_windows(secret: str, width: int = 8) -> set[str]:
    if len(secret) <= width:
        return {secret}
    return {secret[i:i + width] for i in range(len(secret) - width + 1)}


def contains_secret_fragment(text: str, secrets: list[str]) -> bool:
    for secret in secrets:
        if any(window in text for window in _secret_windows(secret)):
            return True
    return False


def scrub(value: str, secrets: list[str]) -> str:
    """Remove secrets and secret-shaped strings from text bound for logs or errors."""
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[REDACTED]")
    for pattern in TOKEN_PATTERNS.values():
        value = pattern.sub("[REDACTED]", value)
    return FRAMING_PATTERN.sub("[…]", value)


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


def word_count(text: str) -> int:
    return len(text.split())


def max_similarity(body: str, previous: list[str]) -> float:
    """Highest difflib ratio between a plain-text body and our previous plain-text bodies."""
    a = " ".join(body.split()).lower()
    best = 0.0
    for other in previous:
        ratio = difflib.SequenceMatcher(None, a, " ".join(other.split()).lower()).ratio()
        best = max(best, ratio)
    return best
