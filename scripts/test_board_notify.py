"""Tests for board notifications: sending, fallback, and turning responses into feedback. Synthetic data only."""
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import gtd
import gtd_board_api as api
import gtd_board_notify as notify
import gtd_dashboard as web

NOON = dt.datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)


class NotifyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = gtd.State(Path(self.temp.name))
        due = (NOON.date() + dt.timedelta(days=1)).isoformat()
        gtd.atomic_json(self.state.base / "last-snapshot.json", {"ok": True, "permissions": {}, "lists": [], "events": [],
                        "reminders": [{"id": "r1", "title": "交报告", "list": "L", "completed": False, "due": due}]})
        (self.state.base / "experiments").mkdir()
        gtd.atomic_json(self.state.base / "experiments" / "activity-cache.json",
                        {"stamp": "x", "days": 14, "version": 2, "projects": {}})
        self.sent = []
        patch.object(api.subprocess, "Popen").start()

    def tearDown(self):
        patch.stopall()
        self.state.db.close()
        self.temp.cleanup()

    def native(self, state, title, body, identifier=None, extra=None):
        self.sent.append({"title": title, "body": body, "identifier": identifier, **(extra or {})})
        return {"channel": "native", "clickable": True, "submitted": True}

    def respond(self, item_id, action, text=""):
        folder = self.state.base / "notifications"
        folder.mkdir(exist_ok=True)
        (folder / f"x.{len(list(folder.iterdir()))}.response.json").write_text(
            json.dumps({"id": "x", "item_id": item_id, "action": action, "text": text, "at": "now"}))
        return notify.collect_responses(self.state, NOON)

    def card_id(self):
        return next(i["id"] for i in api.board(self.state)["items"] if i["title"] == "交报告")

    def test_cycle_sends_quadrant_one_once_with_board_category(self):
        result = notify.cycle(self.state, NOON, self.native)
        self.assertEqual([s["title"] for s in self.sent], ["交报告"])
        self.assertEqual((self.sent[0]["category"], self.sent[0]["view"]), ("board-card", "board"))
        self.assertTrue(self.sent[0]["subtitle"].startswith("先做"))
        self.assertEqual(len(result["sent"]), 1)
        notify.cycle(self.state, NOON + dt.timedelta(hours=1), self.native)     # 12 h dedupe
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(notify.read_status(self.state)["recent"][0]["title"], "交报告")

    def test_quiet_hours_send_nothing(self):
        notify.cycle(self.state, NOON.replace(hour=23), self.native)
        self.assertEqual(self.sent, [])

    def test_fallback_uses_system_script_with_argv(self):
        def unauthorized(*args, **kwargs):
            return {"channel": "system_script", "clickable": False, "detail": "未授权"}
        with patch.object(notify.subprocess, "run") as run:
            result = notify.cycle(self.state, NOON, unauthorized)
        argv = run.call_args.args[0]
        self.assertEqual(argv[:2], ["/usr/bin/osascript", "-e"])
        self.assertNotIn("交报告", argv[2])                                       # never spliced into the script
        self.assertIn("交报告", argv[3:])
        self.assertFalse(result["sent"][0]["buttons"])

    def test_buttons_become_feedback(self):
        notify.cycle(self.state, NOON, self.native)
        item = self.card_id()
        self.respond(item, "done")
        self.assertNotIn(item, [i["id"] for i in api.board(self.state)["items"]])
        log = [json.loads(line) for line in (self.state.base / "memory/feedback.jsonl").read_text().splitlines()]
        self.assertEqual((log[-1]["action"], log[-1]["value"]), ("notification", "done"))

    def test_reply_dismiss_and_unknown_cards_are_logged(self):
        notify.cycle(self.state, NOON, self.native)
        item = self.card_id()
        self.respond(item, "reply", "改到周五交")
        self.respond(item, "dismiss")
        self.respond("reminder:gone", "later")                                   # left the board: logged, no crash
        log = [json.loads(line) for line in (self.state.base / "memory/feedback.jsonl").read_text().splitlines()]
        self.assertEqual([(e["action"], e.get("value")) for e in log][:2], [("reply", "改到周五交"), ("dismiss", None)])
        self.assertEqual(log[0]["title"], "交报告")
        self.assertIn("error", log[-1])
        self.assertFalse(list((self.state.base / "notifications").glob("*.response.json")))

    def test_test_card_only_acknowledges(self):
        result = notify.send_test(self.state, NOON, self.native)
        self.assertEqual((result["channel"], self.sent[0]["item_id"]), ("native", notify.TEST_ITEM))
        self.respond(notify.TEST_ITEM, "reply", "收到")
        self.assertEqual(notify.read_status(self.state)["test_response"]["text"], "收到")
        self.assertFalse((self.state.base / "memory/preferences.json").exists())

    def test_settings_route_validates_quiet_hours(self):
        value = web.action(self.state, "/api/notify/settings", {"enabled": False, "quiet_start": "23:00", "quiet_end": "08:30"})
        self.assertEqual((value["enabled"], value["quiet_hours"]), (False, {"start": "23:00", "end": "08:30"}))
        with self.assertRaises(ValueError):
            web.action(self.state, "/api/notify/settings", {"quiet_start": "late"})


if __name__ == "__main__":
    unittest.main()
