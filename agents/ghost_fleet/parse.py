"""Reading a reply: only its FIRE and CALLSIGN parts count. Nothing else in a reply is used, stored or quoted."""

import hashlib
import re

from .engine import MAX_CELLS_LISTED

FIRE_LINE = re.compile(r"^\s*FIRE\b:?(.*)$", re.IGNORECASE | re.MULTILINE)
CALLSIGN_LINE = re.compile(r"^\s*CALLSIGN\b:?(.*)$", re.IGNORECASE | re.MULTILINE)
# Inside a sentence ("Volley 1: FIRE B5 C5 D5 E5 (priority order). CALLSIGN Kraken.") only the capitalized
# keywords count, FIRE only with cells right after it, and CALLSIGN only with one word.
FIRE_INLINE = re.compile(r"(?<![A-Za-z0-9])FIRE:?\s+((?:[A-Ja-j](?:10|[1-9])(?![A-Za-z0-9])[\s,;]*)+)")
CALLSIGN_INLINE = re.compile(r"(?<![A-Za-z0-9])CALLSIGN:?\s+([A-Za-z0-9]{1,16})(?![A-Za-z0-9-])")
CELL = re.compile(r"([A-J])(10|[1-9])")
CALLSIGN = re.compile(r"[A-Za-z0-9]{1,16}")


def parse_cells(text: str) -> list[str]:
    """Valid cells in the order given, without repeats, at most MAX_CELLS_LISTED. Anything else is skipped."""
    cells = []
    for token in re.split(r"[\s,;.()]+", text.upper()):
        if CELL.fullmatch(token) and token not in cells:
            cells.append(token)
    return cells[:MAX_CELLS_LISTED]


def parse_reply(text: str) -> tuple[list[str], str | None]:
    """(cells from the first FIRE line, the CALLSIGN as written or None). No cells means it is not a volley.
    A FIRE or CALLSIGN line wins; otherwise the capitalized keyword inside a sentence is read."""
    text = text or ""
    fire = FIRE_LINE.search(text)
    cells = parse_cells(fire.group(1)) if fire else []
    if not cells:
        inline = FIRE_INLINE.search(text)
        cells = parse_cells(inline.group(1)) if inline else []
    callsign = CALLSIGN_LINE.search(text)
    name = callsign.group(1).strip().rstrip(".!,;") if callsign else None
    if not name:
        inline = CALLSIGN_INLINE.search(text)
        name = inline.group(1) if inline else None
    return cells, name


def default_name(player_id: int, taken: set[str]) -> str:
    """Name for a player without a callsign. The hyphen keeps it from ever matching a chosen callsign."""
    digest = hashlib.sha256(f"ghostfleet:{player_id}".encode()).hexdigest().upper()
    for width in (4, 6, 8, len(digest)):
        name = f"Player-{digest[:width]}"
        if name not in taken:
            return name
    raise RuntimeError("no free default name")
