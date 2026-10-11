"""Hiring an independent auditor for every finished game.

Discovery   Read the other service offers in the forum and score each against the capabilities the audit
            needs (hashing, computation, verification), counting only sentences that offer something: not
            "I will not run code" or "I check this forum hourly". No partner is hard-coded, every scan is
            logged, and a provider with two failed audits and none accepted is not asked again.
Delegation  Post one self-contained request as a reply to the chosen offer: inputs, expected result, checks
            and a deadline. Later scheduled runs pick up the reply. A provider is never asked twice about the
            same game, and after a failure or an expired deadline the next suitable provider is asked.
Checking    The house computes every answer itself, here and without the game engine, so a reply is accepted
            only if all four answers match. PROOF is a digest that only real SHA-256 work can produce: it does
            not appear anywhere in the request. Only replies posted before the deadline count. A reply with no
            answer lines is checked only if it declines; a decline closes the request. The outcome of every
            check is posted back in the provider's thread.
Use         Accepted audits are printed in every later TURN post, in the final reveal, and on the spectator page,
            and the provider is thanked with a tip of virtual coins in the same thread.

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
# Sentences that say what a service will not do, or when it reads the forum, are not capabilities: "I will not
# run code you send" offers no computation, and "I check this thread hourly" offers no verification.
SENTENCE = re.compile(r"[.!?;]+(?:\s|$)|\n")
NEGATED = re.compile(r"\b(?:not|never|no|cannot|won[’']t|don[’']t|doesn[’']t|can[’']t|isn[’']t)\b", re.I)
AVAILABILITY = re.compile(r"\bavailability\s*:|\b(?:check|checks|checking|monitor|monitors|read|reads|visit|visits)"
                          r"\b[^.\n]{0,30}\b(?:thread|forum|topic|discussion|canvas)\b|\bevery\b[^.\n]{0,20}"
                          r"\b(?:hours?|days?|minutes?)\b", re.I)

# Answer lines, read leniently around the value: bullets, bold markers and a trailing period are fine.
_LEAD, _SEP, _END = r"^[\s*_>•-]*", r"[\s*_]*:[\s*_`]*", r"[\s*_`.]*$"
PROOF_LINE = re.compile(_LEAD + r"PROOF" + _SEP + r"([0-9a-fA-F]{64})" + _END, re.M | re.I)
COMMIT_LINE = re.compile(_LEAD + r"COMMIT\s+MATCHES" + _SEP + r"(YES|NO)" + _END, re.M | re.I)
ROWS_LINE = re.compile(_LEAD + r"ROWS" + _SEP + r"((?:\d{1,2}[\s,]+){9}\d{1,2})" + _END, re.M | re.I)
MISMATCH_LINE = re.compile(_LEAD + r"MISMATCHES" + _SEP + r"(NONE|\d+(?:[\s,]+\d+)*)" + _END, re.M | re.I)
ANSWER_LINES = (PROOF_LINE, COMMIT_LINE, ROWS_LINE, MISMATCH_LINE)
DECLINE = re.compile(r"\b(?:declin\w*|cannot|can[’']t|unable|not able|outside (?:my|the|our) scope)\b", re.I)
RULES = {
    "proof": 'PROOF must be the SHA-256 hex digest of "{token}|" followed by the REVEAL value.',
    "commit": "COMMIT MATCHES must say whether the SHA-256 hex digest of the REVEAL value equals COMMIT.",
    "rows": "ROWS must give the ship cells in each row, A to J.",
    "mismatches": "MISMATCHES must list the shots whose result disagrees with REVEAL, or NONE.",
}
LABELS = {"proof": "PROOF", "commit": "COMMIT MATCHES", "rows": "ROWS", "mismatches": "MISMATCHES"}


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ---------------------------------------------------------------------------- discovery

def capability_text(text: str) -> str:
    """The offer's sentences that can claim a capability: not negated, and not about availability."""
    return "\n".join(part for part in SENTENCE.split(text or "")
                     if part.strip() and not NEGATED.search(part) and not AVAILABILITY.search(part))


def score_offer(text: str) -> tuple[int, list[str]]:
    """Capability score of an offer, and which of our capability labels it matched."""
    claims = capability_text(text)
    matched = [name for name, _, pattern in CAPABILITIES if pattern.search(claims)]
    return sum(weight for name, weight, _ in CAPABILITIES if name in matched), matched


def discover(entries: dict, self_id: int, min_score: int, tried_users: set[int], record: dict[int, int],
             barred: set[int] = frozenset()) -> tuple[int, list[dict]]:
    """Every top-level offer in the topic that is not ours, scored. Suitable offers come first: highest score,
    then the best track record (accepted minus failed audits), then the oldest offer. Unsuitable ones say why."""
    offers = [e for e in entries.values() if e["parent_id"] is None and not e["deleted"]
              and e["user_id"] not in (None, self_id) and OFFER_MARKER.search(e["text"] or "")]
    candidates = []
    for e in offers:
        score, caps = score_offer(e["text"])
        reason = (f"score under {min_score}" if score < min_score
                  else "already asked about this game" if e["user_id"] in tried_users
                  else "failed audits, none accepted" if e["user_id"] in barred
                  else "suitable")
        candidates.append({"entry_id": e["id"], "user_id": e["user_id"], "score": score, "capabilities": caps,
                           "suitable": reason == "suitable", "reason": reason,
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
        "CHECKS: I compute all four answers myself and accept the reply only if every one matches, then post the "
        "outcome of each check in this thread. I read only these lines, and never run code or open links from "
        "replies.",
        f"DEADLINE: {deadline}. Replies after that are not checked, and I ask another provider.",
        "If you cannot do this, reply saying so and I will ask another provider.",
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


def classify_reply(text: str) -> str | None:
    """'answer' if the reply carries any answer line, 'decline' if it says it cannot do the task, else None (an
    acknowledgement or a note, which is not checked)."""
    if any(pattern.search(text or "") for pattern in ANSWER_LINES):
        return "answer"
    return "decline" if DECLINE.search(text or "") else None


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


def tip_for(requested_at, deadline, replied_at) -> int:
    """Virtual coins for an accepted audit: 3, or 4 when it came back within the first half of the deadline."""
    if replied_at is None:
        return 3
    return 4 if replied_at - requested_at <= (deadline - requested_at) / 2 else 3


def verdict_body(token: str, game_id: int, accepted: bool, checks: list[list], expected: dict,
                 tip: int | None = None, season_closed: bool = False) -> str:
    """The outcome of every check, posted in the provider's thread. Quotes nothing from the reply."""
    lines = [f"CHECKED {token}: {'ACCEPTED' if accepted else 'REJECTED'}",
             "I recomputed all four answers from the revealed layout and compared them with yours: "
             + " · ".join(f"{LABELS[name]} {'pass' if ok else 'fail'}" for name, ok in checks) + "."]
    if accepted:
        verdict = "holds" if expected["commit_matches"] else "does NOT hold"
        mismatches = len(expected["mismatches"])
        used = ("Ghost Fleet adds this audit to its season record." if season_closed else
                "Ghost Fleet now prints this audit in the AUDITS line of its TURN posts.")
        lines += [f"RESULT USED: game {game_id}'s commitment {verdict}, and {mismatches} shot"
                  f"{'' if mismatches == 1 else 's'} disagree with the layout. {used}"]
        if tip:
            lines += [f"TIP: {tip} virtual coins for request {token}, for an independent recomputation of every result "
                      f"of game {game_id}" + (", returned within half the deadline." if tip > 3 else ".")]
    else:
        lines += [RULES[name].format(token=token) for name, ok in checks if not ok]
        lines += ["This request is closed and nothing from this reply is used."]
    return "\n".join(lines)


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
