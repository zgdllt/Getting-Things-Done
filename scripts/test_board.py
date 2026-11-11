"""Synthetic board, feedback and notification contracts; no model, history or native calls."""
from __future__ import annotations

import datetime as dt
import io
import json
import os
from pathlib import Path
import stat
import sys
import time
from contextlib import redirect_stdout
import unittest
from unittest.mock import patch

import gtd
import gtd_board as board
import gtd_board_notifications as notifications
import gtd_memory
import gtd_threads
import install

from test_board_support import BoardFixture, NOW


class BoardTests(BoardFixture):
    """Exercise user-visible behavior using isolated, explicitly seeded local sources."""

    def test_quadrants_and_chinese_reasons(self) -> None:
        """Combine learned importance and deadlines into all four quadrants."""
        self.project()
        self.declare("Alpha")
        for key, project, due in (("q1", "Alpha", "2026-11-12"), ("q2", "Alpha", None),
                                  ("q3", None, "2026-11-09"), ("q4", None, None)):
            self.candidate(key, project, due)
        cards = self.build()
        self.assertEqual([cards[f"candidate:q{i}"]["quadrant"] for i in range(1, 5)], [1, 2, 3, 4])
        self.assertIn("明天截止", cards["candidate:q1"]["urgency_reasons"])
        self.assertIn("已逾期 2 天", cards["candidate:q3"]["urgency_reasons"])
        self.assertIn("你声明的重点项目", cards["candidate:q2"]["importance_reasons"])
        stored = board.read_json(self.state.base / "board.json")
        self.assertEqual(sum(stored["counts"].values()), len(cards))
        self.assertEqual(cards["candidate:q1"]["project"], "alpha")

    def test_deadline_thresholds(self) -> None:
        """Keep exact one-, three-, and seven-day urgency boundaries."""
        for hours, expected in ((-1, 1.0), (0, 0.9), (24, 0.9), (24.01, 0.7),
                                (72, 0.7), (72.01, 0.4), (168, 0.4), (168.01, 0.0)):
            with self.subTest(hours=hours):
                due = (NOW + dt.timedelta(hours=hours)).isoformat()
                self.assertEqual(board.urgency(due, "candidate", NOW)[0], expected)

    def test_date_only_deadlines_remain_due_today_until_midnight(self) -> None:
        """Date-only tasks do not become overdue at the start of their due date."""
        due = NOW.date().isoformat()
        self.assertEqual(board.urgency(due, "reminder", NOW), (0.9, ["今天截止"]))
        self.assertEqual(board.urgency(due, "reminder", NOW + dt.timedelta(days=1))[0], 1.0)

    def test_missing_invalid_and_inferred_dates_are_never_invented(self) -> None:
        """Text, model suggestions and ordinary project next_at never become deadlines."""
        identity = self.project(next_at="2026-11-12")
        self.candidate(due="tomorrow")
        self.write("memory/inferred.json", {"Alpha": {"due": "2026-11-11", "left_off": "调参", "remaining": "验收"}})
        cards = self.build()
        for item in cards.values():
            self.assertIsNone(item["due"])
            self.assertIsNone(item["due_source"])
            self.assertEqual(item["urgency"], 0)
        self.assertEqual(cards[identity]["left_off"], "调参")
        self.assertEqual(cards[identity]["remaining"], "验收")

    def test_project_stages_activity_and_normalized_join(self) -> None:
        """Skip completed/tools, include memory-only projects, and use own activity."""
        for name, stage in (("Tool", "tool"), ("Done", "done"), ("Old", "superseded"), ("Blocked", "blocked")):
            self.project(name, stage, recent=True)
        self.project("Fresh", recent=True)
        self.project("AL-PHA", stage=None)
        gtd_memory.record(self.state, "Alpha", "active")
        gtd_memory.record(self.state, "MemoryOnly", "active")
        cards = self.build()
        self.assertEqual(set(cards), {"project:blocked", "project:fresh", "project:alpha", "project:memoryonly"})
        self.assertEqual(cards["project:blocked"]["status"], "started")
        self.assertEqual(cards["project:fresh"]["status"], "doing")
        self.assertEqual(cards["project:memoryonly"]["status"], "started")

    def test_paused_plan_today_and_future(self) -> None:
        """Keep the user's paused plan as the sole project deadline."""
        identity = self.project(stage="paused", next_at="2026-11-11", next_step="部署真机")
        item = self.build()[identity]
        self.assertEqual((item["due"], item["due_source"], item["urgency"]), ("2026-11-11", "plan", 0.8))
        self.assertIn("部署真机", item["title"])
        self.assertEqual(item["urgency_reasons"], ["计划今天继续"])
        gtd_memory.record(self.state, "Alpha", "paused", next_at="2026-11-14", next_step="部署真机")
        self.assertEqual(self.build()[identity]["due"], "2026-11-14")

    def test_native_reminder_shape_dedup_and_project_transfer(self) -> None:
        """Use native due.value and retain project learning after replacing a candidate."""
        self.project()
        self.candidate(status="accepted", target="r")
        self.candidate("inbox", target="r")
        self.candidate("resolved", status="resolved")
        self.candidate("dismissed", status="dismissed")
        self.write("last-snapshot.json", {"reminders": [
            {"id": "r", "title": "正式提醒", "due": {"value": "2026-11-11", "all_day": True}, "completed": False},
            {"id": "done", "title": "完成", "due": "2026-11-11", "completed": True},
            {"id": "none", "title": "无期限", "due": None},
            {"id": "future", "title": "未来", "due": {"value": (NOW + dt.timedelta(days=1)).isoformat(), "all_day": False}}]})
        cards = self.build()
        self.assertEqual(set(cards), {"project:alpha", "reminder:r", "reminder:future"})
        self.assertEqual((cards["reminder:r"]["project"], cards["reminder:r"]["status"]), ("alpha", "doing"))
        self.assertEqual(cards["reminder:future"]["status"], "todo")

    def test_calendar_window_and_occurrence_identity(self) -> None:
        """Include only future three-day starts and distinguish recurring occurrences."""
        events = [{"id": "recur", "title": str(hours), "start": (NOW + dt.timedelta(hours=hours)).isoformat()}
                  for hours in (-1, 0, 24, 72, 73)]
        self.write("last-snapshot.json", {"events": events})
        cards = list(self.build().values())
        self.assertEqual({item["title"] for item in cards}, {"0", "24", "72"})
        self.assertEqual(len({item["id"] for item in cards}), 3)
        self.assertTrue(all(item["due_source"] == "calendar" and item["importance"] == 0.5 for item in cards))

    def test_importance_learning_caps(self) -> None:
        """Scale activity over the scan window and cap positive/negative candidate counts."""
        identity = self.project()
        self.scanned["Alpha"]["dates"] = [(NOW - dt.timedelta(days=i)).date().isoformat() for i in range(14)]
        self.cache()
        self.declare("Alpha")
        for i in range(5):
            self.candidate(str(i), status="resolved")
        self.assertAlmostEqual(self.build()[identity]["importance"], 0.95)
        for i in range(5, 10):
            self.candidate(str(i), status="dismissed")
        self.assertEqual(self.build()[identity]["importance"], 0.8)
        prefs = board.preferences(self.state)
        prefs["weights"]["alpha"] = 0.5
        self.write("memory/preferences.json", prefs)
        self.assertEqual(self.build()[identity]["importance"], 1.0)

    def test_cache_refresh_and_cache_only_mode(self) -> None:
        """Refresh only when allowed, never infer, and reuse even stale inference as-is."""
        identity = self.project(stage=None)
        self.cache(stamp="old")
        self.write("memory/inferred.json", {"Alpha": {"at": "2020-01-01", "left_off": "旧缓存", "stage": "done"}})
        with patch.object(gtd_threads, "scan_activity", return_value=self.scanned) as scan:
            self.assertEqual(self.build()[identity]["left_off"], "旧缓存")
            scan.assert_not_called()
            self.build(refresh=True)
            scan.assert_called_once()
        self.cache()
        self.assertIn(identity, self.build(refresh=True))
        (self.state.base / "experiments/activity-cache.json").unlink()
        self.assertEqual(self.build(), {})

    def test_move_pin_weights_and_fingerprint_expiry(self) -> None:
        """Learn only importance-crossing moves and drop pins when source due/status changes."""
        identity = self.project()
        self.feedback(identity, "move", 2)
        self.assertEqual(board.preferences(self.state)["weights"]["alpha"], 0.1)
        self.assertTrue(self.build()[identity]["pinned"])
        self.feedback(identity, "move", 1)
        self.assertEqual(board.preferences(self.state)["weights"]["alpha"], 0.1)
        self.feedback(identity, "move", 3)
        self.assertEqual(board.preferences(self.state)["weights"]["alpha"], 0)
        self.scanned["Alpha"]["last"] = NOW.isoformat()
        self.cache()
        self.assertFalse(self.build()[identity]["pinned"])
        task = self.candidate()
        self.feedback(task, "move", 1)
        with self.state.db:
            self.state.db.execute("UPDATE candidates SET suggested_due='2026-11-12' WHERE key='task'")
        self.assertFalse(self.build()[task]["pinned"])

    def test_feedback_statuses_update_memory_and_preserve_plan(self) -> None:
        """Translate every project status while keeping project context and plans."""
        for value, stage in (("doing", "active"), ("blocked", "blocked"), ("tool", "tool"), ("done", "done")):
            with self.subTest(value=value):
                identity = self.project(value, "paused", next_at="2026-11-20", next_step="验收", note="记录")
                self.feedback(identity, "status", value)
                entry = gtd_memory.projects(self.state)[value]
                self.assertEqual((entry["stage"], entry["next_step"], entry["next_at"]), (stage, "验收", "2026-11-20"))
                if value in ("done", "tool"):
                    self.assertNotIn(identity, self.build())
                else:
                    self.assertEqual(self.build()[identity]["status"], "doing" if value == "doing" else "started")

    def test_doing_expires_and_external_memory_resumes_automatic_status(self) -> None:
        """Treat starting as 24h of user activity; let changed project memory take over."""
        identity = self.project()
        self.feedback(identity, "status", "doing")
        self.feedback(identity, "move", 2)
        self.feedback(identity, "notification", "start")
        self.assertTrue(self.build()[identity]["pinned"])
        self.assertEqual(self.build()[identity]["status"], "doing")
        self.assertEqual(self.build(NOW + dt.timedelta(days=1))[identity]["status"], "started")
        self.feedback(identity, "status", "doing")
        gtd_memory.record(self.state, "Alpha", "blocked")
        self.assertEqual(self.build()[identity]["status"], "started")

    def test_not_important_hides_30_days_keeps_stage_and_pin(self) -> None:
        """A temporary hide changes project preference but does not erase the user's pin."""
        identity = self.project()
        self.feedback(identity, "move", 2)
        self.feedback(identity, "status", "not_important")
        self.assertEqual(gtd_memory.projects(self.state)["Alpha"]["stage"], "active")
        self.assertAlmostEqual(board.preferences(self.state)["weights"]["alpha"], -0.1)
        self.assertNotIn(identity, self.build(NOW + dt.timedelta(days=29)))
        self.assertTrue(self.build(NOW + dt.timedelta(days=30))[identity]["pinned"])

    def test_task_done_is_local_and_linked_task_start_works(self) -> None:
        """Hide task completion locally, leaving candidate/native sources unmodified."""
        self.project()
        identity = self.candidate()
        self.feedback(identity, "notification", "start")
        self.assertEqual(self.build()[identity]["status"], "doing")
        self.feedback(identity, "status", "done")
        self.assertNotIn(identity, self.build())
        self.assertEqual(self.state.rows("SELECT state FROM candidates")[0]["state"], "inbox")

    def test_feedback_log_and_weight_clamps(self) -> None:
        """Append one pre-action evidence record per call and bound learned weights."""
        identity = self.project()
        for target in (2, 4) * 7:
            self.feedback(identity, "move", target)
        prefs = board.preferences(self.state)
        prefs["weights"]["alpha"] = 0.49
        self.write("memory/preferences.json", prefs)
        self.feedback(identity, "move", 2)
        self.assertEqual(board.preferences(self.state)["weights"]["alpha"], 0.5)
        for i in range(5):
            self.feedback(self.candidate(str(i)), "notification", "not_important")
        self.assertEqual(board.preferences(self.state)["weights"]["alpha"], -0.5)
        rows = [json.loads(line) for line in (self.state.base / "memory/feedback.jsonl").read_text().splitlines()]
        self.assertEqual(len(rows), 20)
        self.assertEqual(set(rows[0]), {"timestamp", "item_id", "project", "action", "value", "quadrant", "importance", "urgency"})
        self.assertEqual(rows[0]["quadrant"], 4)

    def test_invalid_feedback_does_not_log(self) -> None:
        """Reject unsupported inputs before recording a successful feedback event."""
        identity = self.candidate()
        for action, value in (("move", 5), ("move", True), ("oops", "x"), ("status", "oops"),
                              ("notification", "oops"), ("status", "tool"), ("status", "blocked")):
            with self.subTest(action=action, value=value), self.assertRaises(ValueError):
                self.feedback(identity, action, value)
        self.assertFalse((self.state.base / "memory/feedback.jsonl").exists())

    def test_q1_receipts_dedupe_and_snooze(self) -> None:
        """Preview never consumes delivery quota; receipts and snoozes filter Q1."""
        identity = self.candidate(due="2026-11-11")
        self.declare("Alpha")
        self.project(recent=True, next_step="第一行\n第二行\n第三行")
        first = self.select()
        self.assertEqual([row["item_id"] for row in first], [identity])
        self.assertEqual(len(first[0]["body"].splitlines()), 2)
        self.assertEqual(first[0]["actions"], ["开始", "已完成", "不重要", "稍后"])
        self.assertEqual(self.select(), first)
        self.feedback(identity, "notification", "later")
        self.assertEqual(self.select(NOW + dt.timedelta(hours=3)), [])
        self.assertEqual(len(self.select(NOW + dt.timedelta(hours=4))), 1)
        board.mark_notified(self.state, [identity, identity], NOW + dt.timedelta(hours=4))
        self.assertEqual(self.select(NOW + dt.timedelta(hours=15, minutes=59)), [])
        self.assertEqual(len(self.select(NOW + dt.timedelta(hours=16))), 1)

    def test_one_ranked_q2_nudge_per_local_day(self) -> None:
        """Select the highest-ranked eligible Q2 once its daily receipt permits."""
        for name in ("Alpha", "Beta"):
            self.project(name)
        self.declare("Alpha", "Beta")
        at = NOW.replace(hour=9)
        selected = self.select(at)
        self.assertEqual(len(selected), 1)
        self.assertIn("没动了", selected[0]["body"])
        board.mark_notified(self.state, [selected[0]["item_id"]], at)
        self.assertEqual(self.select(at + dt.timedelta(hours=13)), [])
        self.assertEqual(len(self.select(at + dt.timedelta(days=1))), 1)

    def test_nudge_kinds_and_filters(self) -> None:
        """Reuse phrase for every supported rank kind, skipping Q3/Q4 and hidden cards."""
        identity = self.project()
        self.declare("Alpha")
        for kind in ("planned", "decide", "quiet", "confirm"):
            with self.subTest(kind=kind):
                nudge = {"project": "Alpha", "kind": kind, "note": "验收", "next_step": "部署", "idle_days": 3,
                         "stage": "active", "usual_gap_days": 1, "last_message": "继续", "open_items": []}
                with patch.object(gtd_threads, "rank", return_value=[nudge]), patch.object(gtd_threads, "phrase", wraps=gtd_threads.phrase) as phrase:
                    self.assertEqual(self.select()[0]["item_id"], identity)
                    phrase.assert_called_once_with(nudge)
        self.feedback(identity, "notification", "not_important")
        self.assertEqual(self.select(), [])

    def test_quiet_hours_defaults_custom_and_local_day(self) -> None:
        """Apply local quiet boundaries, custom same-day windows, and daily rollover."""
        self.project()
        self.declare("Alpha")
        self.write("config.json", {"project_briefs": {"a": {"title": "Alpha"}}})
        for hour, minute, count in ((8, 59, 0), (9, 0, 1), (21, 59, 1), (22, 0, 0)):
            self.assertEqual(len(self.select(NOW.replace(hour=hour, minute=minute))), count)
        config = self.state.config()
        config["quiet_hours"] = {"start": "12:00", "end": "13:00"}
        self.write("config.json", config)
        self.assertEqual(self.select(), [])
        self.assertEqual(len(self.select(NOW.replace(hour=13))), 1)
        try:
            with patch.dict(os.environ, {"TZ": "Asia/Shanghai"}):
                time.tzset()
                config["quiet_hours"] = False
                self.write("config.json", config)
                before = dt.datetime(2026, 11, 11, 15, 59, tzinfo=dt.timezone.utc)
                row = self.select(before)[0]
                board.mark_notified(self.state, [row["item_id"]], before)
                self.assertIn("2026-11-11", notifications.log_state(self.state)["nudge_days"])
                self.assertEqual(self.select(before + dt.timedelta(minutes=2)), [])
                self.assertEqual(len(self.select(before + dt.timedelta(hours=13))), 1)
        finally:
            time.tzset()

    def test_receipts_reject_multiple_pending_nudges(self) -> None:
        """Outstanding dry runs cannot acknowledge two delivered Q2 nudges on one day."""
        for name in ("Alpha", "Beta"):
            self.project(name)
        self.declare("Alpha", "Beta")
        first = self.select()[0]["item_id"]
        self.feedback(first, "notification", "later")
        second = self.select()[0]["item_id"]
        with self.assertRaises(ValueError):
            board.mark_notified(self.state, [first, second], NOW)
        board.mark_notified(self.state, [first], NOW)
        with self.assertRaises(ValueError):
            board.mark_notified(self.state, [second], NOW)

    def test_notification_feedback_after_event_started(self) -> None:
        """A clicked notification remains actionable after its event leaves the board."""
        start = NOW + dt.timedelta(minutes=5)
        self.write("last-snapshot.json", {"events": [{"id": "e", "title": "会议", "start": start.isoformat()}]})
        identity = self.select()[0]["item_id"]
        board.mark_notified(self.state, [identity], NOW)
        self.build(NOW + dt.timedelta(minutes=10))
        self.feedback(identity, "notification", "done", NOW + dt.timedelta(minutes=10))
        self.assertIn(identity, board.preferences(self.state)["hidden"])
        self.assertEqual(len(board.read_json(self.state.base / "last-snapshot.json")["events"]), 1)

    def test_private_files_and_installer_payload(self) -> None:
        """Persist every new board artifact as 0600 and ship its runtime/test dependencies."""
        identity = self.project()
        self.declare("Alpha")
        self.feedback(identity, "move", 2)
        self.select()
        board.mark_notified(self.state, [identity], NOW)
        for name in ("board.json", "memory/preferences.json", "memory/feedback.jsonl", "memory/notified.json"):
            self.assertEqual(stat.S_IMODE((self.state.base / name).stat().st_mode), 0o600)
        for name in ("gtd_board.py", "gtd_feedback.py", "gtd_board_notifications.py", "gtd_memory.py",
                     "gtd_threads.py", "test_board.py", "test_board_support.py"):
            self.assertIn("scripts/" + name, install.PAYLOAD)

    def test_cli_build_show_feedback_notifications(self) -> None:
        """Exercise all CLI commands against the fixture without sending or scanning."""
        identity = self.project()
        self.declare("Alpha")
        for command in (("build",), ("show",), ("feedback", identity, "move", "2"), ("notifications",)):
            with self.subTest(command=command), patch.object(sys, "argv", ["gtd_board.py", "--state", str(self.state.base), *command]), patch.object(board, "local_time", return_value=NOW):
                stream = io.StringIO()
                with redirect_stdout(stream):
                    board.main()
                self.assertTrue(stream.getvalue())
                self.assertIn("Q2", stream.getvalue()) if command[0] == "show" else json.loads(stream.getvalue())


if __name__ == "__main__":
    unittest.main()
