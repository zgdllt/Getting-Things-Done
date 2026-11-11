"""Notification decisions and receipts only; this module never delivers anything."""
from __future__ import annotations

import datetime as dt
from typing import Any

import gtd
import gtd_board as board
import gtd_threads

ACTIONS = ["开始", "已完成", "不重要", "稍后"]


def quiet_time(config: dict[str, Any], now: dt.datetime) -> bool:
    """Honor a local quiet interval; equal endpoints disable it."""
    hours = config.get("quiet_hours", {"start": "22:00", "end": "09:00"})
    if hours is None or hours is False:
        return False
    start = dt.time.fromisoformat(hours.get("start", "22:00"))
    end = dt.time.fromisoformat(hours.get("end", "09:00"))
    clock = now.timetz().replace(tzinfo=None)
    return start <= clock < end if start <= end else clock >= start or clock < end


def log_state(state: gtd.State) -> dict[str, Any]:
    """Read notification receipts and the most recent preview metadata."""
    result = board.read_json(state.base / "memory/notified.json")
    for key in ("items", "nudge_days", "selected", "cards"):
        result.setdefault(key, {})
    return result


def recently_notified(log: dict[str, Any], identity: str, now: dt.datetime) -> bool:
    """Apply the twelve-hour delivery deduplication window to every card."""
    stamp = board.timestamp(log["items"].get(identity))
    return bool(stamp and now - stamp < dt.timedelta(hours=12))


def compact_body(text: str) -> str:
    """Keep at most two short content lines, removing the old inline action prompts."""
    lines = [line.split("[")[0].strip() for line in text.splitlines() if not line.startswith("[")]
    return "\n".join(line if len(line) <= 70 else line[:69] + "…" for line in [line for line in lines if line][:2])


def select_notifications(state: gtd.State, value: dict[str, Any], now: dt.datetime) -> list[dict[str, Any]]:
    """Log a preview of Q1 alerts and one ranked Q2 nudge, without consuming receipts."""
    now = board.local_time(now)
    log, prefs = log_state(state), board.preferences(state)
    selected: list[dict[str, Any]] = []
    day = now.date().isoformat()
    if not quiet_time(state.config(), now):
        eligible = [item for item in value["items"]
                    if not any(board.suppressed(prefs, identity, now, snoozed=True)
                               or recently_notified(log, identity, now)
                               for identity in [item["id"], *item.get("merged_ids", [])])]
        for item in eligible:
            if item["quadrant"] == 1:
                text = "；".join(item["urgency_reasons"]) + "\n" + (item["remaining"] or "可以开始处理这件事。")
                selected.append({"item_id": item["id"], "title": item["title"],
                                 "body": compact_body(text), "actions": ACTIONS[:]})
        if day not in log["nudge_days"]:
            inferred = board.read_json(state.base / "memory/inferred.json")
            nudges = gtd_threads.rank(state, now, inferred=inferred, activity=board.activity(state, now, False))
            projects = {item["project"]: item for item in eligible if item["kind"] == "project" and item["quadrant"] == 2}
            for nudge in nudges:
                item = projects.get(gtd_threads.norm(nudge["project"]))
                if item and nudge["kind"] in ("planned", "decide", "quiet", "confirm"):
                    lines = compact_body(gtd_threads.phrase(nudge)).splitlines()[:2]
                    selected.append({"item_id": item["id"], "title": item["title"],
                                     "body": "\n".join(lines), "actions": ACTIONS[:]})
                    break
    by_id = {item["id"]: item for item in value["items"]}
    # Keep outstanding preview metadata so acknowledging a prior preview remains possible.
    for notification in selected:
        identity = notification["item_id"]
        log["cards"][identity] = by_id[identity]
        log["selected"][identity] = {"quadrant": by_id[identity]["quadrant"], "selected_at": now.isoformat(),
                                    "nudge_day": day if by_id[identity]["quadrant"] == 2 else None}
    log["preview_at"] = now.isoformat()
    gtd.atomic_json(state.base / "memory/notified.json", log)
    return selected


def mark_notified(state: gtd.State, item_ids: list[str], now: dt.datetime) -> None:
    """Mark successfully delivered previews; only receipts consume the daily nudge."""
    now = board.local_time(now)
    log = log_state(state)
    item_ids = list(dict.fromkeys(item_ids))
    for identity in item_ids:
        selection = log["selected"].get(identity)
        if selection is None:
            raise ValueError(f"item has not been selected: {identity}")
    nudges = [identity for identity in item_ids if log["selected"][identity]["nudge_day"]]
    if len(nudges) > 1 or (nudges and now.date().isoformat() in log["nudge_days"]):
        raise ValueError("only one Q2 nudge may be marked per local day")
    for identity in item_ids:
        selection = log["selected"].pop(identity)
        log["items"][identity] = now.isoformat()
        if selection["nudge_day"]:
            log["nudge_days"][now.date().isoformat()] = identity
    gtd.atomic_json(state.base / "memory/notified.json", log)
