#!/usr/bin/env python3
"""Private, deterministic four-quadrant board; never calls a model or Apple apps."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any, Literal, TypedDict

import gtd
import gtd_board_merge
import gtd_local
import gtd_memory
import gtd_threads

LOG = logging.getLogger(__name__)


class BoardItem(TypedDict):
    """A stable card assembled from local activity and authoritative task snapshots."""

    id: str
    kind: Literal["project", "task", "reminder", "event"]
    title: str
    project: str | None
    status: Literal["doing", "started", "todo"]
    due: str | None
    due_source: Literal["reminder", "calendar", "candidate", "plan"] | None
    importance: float
    importance_reasons: list[str]
    urgency: float
    urgency_reasons: list[str]
    quadrant: int
    pinned: bool
    left_off: str | None
    remaining: str | None
    updated_at: str
    activity_at: float
    parent: str | None
    merged_ids: list[str]
    top_level: bool
    children: list[str]
    children_count: int


def read_json(path: Path) -> dict[str, Any]:
    """Read an existing local JSON object without creating or refreshing it."""
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def local_time(value: dt.datetime | None = None) -> dt.datetime:
    """Use the host's local timezone, including for naive input datetimes."""
    return (value or dt.datetime.now(dt.timezone.utc)).astimezone()


def valid_due(value: Any) -> str | None:
    """Keep only explicit ISO dates or datetimes; never infer a missing deadline."""
    if not isinstance(value, str) or not value:
        return None
    try:
        if len(value) == 10:
            return dt.date.fromisoformat(value).isoformat()
        return dt.datetime.fromisoformat(value).isoformat()
    except ValueError:
        return None


def timestamp(value: Any) -> dt.datetime | None:
    """Parse a local/offset timestamp, treating date-only deadlines as end of day."""
    due = valid_due(value)
    if due is None:
        return None
    if len(due) == 10:
        return dt.datetime.combine(dt.date.fromisoformat(due), dt.time.max).astimezone()
    return dt.datetime.fromisoformat(due).astimezone()


def preferences(state: gtd.State) -> dict[str, Any]:
    """Load feedback state with empty defaults for each independently owned mapping."""
    value = read_json(state.base / "memory/preferences.json")
    for key in ("weights", "pins", "hidden", "snoozed", "statuses"):
        value.setdefault(key, {})
    return value


def fingerprint(item: BoardItem) -> dict[str, str | None]:
    """Identify the due date and status to which a user quadrant pin applies."""
    return {"due": item["due"], "status": item["status"]}


def suppressed(prefs: dict[str, Any], item_id: str, now: dt.datetime,
               snoozed: bool = False) -> bool:
    """Respect permanent/timed hides and, optionally, notification-only snoozes."""
    hidden = prefs["hidden"]
    if item_id in hidden:
        until = timestamp(hidden[item_id])
        if hidden[item_id] is None or (until and until > now):
            return True
    until = timestamp(prefs["snoozed"].get(item_id)) if snoozed else None
    return bool(until and until > now)


def activity(state: gtd.State, now: dt.datetime, refresh: bool) -> dict[str, Any]:
    """Reuse the hourly scan cache, optionally allowing its normal refresh."""
    cache = state.base / "experiments/activity-cache.json"
    if not refresh:
        return read_json(cache).get("projects", {})
    _, roots = gtd_local.configured_sources(state.config())
    return gtd_threads.scan_activity(now, gtd_threads.SCAN_DAYS, roots, cache)


def project_data(scanned: dict[str, Any], memory: dict[str, Any],
                 inferred: dict[str, Any]) -> dict[str, Any]:
    """Join project names by normalization, retaining the memory's original spelling."""
    result: dict[str, Any] = {}
    for field, source in (("activity", scanned), ("memory", memory), ("inference", inferred)):
        for name, value in source.items():
            key = gtd_threads.norm(name)
            if key:
                entry = result.setdefault(key, {"name": name})
                entry[field] = value
                if field == "memory":
                    entry["name"] = name
    return result


def own_activity(context: dict[str, Any], now: dt.datetime, days: int) -> bool:
    """Use only the scan's last own message/commit, never its assistant replies."""
    last = timestamp(context.get("activity", {}).get("last"))
    return bool(last and dt.timedelta(0) <= now - last <= dt.timedelta(days=days))


def recent_update(value: Any, now: dt.datetime) -> bool:
    """Check the inclusive elapsed seven-day candidate update window."""
    stamp = timestamp(value)
    return bool(stamp and dt.timedelta(0) <= now - stamp <= dt.timedelta(days=7))


def recency(*values: Any) -> float:
    """Order source activity/updates by actual instants, with missing values last."""
    return max((stamp.timestamp() for value in values if (stamp := timestamp(value))), default=0.0)


def importance(item: BoardItem, context: dict[str, Any], declared: dict[str, Any],
               decisions: dict[str, Any], prefs: dict[str, Any], now: dt.datetime) -> tuple[float, list[str]]:
    """Explain transparent project signals plus a bounded learned project weight."""
    score, reasons = 0.0, []
    if item["kind"] in ("reminder", "event"):
        score += 0.5
        reasons.append("正式提醒事项" if item["kind"] == "reminder" else "日历安排")
    elif item["kind"] == "task" and item["due"] and item["parent"] is None:
        score += 0.5
        reasons.append("有截止日期")
    project = item["project"]
    if project:
        if own_activity(context, now, 7):
            score += 0.5
            reasons.append("近 7 天你在做")
        elif own_activity(context, now, 14):
            score += 0.35
            reasons.append("近 8–14 天你在做")
        if gtd_threads.match(project, declared):
            score += 0.4
            reasons.append("你声明的重点项目")
        if context.get("memory", {}).get("stage") in ("active", "blocked", "paused"):
            score += 0.2
            reasons.append("项目仍在推进或计划中")
        days = len(set(context.get("activity", {}).get("dates", [])))
        if days:
            score += min(0.2, 0.2 * days / gtd_threads.SCAN_DAYS)
            reasons.append(f"近期活跃 {days} 天")
        linked = gtd_threads.match(project, decisions)
        for field, sign, label in (("kept", 1, "已保留"), ("dismissed", -1, "已忽略")):
            count = sum(decisions[key][field] for key in linked)
            if count:
                score += sign * min(0.15, 0.05 * count)
                reasons.append(f"{label} {count} 条相关事项")
        weight = prefs["weights"].get(project, 0.0)
        if weight:
            score += weight
            reasons.append(f"你的反馈权重 {weight:+.2f}")
    return round(max(0.0, min(1.0, score)), 6), reasons or ["暂无重要性依据"]


def urgency(due: str | None, source: str | None, now: dt.datetime) -> tuple[float, list[str]]:
    """Score only explicit deadlines, with local-day explanations in Chinese."""
    end = timestamp(due)
    if end is None:
        return 0.0, ["未设截止时间"]
    days = (end.date() - now.date()).days
    if end < now:
        if source == "candidate" and now - end > dt.timedelta(days=3):
            return 0.4, [f"已逾期 {-days} 天，可能已处理"]
        return 1.0, [f"已逾期 {-days} 天" if days < 0 else "已逾期（今天）"]
    if source == "plan" and days == 0:
        return 0.8, ["计划今天继续"]
    delta = end - now
    score = next((score for limit, score in ((1, 0.9), (3, 0.7), (7, 0.4))
                  if delta <= dt.timedelta(days=limit)), 0.0)
    reason = "今天截止" if days == 0 else "明天截止" if days == 1 else f"{days} 天后截止"
    return score, [reason]


def build(state: gtd.State, now: dt.datetime, refresh_activity: bool = True) -> dict[str, Any]:
    """Persist a private board using cached inference, with no model/native operations."""
    now = local_time(now)
    prefs = preferences(state)
    original_pins = dict(prefs["pins"])
    config = state.config()
    scanned = activity(state, now, refresh_activity)
    memory = gtd_memory.projects(state)
    contexts = project_data(scanned, memory, read_json(state.base / "memory/inferred.json"))
    decisions = gtd_threads.decisions(state)
    declared = {gtd_threads.norm(brief.get("title") or name): True
                for name, brief in (config.get("project_briefs") or {}).items()}
    items: list[BoardItem] = []
    projects: dict[str, BoardItem] = {}

    def add(identity: str, kind: Any, title: str, project: str | None, status: Any,
            due: Any = None, source: Any = None, context: dict[str, Any] | None = None,
            updated: Any = None) -> None:
        """Score one source card, apply local feedback, and expire changed pins."""
        project = gtd_threads.norm(project or "") or None
        context = context or contexts.get(project, {})
        due = valid_due(due)
        inference, entry = context.get("inference", {}), context.get("memory", {})
        override = prefs["statuses"].get(identity)
        if override and ((kind == "project" and override["memory_at"] != entry.get("updated_at"))
                         or timestamp(override["until"]) <= now):
            del prefs["statuses"][identity]
            override = None
        item: BoardItem = {"id": identity, "kind": kind, "title": title, "project": project,
                           "status": override["status"] if override else status, "due": due,
                           "due_source": source if due else None, "importance": 0.0,
                           "importance_reasons": [], "urgency": 0.0, "urgency_reasons": [],
                           "quadrant": 4, "pinned": False,
                           "left_off": entry.get("left_off") or inference.get("left_off") or entry.get("note"),
                           "remaining": entry.get("remaining") or entry.get("next_step") or inference.get("remaining"),
                           "updated_at": now.isoformat(), "activity_at": recency(updated),
                           "parent": projects.get(project, {}).get("id"), "merged_ids": [],
                           "top_level": True, "children": [], "children_count": 0}
        item["importance"], item["importance_reasons"] = importance(item, context, declared, decisions, prefs, now)
        item["urgency"], item["urgency_reasons"] = urgency(due, item["due_source"], now)
        item["quadrant"] = gtd_board_merge.quadrant(item)
        pin = prefs["pins"].get(identity)
        if pin and pin["fingerprint"] != fingerprint(item):
            del prefs["pins"][identity]
            pin = None
        if pin:
            item["quadrant"], item["pinned"] = pin["quadrant"], True
        items.append(item)

    for key, context in sorted(contexts.items()):
        if "activity" not in context and "memory" not in context:
            continue
        entry = context.get("memory", {})
        if entry.get("stage") in gtd_memory.QUIET_STAGES:
            continue
        title, due = context["name"], None
        last = timestamp(context.get("activity", {}).get("last"))
        status = "doing" if last and dt.timedelta(0) <= now - last <= dt.timedelta(days=1) else "started"
        if entry.get("stage") == "blocked":
            status = "started"
        if entry.get("stage") == "paused":
            due = entry.get("next_at")
            if entry.get("next_step"):
                title += "：" + entry["next_step"]
        add("project:" + key, "project", title, key, status, due, "plan", context,
            max((context.get("activity", {}).get("last") or "", entry.get("updated_at") or ""),
                key=lambda value: recency(value)))

    projects = {item["project"]: item for item in items if item["kind"] == "project"
                and not suppressed(prefs, item["id"], now)}
    unsorted: list[dict[str, Any]] = []
    snapshot = read_json(state.base / "last-snapshot.json")
    reminder_ids = {row["id"] for row in snapshot.get("reminders", [])}
    candidates = state.rows("SELECT * FROM candidates WHERE state IN ('inbox', 'accepted') ORDER BY key")
    reminder_projects = {row["target_id"]: row["project"] for row in candidates if row["target_id"] and row["project"]}
    for row in candidates:
        if row["target_id"] and row["target_id"] in reminder_ids:
            continue
        context = contexts.get(gtd_threads.norm(row["project"] or ""), {})
        evidence = json.loads(row["evidence"] or "[]")
        eligible = (valid_due(row["suggested_due"]) or row["state"] == "accepted" or (
            gtd_threads.norm(row["project"] or "") in projects and own_activity(context, now, 7)
            and recent_update(row["updated_at"], now)))
        if not eligible or (evidence and all(event.get("kind") == "agent.final_answer" for event in evidence)):
            if row["state"] == "inbox" and not suppressed(prefs, "candidate:" + row["key"], now):
                unsorted.append({**row, "id": "candidate:" + row["key"], "evidence": evidence})
            continue
        add("candidate:" + row["key"], "task", row["title"], row["project"], "todo",
            row["suggested_due"], "candidate", updated=row["updated_at"])
    for row in snapshot.get("reminders", []):
        raw_due = row.get("due")
        due = valid_due(raw_due.get("value") if isinstance(raw_due, dict) else raw_due)
        if not row.get("completed") and due:
            status = "doing" if timestamp(due).date() == now.date() else "todo"
            project = row.get("project") or reminder_projects.get(row["id"])
            add("reminder:" + row["id"], "reminder", row["title"], project, status, due, "reminder",
                updated=row.get("modified_at") or row.get("updated_at") or row.get("created_at"))
    for row in snapshot.get("events", []):
        start = timestamp(row.get("start"))
        if start and now <= start <= now + dt.timedelta(days=3):
            due = valid_due(row["start"])
            add(f"event:{row['id']}:{due}", "event", row["title"], row.get("project"), "todo", due, "calendar",
                updated=row.get("modified_at") or row.get("updated_at") or row.get("created_at"))
    if prefs["pins"] != original_pins or prefs != preferences(state):
        gtd.atomic_json(state.base / "memory/preferences.json", prefs)
    items = [item for item in items if not suppressed(prefs, item["id"], now)]
    merged = gtd_board_merge.merge(items, contexts)
    merged_count = len(items) - len(merged)
    items = merged
    items.sort(key=lambda item: (item["quadrant"], -item["urgency"], -item["importance"],
                                 -item["activity_at"], item["id"]))
    gtd_board_merge.hierarchy(items)
    unsorted.sort(key=lambda row: (-recency(row["updated_at"]), row["id"]))
    counts = {quadrant: sum(item["top_level"] and item["quadrant"] == quadrant for item in items)
              for quadrant in range(1, 5)}
    board = {"generated_at": now.isoformat(), "items": items, "counts": counts, "quadrant_counts": counts,
             "nested_count": sum(not item["top_level"] for item in items),
             "unsorted": {"count": len(unsorted), "items": unsorted[:50]}, "merged": merged_count}
    gtd.atomic_json(state.base / "board.json", board)
    return board


def record_feedback(state: gtd.State, item_id: str, action: str, value: Any = None,
                    now: dt.datetime | None = None) -> dict[str, Any]:
    """Apply one user correction and append its pre-correction scoring evidence."""
    from gtd_feedback import record_feedback as record
    return record(state, item_id, action, value, now)


def select_notifications(state: gtd.State, board: dict[str, Any], now: dt.datetime) -> list[dict[str, Any]]:
    """Preview eligible notifications without marking them delivered."""
    from gtd_board_notifications import select_notifications as select
    return select(state, board, now)


def mark_notified(state: gtd.State, item_ids: list[str], now: dt.datetime) -> None:
    """Record notification receipts separately from selection/preview."""
    from gtd_board_notifications import mark_notified as mark
    mark(state, item_ids, now)


def main() -> None:
    """Expose build, compact display, feedback, and notification dry runs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("build")
    sub.add_parser("show")
    sub.add_parser("notifications")
    feedback = sub.add_parser("feedback")
    feedback.add_argument("item")
    feedback.add_argument("action", choices=("move", "status", "notification"))
    feedback.add_argument("value", nargs="?")
    args = parser.parse_args()
    state = gtd.State(args.state)
    try:
        now = local_time()
        if args.command == "feedback":
            result = record_feedback(state, args.item, args.action, args.value, now)
        else:
            result = build(state, now)
            if args.command == "notifications":
                result = select_notifications(state, result, now)
            elif args.command == "show":
                for quadrant, label in enumerate(("重要且紧急", "重要不紧急", "紧急不重要", "不紧急不重要"), 1):
                    print(f"Q{quadrant} {label} ({result['counts'][quadrant]})")
                    for item in result["items"]:
                        if item["quadrant"] == quadrant:
                            print(f"  {item['title']} [{item['status']}] {item['due'] or ''}")
                return
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError) as error:
        LOG.error("%s", error)
        parser.exit(1, str(error) + "\n")
    finally:
        state.db.close()


if __name__ == "__main__":
    main()
