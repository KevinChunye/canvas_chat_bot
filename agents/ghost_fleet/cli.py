"""Command line entry point.

    Practice on your own machine (no Canvas, no token):
    python -m agents.ghost_fleet.cli sandbox reset                  fresh practice forum with the offer posted
    python -m agents.ghost_fleet.cli sandbox reply NAME "FIRE B5 C5"   a reply from player NAME
    python -m agents.ghost_fleet.cli sandbox turn                   run the next hour's turn, print the TURN post
    python -m agents.ghost_fleet.cli sandbox auto [--turns N]       simulated players fire for N turns

    Against the real forum (CANVAS_API_KEY in the environment):
    python -m agents.ghost_fleet.cli dry-run [--at TIME]   read the forum, print the TURN post it would make; no writes
    python -m agents.ghost_fleet.cli post-offer            post offer.txt as a new thread, once
    python -m agents.ghost_fleet.cli set-offer ENTRY_ID    use an offer you already posted
    python -m agents.ghost_fleet.cli run-turn              one live turn (what the hourly schedule runs)
    python -m agents.ghost_fleet.cli status                halt state, offer, games, pending posts, recent runs
    python -m agents.ghost_fleet.cli unhalt                clear the halt flag and the failure count
"""

import argparse
import json
import sys
from datetime import datetime

from . import sandbox
from .config import load_config
from .runner import post_offer, run_turn, turn_of
from .store import Store


def cmd_status(cfg) -> int:
    store = Store(cfg.db_path)
    print(f"database: {cfg.db_path}")
    print(f"forum: course {cfg.course_id}, topic {cfg.forum_topic_id}, offer {cfg.offer_entry_id or store.offer_id()}")
    print(f"season: {cfg.season_start.isoformat()} to {cfg.season_end.isoformat()}; "
          f"turn now {turn_of(cfg, store.clock())}")
    print(f"halted: {store.halt_reason() or 'no'}; consecutive failures: {store.consecutive_failures()}")
    for g in store.games():
        shots = store.db.execute("SELECT COUNT(*) FROM shots WHERE game_id = ?", (g["id"],)).fetchone()[0]
        state = f"ended turn {g['end_turn']}" if g["end_turn"] else "in play"
        print(f"game {g['id']}: {shots} shots, {state}, commit {g['commit_hash']}")
    for row in store.pending_turns():
        print(f"pending TURN post: turn {row['turn']}, attempts {row['attempts']}, created {row['created_at']}")
    print("recent runs:")
    for r in store.db.execute("SELECT run_id, mode, outcome, summary FROM runs ORDER BY started_at DESC LIMIT 5"):
        print(f"  {r['run_id']} [{r['mode']}] {r['outcome']}: {r['summary']}")
    store.close()
    return 0


def show(result: dict) -> None:
    print(f"[{result.get('outcome')}] {result.get('summary')}")
    if result.get("post"):
        print()
        print(result["post"])
        print()


def cmd_sandbox(cfg, args) -> int:
    if args.action == "reset":
        print(f"fresh practice forum in {sandbox.reset(cfg)}")
    elif args.action == "reply":
        if not args.name or not args.text:
            print('usage: sandbox reply NAME "FIRE B5 C5 ..."', file=sys.stderr)
            return 2
        print(f"reply {sandbox.reply(cfg, args.name, args.text)} from {args.name}")
    elif args.action == "turn":
        show(sandbox.next_turn(cfg))
    elif args.action == "auto":
        for result in sandbox.auto(cfg, args.turns, args.seed):
            show(result)
    if args.action in ("turn", "auto"):
        print(f"spectator: python -m http.server -d {sandbox.SANDBOX_DIR} 8000, then open "
              "http://localhost:8000/spectator.html")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agents.ghost_fleet.cli", description="Ghost Fleet house")
    parser.add_argument("--config", default=None, help="path to config.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run-turn")
    dry = sub.add_parser("dry-run")
    dry.add_argument("--at", default=None, help="pretend it is this time, e.g. 2026-10-12T10:00-04:00")
    sub.add_parser("post-offer")
    set_offer = sub.add_parser("set-offer")
    set_offer.add_argument("entry_id", type=int)
    sub.add_parser("status")
    sub.add_parser("unhalt")
    sand = sub.add_parser("sandbox")
    sand.add_argument("action", choices=["reset", "reply", "turn", "auto"])
    sand.add_argument("name", nargs="?")
    sand.add_argument("text", nargs="?")
    sand.add_argument("--turns", type=int, default=6)
    sand.add_argument("--seed", type=int, default=None)
    args = parser.parse_args(argv)
    try:
        return run(args)
    except RuntimeError as e:             # setup problems: missing ids, token, offer
        print(f"error: {e}", file=sys.stderr)
        return 1


def run(args) -> int:
    cfg = load_config(args.config)

    if args.command == "sandbox":
        return cmd_sandbox(cfg, args)
    if args.command == "status":
        return cmd_status(cfg)
    if args.command == "run-turn":
        result = run_turn(cfg)
        print(json.dumps({k: result.get(k) for k in ("run_id", "turn", "outcome", "summary")}))
        return 0 if result.get("outcome") not in ("error",) else 1
    if args.command == "dry-run":
        store = None
        if args.at:
            at = datetime.fromisoformat(args.at)
            store = Store(cfg.db_path, clock=lambda: at)
        try:
            result = run_turn(cfg, dry_run=True, store=store, echo=False)
        finally:
            if store is not None:
                store.close()
        show(result)
        return 0 if result.get("outcome") in ("dry_run", "idle") else 1
    store = Store(cfg.db_path)
    try:
        if args.command == "post-offer":
            entry_id = post_offer(cfg, store=store)
            print(f"offer posted as entry {entry_id}; the first run-turn in the season opens game 1")
        elif args.command == "set-offer":
            store.set_state("offer_entry_id", str(args.entry_id))
            print(f"offer set to entry {args.entry_id}")
        elif args.command == "unhalt":
            previous = store.halt_reason()
            store.clear_halt()
            print(f"halt cleared (was: {previous or 'not halted'}); consecutive failures reset to 0")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
