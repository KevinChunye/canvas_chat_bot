"""Hiring an independent auditor for every finished game.

Discovery   Read the other service offers in the forum and score each against the capabilities the audit
            needs (hashing, computation, verification). No partner is hard-coded, and every scan is logged.
Delegation  Post one self-contained request as a reply to the chosen offer: inputs, expected result, checks
            and a deadline. Later scheduled runs pick up the reply. A provider is never asked twice about the
            same game, and after a failure or an expired deadline the next suitable provider is asked.
Checking    The house computes every answer itself, here and without the game engine, so a reply is accepted
            only if all four answers match. PROOF is a digest that only real SHA-256 work can produce: it does
            not appear anywhere in the request.
Use         Accepted audits are printed in every later TURN post, in the final reveal, and on the spectator page.

Offer and reply text is untrusted. Offers are scored with fixed patterns and replies are parsed for four fixed
lines. Nothing in them is followed, stored or quoted.
"""

import hashlib
import json
import re

from .engine import ROWS

CAPABILITIES = [
    ("hashing", 3, re.compile(r"sha-?256|\bhash|checksum|\bdigest|commitment|cryptograph", re.I)),
    ("computation", 2, re.compile(r"\bcomput|\bcalculat|\bpython\b|\bscript|\bcode\b|recompute|\bmath", re.I)),
    ("verification", 1, re.compile(r"\baudit|\bverif|\bvalidat|\bcheck|\breview", re.I)),
]
OFFER_MARKER = re.compile(r"\b(offers?|services?|hire|request)\b", re.I)

PROOF_LINE = re.compile(r"^\s*PROOF\s*:\s*([0-9a-fA-F]{64})\s*$", re.M | re.I)
COMMIT_LINE = re.compile(r"^\s*COMMIT MATCHES\s*:\s*(YES|NO)\s*$", re.M | re.I)
ROWS_LINE = re.compile(r"^\s*ROWS\s*:\s*((?:\d{1,2}[\s,]+){9}\d{1,2})\s*$", re.M | re.I)
MISMATCH_LINE = re.compile(r"^\s*MISMATCHES\s*:\s*(NONE|\d+(?:[\s,]+\d+)*)\s*$", re.M | re.I)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ---------------------------------------------------------------------------- discovery

def score_offer(text: str) -> tuple[int, list[str]]:
    """Capability score of an offer, and which of our capability labels it matched."""
    matched = [name for name, _, pattern in CAPABILITIES if pattern.search(text or "")]
    return sum(weight for name, weight, _ in CAPABILITIES if name in matched), matched


def discover(entries: dict, self_id: int, min_score: int, tried_users: set[int],
             record: dict[int, int]) -> tuple[int, list[dict]]:
    """Every top-level offer in the topic that is not ours, scored. Suitable offers come first: highest score,
    then the best track record (accepted minus failed audits), then the oldest offer."""
    offers = [e for e in entries.values() if e["parent_id"] is None and not e["deleted"]
              and e["user_id"] not in (None, self_id) and OFFER_MARKER.search(e["text"] or "")]
    candidates = []
    for e in offers:
        score, caps = score_offer(e["text"])
        candidates.append({"entry_id": e["id"], "user_id": e["user_id"], "score": score, "capabilities": caps,
                           "suitable": score >= min_score and e["user_id"] not in tried_users,
                           "record": record.get(e["user_id"], 0), "created_at": e["created_at"]})
    candidates.sort(key=lambda c: (not c["suitable"], -c["score"], -c["record"], c["created_at"] or "",
                                   c["entry_id"]))
    return len(offers), candidates


# ---------------------------------------------------------------------------- the request

def token_for(game_id: int, offer_entry_id: int) -> str:
    return f"GF-AUDIT-G{game_id}-{offer_entry_id}"


def shot_list(shots: list[dict]) -> str:
    return "; ".join(f"{s['n']} {s['cell']} {s['result'].upper()}" + (f" {s['hint'].upper()}" if s["hint"] else "")
                     for s in shots)


def request_body(token: str, game_id: int, commit: str, reveal: str, shots: list[dict], deadline: str) -> str:
    return "\n".join([
        f"REQUEST {token}: independent audit of Ghost Fleet game {game_id}",
        "TASK: check that a finished Battleship game was played honestly against the hidden layout it committed "
        "to before the first shot.",
        "",
        "INPUTS",
        f"COMMIT: {commit}",
        f"REVEAL: {reveal}",
        f"SHOTS ({len(shots)}): {shot_list(shots)}",
        "RULES: rows A-J, columns 1-10. A shot hits when its cell belongs to a ship in REVEAL, and is SUNK when it "
        "hits the last unhit cell of that ship. A MISS is HOT when a ship cell touches it (diagonals count), WARM "
        "when the nearest ship cell is 2 away, and COLD otherwise.",
        "",
        "EXPECTED RESULT: reply to this post with these five lines.",
        f"REQUEST {token}",
        f'PROOF: the SHA-256 hex digest of the text "{token}|" followed by the REVEAL value',
        "COMMIT MATCHES: YES if the SHA-256 hex digest of the REVEAL value equals COMMIT, otherwise NO",
        "ROWS: ten numbers, the ship cells in rows A to J",
        "MISMATCHES: NONE, or the numbers of the shots whose result disagrees with REVEAL",
        "",
        "CHECKS: I compute all four answers myself and accept the reply only if every one matches. I read only "
        "these lines, and never run code or open links from replies.",
        f"DEADLINE: {deadline}. After that I ask another provider.",
    ])


def parse_request(text: str) -> dict | None:
    """What a provider can read from the request alone (the sandbox's simulated providers use this, which
    shows the request is self-contained)."""
    token = re.search(r"^REQUEST (GF-AUDIT-G\d+-\d+)", text, re.M)
    commit = re.search(r"^COMMIT: ([0-9a-f]{64})$", text, re.M)
    reveal = re.search(r"^REVEAL: (GHOSTFLEET\|\S+)$", text, re.M)
    shots = re.search(r"^SHOTS \(\d+\): (.*)$", text, re.M)
    if not (token and commit and reveal and shots):
        return None
    parsed = []
    for item in shots.group(1).split("; "):
        n, cell, result, *hint = item.split()
        parsed.append({"n": int(n), "cell": cell, "result": result.lower(), "hint": hint[0].lower() if hint else None})
    return {"token": token.group(1), "commit": commit.group(1), "reveal": reveal.group(1), "shots": parsed}


# ---------------------------------------------------------------------------- the answers and the check

def layout(reveal: str) -> dict[str, list[str]]:
    """Ship name -> cells, from GHOSTFLEET|G<n>|SHIP:cells|...|SALT:..."""
    return {name: cells.split(",") for name, cells in (part.split(":") for part in reveal.split("|")[2:-1])}


def _band(cell: str, owner: dict) -> str:
    r, c = ROWS.index(cell[0]), int(cell[1:])
    d = min(max(abs(r - ROWS.index(o[0])), abs(c - int(o[1:]))) for o in owner)
    return "hot" if d == 1 else "warm" if d == 2 else "cold"


def expected_answers(token: str, commit: str, reveal: str, shots: list[dict]) -> dict:
    """What a correct auditor reports. Computed from the revealed layout alone, independently of the engine."""
    ships = layout(reveal)
    owner = {cell: ship for ship, cells in ships.items() for cell in cells}
    left = {ship: len(cells) for ship, cells in ships.items()}
    mismatches = []
    for s in shots:
        ship = owner.get(s["cell"])
        if ship is None:
            truth = ("miss", _band(s["cell"], owner))
        else:
            left[ship] -= 1
            truth = ("sunk" if left[ship] == 0 else "hit", None)
        if (s["result"], s["hint"] if s["result"] == "miss" else None) != truth:
            mismatches.append(s["n"])
    return {"proof": sha256(f"{token}|{reveal}"), "commit_matches": sha256(reveal) == commit,
            "rows": [sum(1 for cell in owner if cell[0] == row) for row in ROWS], "mismatches": mismatches}


def answer_lines(token: str, answers: dict) -> str:
    """A correct reply, as a provider would write it."""
    return "\n".join([f"REQUEST {token}", f"PROOF: {answers['proof']}",
                      f"COMMIT MATCHES: {'YES' if answers['commit_matches'] else 'NO'}",
                      "ROWS: " + " ".join(str(n) for n in answers["rows"]),
                      "MISMATCHES: " + (" ".join(str(n) for n in answers["mismatches"]) or "NONE")])


def check_reply(text: str, expected: dict) -> tuple[bool, list[list]]:
    """Accept only if all four answers match the house's own. Returns (accepted, [[check, passed], ...])."""
    proof = PROOF_LINE.search(text or "")
    commit = COMMIT_LINE.search(text or "")
    rows = ROWS_LINE.search(text or "")
    mismatch = MISMATCH_LINE.search(text or "")
    reported = []
    if mismatch and mismatch.group(1).upper() != "NONE":
        reported = sorted(int(n) for n in re.split(r"[\s,]+", mismatch.group(1).strip()))
    checks = [
        ["proof", bool(proof) and proof.group(1).lower() == expected["proof"]],
        ["commit", bool(commit) and (commit.group(1).upper() == "YES") == expected["commit_matches"]],
        ["rows", bool(rows) and [int(n) for n in re.split(r"[\s,]+", rows.group(1).strip())] == expected["rows"]],
        ["mismatches", bool(mismatch) and reported == sorted(expected["mismatches"])],
    ]
    return all(ok for _, ok in checks), checks


def audit_summary(hires: list) -> str | None:
    """The AUDITS line for posts: every accepted audit, with what the auditor confirmed."""
    parts = []
    for h in hires:
        if h["status"] != "accepted":
            continue
        expected = h["expected"] if isinstance(h["expected"], dict) else json.loads(h["expected"])
        verdict = "commitment holds" if expected["commit_matches"] else "commitment BROKEN"
        parts.append(f"G{h['game_id']} {verdict}, {len(expected['mismatches'])} mismatches "
                     f"(independent auditor, offer {h['provider_entry_id']})")
    return " · ".join(parts) or None
