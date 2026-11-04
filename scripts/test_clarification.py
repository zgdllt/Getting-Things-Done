"""Model selection and answer processing against isolated synthetic state only."""
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import gtd
import gtd_clarify as clarify
import gtd_dashboard as web
import gtd_models as models


class ClarificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = gtd.State(self.temp.name)
        self.key = gtd.capture(self.state, "排查合成机器人抖动")['candidate_key']
        with self.state.db:
            self.state.db.execute("INSERT INTO questions VALUES(?,?,?,?,?,?,?)",
                                  ("q", self.key, "目前修复到哪里？", "open", "", None, gtd.now()))

    def tearDown(self):
        self.state.db.close()
        self.temp.cleanup()

    def save(self):
        return web.save_answers(self.state, [{"key": "q", "text": "已基本修复，先做回归检查"}])

    def result(self, disposition="actionable", follow_up=None):
        return {"summary": "已根据你的答复整理下一步。", "updates": [{
            "candidate_key": self.key, "title": "回归验证合成机器人的抖动修复", "bucket": "next",
            "project": "合成项目", "suggested_due": None, "disposition": disposition,
            "explanation": "你反馈已基本修复，下一步用一次回归检查确认剩余问题。", "follow_up": follow_up}]}

    def process(self, result=None):
        with patch.object(gtd, "extractor_command", return_value=["synthetic-codex", "--model", "fixture-model"]), patch.object(gtd, "run_extractor", return_value=result or self.result()), patch.object(gtd, "native") as native:
            clarify.process(self.state)
            native.assert_not_called()

    def test_defaults_and_menu_change_only_gtd_model(self):
        home = Path(self.temp.name) / "config-home"
        home.mkdir()
        original = 'model="chat-model"\nmodel_provider="fixture-provider"\nmodel_reasoning_effort="max"\n'
        (home / "config.toml").write_text(original)
        self.assertEqual(models.selection(self.state), {"model": "gpt-6.1-terra", "reasoning_effort": "high"})
        self.assertEqual(models.catalog(self.state, home)["provider"], "fixture-provider")
        gtd.atomic_json(self.state.base / "config.json", {"project_briefs": {"existing": {"title": "保留项目"}}})
        with patch.object(gtd.shutil, "which", return_value="/fixture/codex"):
            command = gtd.extractor_command(self.state, home)
            self.assertEqual(command[command.index("--model") + 1], "gpt-6.1-terra")
            self.assertIn('model_reasoning_effort="high"', command)
            models.save(self.state, "fixture-cheap-model", "medium", gtd.atomic_json)
            command = gtd.extractor_command(self.state, home)
        self.assertIn("fixture-cheap-model", command)
        self.assertIn('model_reasoning_effort="medium"', command)
        self.assertIn("existing", self.state.config()["project_briefs"])
        self.assertEqual((home / "config.toml").read_text(), original)

    def test_save_is_atomic_idempotent_and_invalidates_old_preview(self):
        before = self.state.rows("SELECT updated_at FROM candidates")[0]["updated_at"]
        with self.assertRaises(ValueError):
            web.save_answers(self.state, [{"key": "q", "text": "答复"}, {"key": "gone", "text": "答复"}])
        self.assertFalse(self.state.rows("SELECT * FROM answer_jobs"))
        self.assertEqual(self.state.rows("SELECT state FROM questions")[0]["state"], "open")
        first, second = self.save(), self.save()
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertNotEqual(before, self.state.rows("SELECT updated_at FROM candidates")[0]["updated_at"])

    def test_immediate_processing_bypasses_history_backlog_and_keeps_formal_writes_explicit(self):
        self.save()
        with self.state.db:
            self.state.db.execute("INSERT INTO batches VALUES('backlog',?,'pending','[]','','')", (gtd.now(),))
        gtd.atomic_json(self.state.base / "last-snapshot.json", {"lists": [{"id": "list", "writable": True, "title": "合成"}]})
        with self.assertRaisesRegex(ValueError, "正在根据答复"):
            web.prepare(self.state, {"items": [{"candidate_key": self.key, "title": "合成下一步", "list_id": "list"}]})
        # A history processor can still be running; the clarification lock is separate.
        with self.state.lock():
            self.process()
        self.assertEqual(self.state.rows("SELECT state FROM batches")[0]["state"], "pending")
        candidate = self.state.rows("SELECT * FROM candidates")[0]
        self.assertEqual(candidate["title"], "回归验证合成机器人的抖动修复")
        self.assertEqual(candidate["state"], "inbox")
        self.assertFalse(self.state.rows("SELECT * FROM operations"))
        self.assertEqual(web.dashboard_state(self.state)["answer_jobs"][0]["state"], "done")

    def test_failure_keeps_answers_retry_uses_saved_answers(self):
        job = self.save()
        with patch.object(gtd, "extractor_command", return_value=["synthetic-codex", "--model", "fixture-model"]), patch.object(gtd, "run_extractor", side_effect=TimeoutError("合成服务暂不可用")):
            clarify.process(self.state)
        self.assertEqual(self.state.rows("SELECT state FROM answer_jobs")[0]["state"], "error")
        self.assertEqual(self.state.rows("SELECT answer FROM questions")[0]["answer"], "已基本修复，先做回归检查")
        with patch.object(clarify, "start"):
            clarify.retry(self.state, job_id=job["job_id"])
        self.process()
        self.assertEqual(len(self.state.rows("SELECT * FROM answer_jobs WHERE state='done'")), 1)

    def test_spawn_failure_is_visible_and_retry_can_recover(self):
        with patch.object(clarify, "start", side_effect=OSError("合成启动失败")):
            result = web.action(self.state, "/api/answers", {"items": [{"key": "q", "text": "已完成"}]})
        self.assertIn("processing_error", result)
        with patch.object(clarify, "start", side_effect=OSError("合成重试失败")):
            clarify.retry(self.state, self.key)
        self.assertFalse(self.state.rows("SELECT * FROM answer_jobs WHERE state='pending'"))
        with patch.object(clarify, "start") as start:
            clarify.retry(self.state, self.key)
            start.assert_called_once()
        self.process()

    def test_gui_worker_finds_user_cli_with_minimal_login_path(self):
        home = self.state.base / "synthetic-home"
        binary = home / ".local/bin/codex"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o700)
        with patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}), patch.object(Path, "home", return_value=home), \
                patch.object(clarify.subprocess, "Popen") as launch:
            clarify.start(self.state)
        environment = launch.call_args.kwargs["env"]
        self.assertEqual(shutil.which("codex", path=environment["PATH"]), str(binary))
        self.assertTrue(environment["PATH"].startswith("/usr/bin:/bin:"))

    def test_worker_recovers_claimed_job_and_does_not_overwrite_concurrent_change(self):
        self.save()
        with self.state.db:
            self.state.db.execute("UPDATE answer_jobs SET state='processing'")
        def changed(*_args):
            other = gtd.State(self.temp.name)
            with other.db:
                other.db.execute("UPDATE candidates SET title='另一处的新决定',updated_at='newer'")
            other.db.close()
            return self.result()
        with patch.object(gtd, "extractor_command", return_value=["synthetic-codex", "--model", "fixture-model"]), patch.object(gtd, "run_extractor", side_effect=changed):
            clarify.process(self.state)
        self.assertEqual(self.state.rows("SELECT title FROM candidates")[0]["title"], "另一处的新决定")
        self.assertEqual(self.state.rows("SELECT state FROM answer_jobs")[0]["state"], "error")

    def test_resolved_candidate_is_visible_and_dismissible_without_completing_native(self):
        self.save()
        self.process(self.result("resolved"))
        candidate = web.dashboard_state(self.state)["candidates"][0]
        self.assertEqual(candidate["state"], "resolved")
        self.assertEqual(candidate["resolution"]["explanation"], self.result()["updates"][0]["explanation"])
        web.action(self.state, "/api/dismiss", {"candidate_key": self.key})
        self.assertEqual(self.state.rows("SELECT state FROM candidates")[0]["state"], "dismissed")

    def test_resolved_requires_all_existing_questions_answered(self):
        with self.state.db:
            self.state.db.execute("INSERT INTO questions VALUES('q2',?,'另一个重要问题','open','',NULL,?)", (self.key, gtd.now()))
        self.save()
        self.process(self.result("resolved"))
        self.assertEqual(self.state.rows("SELECT state FROM candidates")[0]["state"], "inbox")
        self.assertEqual(self.state.rows("SELECT state FROM answer_jobs")[0]["state"], "error")

    def test_answered_follow_up_cannot_be_reopened_as_an_empty_clarification(self):
        self.save()
        self.process(self.result("needs_clarification", "目前修复到哪里？"))
        self.assertEqual(self.state.rows("SELECT state FROM answer_jobs")[0]["state"], "error")
        self.assertFalse(self.state.rows("SELECT * FROM questions WHERE state='open'"))

    def test_accepted_candidate_response_never_rewrites_formal_item(self):
        with self.state.db:
            self.state.db.execute("UPDATE candidates SET state='accepted',target_id='native-fixture'")
        self.save()
        self.process(self.result("resolved"))
        row = self.state.rows("SELECT * FROM candidates")[0]
        self.assertEqual((row["state"], row["target_id"], row["title"]), ("accepted", "native-fixture", "排查合成机器人抖动"))

    def test_late_history_cannot_revert_answer_or_insert_obsolete_question(self):
        version = self.state.rows("SELECT updated_at FROM candidates")[0]["updated_at"]
        self.save()
        analysis = {"summary": "旧历史", "candidates": [{"key": self.key, "title": "旧标题", "bucket": "inbox", "event_keys": ["old"]}],
                    "questions": [{"key": "obsolete", "candidate_key": self.key, "question": "旧问题"}],
                    "_candidate_versions": {self.key: version}}
        gtd.ingest(self.state, "unused", analysis, [{"event_key": "old", "text": "旧的屏幕信息"}])
        self.assertFalse(self.state.rows("SELECT * FROM questions WHERE key='obsolete'"))
        self.process()
        # A new, genuinely later observation still proposes a change for user confirmation.
        analysis.pop("_candidate_versions")
        future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=5)).isoformat()
        gtd.ingest(self.state, "unused", analysis, [{"event_key": "old", "timestamp": future, "text": "新的变化"}])
        self.assertTrue(self.state.rows("SELECT * FROM questions WHERE key LIKE 'changed:%'"))
        self.assertNotEqual(self.state.rows("SELECT title FROM candidates")[0]["title"], "旧标题")

    def test_standalone_failure_can_be_retried_by_job_id(self):
        with self.state.db:
            self.state.db.execute("UPDATE questions SET candidate_key=NULL")
        job = self.save()
        with patch.object(gtd, "extractor_command", return_value=["synthetic-codex", "--model", "fixture-model"]), patch.object(gtd, "run_extractor", side_effect=TimeoutError("合成超时")):
            clarify.process(self.state)
        with patch.object(clarify, "start"):
            web.action(self.state, "/api/clarify", {"job_id": job["job_id"]})
        with patch.object(gtd, "extractor_command", return_value=["synthetic-codex", "--model", "fixture-model"]), patch.object(gtd, "run_extractor", return_value={"summary": "已记录你的系统设置决定", "updates": []}):
            clarify.process(self.state)
        self.assertEqual(web.dashboard_state(self.state)["answer_jobs"][0]["summary"], "已记录你的系统设置决定")


if __name__ == "__main__":
    unittest.main()
