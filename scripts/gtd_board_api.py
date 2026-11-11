"""Workbench routes for the four-quadrant board, undoable feedback and the user's memory.

The board is rebuilt on request from caches only (fast, no model calls). When the activity
cache is older than ACTIVITY_STALE, a detached `gtd_board.py build` refreshes it in the
background. Every feedback action snapshots the files it may change so the latest action
can be undone for UNDO_MINUTES.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any, Dict, List

import gtd
import gtd_board
import gtd_memory

logger = logging.getLogger(__name__)

ACTIVITY_STALE = dt.timedelta(hours=1)
REFRESH_THROTTLE = dt.timedelta(minutes=10)
UNDO_MINUTES = 10
PROFILE_LIMIT = 20000
SNAPSHOT_FILES = ("memory/preferences.json", "memory/projects.json")
CARD_FIELDS = ("id", "kind", "title", "project", "status", "due", "due_source", "importance",
               "importance_reasons", "urgency", "urgency_reasons", "quadrant", "pinned", "left_off",
               "remaining", "updated_at", "parent", "top_level", "children", "children_count", "merged_ids")


def now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def activity_at(state: gtd.State) -> str | None:
    path = state.base / "experiments" / "activity-cache.json"
    return dt.datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat() if path.exists() else None


def start_refresh(state: gtd.State, force: bool = False) -> bool:
    """Rebuild the activity cache in a detached process; throttled so it never piles up."""
    stamp = activity_at(state)
    fresh = stamp and now() - dt.datetime.fromisoformat(stamp) < ACTIVITY_STALE
    lock = state.base / "board-refresh.lock"
    recent = lock.exists() and now() - dt.datetime.fromtimestamp(lock.stat().st_mtime).astimezone() < REFRESH_THROTTLE
    if (fresh and not force) or recent:
        return False
    lock.touch()
    script = Path(__file__).resolve().parent / "gtd_board.py"
    subprocess.Popen([gtd.stable_python(), "-B", str(script), "--state", str(state.base), "build"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, cwd=str(state.base))
    return True


def board(state: gtd.State) -> Dict[str, Any]:
    """Current board for the page: cards without raw evidence, plus freshness metadata."""
    value = gtd_board.build(state, now(), refresh_activity=False)
    refreshing = start_refresh(state)
    unsorted = [{key: row.get(key) for key in ("id", "key", "title", "project", "bucket", "updated_at")}
                for row in value["unsorted"]["items"]]
    return {"ok": True, "generated_at": value["generated_at"], "activity_at": activity_at(state),
            "refreshing": refreshing,
            "items": [{key: item.get(key) for key in CARD_FIELDS} for item in value["items"]],
            "counts": value.get("quadrant_counts") or value.get("counts"),
            "nested_count": value.get("nested_count", 0), "merged": value.get("merged", 0),
            "unsorted": {"count": value["unsorted"]["count"], "items": unsorted}}


def snapshot(state: gtd.State, item_id: str, action: str, value: Any, title: str = "") -> str:
    token = uuid.uuid4().hex
    files = {}
    for name in SNAPSHOT_FILES:
        path = state.base / name
        files[name] = path.read_text(encoding="utf-8") if path.exists() else None
    gtd.atomic_json(gtd_memory.folder(state) / "undo.json",
                    {"token": token, "at": now().isoformat(), "item_id": item_id, "title": title, "action": action,
                     "value": value, "files": files})
    return token


def feedback(state: gtd.State, data: Dict[str, Any]) -> Dict[str, Any]:
    item_id, action = data.get("item_id"), data.get("action")
    if not isinstance(item_id, str) or not isinstance(action, str):
        raise ValueError("缺少卡片或操作")
    token = snapshot(state, item_id, action, data.get("value"), str(data.get("title") or "")[:200])
    try:
        gtd_board.record_feedback(state, item_id, action, data.get("value"), now())
    except ValueError:
        (gtd_memory.folder(state) / "undo.json").unlink(missing_ok=True)
        raise
    return {**board(state), "undo": token}


def undo(state: gtd.State, data: Dict[str, Any]) -> Dict[str, Any]:
    path = gtd_memory.folder(state) / "undo.json"
    saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    if not saved or saved.get("token") != data.get("token"):
        raise ValueError("只能撤销最近一次操作")
    if now() - dt.datetime.fromisoformat(saved["at"]) > dt.timedelta(minutes=UNDO_MINUTES):
        raise ValueError(f"只能撤销 {UNDO_MINUTES} 分钟内的操作")
    for name, content in saved["files"].items():
        target = state.base / name
        if content is None:
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".undo.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(content)
            os.replace(tmp, target)
    log = gtd_memory.folder(state) / "feedback.jsonl"
    with log.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"timestamp": now().isoformat(), "action": "undo", "item_id": saved["item_id"], "title": saved.get("title", ""),
                                 "undone_action": saved["action"], "undone_value": saved["value"]},
                                ensure_ascii=False) + "\n")
    os.chmod(log, 0o600)
    path.unlink()
    return board(state)


def recent_feedback(state: gtd.State, limit: int = 30) -> List[Dict[str, Any]]:
    path = gtd_memory.folder(state) / "feedback.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return list(reversed(rows))


def memory(state: gtd.State) -> Dict[str, Any]:
    return {"ok": True, "profile": gtd_memory.profile(state), "projects": gtd_memory.projects(state),
            "stages": list(gtd_memory.STAGES), "feedback": recent_feedback(state)}


def save_profile(state: gtd.State, data: Dict[str, Any]) -> Dict[str, Any]:
    text = data.get("text")
    if not isinstance(text, str) or len(text) > PROFILE_LIMIT:
        raise ValueError(f"画像内容需为不超过 {PROFILE_LIMIT} 字的文本")
    path = gtd_memory.folder(state) / "profile.md"
    tmp = path.with_name("profile.md.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(text)
    os.replace(tmp, path)
    return memory(state)


def save_project(state: gtd.State, data: Dict[str, Any]) -> Dict[str, Any]:
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("缺少项目名称")
    gtd_memory.record(state, name.strip(), data.get("stage", ""), str(data.get("note") or ""),
                      str(data.get("next_step") or ""), data.get("next_at") or None,
                      source="user:workbench")
    return memory(state)


def get(state: gtd.State, route: str) -> Dict[str, Any] | None:
    if route == "/api/board":
        return board(state)
    if route == "/api/memory":
        return memory(state)
    return None


def post(state: gtd.State, route: str, data: Dict[str, Any]) -> Dict[str, Any] | None:
    handlers = {"/api/board/feedback": feedback, "/api/board/undo": undo,
                "/api/memory/profile": save_profile, "/api/memory/project": save_project,
                "/api/board/refresh": lambda s, d: {**board(s), "refreshing": start_refresh(s, force=True)}}
    handler = handlers.get(route)
    return handler(state, data) if handler else None
