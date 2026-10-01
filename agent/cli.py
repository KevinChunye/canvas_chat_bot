"""Command line entry point.

    python -m agent.cli run-cycle          one live cycle, then exit
    python -m agent.cli dry-run            full cycle with a real LLM decision, no writes, no memory changes
    python -m agent.cli status             halt state, failures, pending actions, spend
    python -m agent.cli unhalt             clear the halt flag and the failure count
    python -m agent.cli report [--out F]   markdown evidence report
    python -m agent.cli fault drop_ack_once   arm the one-shot lost-acknowledgement fault
"""

import argparse
import json
import sys

from .budget import Ledger
from .config import load_config
from .cycle import run_cycle
from .report import build_report
from .store import Store

FAULTS = {"drop_ack_once"}


def cmd_status(cfg) -> int:
    store = Store(cfg.db_path)
    ledger = Ledger(store, cfg)
    print(f"database: {cfg.db_path}")
    print(f"forum: course {cfg.course_id}, topic {cfg.forum_topic_id}")
    print(f"halted: {store.halt_reason() or 'no'}")
    print(f"consecutive failures: {store.consecutive_failures()}")
    print(f"lifetime spend: ${ledger.lifetime_spend():.4f} / ${cfg.lifetime_cap_usd:.2f}; "
          f"today: ${ledger.day_spend():.4f} / ${cfg.day_cap_usd:.2f}")
    for name in sorted(FAULTS):
        print(f"fault {name}: {'armed' if store.get_state(f'fault.{name}') == '1' else 'off'}")
    pending = store.pending_actions()
    print(f"pending actions: {len(pending)}")
    for a in pending:
        print(f"  {a['intent_id']} {a['kind']} -> {a['target_entry_id']} attempts={a['attempts']} "
              f"created={a['created_at']}")
    rows = store.db.execute("SELECT cycle_id, mode, outcome, decision_summary FROM cycles "
                            "ORDER BY started_at DESC LIMIT 5").fetchall()
    print("recent cycles:")
    for r in rows:
        print(f"  {r['cycle_id']} [{r['mode']}] {r['outcome']}: {r['decision_summary']}")
    store.close()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent.cli", description="Footnote, a forum agent")
    parser.add_argument("--config", default=None, help="path to config.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run-cycle")
    sub.add_parser("dry-run")
    sub.add_parser("status")
    sub.add_parser("unhalt")
    report = sub.add_parser("report")
    report.add_argument("--out", default=None, help="write markdown here instead of stdout")
    fault = sub.add_parser("fault")
    fault.add_argument("name", choices=sorted(FAULTS))
    fault.add_argument("--off", action="store_true", help="disarm instead of arm")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)

    if args.command == "run-cycle":
        result = run_cycle(cfg)
        print(json.dumps({k: result.get(k) for k in ("cycle_id", "outcome", "summary", "posts_made")}))
        return 0 if result.get("outcome") not in ("error",) else 1
    if args.command == "dry-run":
        result = run_cycle(cfg, dry_run=True)
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 0 if result.get("outcome") == "dry_run" or result.get("summary") == "nothing new" else 1
    if args.command == "status":
        return cmd_status(cfg)
    if args.command == "unhalt":
        store = Store(cfg.db_path)
        previous = store.halt_reason()
        store.clear_halt()
        store.close()
        print(f"halt cleared (was: {previous or 'not halted'}); consecutive failures reset to 0")
        return 0
    if args.command == "report":
        store = Store(cfg.db_path)
        text = build_report(cfg, store)
        store.close()
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(text)
            print(f"wrote {args.out}")
        else:
            print(text)
        return 0
    if args.command == "fault":
        store = Store(cfg.db_path)
        store.set_state(f"fault.{args.name}", None if args.off else "1")
        store.close()
        print(f"fault {args.name} {'disarmed' if args.off else 'armed: fires once on the next real POST'}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
