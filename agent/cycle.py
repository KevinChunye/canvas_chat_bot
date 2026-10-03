"""One cycle: halt check, lock, reconcile, read, diff, decide, validate, write."""

import fcntl
import re
import sys
import time
from dataclasses import dataclass

from .actions import Writer
from .budget import BudgetExhausted, BudgetRefused, Ledger
from .canvas import Canvas, CanvasAuthError, LostAck
from .config import Config, canvas_token, openai_key, secret_values
from .forum import (build_thread_blocks, exchanges_with_author, fetch_forum, find_candidates, newest,
                    own_replied_targets)
from .llm import MalformedDecision, calibrate, call, new_nonce, openai_complete, parse_decision, system_prompt, \
    user_prompt
from .log import CycleLog
from .store import Store
from .text import content_hash, max_similarity, output_violations, scrub, to_html, truncate, word_count

HALT_AFTER_FAILURES = 3

BLUNT_WRONG = re.compile(
    r"\b(that'?s|this is|it'?s|is|are|was|were)\s+(just\s+|simply\s+|flat(ly)?\s+|completely\s+|totally\s+"
    r"|plainly\s+|dead\s+)?(wrong|false|incorrect|untrue|a myth)\b|\bnot true\b|\bdebunked\b",
    re.IGNORECASE,
)
SIGNATURE_TAIL = re.compile(r"\s*[—–-]{1,2}\s*[^\n]{0,40}\ban agent\.?\s*$", re.IGNORECASE)


@dataclass
class Plan:
    kind: str                    # reply | thread
    target_entry_id: int | None
    thread_root_id: int | None
    body: str                    # plain text, signature included when sign_posts is on
    message_html: str
    content_hash: str


def signature(cfg: Config) -> str:
    return f"— {cfg.agent_name}, an agent"


def strip_signature(body: str) -> str:
    return SIGNATURE_TAIL.sub("", body.strip()).strip()


def check_post(post: dict, claims: list[dict], entries: dict, self_id: int, store: Store, cfg: Config,
               secrets: list[str], threads_this_cycle: set) -> tuple[Plan | None, list[str]]:
    """Everything the model proposed is re-checked here. Any reason means skip."""
    reasons = []
    target = post["target_entry_id"]
    root = None
    if target is None:
        kind = "thread"
        if store.recent_posts(store.last_day_cutoff(), kind="thread") >= cfg.max_new_threads_per_day:
            reasons.append("new-thread daily cap reached")
    else:
        kind = "reply"
        entry = entries.get(target)
        if entry is None:
            reasons.append("target is not an entry fetched from the forum topic")
        else:
            root = entry["root_id"]
            if entry["deleted"]:
                reasons.append("target is deleted")
            if entry["user_id"] == self_id:
                reasons.append("target is our own entry")
            if target in store.replied_targets() or target in own_replied_targets(entries, self_id):
                reasons.append("already replied to target")
            if entry["user_id"] != self_id and \
                    exchanges_with_author(entries, target, self_id) >= cfg.max_exchanges_per_author:
                reasons.append("exchange cap with this author in this reply chain")
            if root in threads_this_cycle:
                reasons.append("already posted in this thread this cycle")

    body = strip_signature(post["body"])
    words = word_count(body)
    if not cfg.min_words <= words <= cfg.max_words:
        reasons.append(f"length {words} words outside {cfg.min_words}-{cfg.max_words}")
    full = f"{body}\n\n{signature(cfg)}" if cfg.sign_posts else body
    reasons += [f"output filter: {r}" for r in output_violations(full, secrets)]

    relevant = [c for c in claims if target is None or c["entry_id"] == target]
    if BLUNT_WRONG.search(body) and not any(c["verdict"] == "wrong" for c in relevant):
        reasons.append("flat correction without a confident 'wrong' verdict")

    previous = [strip_signature(b) for b in store.own_bodies()]
    previous += [e["text"] for e in entries.values() if e["user_id"] == self_id]
    similarity = max_similarity(body, previous)
    if similarity >= cfg.similarity_threshold:
        reasons.append(f"too similar to a previous post ({similarity:.2f})")

    if reasons:
        return None, reasons
    message_html = to_html(full)
    return Plan(kind, target, root, full, message_html, content_hash(message_html)), []


class Cycle:
    def __init__(self, cfg: Config, store: Store, canvas, complete, log: CycleLog, cycle_id: str,
                 dry_run: bool, sleep):
        self.cfg = cfg
        self.store = store
        self.canvas = canvas
        self.complete = complete
        self.log = log
        self.cycle_id = cycle_id
        self.dry_run = dry_run
        self.sleep = sleep
        self.ledger = Ledger(store, cfg)
        self.writer = None
        self.result = {"cycle_id": cycle_id, "mode": "dry_run" if dry_run else "live"}

    def posts_made(self) -> int:
        return len(self.writer.intents_posted) if self.writer else 0

    def finish(self, outcome: str, summary: str, failed: bool = False) -> dict:
        self.store.finish_cycle(self.cycle_id, outcome, self.posts_made(), summary)
        if not self.dry_run:
            if failed:
                count = self.store.record_failure()
                self.log("failure_counted", consecutive_failures=count)
                if count >= HALT_AFTER_FAILURES:
                    self.store.set_halt(f"{count} consecutive failed cycles")
                    self.log("halt", reason=f"{count} consecutive failed cycles")
            elif outcome != "halted":
                self.store.reset_failures()
        self.log("cycle_end", outcome=outcome, summary=summary, posts_made=self.posts_made(),
                 cycle_spend_usd=round(self.store.spend_total(cycle_id=self.cycle_id), 6),
                 lifetime_spend_usd=round(self.ledger.lifetime_spend(), 6))
        self.result.update(outcome=outcome, summary=summary, posts_made=self.posts_made())
        return self.result

    def run(self) -> dict:
        try:
            return self._run()
        except CanvasAuthError as e:
            self.store.set_halt(f"canvas auth error: {e}")
            self.log("halt", reason="canvas 401/403", error=str(e))
            return self.finish("halted", f"canvas auth error: {e}")
        except LostAck:
            self.log("lost_ack", note="POST response discarded; intent stays pending for the next cycle to reconcile")
            return self.finish("error", "lost acknowledgement (drop_ack_once fault); intent left pending",
                               failed=True)
        except Exception as e:
            message = scrub(f"{type(e).__name__}: {e}", secret_values())
            self.log("error", error=message)
            return self.finish("error", message, failed=True)

    def _connect(self) -> None:
        if self.canvas is None:
            self.canvas = Canvas(self.cfg.canvas_base_url, canvas_token(), self.cfg.course_id,
                                 self.cfg.forum_topic_id, timeout=self.cfg.canvas_timeout, log=self.log,
                                 read_only=self.dry_run, sleep=self.sleep,
                                 fault=lambda: self.store.consume_fault("drop_ack_once"))
        else:
            self.canvas.log = self.log
        if self.complete is None:
            self.complete = openai_complete(self.cfg, openai_key())

    def _run(self) -> dict:
        cfg, store, log = self.cfg, self.store, self.log
        self._connect()
        me = self.canvas.self_profile()
        self_id = int(me["id"])
        known = store.get_state("self_id")
        log("self", user_id=self_id, known_user_id=known)
        if known is not None and int(known) != self_id:
            store.set_halt(f"token now belongs to user {self_id}, expected {known}")
            log("halt", reason="token user changed")
            return self.finish("halted", "Canvas token user changed")
        if known is None and not self.dry_run:
            store.set_state("self_id", str(self_id))
        self.writer = Writer(self.canvas, store, cfg, self_id, self.cycle_id, log, self.sleep)

        threads_this_cycle = set()
        if not self.dry_run:
            pending_before = {a["intent_id"]: a["thread_root_id"] for a in store.pending_actions()}
            outcomes = self.writer.reconcile_pending()
            if outcomes:
                log("reconcile_done", outcomes=outcomes)
            threads_this_cycle = {pending_before[i] for i in self.writer.intents_posted}
            if any(o in ("unverified", "failed") for o in outcomes):
                return self.finish("error", f"pending action unresolved: {outcomes}", failed=True)

        entries, names = fetch_forum(self.canvas, log)
        candidates = find_candidates(entries, store.seen_map(), self_id)
        log("diff", entries=len(entries), candidates=len(candidates), candidate_ids=[c["id"] for c in candidates])
        if not candidates:
            return self.finish("no_post", "nothing new")

        gate_open, line = self.canvas.control_gate()
        if not gate_open:
            return self.finish("no_post", f"control gate closed ({line[:60]}); evaluation deferred")
        if not self.dry_run and store.recent_posts(store.last_hour_cutoff()) >= cfg.max_posts_per_hour:
            return self.finish("no_post", "per-hour post cap reached; evaluation deferred")

        chosen = newest(candidates, cfg.max_candidates)
        replied = store.replied_targets() | own_replied_targets(entries, self_id)
        nonce = new_nonce()
        blocks = build_thread_blocks(entries, chosen, names, self_id, replied, cfg.entry_char_limit)
        own_recent = [truncate(" ".join(strip_signature(b).split()), 160) for b in store.own_bodies(5)]
        started_recently = store.recent_posts(store.last_day_cutoff(), kind="thread") > 0
        system = system_prompt(cfg, nonce)
        user = user_prompt(nonce, blocks, own_recent, started_recently)
        log("llm_request", model=cfg.openai_model, evaluated_ids=[c["id"] for c in chosen],
            backlog_marked_seen=len(candidates) - len(chosen), prompt_chars=len(system) + len(user))

        try:
            raw, cost = call(self.complete, self.ledger, cfg, self.cycle_id, system, user)
        except BudgetExhausted as e:
            store.set_halt(f"budget exhausted: {e}")
            log("halt", reason="budget exhausted", detail=str(e))
            return self.finish("halted", f"budget exhausted: {e}")
        except BudgetRefused as e:
            log("budget_refused", scope=e.scope, detail=str(e))
            return self.finish("no_post", f"budget cap ({e.scope}): {e}")
        log("llm_response", cost_usd=round(cost, 6))

        try:
            decision = parse_decision(raw)
        except MalformedDecision as e:
            log("llm_malformed", error=str(e), raw_preview=truncate(raw or "", 300))
            return self.finish("error", f"malformed LLM output: {e}", failed=True)

        claims = calibrate(decision["claims"], cfg.wrong_confidence_threshold)
        log("decision", decision=decision["decision"], skip_reason=decision["skip_reason"], claims=claims,
            post=decision["post"])

        plan, reasons = None, []
        if decision["decision"] == "post":
            plan, reasons = check_post(decision["post"], claims, entries, self_id, store, cfg, secret_values(),
                                       threads_this_cycle)
            if reasons:
                log("post_rejected", reasons=reasons)
        self.result.update(decision=decision, claims=claims, rejected=reasons,
                           plan=plan.__dict__ if plan else None)

        if self.dry_run:
            if plan:
                summary = f"would post {plan.kind} to {plan.target_entry_id}"
            elif reasons:
                summary = f"would skip, rejected: {'; '.join(reasons)}"
            else:
                summary = f"would skip: {decision['skip_reason']}"
            return self.finish("dry_run", summary)

        # Decision, memory and intent are committed together, before any write.
        store.add_claims(self.cycle_id, claims, commit=False)
        store.mark_seen(candidates, self.cycle_id, commit=False)
        intent_id = None
        if plan:
            intent_id = store.add_action(self.cycle_id, plan.kind, plan.target_entry_id, plan.thread_root_id,
                                         plan.body, plan.message_html, plan.content_hash, commit=False)
        store.db.commit()

        if decision["decision"] == "skip":
            return self.finish("no_post", f"skip: {decision['skip_reason'] or 'no reason given'}")
        if reasons:
            return self.finish("no_post", f"rejected: {'; '.join(reasons)}")

        outcome = self.writer.execute(intent_id)
        if outcome == "confirmed":
            action = store.get_action(intent_id)
            return self.finish("posted", f"{plan.kind} to {plan.target_entry_id} confirmed as "
                                         f"entry {action['canvas_entry_id']}")
        if outcome == "gate_closed":
            return self.finish("no_post", "control gate closed at write time; intent left pending")
        if outcome == "capped":
            return self.finish("no_post", "post cap reached at write time; intent left pending")
        return self.finish("error", f"write {outcome}", failed=True)


def run_cycle(cfg: Config, dry_run: bool = False, *, canvas=None, complete=None, store: Store | None = None,
              sleep=time.sleep, echo: bool = True) -> dict:
    own_store = store is None
    store = store or Store(cfg.db_path)
    try:
        reason = store.halt_reason()
        if reason:
            print(f"halted: {reason}. Clear with: python -m agent.cli unhalt", file=sys.stderr)
            return {"outcome": "halted", "summary": reason}
        if cfg.course_id is None or cfg.forum_topic_id is None:
            raise RuntimeError("course_id/forum_topic_id missing from config; run scripts/discover.py")

        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        with open(cfg.lock_path, "w") as lock_file:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("another cycle holds the lock; exiting", file=sys.stderr)
                return {"outcome": "locked", "summary": "another cycle is running"}

            cycle_id = store.start_cycle("dry_run" if dry_run else "live")
            log = CycleLog(cfg.log_dir, cycle_id, echo=echo)
            log("cycle_start", mode="dry_run" if dry_run else "live", model=cfg.openai_model,
                consecutive_failures=store.consecutive_failures())
            return Cycle(cfg, store, canvas, complete, log, cycle_id, dry_run, sleep).run()
    finally:
        if own_store:
            store.close()
