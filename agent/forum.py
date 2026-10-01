"""Reading the forum: flatten Canvas threads into one entry table, detect a
lagging cached view, diff against memory, and build the lean LLM context."""

import pandas as pd

from .store import parse_time
from .text import content_hash, strip_html, truncate


def _entry(raw: dict, root_id: int | None) -> dict:
    message = raw.get("message") or ""
    return {
        "id": int(raw["id"]),
        "parent_id": int(raw["parent_id"]) if raw.get("parent_id") else None,
        "root_id": root_id,
        "user_id": int(raw["user_id"]) if raw.get("user_id") is not None else None,
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at") or raw.get("created_at"),
        "deleted": bool(raw.get("deleted")),
        "text": strip_html(message),
        "text_hash": content_hash(message),
    }


def _fill_roots(entries: dict[int, dict]) -> None:
    for e in entries.values():
        node = e
        seen = set()
        while node["parent_id"] is not None and node["parent_id"] in entries and node["id"] not in seen:
            seen.add(node["id"])
            node = entries[node["parent_id"]]
        e["root_id"] = node["id"] if node["parent_id"] is None else (e["root_id"] or node["id"])


def flatten_view(view: dict) -> dict[int, dict]:
    entries: dict[int, dict] = {}

    def walk(items, root_id):
        for raw in items or []:
            if not isinstance(raw, dict) or "id" not in raw:
                continue
            this_root = root_id if root_id is not None else int(raw["id"])
            entries[int(raw["id"])] = _entry(raw, this_root)
            walk(raw.get("replies"), this_root)

    walk(view.get("view"), None)
    for raw in view.get("new_entries") or []:
        if isinstance(raw, dict) and "id" in raw:
            entries[int(raw["id"])] = _entry(raw, None)
    _fill_roots(entries)
    return entries


def flatten_api(top_level: list, replies_by_root: dict[int, list]) -> dict[int, dict]:
    entries: dict[int, dict] = {}
    for raw in top_level:
        root_id = int(raw["id"])
        entries[root_id] = _entry(raw, root_id)
        for reply in replies_by_root.get(root_id, []):
            entries[int(reply["id"])] = _entry(reply, root_id)
    _fill_roots(entries)
    return entries


def view_is_stale(topic: dict, entries: dict[int, dict]) -> bool:
    """The cached /view can lag. Compare it with the topic's own counters."""
    live = [e for e in entries.values() if not e["deleted"]]
    expected = topic.get("discussion_subentry_count")
    if isinstance(expected, int) and len(live) < expected:
        return True
    last_reply = parse_time(topic.get("last_reply_at"))
    newest = max((parse_time(e["created_at"]) for e in live if e["created_at"]), default=None)
    if last_reply and (newest is None or newest < last_reply):
        return True
    return False


def fetch_forum(canvas, log) -> tuple[dict[int, dict], dict[int, str]]:
    """Entries of FORUM_TOPIC_ID keyed by id, plus participant display names."""
    topic = canvas.topic()
    view = canvas.topic_view()
    entries = flatten_view(view)
    names = {int(p["id"]): str(p.get("display_name") or "") for p in view.get("participants") or []
             if isinstance(p, dict) and "id" in p}
    stale = view_is_stale(topic, entries)
    log("forum_fetched", source="view", entries=len(entries), stale=stale,
        topic_count=topic.get("discussion_subentry_count"))
    if stale:
        top_level = canvas.top_level_entries()
        replies = {int(e["id"]): canvas.entry_replies(int(e["id"])) for e in top_level}
        fresh = flatten_api(top_level, replies)
        for raw in top_level:
            uid = raw.get("user_id")
            if uid is not None and int(uid) not in names and raw.get("user_name"):
                names[int(uid)] = str(raw["user_name"])
        entries.update(fresh)
        _fill_roots(entries)
        log("forum_fetched", source="entries_api", entries=len(entries))
    return entries, names


def own_replied_targets(entries: dict[int, dict], self_id: int) -> set[int]:
    return {e["parent_id"] for e in entries.values() if e["user_id"] == self_id and e["parent_id"] is not None}


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


def ancestors(entries: dict[int, dict], entry_id: int) -> list[dict]:
    """Parent chain from the thread root down to (not including) the entry."""
    chain = []
    node = entries.get(entry_id)
    seen = set()
    while node and node["parent_id"] is not None and node["parent_id"] in entries and node["id"] not in seen:
        seen.add(node["id"])
        node = entries[node["parent_id"]]
        chain.append(node)
    return list(reversed(chain))


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
