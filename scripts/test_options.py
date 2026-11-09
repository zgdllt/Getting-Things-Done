"""Tests for gtd_options: cached quick-fill answers for the workbench. No model is ever called."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import gtd
import gtd_dashboard as web
import gtd_options


class OptionsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = gtd.State(Path(self.temp.name))
        gtd_options.ensure_schema(self.state)
        now = gtd.now()
        with self.state.db:
            for i in range(3):
                self.state.db.execute("INSERT INTO candidates VALUES(?,?,?,?,?,?,?,?,?,?)",
                                      (f"c{i}", f"候选{i}", "inbox", "", None, "[]", "inbox", None, now, now))
                self.state.db.execute("INSERT INTO questions VALUES(?,?,?,?,?,?,?)",
                                      (f"q{i}", f"c{i}", f"问题{i}？", "open", None, None, f"2026-11-0{i + 1}"))

    def tearDown(self):
        self.state.db.close()
        self.temp.cleanup()

    def fake_run(self, state, command, prompt, record):
        return {"options": ["已完成", "继续等待", "不用跟进"]}

    def test_dashboard_attaches_cached_options_without_spawning_by_default(self):
        with patch.object(gtd_options, "command", return_value=["codex"]):
            gtd_options.generate(self.state, "q0", self.fake_run)
        with patch.object(gtd_options, "start") as start:
            questions = {q["key"]: q for q in web.dashboard_state(self.state)["questions"]}
        self.assertEqual(questions["q0"]["options"], ["已完成", "继续等待", "不用跟进"])
        self.assertEqual(questions["q1"]["options"], [])
        start.assert_not_called()

    def test_dashboard_starts_worker_only_when_enabled(self):
        (self.state.base / "config.json").write_text(json.dumps({"quick_answers": True}))
        with patch.object(gtd_options, "start") as start:
            web.dashboard_state(self.state)
        start.assert_called_once()

    def test_start_is_throttled(self):
        with patch.object(gtd_options.subprocess, "Popen") as popen:
            self.assertTrue(gtd_options.start(self.state))
            self.assertFalse(gtd_options.start(self.state))
        popen.assert_called_once()

    def test_process_generates_at_most_limit_and_skips_cached(self):
        with patch.object(gtd_options, "command", return_value=["codex"]), \
             patch.object(gtd, "run_extractor", side_effect=self.fake_run) as run:
            self.assertEqual(gtd_options.process(self.state, limit=2)["generated"], 2)
            self.assertEqual(gtd_options.process(self.state, limit=2)["generated"], 1)
            self.assertEqual(gtd_options.process(self.state, limit=2)["generated"], 0)
        self.assertEqual(run.call_count, 3)

    def test_answered_questions_are_not_generated(self):
        with self.state.db:
            self.state.db.execute("UPDATE questions SET state='answered', answer='x' WHERE key='q0'")
        self.assertEqual(gtd_options.open_keys(self.state), ["q1", "q2"])


if __name__ == "__main__":
    unittest.main()
