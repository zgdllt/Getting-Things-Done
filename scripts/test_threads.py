"""Tests for project stages, memory rules and nudge ranking. Synthetic data only, no model calls."""
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import gtd
import gtd_memory
import gtd_threads

NOW = dt.datetime(2026, 11, 11, 12, 0, tzinfo=dt.timezone.utc)


def activity(dates, last, messages=("继续做",), mentioned=None):
    return {"dates": dates, "last": last, "messages": [[last, text] for text in messages], "replies": [],
            "commit_subjects": [], "commits": 1, "mentioned_by": mentioned or {}}


class ThreadsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = gtd.State(Path(self.temp.name) / "state")
        quiet = activity(["2026-11-01", "2026-11-02", "2026-11-03"], "2026-11-03T10:00:00+00:00")
        self.activity = {"Alpha": quiet, "Beta": dict(quiet), "Gamma": dict(quiet),
                         "Fresh": activity(["2026-11-10", "2026-11-11"], "2026-11-11T11:00:00+00:00")}

    def tearDown(self):
        self.state.db.close()
        self.temp.cleanup()

    def rank(self, inferred=None):
        with patch.object(gtd_threads, "scan_activity", return_value=self.activity):
            return {n["project"]: n for n in gtd_threads.rank(self.state, NOW, inferred=inferred)}

    def test_project_of_folds_paths(self):
        home, root = Path.home(), Path.home() / "workspace"
        self.assertEqual(gtd_threads.project_of(str(root / "文枢" / "opc-2026" / "trial"), "trial", [root]), "文枢")
        self.assertEqual(gtd_threads.project_of(str(home / ".codex/worktrees/gtd-x/Getting-Things-Done/scripts"), "", [root]),
                         "Getting-Things-Done")
        self.assertIsNone(gtd_threads.project_of("/private/tmp/scratch", "scratch", [root]))
        self.assertIsNone(gtd_threads.project_of(str(root), "workspace", [root]))

    def test_quiet_projects_nudge_and_active_ones_do_not(self):
        nudges = self.rank()
        self.assertEqual(set(nudges), {"Alpha", "Beta", "Gamma"})
        self.assertTrue(all(n["kind"] == "quiet" for n in nudges.values()))

    def test_memory_stage_decides(self):
        gtd_memory.record(self.state, "Alpha", "tool", "已成为工具")
        gtd_memory.record(self.state, "Beta", "paused", "达标先停", "真机部署", "2026-11-12")
        gtd_memory.record(self.state, "Gamma", "blocked", "多开难以复用资源")
        nudges = self.rank()
        self.assertNotIn("Alpha", nudges)
        self.assertNotIn("Beta", nudges)                      # planned for tomorrow
        self.assertEqual(nudges["Gamma"]["kind"], "decide")
        self.assertIn("还卡着", gtd_threads.phrase(nudges["Gamma"]))
        with patch.object(gtd_threads, "scan_activity", return_value=self.activity):
            tomorrow = {n["project"]: n for n in gtd_threads.rank(self.state, NOW + dt.timedelta(days=1))}
        self.assertEqual(tomorrow["Beta"]["kind"], "planned")
        self.assertIn("真机部署", gtd_threads.phrase(tomorrow["Beta"]))

    def test_blocked_is_not_repeated_within_three_days(self):
        entry = gtd_memory.record(self.state, "Gamma", "blocked", "卡住")
        entry["last_nudged_at"] = (NOW - dt.timedelta(days=1)).isoformat()
        self.assertEqual(gtd_memory.nudge_rule(entry, NOW), "skip")
        entry["last_nudged_at"] = (NOW - dt.timedelta(days=4)).isoformat()
        self.assertEqual(gtd_memory.nudge_rule(entry, NOW), "decide")

    def test_inferred_stage_only_asks_once_then_memory_decides(self):
        inferred = {"Alpha": {"stage": "tool", "confidence": 0.9},
                    "Beta": {"stage": "done", "confidence": 0.4},
                    "Gamma": {"stage": "active", "confidence": 0.8, "left_off": "停在调参", "remaining": "跑完验收"}}
        nudges = self.rank(inferred)
        # Inferred stages never decide on their own, however confident: each becomes one confirmation.
        self.assertEqual({n["kind"] for n in nudges.values()}, {"confirm"})
        self.assertIn("看起来已经变成工具在用，对吗", gtd_threads.phrase(nudges["Alpha"]))
        self.assertIn("停在：停在调参", gtd_threads.phrase(nudges["Gamma"]))
        gtd_memory.record(self.state, "Alpha", "tool", "用户确认")   # once confirmed, memory decides
        self.assertNotIn("Alpha", self.rank(inferred))

    def test_record_rejects_unknown_stage_and_bad_date(self):
        with self.assertRaises(ValueError):
            gtd_memory.record(self.state, "X", "sleeping")
        with self.assertRaises(ValueError):
            gtd_memory.record(self.state, "X", "paused", next_at="tomorrow")


if __name__ == "__main__":
    unittest.main()
