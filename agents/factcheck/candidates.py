"""Choosing what to evaluate: new or edited entries, never our own, and the lean
per-thread context the model sees. All forum text in it is untrusted data."""

import pandas as pd

from canvas_core.forum import ancestors
from canvas_core.text import truncate


def find_candidates(entries: dict[int, dict], seen: dict, self_id: int) -> list[dict]:
    """New entries, or entries whose text changed since we saw them. Never our own."""
    out = []
    for e in entries.values():
        if e["deleted"] or e["user_id"] == self_id or e["user_id"] is None:
            continue
        prior = seen.get(e["id"])
        if prior is None:
            out.append(e)
        elif prior["updated_at"] != e["updated_at"] and prior["text_hash"] != e["text_hash"]:
            out.append(e)
    return out


def newest(candidates: list[dict], limit: int) -> list[dict]:
    if not candidates:
        return []
    df = pd.DataFrame(candidates)[["id", "root_id", "created_at"]]
    df = df.sort_values("created_at", ascending=False).head(limit).sort_values(["root_id", "created_at"])
    by_id = {c["id"]: c for c in candidates}
    return [by_id[int(i)] for i in df["id"]]


def exchanges_with_author(entries: dict[int, dict], target_id: int, self_id: int) -> int:
    """How many times we already answered the target's author in the target's reply chain."""
    target = entries[target_id]
    author = target["user_id"]
    chain = ancestors(entries, target_id) + [target]
    count = 0
    for parent, child in zip(chain, chain[1:]):
        if child["user_id"] == self_id and parent["user_id"] == author:
            count += 1
    return count


def build_thread_blocks(entries: dict[int, dict], chosen: list[dict], names: dict[int, str], self_id: int,
                        replied: set[int], char_limit: int) -> list[str]:
    """One text block per thread: root, parent chains, then the new entries. All of it is untrusted data."""
    def label(e: dict, tag: str) -> str:
        if e["user_id"] == self_id:
            who = "you"
        else:
            who = truncate(names.get(e["user_id"], "") or f"user {e['user_id']}", 40)
        flags = []
        if e["user_id"] == self_id:
            flags.append("yours")
        if e["id"] in replied:
            flags.append("already replied")
        flag_text = f" ({', '.join(flags)})" if flags else ""
        parent = f", reply to {e['parent_id']}" if e["parent_id"] else ", thread start"
        limit = char_limit if tag == "NEW" else char_limit // 2
        return f"[{tag}] entry_id={e['id']} author={who}{parent}{flag_text}\n{truncate(e['text'], limit)}"

    df = pd.DataFrame(chosen)[["id", "root_id", "created_at"]]
    blocks = []
    for root_id, group in df.groupby("root_id", sort=True):
        new_ids = [int(i) for i in group.sort_values("created_at")["id"]]
        shown: list[int] = []
        context: list[dict] = []
        for entry_id in new_ids:
            for a in ancestors(entries, entry_id):
                if a["id"] not in shown and a["id"] not in new_ids:
                    shown.append(a["id"])
                    context.append(a)
        lines = [f"=== thread {int(root_id)} ==="]
        lines += [label(a, "context") for a in context]
        lines += [label(entries[i], "NEW") for i in new_ids]
        blocks.append("\n\n".join(lines))
    return blocks
