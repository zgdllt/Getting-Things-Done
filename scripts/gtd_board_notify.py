"""Board notifications with one-tap answers and a reply box; every response becomes feedback.

Delivery: `cycle` asks the board which cards deserve a notification now (quadrant 1 at most
every 12 h, at most one "possibly forgotten" nudge a day, nothing in quiet hours) and sends
each through the native app with the `board-card` category. Without native permission the
system script channel shows a plain notification instead.

Responses: the native app writes `notifications/*.response.json`; `collect_responses` turns
the four buttons into board feedback, interprets reply text immediately, and logs a
dismissal as a weak signal. A response to the test card is only acknowledged.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import gtd
import gtd_board
import gtd_memory
import gtd_notifications

logger = logging.getLogger(__name__)

TEST_ITEM = "test:notification"
ANSWERS = {"start": "start", "done": "done", "not_important": "not_important", "later": "later"}
MAX_PER_CYCLE = 3
REPLY_LIMIT = 500
Deliver = Callable[..., Dict[str, Any]]


def status_path(state: gtd.State) -> Path:
    return gtd_memory.folder(state) / "notify-status.json"


def read_status(state: gtd.State) -> Dict[str, Any]:
    path = status_path(state)
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except ValueError:
        return {}


def write_status(state: gtd.State, **changes: Any) -> Dict[str, Any]:
    value = {**read_status(state), **changes}
    gtd.atomic_json(status_path(state), value)
    return value


def subtitle(card: Dict[str, Any]) -> str:
    """Why now, in a few characters: the quadrant plus the strongest reason."""
    reasons = [r for r in (card.get("urgency_reasons") or []) + (card.get("importance_reasons") or []) if "未设截止" not in r]
    head = "先做" if card.get("quadrant") == 1 else "可能忘了"
    return head + (" · " + reasons[0] if reasons else "")


def system_script(title: str, body: str, sub: str) -> None:
    """Plain fallback notification; arguments are passed as argv, never spliced into the script."""
    script = "on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv) subtitle (item 3 of argv)\nend run"
    subprocess.run(["/usr/bin/osascript", "-e", script, title, body, sub], check=False, capture_output=True, timeout=10)


def send(state: gtd.State, card_id: str, title: str, body: str, sub: str, now: dt.datetime,
         deliver: Deliver = gtd_notifications.deliver) -> Dict[str, Any]:
    """Send one board card; a per-minute identifier lets the same card be sent again later."""
    extra = {"category": "board-card", "item_id": card_id, "subtitle": sub, "view": "board"}
    result = deliver(state, title, body, identifier=f"board:{card_id}:{now:%Y%m%d%H%M}", extra=extra)
    if result.get("channel") == "system_script":
        system_script(title, body, sub)
        result = {**result, "submitted": True, "buttons": False}
    return result


def cycle(state: gtd.State, now: Optional[dt.datetime] = None, deliver: Deliver = gtd_notifications.deliver) -> Dict[str, Any]:
    """Send what the board selects now and record which cards were actually delivered."""
    now = gtd_board.local_time(now)
    board = gtd_board.build(state, now, refresh_activity=False)
    cards = {item["id"]: item for item in board["items"]}
    selected = gtd_board.select_notifications(state, board, now)[:MAX_PER_CYCLE]
    sent, failed = [], []
    for note in selected:
        card = cards.get(note["item_id"], {})
        try:
            result = send(state, note["item_id"], note["title"], note["body"], subtitle(card), now, deliver)
        except (OSError, RuntimeError, ValueError) as error:
            logger.warning("notification failed for %s: %s", note["item_id"], error)
            failed.append(note["item_id"])
            continue
        if result.get("submitted") or result.get("ack_pending"):
            sent.append({"item_id": note["item_id"], "title": note["title"], "at": now.isoformat(),
                         "buttons": result.get("buttons", True), "channel": result.get("channel")})
        else:
            failed.append(note["item_id"])
    if sent:
        gtd_board.mark_notified(state, [item["item_id"] for item in sent], now)
    history = (sent + read_status(state).get("recent", []))[:20]
    write_status(state, checked_at=now.isoformat(), recent=history, last_failed=failed)
    return {"selected": len(selected), "sent": sent, "failed": failed}


def log_event(state: gtd.State, event: Dict[str, Any]) -> None:
    path = gtd_memory.folder(state) / "feedback.jsonl"
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")


def title_of(state: gtd.State, item_id: str) -> str:
    notified = gtd_board.read_json(state.base / "memory" / "notified.json")
    card = (notified.get("cards") or {}).get(item_id) or {}
    if not card:
        card = next((i for i in gtd_board.read_json(state.base / "board.json").get("items", []) if i.get("id") == item_id), {})
    return card.get("title") or ""


def apply(state: gtd.State, response: Dict[str, Any], now: dt.datetime) -> Dict[str, Any]:
    """Turn one native response into feedback; returns what was recorded."""
    item_id, action = str(response.get("item_id") or ""), str(response.get("action") or "")
    text = str(response.get("text") or "").strip()[:REPLY_LIMIT]
    base = {"timestamp": now.isoformat(), "item_id": item_id, "title": "测试通知" if item_id == TEST_ITEM else title_of(state, item_id),
            "source": "notification"}
    if item_id == TEST_ITEM:
        write_status(state, test_response={"action": action, "text": text, "at": now.isoformat()})
        return {**base, "action": "test", "value": action}
    if action in ANSWERS:
        try:
            gtd_board.record_feedback(state, item_id, "notification", ANSWERS[action], now)
        except ValueError as error:   # the card may have left the board since the notification
            log_event(state, {**base, "action": "notification", "value": action, "error": str(error)[:200]})
        return {**base, "action": "notification", "value": action}
    if action == "reply" and text:
        event = {**base, "action": "reply", "value": text}
    elif action in ("dismiss", "open"):
        event = {**base, "action": action, "value": None}
    else:
        return {**base, "action": "ignored", "value": action}
    log_event(state, event)
    if action == "reply":
        import gtd_reply
        event["interpretation"] = gtd_reply.handle(state, item_id, text, now)
    return event


def collect_responses(state: gtd.State, now: Optional[dt.datetime] = None) -> List[Dict[str, Any]]:
    """Claim collection while replies are interpreted; another collector returns immediately."""
    try:
        with state.lock("board-responses.lock"):
            return _collect_responses(state, gtd_board.local_time(now))
    except gtd.ProcessorBusy:
        return []


def _collect_responses(state: gtd.State, now: dt.datetime) -> List[Dict[str, Any]]:
    """Process every pending response file once (oldest first) and delete it."""
    folder = state.base / "notifications"
    results = []
    for path in sorted(folder.glob("*.response.json")) if folder.is_dir() else []:
        try:
            response = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            path.unlink(missing_ok=True)
            continue
        results.append(apply(state, response, now))
        path.unlink(missing_ok=True)
    if results:
        write_status(state, last_response=results[-1])
    return results


def send_test(state: gtd.State, now: Optional[dt.datetime] = None, deliver: Deliver = gtd_notifications.deliver) -> Dict[str, Any]:
    """A sample card that exercises permission, buttons and the reply box without touching real cards."""
    now = gtd_board.local_time(now)
    result = send(state, TEST_ITEM, "测试通知：文枢在这里", "点一个按钮，或者点「回复…」写一句话。\n设置页会显示收到的回应。",
                  "先做 · 测试", now, deliver)
    write_status(state, test_sent_at=now.isoformat(), test_channel=result.get("channel"), test_response=None)
    return {"ok": True, "channel": result.get("channel"), "buttons": result.get("buttons", True),
            "detail": result.get("detail", "")}


def summary(state: gtd.State) -> Dict[str, Any]:
    """What the settings page shows: channel/permission, quiet hours, recent sends and responses."""
    native = gtd_board.read_json(state.base / "notification-status.json")
    quiet = state.config().get("quiet_hours", {"start": "22:00", "end": "09:00"})
    return {"ok": True, "channel": native.get("channel"), "detail": native.get("detail", ""),
            "quiet_hours": quiet, "enabled": state.config().get("board_notifications", True), **read_status(state)}
