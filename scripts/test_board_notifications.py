"""Existing board notification contracts, imported by the main board test suite."""
from __future__ import annotations

import datetime as dt
import os
import time
from unittest.mock import patch

import gtd_board as board
import gtd_board_notifications as notifications
import gtd_threads
from test_board_support import BoardFixture, NOW


class NotificationTests(BoardFixture):
    """Verify preview, delivery receipts and local-time notification behavior."""

    def test_nested_ids_support_notification_receipts_and_feedback(self) -> None:
        """Keep nested tasks actionable even when they do not count as top-level cards."""
        project = self.project()
        identity = self.candidate(title="整理传感器线束")
        self.feedback(identity, "move", 1)
        item = self.build()[identity]
        self.assertFalse(item["top_level"])
        self.assertEqual(item["parent"], project)
        self.assertIn(identity, [row["item_id"] for row in self.select()])
        self.feedback(identity, "notification", "later")
        self.assertNotIn(identity, [row["item_id"] for row in self.select()])
        later = NOW + dt.timedelta(hours=4)
        self.assertIn(identity, [row["item_id"] for row in self.select(later)])
        board.mark_notified(self.state, [identity], later)
        self.assertNotIn(identity, [row["item_id"] for row in self.select(later)])
        self.feedback(identity, "notification", "start", later)
        self.assertEqual(self.build(later)[identity]["status"], "doing")
        self.feedback(identity, "notification", "done", later)
        self.assertNotIn(identity, self.build(later))
        self.assertEqual(self.build(later)[project]["children"], [])
        self.assertEqual(self.state.rows("SELECT state FROM candidates")[0]["state"], "inbox")

    def test_stale_candidates_lose_urgency_but_reminders_still_notify(self) -> None:
        """Lower only candidates more than 72 hours overdue, preserving formal alerts."""
        due = (NOW - dt.timedelta(days=4)).date().isoformat()
        stale = self.candidate("stale", None, due, title="核对医保资料")
        self.write("last-snapshot.json", {"reminders": [{"id": "r", "title": "缴纳住宿费", "due": due}]})
        cards = self.build()
        self.assertEqual(cards[stale]["urgency"], 0.4)
        self.assertEqual(cards[stale]["urgency_reasons"], ["已逾期 4 天，可能已处理"])
        self.assertEqual(cards[stale]["quadrant"], 2)
        self.assertEqual(cards["reminder:r"]["urgency"], 1.0)
        self.assertEqual([row["item_id"] for row in self.select()], ["reminder:r"])
        for source in ("reminder", "calendar", "plan"):
            self.assertEqual(board.urgency(due, source, NOW), (1.0, ["已逾期 4 天"]))
        for days in (2, 3):
            self.assertEqual(board.urgency((NOW - dt.timedelta(days=days)).date().isoformat(), "candidate", NOW)[0], 1.0)

    def test_merged_aliases_preserve_snoozes_and_delivery_receipts(self) -> None:
        """A higher-priority native source cannot reset an alias's notification limits."""
        for mode, hours in (("snooze", 4), ("receipt", 12)):
            with self.subTest(mode=mode):
                start = (NOW + dt.timedelta(days=2)).isoformat()
                event = {"id": mode, "title": "提交报告", "start": start}
                self.write("last-snapshot.json", {"events": [event]})
                identity = self.select()[0]["item_id"]
                if mode == "snooze":
                    self.feedback(identity, "notification", "later")
                else:
                    board.mark_notified(self.state, [identity], NOW)
                reminder = {"id": mode, "title": "提交报告", "due": start[:10]}
                self.write("last-snapshot.json", {"events": [event], "reminders": [reminder]})
                self.assertEqual(self.select(NOW + dt.timedelta(minutes=1)), [])
                self.assertEqual(self.select(NOW + dt.timedelta(hours=hours, seconds=-1)), [])
                self.assertEqual(self.select(NOW + dt.timedelta(hours=hours))[0]["item_id"], "reminder:" + mode)

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

