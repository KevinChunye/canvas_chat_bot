"""Idempotent writes on an API without idempotency keys.

    1. an `actions` row is committed as pending (with the content hash) before any POST;
    2. POST;
    3. verify by re-fetching the entry (author + content hash) before marking it confirmed;
    4. if the POST outcome is unknown (timeout, connection error, 5xx, malformed body, lost ack),
       look for our entry in the target thread first; retry only if it is not there;
    5. every cycle starts by reconciling all pending rows the same way.
An action that already has a canvas_entry_id is never POSTed again.
"""

import random
import time
from datetime import timedelta

from .canvas import CanvasError, CanvasTransientError, GateClosed, LostAck
from .store import parse_time
from .text import content_hash

CLOCK_SKEW = timedelta(minutes=10)


class Writer:
    def __init__(self, canvas, store, cfg, self_id: int, cycle_id: str, log, sleep=time.sleep):
        self.canvas = canvas
        self.store = store
        self.cfg = cfg
        self.self_id = self_id
        self.cycle_id = cycle_id
        self.log = log
        self.sleep = sleep
        self.intents_posted = set()  # distinct intents POSTed in this cycle

    # ------------------------------------------------------------------ matching

    def _matches(self, action, raw: dict) -> bool:
        if not isinstance(raw, dict) or raw.get("user_id") is None:
            return False
        if int(raw["user_id"]) != self.self_id:
            return False
        if content_hash(raw.get("message") or "") != action["content_hash"]:
            return False
        parent = int(raw["parent_id"]) if raw.get("parent_id") else None
        if parent != action["target_entry_id"]:
            return False
        created = parse_time(raw.get("created_at"))
        intent_time = parse_time(action["created_at"])
        if created and intent_time and created < intent_time - CLOCK_SKEW:
            return False
        return True

    def lookup(self, action) -> int | None:
        """Search the target thread (fresh endpoints, not the cached view) for our entry."""
        if action["kind"] == "reply":
            candidates = self.canvas.entry_replies(action["thread_root_id"])
        else:
            candidates = self.canvas.top_level_entries()
        taken = self.store.claimed_canvas_ids()
        for raw in candidates:
            if self._matches(action, raw) and int(raw["id"]) not in taken:
                return int(raw["id"])
        return None

    def lookup_with_backoff(self, action, attempts: int = 3) -> int | None:
        for i in range(attempts):
            found = self.lookup(action)
            if found is not None:
                return found
            if i < attempts - 1:
                self.sleep(2 ** (i + 1) + random.uniform(0, 1))
        return None

    def verify(self, action, canvas_entry_id: int, attempts: int = 4) -> bool:
        """Re-fetch until the entry appears with our author id and the same content hash."""
        for i in range(attempts):
            try:
                found = self.canvas.entry_list([canvas_entry_id])
            except CanvasTransientError as e:
                self.log("verify_fetch_error", intent_id=action["intent_id"], error=str(e))
                found = []
            if any(int(raw.get("id", -1)) == canvas_entry_id and self._matches(action, raw) for raw in found):
                return True
            if i < attempts - 1:
                self.sleep(2 ** i + random.uniform(0, 1))
        return False

    # ------------------------------------------------------------------ writing

    def _event(self, intent_id: str, event: str, **detail) -> None:
        self.store.add_action_event(intent_id, self.cycle_id, event, detail)
        self.log(f"action_{event}", intent_id=intent_id, **detail)

    def _confirm(self, action, canvas_entry_id: int, note: str) -> str:
        self.store.confirm_action(action["intent_id"], canvas_entry_id, note)
        self._event(action["intent_id"], "confirmed", canvas_entry_id=canvas_entry_id, note=note)
        return "confirmed"

    def can_post_now(self, intent_id: str) -> str | None:
        """Hard caps, checked right before every POST. Returns a reason when blocked."""
        if intent_id not in self.intents_posted and len(self.intents_posted) >= self.cfg.max_posts_per_cycle:
            return "per-cycle post cap reached"
        if self.store.recent_posts(self.store.last_hour_cutoff(), exclude=intent_id) >= self.cfg.max_posts_per_hour:
            return "per-hour post cap reached"
        return None

    def execute(self, intent_id: str) -> str:
        """POST a pending action. Returns confirmed | gate_closed | capped | rejected | unverified | failed."""
        action = self.store.get_action(intent_id)
        if action["status"] != "pending" or action["canvas_entry_id"] is not None:
            raise RuntimeError(f"execute called on non-postable action {intent_id}")

        for attempt in range(1, self.cfg.max_post_attempts + 1):
            blocked = self.can_post_now(intent_id)
            if blocked:
                self._event(intent_id, "blocked", reason=blocked)
                return "capped"
            self.store.bump_attempts(intent_id)
            self.intents_posted.add(intent_id)
            self._event(intent_id, "post_attempt", attempt=attempt, target=action["target_entry_id"])
            try:
                data = self.canvas.create_entry(self.cfg.forum_topic_id, action["message_html"],
                                                action["target_entry_id"])
            except GateClosed as e:
                self._event(intent_id, "gate_closed", line=str(e)[:80])
                return "gate_closed"
            except LostAck:
                self._event(intent_id, "ack_lost_fault",
                            note="drop_ack_once fired: POST sent, response discarded; leaving pending")
                raise
            except CanvasError as e:
                # A definite 4xx (e.g. the target was deleted): nothing was created, retrying won't help.
                self.store.abandon_action(intent_id, f"rejected by Canvas: {e}")
                self._event(intent_id, "abandoned", reason=f"rejected by Canvas: {e}")
                return "rejected"
            except CanvasTransientError as e:
                self._event(intent_id, "outcome_unknown", attempt=attempt, error=f"{type(e).__name__}: {e}")
                found = self.lookup_with_backoff(action)
                if found is not None:
                    self._event(intent_id, "reconcile_found", canvas_entry_id=found)
                    return self._confirm(action, found, "reconciled after unknown outcome")
                if attempt == self.cfg.max_post_attempts:
                    break
                delay = 2 ** attempt + random.uniform(0, 1)
                self._event(intent_id, "reconcile_absent_retrying", delay=round(delay, 2))
                self.sleep(delay)
                continue

            canvas_entry_id = int(data["id"])
            self.store.set_canvas_entry(intent_id, canvas_entry_id)
            self._event(intent_id, "post_acknowledged", canvas_entry_id=canvas_entry_id)
            if self.verify(action, canvas_entry_id):
                return self._confirm(action, canvas_entry_id, "verified after post")
            self._event(intent_id, "verify_failed", canvas_entry_id=canvas_entry_id)
            return "unverified"

        self._event(intent_id, "attempts_exhausted")
        return "failed"

    def reconcile_pending(self) -> list[str]:
        """Resolve every pending action before doing anything new. Returns per-action outcomes."""
        outcomes = []
        max_age = timedelta(hours=self.cfg.pending_max_age_hours)
        for action in self.store.pending_actions():
            intent_id = action["intent_id"]
            age = self.store.clock() - parse_time(action["created_at"])
            self._event(intent_id, "reconcile_start", age_minutes=round(age.total_seconds() / 60, 1),
                        canvas_entry_id=action["canvas_entry_id"], attempts=action["attempts"])

            if action["canvas_entry_id"] is not None:
                if self.verify(action, action["canvas_entry_id"]):
                    outcomes.append(self._confirm(action, action["canvas_entry_id"], "verified at reconcile"))
                elif age > max_age:
                    self.store.abandon_action(intent_id, "acknowledged but never verified")
                    self._event(intent_id, "abandoned", reason="acknowledged but never verified")
                    outcomes.append("abandoned")
                else:
                    outcomes.append("unverified")
                continue

            found = self.lookup_with_backoff(action)
            if found is not None:
                self._event(intent_id, "reconcile_found", canvas_entry_id=found)
                outcomes.append(self._confirm(action, found, "reconciled at cycle start"))
                continue
            if age > max_age:
                self.store.abandon_action(intent_id, "never appeared and too old to retry")
                self._event(intent_id, "abandoned", reason="never appeared and too old to retry")
                outcomes.append("abandoned")
                continue
            self._event(intent_id, "reconcile_absent_retrying")
            outcomes.append(self.execute(intent_id))
        return outcomes
