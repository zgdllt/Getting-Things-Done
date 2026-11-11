"""Board corrections: private learning, local card state, and one evidence log per action."""
from __future__ import annotations

import datetime as dt
import json
import os
from typing import Any

import gtd
import gtd_board as board
import gtd_board_merge
import gtd_memory
import gtd_threads


def adjust_weight(prefs: dict[str, Any], project: str | None, delta: float) -> None:
    """Apply bounded feedback to a normalized project, if the card has one."""
    if project:
        prefs["weights"][project] = round(max(-0.5, min(0.5, prefs["weights"].get(project, 0.0) + delta)), 6)


def set_status(state: gtd.State, item: board.BoardItem, value: str,
               prefs: dict[str, Any], now: dt.datetime) -> None:
    """Change project memory or local card visibility without modifying native tasks."""
    identity = item["id"]
    if value in ("tool", "blocked") and item["kind"] != "project":
        raise ValueError("tool and blocked statuses apply only to projects")
    if value == "not_important":
        adjust_weight(prefs, item["project"], -0.2)
        for alias in [identity, *item.get("merged_ids", [])]:
            prefs["hidden"][alias] = (now + dt.timedelta(days=30)).isoformat()
        return
    if item["kind"] == "project":
        entries = gtd_memory.projects(state)
        name = next((name for name in entries if gtd_threads.norm(name) == item["project"]), None)
        if name is None:
            # A scanned project's title is its original spelling (no paused-plan suffix).
            name = item["title"]
        entry = entries.get(name, {})
        stage = {"doing": "active", "done": "done", "tool": "tool", "blocked": "blocked"}[value]
        entry = gtd_memory.record(state, name, stage, entry.get("note") or "", entry.get("next_step") or "",
                                  entry.get("next_at"), entry.get("related"), source="board_feedback")
    else:
        entry = {}
    if value in ("done", "tool"):
        for alias in item.get("merged_ids", []):
            prefs["hidden"][alias] = None
        if item["kind"] != "project":
            prefs["hidden"][identity] = None
        prefs["statuses"].pop(identity, None)
        prefs["pins"].pop(identity, None)
    elif value == "doing":
        prefs["statuses"][identity] = {"status": "doing", "until": (now + dt.timedelta(days=1)).isoformat(),
                                       "memory_at": entry.get("updated_at")}
    else:
        prefs["statuses"].pop(identity, None)


def record_feedback(state: gtd.State, item_id: str, action: str, value: Any = None,
                    now: dt.datetime | None = None) -> dict[str, Any]:
    """Validate and apply one correction, logging the card's scores before the action."""
    now = board.local_time(now)
    if action == "move":
        if isinstance(value, bool) or str(value) not in ("1", "2", "3", "4"):
            raise ValueError("move requires a quadrant from 1 to 4")
        value = int(value)
    elif action == "status":
        if value not in ("done", "tool", "blocked", "not_important", "doing"):
            raise ValueError("unknown status feedback")
    elif action == "notification":
        if value not in ("start", "done", "not_important", "later"):
            raise ValueError("unknown notification feedback")
    else:
        raise ValueError("unknown feedback action")
    previous = board.read_json(state.base / "board.json")
    current = board.build(state, now, refresh_activity=False)
    item = gtd_board_merge.find(current["items"], item_id)
    if item is None and action == "notification":
        notified = board.read_json(state.base / "memory/notified.json")
        item = gtd_board_merge.find(list(notified.get("cards", {}).values()), item_id) or gtd_board_merge.find(
            previous.get("items", []), item_id)
    if item is None:
        raise ValueError(f"unknown or hidden board item: {item_id}")
    item_id = item["id"]
    prefs = board.preferences(state)
    if action == "move":
        old_important, new_important = item["quadrant"] in (1, 2), value in (1, 2)
        if old_important != new_important:
            adjust_weight(prefs, item["project"], 0.1 if new_important else -0.1)
        prefs["pins"][item_id] = {"quadrant": value, "fingerprint": board.fingerprint(item)}
    elif action == "notification" and value == "later":
        prefs["snoozed"][item_id] = (now + dt.timedelta(hours=4)).isoformat()
    else:
        status = "doing" if action == "notification" and value == "start" else value
        set_status(state, item, status, prefs, now)
        prefs["snoozed"].pop(item_id, None)
    gtd.atomic_json(state.base / "memory/preferences.json", prefs)
    event = {"timestamp": now.isoformat(), "item_id": item_id, "project": item["project"],
             "action": action, "value": value, "quadrant": item["quadrant"],
             "importance": item["importance"], "urgency": item["urgency"]}
    path = gtd_memory.folder(state) / "feedback.jsonl"
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")
    board.build(state, now, refresh_activity=False)
    return event
