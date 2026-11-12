"""文枢's own calendar and reminder list: setup, ownership checks and direct writes.

The user's calendars stay guarded (proposals the user confirms). Items that live in the
calendar or reminder list 文枢 created for itself may be written directly, still through
``gtd.apply_plan`` so op_ids stay idempotent, ``modified_at`` is checked and an uncertain
write stops instead of being retried blindly.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import gtd
import gtd_board

LOG = logging.getLogger(__name__)
TITLE = "文枢"
KEYS = ("owned_calendar_id", "owned_list_id")


def owned_ids(state: gtd.State) -> dict[str, str | None]:
    """Configured owned calendar and reminder-list identifiers (None when not set up)."""
    config = state.config()
    return {key: config.get(key) or None for key in KEYS}


def status(state: gtd.State) -> dict[str, Any]:
    """Setup state for the settings page, with titles resolved from the last snapshot."""
    ids = owned_ids(state)
    snapshot = gtd_board.read_json(state.base / "last-snapshot.json")
    calendar = next((row for row in snapshot.get("calendars", []) if row["id"] == ids["owned_calendar_id"]), None)
    reminders = next((row for row in snapshot.get("lists", []) if row["id"] == ids["owned_list_id"]), None)
    return {"ready": bool(ids["owned_calendar_id"] and ids["owned_list_id"]), **ids,
            "calendar": calendar, "list": reminders}


def setup(state: gtd.State) -> dict[str, Any]:
    """Create (or reuse) the 文枢 calendar and reminder list on the default account; idempotent.

    Existing ids that still resolve are kept, so a repeated setup never creates duplicates.
    """
    with state.lock("owned-setup.lock", blocking=True):
        ids = owned_ids(state)
        snapshot = gtd_board.read_json(state.base / "last-snapshot.json")
        known_calendars = {row["id"] for row in snapshot.get("calendars", [])}
        known_lists = {row["id"] for row in snapshot.get("lists", [])}
        created: dict[str, Any] = {}
        if not ids["owned_calendar_id"] or ids["owned_calendar_id"] not in known_calendars:
            created["calendar"] = gtd.native(state, "apply", {"action": "create_calendar", "op_id": "owned-calendar",
                                                              "title": TITLE})["item"]
        if not ids["owned_list_id"] or ids["owned_list_id"] not in known_lists:
            created["list"] = gtd.native(state, "apply", {"action": "create_list", "op_id": "owned-list",
                                                          "title": TITLE})["item"]
        if created:
            with state.lock():
                config = state.config()
                if "calendar" in created:
                    config["owned_calendar_id"] = created["calendar"]["id"]
                if "list" in created:
                    config["owned_list_id"] = created["list"]["id"]
                gtd.atomic_json(state.base / "config.json", config)
            LOG.info("Owned 文枢 calendar/list ready: %s", ", ".join(sorted(created)))
            try:
                gtd.snapshot(state)
            except Exception:  # the ids are saved; the next daemon snapshot will pick them up
                LOG.warning("Snapshot after owned setup failed")
    return {**status(state), "created": sorted(created)}


def event_row(state: gtd.State, item_id: str) -> dict[str, Any] | None:
    """Snapshot row behind an ``event:<id>:<due>`` card, or None."""
    if not item_id.startswith("event:"):
        return None
    identity = item_id.split(":", 2)[1]
    snapshot = gtd_board.read_json(state.base / "last-snapshot.json")
    return next((row for row in snapshot.get("events", []) if row["id"] == identity), None)


def reminder_row(state: gtd.State, item_id: str) -> dict[str, Any] | None:
    """Snapshot row behind a ``reminder:<id>`` card, or None."""
    if not item_id.startswith("reminder:"):
        return None
    snapshot = gtd_board.read_json(state.base / "last-snapshot.json")
    return next((row for row in snapshot.get("reminders", []) if row["id"] == item_id.removeprefix("reminder:")), None)


def is_owned(state: gtd.State, item_id: str) -> bool:
    """Whether a card's underlying item lives in 文枢's own calendar or list."""
    ids = owned_ids(state)
    event = event_row(state, item_id)
    if event is not None:
        return bool(ids["owned_calendar_id"]) and event.get("calendar_id") == ids["owned_calendar_id"]
    reminder = reminder_row(state, item_id)
    if reminder is not None:
        return bool(ids["owned_list_id"]) and reminder.get("list_id") == ids["owned_list_id"]
    return False


def require_owned(state: gtd.State, operation: dict[str, Any]) -> None:
    """Reject a direct write unless its target and destination are 文枢's own.

    Raises:
        PermissionError: When the operation touches a calendar or list the user owns.
    """
    ids = owned_ids(state)
    action = operation.get("action", "")
    if action in ("create_event", "update_event"):
        target = operation.get("calendar_id")
        if action == "update_event":
            row = event_row(state, "event:" + str(operation.get("id")) + ":")
            target = target or (row or {}).get("calendar_id")
        if not ids["owned_calendar_id"] or target != ids["owned_calendar_id"]:
            raise PermissionError("只能直接改文枢自己的日历；你的日历需要你确认")
        return
    if action in ("create_reminder", "update_reminder"):
        target = operation.get("list_id")
        if action == "update_reminder":
            row = reminder_row(state, "reminder:" + str(operation.get("id")))
            target = target or (row or {}).get("list_id")
        if not ids["owned_list_id"] or target != ids["owned_list_id"]:
            raise PermissionError("只能直接改文枢自己的提醒列表；你的提醒事项需要你确认")
        return
    raise PermissionError(f"Unsupported direct write: {action}")


def apply_owned(state: gtd.State, operation: dict[str, Any], plan_name: str) -> dict[str, Any]:
    """Persist one checked operation as a plan and apply it (resumes the same op on retry)."""
    require_owned(state, operation)
    path = state.base / "workbench-plans" / f"{plan_name}.json"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.exists():
        gtd.atomic_json(path, {"operations": [operation]})
    result = gtd.apply_plan(state, path)
    try:
        gtd.snapshot(state)
    except Exception:
        result["refresh_pending"] = True
    return result


def _iso(value: dt.datetime) -> str:
    """Second-precision ISO 8601 with offset, the format the native bridge parses."""
    return value.replace(microsecond=0).isoformat()


def reschedule_event(state: gtd.State, item_id: str, day: str, op_id: str) -> dict[str, Any]:
    """Move an owned event to ``day`` (YYYY-MM-DD), keeping its time of day and duration."""
    row = event_row(state, item_id)
    if row is None:
        raise ValueError("请刷新后再改期这个日程")
    # Snapshot times are UTC; move the local calendar date so evening events keep their day.
    start = gtd_board.local_time(dt.datetime.fromisoformat(row["start"].replace("Z", "+00:00")))
    end = dt.datetime.fromisoformat(row["end"].replace("Z", "+00:00"))
    target = dt.date.fromisoformat(day)
    new_start = start.replace(year=target.year, month=target.month, day=target.day)
    operation = {"op_id": op_id, "action": "update_event", "id": row["id"],
                 "original_start": row["original_start"], "expected_modified_at": row.get("modified_at"),
                 "start": _iso(new_start), "end": _iso(new_start + (end - start))}
    if row.get("occurrence_date"):
        operation["occurrence_date"] = row["occurrence_date"]
    if not operation["expected_modified_at"]:
        raise ValueError("请刷新后再改期这个日程")
    return apply_owned(state, operation, "owned-" + op_id)


def create_reminder(state: gtd.State, title: str, due: str | None, op_id: str) -> dict[str, Any]:
    """Create a reminder in 文枢's own list (used for confirmed new tasks)."""
    ids = owned_ids(state)
    if not ids["owned_list_id"]:
        raise ValueError("文枢的提醒列表还没创建")
    operation = {"op_id": op_id, "action": "create_reminder", "list_id": ids["owned_list_id"], "title": title}
    if due:
        operation["due"] = due
    return apply_owned(state, operation, "owned-" + op_id)
