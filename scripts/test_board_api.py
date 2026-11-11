"""Tests for the board, undo and memory workbench routes. Synthetic data only; no model or native calls."""
import datetime as dt
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import gtd
import gtd_board_api as api
import gtd_dashboard as web
import gtd_memory


class BoardApiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = gtd.State(Path(self.temp.name))
        self.key = gtd.capture(self.state, "合成线索")["candidate_key"]
        gtd.atomic_json(self.state.base / "last-snapshot.json", {"ok": True, "permissions": {}, "lists": [],
                        "reminders": [{"id": "r1", "title": "交报告", "list": "L", "completed": False,
                                       "due": (dt.date.today() + dt.timedelta(days=1)).isoformat()}], "events": []})
        (self.state.base / "experiments").mkdir()
        gtd.atomic_json(self.state.base / "experiments" / "activity-cache.json",
                        {"stamp": "x", "days": 14, "version": 2, "projects": {}})
        self.popen = patch.object(api.subprocess, "Popen").start()

    def tearDown(self):
        patch.stopall()
        self.state.db.close()
        self.temp.cleanup()

    def test_board_has_cards_and_no_raw_evidence(self):
        value = web.action(self.state, "/api/board/refresh", {})
        payload = json.dumps(value, ensure_ascii=False)
        self.assertIn("交报告", payload)
        self.assertNotIn("evidence", payload)
        self.assertTrue(all(set(card) <= set(api.CARD_FIELDS) for card in value["items"]))
        self.assertIn("count", value["unsorted"])

    def test_feedback_can_be_undone_and_both_are_logged(self):
        card = next(i for i in api.board(self.state)["items"] if i["title"] == "交报告")
        moved = web.action(self.state, "/api/board/feedback",
                           {"item_id": card["id"], "action": "move", "value": 4, "title": "交报告"})
        self.assertEqual(next(i for i in moved["items"] if i["id"] == card["id"])["quadrant"], 4)
        restored = web.action(self.state, "/api/board/undo", {"token": moved["undo"]})
        self.assertEqual(next(i for i in restored["items"] if i["id"] == card["id"])["quadrant"], card["quadrant"])
        log = [json.loads(line) for line in (self.state.base / "memory" / "feedback.jsonl").read_text().splitlines()]
        self.assertEqual([entry["action"] for entry in log], ["move", "undo"])
        self.assertEqual(log[-1]["title"], "交报告")
        with self.assertRaises(ValueError):          # one undo per action
            web.action(self.state, "/api/board/undo", {"token": moved["undo"]})

    def test_undo_expires_and_rejects_foreign_tokens(self):
        card = api.board(self.state)["items"][0]
        token = web.action(self.state, "/api/board/feedback", {"item_id": card["id"], "action": "move", "value": 3})["undo"]
        with self.assertRaises(ValueError):
            web.action(self.state, "/api/board/undo", {"token": "someone-else"})
        later = api.now() + dt.timedelta(minutes=api.UNDO_MINUTES + 1)
        with patch.object(api, "now", return_value=later), self.assertRaises(ValueError):
            web.action(self.state, "/api/board/undo", {"token": token})

    def test_invalid_feedback_leaves_no_undo_snapshot(self):
        with self.assertRaises(ValueError):
            web.action(self.state, "/api/board/feedback", {"item_id": "nope", "action": "move", "value": 2})
        self.assertFalse((self.state.base / "memory" / "undo.json").exists())

    def test_memory_profile_and_project_stage_are_saved_privately(self):
        value = web.action(self.state, "/api/memory/profile", {"text": "我做完能用就转工具。"})
        self.assertEqual(value["profile"], "我做完能用就转工具。")
        self.assertEqual(os.stat(self.state.base / "memory" / "profile.md").st_mode & 0o777, 0o600)
        value = web.action(self.state, "/api/memory/project", {"name": "Alpha", "stage": "paused",
                                                              "next_step": "真机部署", "next_at": "2026-11-20"})
        self.assertEqual(value["projects"]["Alpha"]["stage"], "paused")
        self.assertEqual(gtd_memory.projects(self.state)["Alpha"]["source"], "user:workbench")
        with self.assertRaises(ValueError):
            web.action(self.state, "/api/memory/project", {"name": "Alpha", "stage": "sleeping"})
        with self.assertRaises(ValueError):
            web.action(self.state, "/api/memory/profile", {"text": "x" * (api.PROFILE_LIMIT + 1)})

    def test_background_refresh_is_throttled(self):
        os.utime(self.state.base / "experiments" / "activity-cache.json", (0, 0))     # stale cache
        self.assertTrue(api.start_refresh(self.state))
        self.assertFalse(api.start_refresh(self.state))                              # within throttle
        self.assertEqual(self.popen.call_count, 1)
        args = self.popen.call_args.args[0]
        self.assertEqual(args[-3:], ["--state", str(self.state.base), "build"])


if __name__ == "__main__":
    unittest.main()
