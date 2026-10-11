"""OpenAI spend ledger with a hard lifetime cap.

Every call reserves its worst case (estimated input + max output at configured
prices) in the spend table before it is sent, and the reservation is replaced
by actual usage afterwards. A crash mid-call therefore leaves the worst case
on the books, never less.
"""

from datetime import timezone


class BudgetRefused(Exception):
    """This call would break the per-cycle or per-day cap. Skip, try later."""

    def __init__(self, scope: str, message: str):
        super().__init__(message)
        self.scope = scope


class BudgetExhausted(BudgetRefused):
    """This call would break the lifetime cap. Halt."""

    def __init__(self, message: str):
        super().__init__("lifetime", message)


def estimate_tokens(text: str) -> int:
    """Deliberately high: ~3 characters per token plus framing overhead."""
    return len(text) // 3 + 50


class Ledger:
    def __init__(self, store, cfg):
        self.store = store
        self.cfg = cfg

    def prices(self, model: str) -> tuple[float, float]:
        entry = self.cfg.prices.get(model)
        if not entry:
            raise BudgetRefused("price", f"no configured price for model {model}; refusing to call it")
        return float(entry["input_usd_per_mtok"]), float(entry["output_usd_per_mtok"])

    def cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        price_in, price_out = self.prices(model)
        return (input_tokens * price_in + output_tokens * price_out) / 1_000_000

    def lifetime_spend(self) -> float:
        return self.store.spend_total() + self.cfg.carryover_spend_usd

    def day_spend(self) -> float:
        start = self.store.clock().astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        return self.store.spend_total(since=start)

    def reserve(self, cycle_id: str, model: str, prompt_text: str, max_output: int) -> str:
        """Check every cap against the worst case, then book the worst case. Returns call_id."""
        input_estimate = estimate_tokens(prompt_text)
        worst = self.cost(model, input_estimate, max_output)
        lifetime = self.lifetime_spend()
        if lifetime + worst > self.cfg.lifetime_cap_usd:
            raise BudgetExhausted(f"lifetime spend {lifetime:.4f} + worst case {worst:.4f} "
                                  f"> cap {self.cfg.lifetime_cap_usd:.2f}")
        day = self.day_spend()
        if day + worst > self.cfg.day_cap_usd:
            raise BudgetRefused("day", f"today's spend {day:.4f} + worst case {worst:.4f} "
                                       f"> day cap {self.cfg.day_cap_usd:.2f}")
        cycle = self.store.spend_total(cycle_id=cycle_id)
        if cycle + worst > self.cfg.cycle_cap_usd:
            raise BudgetRefused("cycle", f"cycle spend {cycle:.4f} + worst case {worst:.4f} "
                                         f"> cycle cap {self.cfg.cycle_cap_usd:.2f}")
        return self.store.reserve_spend(cycle_id, model, worst, input_estimate, max_output)

    def record(self, call_id: str, model: str, input_tokens: int, output_tokens: int) -> float:
        actual = self.cost(model, input_tokens, output_tokens)
        self.store.record_spend(call_id, input_tokens, output_tokens, actual)
        return actual
