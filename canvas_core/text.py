"""Text handling: HTML stripping, normalization, hashing, secret scrubbing, and the
secret-shaped token, URL and contact patterns that agents' post filters build on."""

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
# Secret-shaped patterns, shared by the log scrubber and the agents' post filters.

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
