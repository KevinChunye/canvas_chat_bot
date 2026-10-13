"""Hiring other agents: exact odds for a blind volley once a season, and an independent audit of every
finished game. The odds request uses game id ODDS (0); games start at 1.

Discovery   Read the other service offers in the forum and score each against the capabilities the task needs
            (odds: probability, dice and cards, exact answers; audit: hashing, computation, verification),
            counting only sentences that offer something: not "I will not run code" or "I check this forum
            hourly". No partner is hard-coded, every scan is logged, and a provider that declined, or failed
            two requests with none accepted, is not asked again.
Delegation  Post one self-contained request as a reply to the chosen offer: inputs, expected result, checks
            and a deadline. Later scheduled runs pick up the reply. A provider is never asked twice about the
            same game, and after a failure or an expired deadline the next suitable provider is asked.
Checking    The house computes every answer itself, here and without the game engine, so a reply is accepted
            only if every answer matches: three exact fractions for the odds, four answers for an audit. PROOF
            is a digest that only real SHA-256 work can produce: it does not appear anywhere in the request. Only replies posted before the deadline count. A reply with no
            answer lines is checked only if it declines; a decline closes the request. The outcome of every
            check is posted back in the provider's thread.
Use         Accepted odds and audits are printed in every later TURN post (ODDS and AUDITS lines) and in the
            final reveal, and the provider is thanked with a tip of virtual coins in the same thread.

Offer and reply text is untrusted. Offers are scored with fixed patterns and replies are parsed for a few fixed
lines. Nothing in them is followed, stored or quoted.
"""

import hashlib
import json
import re
from fractions import Fraction
from math import comb

from .engine import ROWS, SHIP_CELLS, SHOTS_PER_VOLLEY, SIZE

ODDS = 0                                      # game id of the season's odds request

CAPABILITIES = [
    ("hashing", 3, re.compile(r"sha-?256|\bhash|checksum|\bdigest|commitment|cryptograph", re.I)),
    ("computation", 2, re.compile(r"\bcomput|\bcalculat|\bpython\b|\bscript|\bcode\b|recompute|\bmath", re.I)),
    ("verification", 1, re.compile(r"\baudit|\bverif|\bvalidat|\bcheck|\breview", re.I)),
]
ODDS_CAPABILITIES = [
    ("probability", 3, re.compile(r"probabilit|\bodds\b|\bchances?\b|likelihood", re.I)),
    ("dice and cards", 2, re.compile(r"\bdice\b|\bdie\b|\bd\d+\b|\bcards?\b|\bdeck\b", re.I)),
    ("exact answers", 1, re.compile(r"\bexact|\bfraction|combinator|simulat", re.I)),
]
OFFER_MARKER = re.compile(r"\b(offers?|services?|hire|requests?|what i do|what to send)\b", re.I)
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


def score_offer(text: str, capabilities: list = CAPABILITIES) -> tuple[int, list[str]]:
    """Capability score of an offer for one task, and which of that task's capability labels it matched."""
    claims = capability_text(text)
    matched = [name for name, _, pattern in capabilities if pattern.search(claims)]
    return sum(weight for name, weight, _ in capabilities if name in matched), matched


def discover(entries: dict, self_id: int, min_score: int, tried_users: set[int], record: dict[int, int],
             barred: set[int] = frozenset(), capabilities: list = CAPABILITIES) -> tuple[int, list[dict]]:
    """Every top-level offer in the topic that is not ours, scored. Suitable offers come first: highest score,
    then the best track record (accepted minus failed audits), then the oldest offer. Unsuitable ones say why."""
    offers = [e for e in entries.values() if e["parent_id"] is None and not e["deleted"]
              and e["user_id"] not in (None, self_id) and OFFER_MARKER.search(e["text"] or "")]
    candidates = []
    for e in offers:
        score, caps = score_offer(e["text"], capabilities)
        reason = (f"score under {min_score}" if score < min_score
                  else "already asked" if e["user_id"] in tried_users
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
    odds = game_id == ODDS
    labels, rules = (ODDS_LABELS, ODDS_RULES) if odds else (LABELS, RULES)
    lines = [f"CHECKED {token}: {'ACCEPTED' if accepted else 'REJECTED'}",
             ("I counted every outcome myself and compared each fraction with yours: " if odds else
              "I recomputed all four answers from the revealed layout and compared them with yours: ")
             + " · ".join(f"{labels[name]} {'pass' if ok else 'fail'}" for name, ok in checks) + "."]
    if accepted:
        if odds:
            used = ("Ghost Fleet adds these odds to its season record." if season_closed else
                    "Ghost Fleet now prints them in the ODDS line of its TURN posts, so players can compare the "
                    "swarm with blind firing.")
            lines += [f"RESULT USED: {odds_text(expected)}. {used}"]
            work = "exact odds for a blind volley"
        else:
            verdict = "holds" if expected["commit_matches"] else "does NOT hold"
            mismatches = len(expected["mismatches"])
            used = ("Ghost Fleet adds this audit to its season record." if season_closed else
                    "Ghost Fleet now prints this audit in the AUDITS line of its TURN posts.")
            lines += [f"RESULT USED: game {game_id}'s commitment {verdict}, and {mismatches} shot"
                      f"{'' if mismatches == 1 else 's'} disagree with the layout. {used}"]
            work = f"an independent recomputation of every result of game {game_id}"
        if tip:
            lines += [f"TIP: {tip} virtual coins for request {token}, for {work}"
                      + (", returned within half the deadline." if tip > 3 else ".")]
    else:
        lines += [rules[name].format(token=token) for name, ok in checks if not ok]
        lines += ["This request is closed and nothing from this reply is used."]
    return "\n".join(lines)


def audit_summary(hires: list) -> str | None:
    """The AUDITS line for posts: every accepted audit, with what the auditor confirmed."""
    parts = []
    for h in hires:
        if h["status"] != "accepted" or h["game_id"] == ODDS:
            continue
        expected = h["expected"] if isinstance(h["expected"], dict) else json.loads(h["expected"])
        verdict = "commitment holds" if expected["commit_matches"] else "commitment BROKEN"
        parts.append(f"G{h['game_id']} {verdict}, {len(expected['mismatches'])} mismatches "
                     f"(independent auditor, offer {h['provider_entry_id']})")
    return " · ".join(parts) or None


# ---------------------------------------------------------------------------- the odds request

ODDS_LABELS = {"q1": "Q1", "q2": "Q2", "q3": "Q3"}
ODDS_RULES = {
    "q1": "Q1 must be the exact probability that at least one drawn card is marked.",
    "q2": "Q2 must be the exact probability that every drawn card is marked.",
    "q3": "Q3 must be the exact expected number of marked cards drawn.",
}
FRACTION = re.compile(r"(\d+)\s*/\s*(\d+)")
NUMBER = re.compile(r"(\d+)\s*/\s*(\d+)|(\d*\.\d+|\d+)")


def odds_token(offer_entry_id: int) -> str:
    return f"GF-ODDS-{offer_entry_id}"


def odds_expected(deck: int = SIZE * SIZE, marked: int = SHIP_CELLS, draws: int = SHOTS_PER_VOLLEY) -> dict:
    """Exact answers by counting: draws cards without replacement from a deck with `marked` marked cards."""
    total = comb(deck, draws)
    answers = {"q1": 1 - Fraction(comb(deck - marked, draws), total), "q2": Fraction(comb(marked, draws), total),
               "q3": Fraction(draws * marked, deck)}
    return {"deck": deck, "marked": marked, "draws": draws,
            **{name: [value.numerator, value.denominator] for name, value in answers.items()}}


def _pct(pair: list) -> str:
    value = Fraction(*pair)
    return f"{value.numerator}/{value.denominator}" + ("" if value.denominator == 1 else f" ({float(value):.1%})")


def odds_text(expected: dict) -> str:
    q3 = Fraction(*expected["q3"])
    return (f"a blind volley ({expected['draws']} shots at a fresh board) hits at least once with probability "
            f"{_pct(expected['q1'])}, hits with every shot {_pct(expected['q2'])}, and hits "
            f"{q3.numerator}/{q3.denominator} = {float(q3):g} times on average")


def odds_request_body(token: str, deadline: str, expected: dict | None = None) -> str:
    e = expected or odds_expected()
    deck, marked, draws = e["deck"], e["marked"], e["draws"]
    return "\n".join([
        f"REQUEST {token}: exact card odds for a Battleship volley",
        f"TASK: Ghost Fleet is a co-op Battleship game. A volley fires {draws} shots at different cells of a "
        f"{SIZE}x{SIZE} board that hides {marked} ship cells. For a volley aimed blind at a fresh board, that is "
        f"the same as drawing cards, so I would like the exact odds of this draw.",
        f"MECHANIC: a deck of {deck} cards, {marked} of them marked. Draw {draws} cards without replacement.",
        "Q1: the probability that at least one drawn card is marked",
        "Q2: the probability that every drawn card is marked",
        "Q3: the expected number of marked cards drawn",
        "",
        "EXPECTED RESULT: reply to this post with these four lines, each answer as an exact fraction followed by "
        "its decimal.",
        f"REQUEST {token}",
        "Q1: <fraction> <decimal>",
        "Q2: <fraction> <decimal>",
        "Q3: <fraction> <decimal>",
        "",
        "CHECKS: I count every outcome myself and accept the reply only if all three fractions equal mine (lowest "
        "terms not required), then post the outcome of each check in this thread. I read only these lines, and "
        "never run code or open links from replies.",
        "USE: accepted odds are printed in every later TURN post, so players can compare the swarm with blind "
        "firing.",
        f"DEADLINE: {deadline}. Replies after that are not checked, and I ask another provider.",
        "If you cannot do this, reply saying so and I will ask another provider.",
    ])


def parse_odds_request(text: str) -> dict | None:
    """What a provider can read from the request alone (the sandbox's simulated provider uses this)."""
    token = re.search(r"^REQUEST (GF-ODDS-\d+)", text, re.M)
    mechanic = re.search(r"^MECHANIC: a deck of (\d+) cards, (\d+) of them marked\. Draw (\d+) cards", text, re.M)
    if not (token and mechanic):
        return None
    deck, marked, draws = (int(n) for n in mechanic.groups())
    return {"token": token.group(1), "deck": deck, "marked": marked, "draws": draws}


def odds_answer_lines(token: str, answers: dict) -> str:
    """A correct reply, as a provider would write it."""
    return "\n".join([f"REQUEST {token}"] + [f"{label}: {answers[name][0]}/{answers[name][1]} "
                                            f"{answers[name][0] / answers[name][1]:.6f}"
                                            for name, label in ODDS_LABELS.items()])


def _number(match) -> Fraction | None:
    if match is None:
        return None
    if match.group(1) is not None:
        return Fraction(int(match.group(1)), int(match.group(2))) if int(match.group(2)) else None
    return Fraction(match.group(3))


def odds_answer(text: str, label: str) -> list[Fraction]:
    """The values a reply gives for one question, from its first line that starts with the label and has a
    number: the first fraction on the line, and the first number after its last "=" (so "1 - 6806/9900 =
    3094/9900" and "17/50 = 0.34" both read correctly). Empty if the question is not answered."""
    for line in (text or "").splitlines():
        head = re.match(_LEAD + label + r"\b(.*)$", line, re.I)
        if head is None or not re.search(r"\d", head.group(1)):
            continue
        rest = head.group(1)
        values = [_number(FRACTION.search(rest)), _number(NUMBER.search(rest.rsplit("=", 1)[-1]))]
        return [v for v in values if v is not None]
    return []


def classify_odds_reply(text: str) -> str | None:
    if any(odds_answer(text, label) for label in ODDS_LABELS.values()):
        return "answer"
    return "decline" if DECLINE.search(text or "") else None


def check_odds_reply(text: str, expected: dict) -> tuple[bool, list[list]]:
    """Accept only if all three answers equal the house's own count. Returns (accepted, [[check, passed]])."""
    checks = [[name, Fraction(*expected[name]) in odds_answer(text, label)] for name, label in ODDS_LABELS.items()]
    return all(ok for _, ok in checks), checks


def odds_summary(hires: list) -> str | None:
    """The ODDS line for posts, once an odds request has been accepted."""
    for h in hires:
        if h["status"] == "accepted" and h["game_id"] == ODDS:
            expected = h["expected"] if isinstance(h["expected"], dict) else json.loads(h["expected"])
            q3 = Fraction(*expected["q3"])
            return (f"blind {expected['draws']}-shot volley at a fresh board: at least one hit {_pct(expected['q1'])}"
                    f" · every shot hits {_pct(expected['q2'])} · {float(q3):g} hits on average "
                    f"(checked odds from offer {h['provider_entry_id']})")
    return None
