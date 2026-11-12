"""Pending reply proposals, explicit decisions, and board-facing reply context."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import gtd
import gtd_board
import gtd_board_merge
import gtd_memory
import gtd_owned


def pending(state: gtd.State) -> list[dict[str, Any]]:
    """Read only pending proposals; accepted and rejected entries are removed."""
    return gtd_board.read_json(state.base / "memory/proposals.json").get("proposals", [])


def create(state: gtd.State, item_id: str, result: dict[str, Any], text: str,
           now: dt.datetime) -> dict[str, Any]:
    """Save a private proposal without invoking a native write or candidate capture."""
    proposal = {"id": uuid.uuid4().hex, "item_id": item_id, "kind": result["intent"],
                "due": result["due"], "title": result["title"], "text": text,
                "summary": result["summary"], "created_at": now.isoformat()}
    with state.lock("proposals.lock", blocking=True):
        gtd.atomic_json(gtd_memory.folder(state) / "proposals.json", {"proposals": [*pending(state), proposal]})
    return proposal


def enrich(state: gtd.State, items: list[dict[str, Any]], prefs: dict[str, Any]) -> None:
    """Attach the three newest notes and all proposals for surviving or merged IDs."""
    proposals = pending(state)
    owned = gtd_owned.owned_ids(state)
    snapshot = gtd_board.read_json(state.base / "last-snapshot.json") if any(owned.values()) else {}
    owned_events = {row["id"] for row in snapshot.get("events", []) if row.get("calendar_id") == owned["owned_calendar_id"]}
    owned_reminders = {row["id"] for row in snapshot.get("reminders", []) if row.get("list_id") == owned["owned_list_id"]}
    for item in items:
        identities = {item["id"], *item.get("merged_ids", [])}
        item["owned"] = any(i.startswith("event:") and i.split(":", 2)[1] in owned_events
                            or i.startswith("reminder:") and i.removeprefix("reminder:") in owned_reminders
                            for i in identities)
        notes = [note for identity in identities for note in prefs["notes"].get(identity, [])]
        item["notes"] = sorted(notes, key=lambda note: note["timestamp"], reverse=True)[:3]
        item["proposals"] = [proposal for proposal in proposals if proposal["item_id"] in identities]


def reschedule_reminder(state: gtd.State, proposal: dict[str, Any], card: dict[str, Any]) -> dict[str, Any]:
    """Reuse one persisted, version-checked operation; uncertain writes stop in apply_plan."""
    path = state.base / "workbench-plans" / ("reply-" + proposal["id"] + ".json")
    if not path.exists():
        identity = card["id"].removeprefix("reminder:")
        snapshot = gtd_board.read_json(state.base / "last-snapshot.json")
        reminder = next((row for row in snapshot.get("reminders", []) if row["id"] == identity), None)
        if not reminder or not reminder.get("modified_at"):
            raise ValueError("请刷新后再改期这条提醒事项")
        gtd.atomic_json(path, {"operations": [{"op_id": proposal["id"], "action": "update_reminder",
                        "id": identity, "expected_modified_at": reminder["modified_at"], "due": proposal["due"]}]})
    result = gtd.apply_plan(state, path)
    try:
        gtd.snapshot(state)
    except Exception:
        result["refresh_pending"] = True
    return result


def decide(state: gtd.State, identity: str, accept: bool, now: dt.datetime) -> dict[str, Any]:
    """Resolve one pending proposal only after its requested effect succeeds."""
    from gtd_reply import log_event
    if not isinstance(identity, str) or type(accept) is not bool:
        raise ValueError("提案需要 id 和布尔 accept")
    result: dict[str, Any] = {}
    with state.lock("proposals.lock", blocking=True):
        proposals = pending(state)
        proposal = next((entry for entry in proposals if entry["id"] == identity), None)
        if proposal is None:
            raise ValueError("提案已处理或不存在")
        if accept:
            if proposal["kind"] == "new_task" and proposal["due"] and gtd_owned.owned_ids(state)["owned_list_id"]:
                # A confirmed, dated task becomes a real reminder in 文枢's own list (syncs and rings).
                result = gtd_owned.create_reminder(state, proposal["title"], proposal["due"], proposal["id"])
                result["applied_to"] = "owned_list"
            elif proposal["kind"] == "new_task":
                result = gtd.capture(state, proposal["title"], suggested_due=proposal["due"])
            elif proposal["item_id"].startswith("event:") and gtd_owned.is_owned(state, proposal["item_id"]):
                # 文枢's own calendar: move the real event, not just the board's view.
                result = gtd_owned.reschedule_event(state, proposal["item_id"], proposal["due"], proposal["id"])
                result["applied_to"] = "owned_calendar"
            elif (state.base / "workbench-plans" / ("reply-" + proposal["id"] + ".json")).exists():
                result = reschedule_reminder(state, proposal, {})
            else:
                items = gtd_board.build(state, now, refresh_activity=False)["items"]
                card = gtd_board_merge.find(items, proposal["item_id"])
                # A disappeared reminder must never silently become a local override.
                if proposal["item_id"].startswith("reminder:") and not card:
                    card = {"id": proposal["item_id"], "kind": "reminder"}
                if card and card["kind"] == "reminder":
                    result = reschedule_reminder(state, proposal, card)
                else:
                    prefs = gtd_board.preferences(state)
                    prefs["due_overrides"][card["id"] if card else proposal["item_id"]] = proposal["due"]
                    gtd.atomic_json(gtd_memory.folder(state) / "preferences.json", prefs)
                    result["applied_to"] = "board_only"
        gtd.atomic_json(gtd_memory.folder(state) / "proposals.json",
                        {"proposals": [entry for entry in proposals if entry["id"] != identity]})
        log_event(state, {"action": "proposal_accepted" if accept else "proposal_rejected",
                          "timestamp": now.isoformat(), **proposal})
    return result
