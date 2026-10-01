import dataclasses

import pytest

from agent.budget import BudgetExhausted, BudgetRefused, Ledger
from agent.config import load_config


def test_reserve_books_worst_case_then_actual(cfg, store):
    ledger = Ledger(store, cfg)
    call_id = ledger.reserve("c1", "test-model", "x" * 3000, 1000)
    worst = store.spend_total()
    assert worst == pytest.approx(ledger.cost("test-model", 3000 // 3 + 50, 1000))
    ledger.record(call_id, "test-model", 900, 100)
    assert store.spend_total() == pytest.approx(ledger.cost("test-model", 900, 100))
    assert store.spend_total() < worst


def test_lifetime_cap_refuses_the_call_that_would_exceed_it(cfg, store):
    ledger = Ledger(store, cfg)
    store.reserve_spend("old", "test-model", 4.9995, 0, 0)
    with pytest.raises(BudgetExhausted):
        ledger.reserve("c1", "test-model", "x" * 3000, 1000)
    # a smaller call still fits
    small = dataclasses.replace(cfg, day_cap_usd=10, cycle_cap_usd=10)
    Ledger(store, small).reserve("c1", "test-model", "x" * 30, 10)


def test_carryover_counts_toward_lifetime_cap(cfg, store):
    capped = dataclasses.replace(cfg, carryover_spend_usd=4.9999)
    with pytest.raises(BudgetExhausted):
        Ledger(store, capped).reserve("c1", "test-model", "x" * 3000, 1000)


def test_day_and_cycle_caps_refuse_without_exhausting(cfg, store):
    store.reserve_spend("today", "test-model", 0.4999, 0, 0)
    with pytest.raises(BudgetRefused) as e:
        Ledger(store, cfg).reserve("c1", "test-model", "x" * 3000, 1000)
    assert e.value.scope == "day" and not isinstance(e.value, BudgetExhausted)

    fresh = dataclasses.replace(cfg, day_cap_usd=10)
    store.reserve_spend("cyc", "test-model", 0.0499, 0, 0)
    store.db.execute("UPDATE spend SET cycle_id = 'c2' WHERE call_id IS NOT NULL")
    store.db.commit()
    with pytest.raises(BudgetRefused) as e:
        Ledger(store, fresh).reserve("c2", "test-model", "x" * 3000, 1000)
    assert e.value.scope == "cycle"


def test_unpriced_model_is_refused(cfg, store):
    with pytest.raises(BudgetRefused):
        Ledger(store, cfg).reserve("c1", "gpt-unknown", "hello", 10)


def test_config_cannot_raise_hard_caps(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text("[budget]\nlifetime_cap_usd = 50\n[limits]\nmax_posts_per_hour = 30\nmax_posts_per_cycle = 9\n")
    cfg = load_config(path)
    assert cfg.lifetime_cap_usd == 5.00
    assert cfg.max_posts_per_hour == 3
    assert cfg.max_posts_per_cycle == 2
