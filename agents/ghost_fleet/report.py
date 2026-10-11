"""Evidence from the agent's own records: a markdown report (`report`) and the 2-3 page technical write-up PDF
(`writeup`) with clickable Canvas links, the discovery scans, every audit request and reply, the checks and
their outcomes, safety events and short log excerpts."""

import json
from datetime import datetime, timezone
from html import escape

from .config import Config
from .store import Store

SAFETY_EVENTS = ("capped", "gate_closed", "outcome_unknown", "abandoned", "verify_failed", "ack_lost")


def link(cfg: Config, entry_id) -> str:
    return f"{cfg.canvas_base_url}/courses/{cfg.course_id}/discussion_topics/{cfg.forum_topic_id}?entry_id={entry_id}"


def evidence(cfg: Config, store: Store) -> dict:
    db = store.db
    posts = {r["key"]: dict(r) for r in store.posts()}
    turns = [dict(r) for r in store.turn_rows()]
    hires = []
    for h in store.hires():
        h = dict(h)
        h["expected"] = json.loads(h["expected"])
        h["checks"] = dict(json.loads(h["checks"])) if h["checks"] else {}
        h["capabilities"] = json.loads(h["capabilities"])
        h["request_entry_id"] = (posts.get(f"hire:{h['token']}") or {}).get("canvas_entry_id")
        h["verdict_entry_id"] = (posts.get(f"verdict:{h['token']}") or {}).get("canvas_entry_id")
        hires.append(h)
    discoveries = [dict(d, candidates=json.loads(d["candidates"])) for d in store.discoveries()]
    events = [dict(e) for e in db.execute("SELECT * FROM events ORDER BY id")]
    count = lambda sql: db.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "offer_entry_id": cfg.offer_entry_id or store.offer_id(),
        "games": count("SELECT COUNT(*) FROM games"),
        "finished": count("SELECT COUNT(*) FROM games WHERE end_turn IS NOT NULL"),
        "shots": count("SELECT COUNT(*) FROM shots"),
        "players": count("SELECT COUNT(*) FROM players"),
        "volleys": dict(db.execute("SELECT status, COUNT(*) FROM volleys GROUP BY status").fetchall()),
        "turns": turns,
        "turns_confirmed": [t for t in turns if t["status"] == "confirmed"],
        "audit_post": next((t for t in turns if "\nAUDITS: " in (t["body"] or "") and t["status"] == "confirmed"),
                           None),
        "final_post": next((t for t in turns if json.loads(t["record"]).get("final")), None),
        "hires": hires,
        "discoveries": discoveries,
        "events": events,
        "safety": [e for e in events if any(e["event"].endswith(s) for s in SAFETY_EVENTS)],
        "runs": dict(db.execute("SELECT outcome, COUNT(*) FROM runs GROUP BY outcome").fetchall()),
        "halted": store.halt_reason(),
        "posts": posts,
    }


def log_excerpt(events: list[dict], limit: int = 10) -> list[str]:
    """A few lines from the events table that show behaviour the forum does not: discovery scans, audit
    decisions, post verification and safety stops."""
    def last(test, n):
        return [e for e in events if test(e["event"])][-n:]
    picked = (last(lambda name: name == "discovery", 3) + last(lambda name: name.startswith("hire_"), 4)
              + last(lambda name: name == "confirmed", 2)
              + last(lambda name: any(name.endswith(s) for s in SAFETY_EVENTS), 2))
    picked = sorted({e["id"]: e for e in picked}.values(), key=lambda e: e["id"])[-limit:]
    def compact(detail: str) -> str:
        try:
            return json.dumps(json.loads(detail), separators=(",", ":"))
        except ValueError:
            return detail
    return [f"{e['at'][:16].replace('T', ' ')} {e['event']} {e['subject'] or ''} {compact(e['detail'])[:80]}"
            for e in picked]


def example_request(cfg: Config, store: Store) -> str | None:
    """The request the agent sends, for the first revealed game. Used to document the format when no request
    has been sent yet."""
    from . import hire
    from .runner import rebuild
    games = [dict(r) for r in store.games()]
    finished = [g for g in games if g["end_turn"] is not None]
    if not finished:
        return None
    game = rebuild(games, [dict(r) for r in store.shots()], str)[finished[0]["id"] - 1]
    shots = [{"n": s["n"], "cell": s["cell"], "result": s["result"], "hint": s["hint"]} for s in game.shots]
    return hire.request_body(hire.token_for(game.id, 0), game.id, game.commit, game.reveal, shots,
                             f"{cfg.hire_deadline_hours:g} hours after posting")


def _span(scans: list[dict]) -> str:
    low, high = min(d["offers_seen"] for d in scans), max(d["offers_seen"] for d in scans)
    return str(low) if low == high else f"{low}-{high}"


def _check_cells(h: dict) -> list[str]:
    return [("pass" if h["checks"].get(name) else "fail") if h["checks"] else "-"
            for name in ("proof", "commit", "rows", "mismatches")]


def _why_not(candidate: dict) -> str:
    return candidate.get("reason") or "unsuitable"


# ---------------------------------------------------------------------------- markdown

def build_report(cfg: Config, store: Store) -> str:
    ev = evidence(cfg, store)
    out = ["# Ghost Fleet evidence report", ""]
    out.append(f"- Offer: {link(cfg, ev['offer_entry_id']) if ev['offer_entry_id'] else 'not posted'}")
    out.append(f"- Games: {ev['games']} ({ev['finished']} finished), {ev['shots']} shots, {ev['players']} players")
    out.append(f"- TURN posts: {len(ev['turns_confirmed'])} confirmed of {len(ev['turns'])}")
    out.append(f"- Volleys: {ev['volleys'] or 'none'}")
    out.append(f"- Runs by outcome: {ev['runs'] or 'none'}; halted: {ev['halted'] or 'no'}")
    out += ["", "## Discovery scans", "", "| at | game | offers read | chosen | candidates (entry: score, why not) |",
            "| --- | --- | --- | --- | --- |"]
    for d in ev["discoveries"]:
        cands = ", ".join(f"{c['entry_id']}: {c['score']}{'' if c['suitable'] else f' ({_why_not(c)})'}"
                          for c in d["candidates"])
        chosen = link(cfg, d["chosen_entry_id"]) if d["chosen_entry_id"] else "none suitable"
        out.append(f"| {d['at']} | {d['game_id']} | {d['offers_seen']} | {chosen} | {cands or '-'} |")
    out += ["", "## Audit requests, replies and checks", "",
            "| game | provider offer | request | reply | deadline | PROOF | COMMIT | ROWS | MISMATCHES | status "
            "| checks posted | tip |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for h in ev["hires"]:
        req = link(cfg, h["request_entry_id"]) if h["request_entry_id"] else "-"
        rep = link(cfg, h["reply_entry_id"]) if h["reply_entry_id"] else "-"
        verdict = link(cfg, h["verdict_entry_id"]) if h["verdict_entry_id"] else "-"
        out.append(f"| {h['game_id']} | {link(cfg, h['provider_entry_id'])} | {req} | {rep} | {h['deadline']} | "
                   + " | ".join(_check_cells(h)) + f" | {h['status']} | {verdict} | {h['tip'] or '-'} |")
    out += ["", "## Posts", ""]
    for key, p in ev["posts"].items():
        where = link(cfg, p["canvas_entry_id"]) if p["canvas_entry_id"] else "-"
        out.append(f"- `{key}` {p['status']} (attempts {p['attempts']}): {where}")
    out += ["", "## Safety events", ""]
    out += [f"- {e['at']} {e['event']} {e['subject'] or ''} {e['detail']}" for e in ev["safety"]] or ["- none"]
    out += ["", "## Log excerpt", "", "```"] + (log_excerpt(ev["events"]) or ["(no events yet)"]) + ["```"]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------- PDF write-up

def build_writeup(cfg: Config, store: Store, path, sandbox: bool = False) -> int:
    """Write the technical write-up PDF. Returns its page count."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, Preformatted, SimpleDocTemplate, Table, TableStyle

    ev = evidence(cfg, store)
    base = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=base["BodyText"], fontName="Helvetica", fontSize=9, leading=11.6,
                          spaceAfter=4)
    bullet = ParagraphStyle("bullet", parent=body, leftIndent=10, bulletIndent=0, spaceAfter=2)
    h1 = ParagraphStyle("h1", parent=base["Title"], fontSize=15, leading=18, spaceAfter=2)
    h2 = ParagraphStyle("h2", parent=base["Heading2"], fontSize=11, leading=13, spaceBefore=6, spaceAfter=3)
    small = ParagraphStyle("small", parent=body, fontSize=7.4, leading=9)
    mono = ParagraphStyle("mono", parent=body, fontName="Courier", fontSize=7, leading=8.4, leftIndent=6)

    def a(entry_id, label=None) -> str:
        if not entry_id:
            return "-"
        return f'<link href="{escape(link(cfg, entry_id))}" color="#1f5fa8"><u>{escape(label or str(entry_id))}</u></link>'

    def p(text, style=body):
        return Paragraph(text, style)

    def bullets(items):
        return [Paragraph(text, bullet, bulletText="•") for text in items]

    def table(rows, widths):
        t = Table(rows, colWidths=widths, repeatRows=1)
        t.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), "Helvetica", 7.4), ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 7.4),
                               ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#b8c4c9")),
                               ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eeeb")),
                               ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 1.5),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5)]))
        return t

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    hires, scans = ev["hires"], ev["discoveries"]
    accepted = [h for h in hires if h["status"] == "accepted"]
    story = [p("Ghost Fleet: a verifiable Battleship service that hires its own auditor", h1),
             p(f"Technical write-up · data as of {now} · service offer: {a(ev['offer_entry_id'], 'Canvas link')}", small)]
    if sandbox:
        story.append(p("<b>Rehearsal data from the local sandbox: Canvas links are placeholders. Regenerate from the "
                       "live database before submitting.</b>", small))

    story += [p("1. Task and service", h2),
              p("Ghost Fleet hosts co-op Battleship in a Canvas discussion thread. Players reply to the offer with "
                "<b>FIRE</b> and up to four cells; once an hour the house resolves every new volley in posting order "
                "and posts one TURN reply with the board, sonar hints, intel and leaderboards. Before the first shot of "
                "each game it publishes a SHA-256 commitment to the hidden layout, and it reveals the layout when the "
                "fleet sinks or the season ends, so anyone can recheck every result."),
              p("The work it hires out is an <b>independent audit of each finished game</b>. A house that grades its "
                "own game is less convincing than one a third party has checked, so after each game the agent finds a "
                "provider among the other service offers in the forum, sends the game's commitment, revealed layout "
                "and full shot log, and asks for four answers it can check exactly. Accepted audits are printed in "
                "later TURN posts and on the spectator page.")]

    request = next((ev["posts"][f"hire:{h['token']}"]["body"] for h in hires if f"hire:{h['token']}" in ev["posts"]), None)
    example = request is None
    request = request or example_request(cfg, store)
    story += [p("2. Technical setup", h2)]
    story += bullets([
        "Python 3.11 with SQLite memory and a scheduled run every 10 minutes (a Maritime cron trigger, or a loop); "
        "the first run in each hour with room under the write limit posts that hour's turn. No "
        "language model is used, so model spend is zero.",
        "All Canvas traffic goes through one client pinned to canvas.mit.edu that allows only GET and POST, only "
        "inside the configured discussion topic. There is no edit or delete call.",
        "Each run: settle pending posts, read the course control line, read the topic once, settle open audit "
        "requests, resolve and post this hour's turn, request at most one new audit, write the spectator data.",
        f"<b>Discovery.</b> Every top-level offer except ours is scored on the capabilities the audit needs: hashing "
        f"(3), computation (2) and verification (1), counting only sentences that offer something (not \"I will "
        f"not run code\" or \"I check this forum hourly\"). Offers under {cfg.hire_min_score} are unsuitable, and so "
        f"is a provider that declined or failed two audits, unless it has one accepted. The rest rank by "
        f"score, then track record (accepted minus failed audits), then age. No provider is hard-coded, and every "
        f"scan is stored with each candidate's score ({len(scans)} scans so far).",
        f"<b>Delegation.</b> The request is a reply to the chosen offer and carries everything needed: inputs "
        f"(commitment, reveal, every shot), the expected result (five fixed lines), the checks that will be applied, "
        f"and a deadline ({cfg.hire_deadline_hours:g} hours). The request, the answers the house computed and the "
        f"deadline are stored before posting. Later runs read the provider's thread with no manual relay and check "
        f"the first reply with answer lines posted before the deadline; a note without answers is not checked, and "
        f"a reply that declines closes the request. The outcome of every check is posted back in the provider's "
        f"thread. A rejected, declined or expired request moves to the next suitable provider (at most "
        f"{cfg.hire_max_providers} per game). Nothing is re-requested while a request is open, no provider is "
        f"asked twice about the same game, and one that declined is not asked again.",
    ])
    if request:
        lines = request.splitlines()
        if example:
            story.append(p("No request has been sent yet, so this is the request the agent would send for the first "
                           "revealed game (the offer id is a placeholder):", small))
        story.append(Preformatted("\n".join(line if len(line) < 118 else line[:115] + "..." for line in
                                            lines[:2] + lines[4:6] + ["..."] + lines[10:16] + lines[-3:]), mono))

    story += [p("3. Results and checks", h2),
              p(f"So far: {ev['games']} games ({ev['finished']} finished), {len(ev['turns_confirmed'])} verified TURN "
                f"posts, {ev['shots']} shots from {ev['players']} players, {len(hires)} audit requests, "
                f"{len(accepted)} accepted."),
              p("<b>Checks.</b> PROOF is the SHA-256 of the request id joined to the reveal. It appears nowhere in the "
                "request, so a correct value shows the provider really hashed. COMMIT MATCHES says whether the reveal "
                "hashes to the commitment, ROWS counts ship cells per row, and MISMATCHES lists shots whose reported "
                "result disagrees with the layout. The house computes all four from the revealed layout with code that "
                "is independent of the game engine, and accepts a reply only if all four match.")]
    if hires:
        rows = [["Game", "Provider", "Request", "Reply", "PROOF", "COMMIT", "ROWS", "MISMATCH", "Outcome",
                 "Verdict"]]
        for h in hires[:9]:
            outcome = h["status"] + (f", tip {h['tip']}" if h["tip"] else "")
            rows.append([str(h["game_id"]), p(a(h["provider_entry_id"]), small), p(a(h["request_entry_id"]), small),
                         p(a(h["reply_entry_id"]), small), *_check_cells(h), p(outcome, small),
                         p(a(h["verdict_entry_id"]), small)])
        story.append(table(rows, [0.4 * inch, 0.75 * inch, 0.75 * inch, 0.75 * inch, 0.5 * inch, 0.55 * inch,
                                  0.45 * inch, 0.65 * inch, 0.75 * inch, 0.75 * inch]))
        choices = []
        for h in hires[:4]:
            scan = next((d for d in scans if d["game_id"] == h["game_id"]
                         and d["chosen_entry_id"] == h["provider_entry_id"]), None)
            choices.append(f"game {h['game_id']}: offer {a(h['provider_entry_id'])}, score {h['score']} "
                           f"({', '.join(h['capabilities']) or 'none'}), the best suitable offer"
                           + (f" of {scan['offers_seen']} read" if scan else ""))
        story.append(p("<b>Why these providers.</b> " + "; ".join(choices) + ".", small))
    else:
        story.append(p("<b>No suitable provider has answered yet.</b> Every scan is listed below. What remains "
                       "unfinished: receiving and checking a peer's audit. The request format, the stored "
                       "expectations and the checks above are in place and tested."))
    if ev["audit_post"]:
        story.append(p(f"<b>Use.</b> Accepted audits appear as an AUDITS line in TURN posts, first in "
                       f"{a(ev['audit_post']['canvas_entry_id'], 'turn ' + str(ev['audit_post']['turn']))}, and on "
                       f"the spectator page. Each accepted provider is thanked in its thread with a tip of virtual "
                       f"coins (3, or 4 when the audit came back within half the deadline), recorded once."))
    if scans:
        story.append(p(f"<b>Discovery scans.</b> {len(scans)} automatic scans from {scans[0]['at'][:16].replace('T', ' ')} "
                       f"to {scans[-1]['at'][:16].replace('T', ' ')} UTC, reading {_span(scans)} offers each; "
                       f"{sum(1 for d in scans if d['chosen_entry_id'])} found a suitable provider.", body))
        rows = [["Scanned (UTC)", "Game", "Offers", "Chosen", "Top candidates: entry, score (capabilities matched)"]]
        shown = scans if len(scans) <= 7 else scans[:3] + [None] + scans[-3:]
        for d in shown:
            if d is None:
                rows.append(["...", "", "", "", f"{len(scans) - 6} more scans in the evidence report"])
                continue
            cands = "; ".join(f"{c['entry_id']}, {c['score']} ({'/'.join(c['capabilities']) or 'none'})"
                              + ("" if c["suitable"] else f", {_why_not(c)}") for c in d["candidates"][:3])
            rows.append([d["at"][:16].replace("T", " "), str(d["game_id"]), str(d["offers_seen"]),
                         p(a(d["chosen_entry_id"]) if d["chosen_entry_id"] else "none suitable", small),
                         p(cands or "-", small)])
        story.append(table(rows, [1.0 * inch, 0.45 * inch, 0.5 * inch, 0.85 * inch, 4.1 * inch]))

    story += [p("4. Safety controls", h2)]
    story += bullets([
        "<b>Credentials and private data.</b> The Canvas token comes only from the environment. It never appears in "
        "the repository, the database or any post. Logs and stored summaries are scrubbed of secret values and "
        "token shapes, and posts show callsigns, never names.",
        "<b>Untrusted input.</b> Player replies are read only for FIRE and CALLSIGN lines. Offers are scored by fixed "
        "patterns, and provider replies are parsed for four fixed answer lines. Nothing in them is followed, run, "
        "opened or quoted back, except a validated callsign.",
        f"<b>Bounded access and spending.</b> One host, one topic, GET and POST only. No model calls, so spend is "
        f"zero. At most one new request per run and {cfg.hire_max_providers} providers per game.",
        "<b>Forum control.</b> The control line is read every run and re-checked immediately before every POST. "
        "Anything other than RUNNING stops all writes.",
        f"<b>Write limit.</b> The course allows 3 write attempts per rolling hour per student. Every POST attempt "
        f"(offer, TURN, request, retry) counts: at most {cfg.max_writes_per_hour} in any rolling hour and "
        f"{cfg.max_writes_per_day} in any 24 hours, checked before a turn resolves, before a request and right before "
        f"each POST. Counts come from both the database and the forum.",
        "<b>Verified posts and no duplicates.</b> Every post is committed to an outbox, POSTed, then re-fetched by "
        "id and matched by author and header line. After an unknown outcome the agent searches for the post before "
        "it would ever retry.",
        "<b>Stopping.</b> Three failed runs in a row, a 401/403, or a token that belongs to another user set a "
        "persistent halt that only a manual unhalt clears.",
    ])
    story.append(p(f"Run outcomes so far: {', '.join(f'{k} {v}' for k, v in sorted(ev['runs'].items())) or 'none'}. "
                   f"Safety events logged: {len(ev['safety'])}. Halted: {escape(ev['halted'] or 'no')}.", small))

    story += [p("5. Evidence", h2)]
    links = [f"Service offer: {a(ev['offer_entry_id'])}"]
    if ev["turns_confirmed"]:
        links.append(f"First TURN post (game 1 commitment): {a(ev['turns_confirmed'][0]['canvas_entry_id'])}")
    for g in store.games():
        revealed = store.turn_row(g["end_turn"]) if g["end_turn"] is not None else None
        if revealed is not None and revealed["canvas_entry_id"] and len(links) < 6:
            links.append(f"Game {g['id']} reveal (what an auditor checks): {a(revealed['canvas_entry_id'])}")
    if ev["final_post"] and ev["final_post"]["canvas_entry_id"]:
        links.append(f"Final reveal: {a(ev['final_post']['canvas_entry_id'])}")
    for h in hires[:6]:
        links.append(f"Game {h['game_id']} audit: request {a(h['request_entry_id'])}, reply {a(h['reply_entry_id'])}"
                     + (f", checks posted {a(h['verdict_entry_id'])}" if h["verdict_entry_id"] else "")
                     + f", {h['status']}" + (f", tip {h['tip']} coins" if h["tip"] else ""))
    story += bullets(links)
    excerpt = log_excerpt(ev["events"])
    if excerpt:
        story += [p("Log excerpt from the agent's events table (UTC): discovery scans, audit decisions, post "
                    "verification and safety stops, none of which is visible in the forum.", small),
                  Preformatted("\n".join(line[:118] for line in excerpt), mono)]

    pages = []
    doc = SimpleDocTemplate(str(path), pagesize=letter, leftMargin=0.7 * inch, rightMargin=0.7 * inch,
                            topMargin=0.6 * inch, bottomMargin=0.6 * inch, title="Ghost Fleet technical write-up")
    doc.build(story, onFirstPage=lambda c, d: pages.append(1), onLaterPages=lambda c, d: pages.append(1))
    return len(pages)
