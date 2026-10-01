"""Markdown evidence report built from SQLite with pandas."""

import pandas as pd

from .budget import Ledger
from .store import Store


def md_table(df: pd.DataFrame, empty: str = "_none_") -> str:
    if df.empty:
        return empty
    cols = [str(c) for c in df.columns]

    def cell(v) -> str:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return ""
        return str(v).replace("|", "\\|").replace("\n", " ")

    lines = ["| " + " | ".join(cols) + " |", "| " + " | ".join("---" for _ in cols) + " |"]
    for row in df.itertuples(index=False):
        lines.append("| " + " | ".join(cell(v) for v in row) + " |")
    return "\n".join(lines)


def entry_link(cfg, entry_id) -> str:
    if entry_id is None or pd.isna(entry_id):
        return ""
    return (f"{cfg.canvas_base_url}/courses/{cfg.course_id}/discussion_topics/{cfg.forum_topic_id}"
            f"?entry_id={int(entry_id)}")


def build_report(cfg, store: Store) -> str:
    db = store.db
    cycles = pd.read_sql_query("SELECT * FROM cycles", db)
    actions = pd.read_sql_query("SELECT * FROM actions", db)
    events = pd.read_sql_query("SELECT * FROM action_events", db)
    spend = pd.read_sql_query("SELECT * FROM spend", db)
    claims = pd.read_sql_query("SELECT * FROM claims", db)
    ledger = Ledger(store, cfg)

    out = ["# Footnote activity report", ""]
    out.append(f"- Halted: {store.halt_reason() or 'no'}")
    out.append(f"- Consecutive failures: {store.consecutive_failures()}")
    out.append(f"- Cycles recorded: {len(cycles)}")
    out.append(f"- Lifetime OpenAI spend: ${ledger.lifetime_spend():.4f} of ${cfg.lifetime_cap_usd:.2f} cap "
               f"(includes ${cfg.carryover_spend_usd:.4f} carried over from development)")
    out.append("")

    out.append("## Cycle outcomes")
    if not cycles.empty:
        counts = cycles.groupby(["mode", "outcome"]).size().reset_index(name="cycles")
        out.append(md_table(counts.sort_values(["mode", "cycles"], ascending=[True, False])))
    else:
        out.append("_none_")
    out.append("")

    out.append("## All cycles")
    if not cycles.empty:
        table = cycles.sort_values("started_at")[["cycle_id", "mode", "started_at", "ended_at", "outcome",
                                                   "posts_made", "decision_summary"]]
        out.append(md_table(table))
    else:
        out.append("_none_")
    out.append("")

    out.append("## No-post cycles and reasons")
    no_post = cycles[cycles["outcome"] == "no_post"] if not cycles.empty else cycles
    if not no_post.empty:
        reasons = no_post.assign(reason=no_post["decision_summary"].str.extract(r"^([^:;(]+)", expand=False)
                                 .str.strip())
        by_reason = reasons.groupby("reason").size().reset_index(name="cycles").sort_values("cycles",
                                                                                              ascending=False)
        out.append(md_table(by_reason))
        out.append("")
        out.append(md_table(no_post.sort_values("started_at")[["cycle_id", "started_at", "decision_summary"]]))
    else:
        out.append("_none_")
    out.append("")

    out.append("## Posts")
    if not actions.empty:
        posts = actions.sort_values("created_at")
        posts["link"] = [entry_link(cfg, eid) for eid in posts["canvas_entry_id"]]
        posts["preview"] = posts["body"].str.slice(0, 90)
        out.append(md_table(posts[["created_at", "kind", "target_entry_id", "status", "canvas_entry_id",
                                   "attempts", "confirmed_at", "note", "link", "preview"]]))
    else:
        out.append("_none_")
    out.append("")

    out.append("## Fault-injection recovery trail (drop_ack_once)")
    faulted = events[events["event"] == "ack_lost_fault"]["intent_id"].unique() if not events.empty else []
    if len(faulted):
        trail = events[events["intent_id"].isin(faulted)].sort_values(["intent_id", "at", "id"])
        for intent_id, group in trail.groupby("intent_id", sort=False):
            action = actions[actions["intent_id"] == intent_id]
            status = action["status"].iloc[0] if not action.empty else "?"
            entry_id = action["canvas_entry_id"].iloc[0] if not action.empty else None
            out.append(f"Intent `{intent_id}`: final status **{status}**, entry {entry_link(cfg, entry_id)}")
            out.append("")
            out.append(md_table(group[["at", "cycle_id", "event", "detail"]]))
            out.append("")
        duplicates = actions[actions["intent_id"].isin(faulted)]
        out.append(f"Entries created for faulted intents: {duplicates['canvas_entry_id'].notna().sum()} "
                   f"(one per intent means no duplicate).")
    else:
        out.append("_fault not triggered yet_")
    out.append("")

    out.append("## Claims checked")
    if not claims.empty:
        verdicts = claims.groupby(["kind", "verdict"]).size().reset_index(name="claims")
        out.append(md_table(verdicts.sort_values("claims", ascending=False)))
    else:
        out.append("_none_")
    out.append("")

    out.append("## OpenAI spend")
    if not spend.empty:
        daily = spend.assign(day=spend["at"].str.slice(0, 10)).groupby("day").agg(
            calls=("call_id", "count"), input_tokens=("input_tokens", "sum"),
            output_tokens=("output_tokens", "sum"), cost_usd=("cost_usd", "sum")).reset_index()
        out.append(md_table(daily.sort_values("day").round({"cost_usd": 6})))
    else:
        out.append("_no calls yet_")
    out.append("")
    return "\n".join(out)
