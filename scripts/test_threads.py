"""Tests for project stages, memory rules and nudge ranking. Synthetic data only, no model calls."""
import datetime as dt
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import gtd
import gtd_board_api
import gtd_inference
import gtd_memory
import gtd_provider
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

    def provider(self) -> None:
        """Configure a synthetic provider; never contact its fake endpoint."""
        gtd.atomic_json(self.state.base / "config.json", {"extractor_backend": "provider"})
        gtd.atomic_json(self.state.base / "secrets/provider.json",
                        {"base_url": "https://invalid.test", "api_key": "fake", "model": "fake"})

    def test_daily_launch_gates_and_local_day_throttle(self) -> None:
        """Launch once after local 09:00, including the next date and offset conversion."""
        local = dt.datetime(2026, 11, 11, 9).astimezone()
        with patch.object(gtd_inference.subprocess, "Popen") as popen:
            self.assertFalse(gtd_inference.start(self.state, local))
            self.provider()
            self.assertFalse(gtd_inference.start(self.state, local.replace(hour=8, minute=59)))
            self.assertTrue(gtd_inference.start(self.state, local))
            self.assertFalse(gtd_inference.start(self.state, local + dt.timedelta(hours=14)))
            # A timestamp on another UTC date is still the same host-local date.
            same_instant = (local + dt.timedelta(hours=14)).astimezone(dt.timezone(dt.timedelta(hours=14)))
            self.assertFalse(gtd_inference.start(self.state, same_instant))
            self.assertFalse(gtd_inference.start(self.state, (local + dt.timedelta(days=1)).replace(hour=8)))
            self.assertTrue(gtd_inference.start(self.state, local + dt.timedelta(days=1)))
        self.assertEqual(popen.call_count, 2)
        self.assertEqual(popen.call_args.args[0][-3:], ["--state", str(self.state.base), "--infer"])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(gtd_inference.status(self.state)["projects_inferred"], 0)
        self.assertEqual((self.state.base / "memory/inference-status.json").stat().st_mode & 0o777, 0o600)

    def test_daily_launch_lock_and_failed_attempt_are_not_retried(self) -> None:
        """An active worker blocks another launcher; a failed launch consumes today's slot."""
        self.provider()
        local = dt.datetime(2026, 11, 11, 12).astimezone()
        with patch.object(gtd_inference.subprocess, "Popen", side_effect=OSError("blocked")) as popen:
            with self.state.lock("inference.lock"):
                self.assertFalse(gtd_inference.start(self.state, local))
            popen.assert_not_called()
            self.assertFalse(gtd_inference.start(self.state, local))
            self.assertTrue(gtd_inference.status(self.state)["error"])
            self.assertFalse(gtd_inference.start(self.state, local))
        self.assertEqual(popen.call_count, 1)

    def test_inference_worker_reads_profile_and_never_confirms_stages(self) -> None:
        """The worker invokes real cached inference with the profile and reports fresh successes."""
        self.provider()
        gtd_memory.folder(self.state).joinpath("profile.md").write_text("我做完能用就转工具。", encoding="utf-8")
        with patch.object(gtd_threads, "scan_activity", return_value=self.activity), \
             patch.object(gtd_provider, "chat_json", return_value={"stage": "tool", "confidence": 0.9}) as model:
            value = gtd_inference.run(self.state, NOW)
        self.assertEqual(value["projects_inferred"], 3)
        self.assertEqual(set(value["projects"]), {"Alpha", "Beta", "Gamma"})
        self.assertTrue(value["finished_at"])
        self.assertIsNone(value["error"])
        self.assertIn("我做完能用就转工具", model.call_args.args[2])
        self.assertEqual(gtd_memory.projects(self.state), {})
        self.assertEqual(gtd_board_api.get(self.state, "/api/memory")["inference_status"], value)
        inferred = gtd_threads.json.loads((self.state.base / "memory/inferred.json").read_text())
        self.assertTrue(all(n["kind"] == "confirm" for n in self.rank(inferred).values()))

    def test_inference_status_records_partial_errors_and_cache_hits(self) -> None:
        """Provider failures are visible; cached entries do not count as new inferences."""
        self.provider()
        with patch.object(gtd_threads, "scan_activity", return_value=self.activity), \
             patch.object(gtd_provider, "chat_json", side_effect=[{"stage": "tool"},
                          gtd_provider.ProviderError("offline"), {"stage": "active"}]) as model:
            value = gtd_inference.run(self.state, NOW)
            self.assertEqual(value["projects_inferred"], 2)
            self.assertIn("offline", value["error"])
            cached = gtd_inference.run(self.state, NOW + dt.timedelta(hours=1))
        self.assertEqual(model.call_count, 3)
        self.assertEqual(cached["projects_inferred"], 0)
        with patch.object(gtd_threads, "cached_inference", side_effect=OSError("unavailable")):
            failed = gtd_inference.run(self.state, NOW + dt.timedelta(days=1))
        self.assertIn("unavailable", failed["error"])
        self.assertTrue(failed["finished_at"])

    def test_infer_cli_does_not_run_report_or_evaluation(self) -> None:
        """The --infer entry point only delegates to the inference worker."""
        with patch("sys.argv", ["gtd_threads.py", "--state", str(self.state.base), "--infer"]), \
             patch.object(gtd_inference, "run", return_value={}) as run, \
             patch.object(gtd_threads, "report") as report, patch.object(gtd_threads, "evaluate") as evaluate:
            gtd_threads.main()
        self.assertEqual(run.call_count, 1)
        report.assert_not_called()
        evaluate.assert_not_called()

    def test_daemon_launches_inference_without_running_it_inline(self) -> None:
        """Daily work is detached even when notification delivery is disabled."""
        import gtd_board_notify
        self.provider()
        with patch.object(gtd_inference, "start") as start, \
             patch.object(gtd_threads, "cached_inference", side_effect=AssertionError("inline inference")), \
             patch.object(gtd_board_notify, "collect_responses"), patch.object(gtd_board_notify, "cycle"):
            gtd.board_notifications(self.state, self.state.config(), False)
        start.assert_called_once_with(self.state)

    def test_worker_waits_for_launcher_lock_handoff(self) -> None:
        """A fast child waits for launch reservation to release its shared lock."""
        entered, finished = threading.Event(), threading.Event()
        values: list[dict] = []

        def worker() -> None:
            """Run the worker in another thread while the launcher holds its lock."""
            entered.set()
            values.append(gtd_inference.run(self.state, NOW))
            finished.set()

        with patch.object(gtd_threads, "cached_inference", return_value={}):
            with self.state.lock("inference.lock"):
                thread = threading.Thread(target=worker)
                thread.start()
                self.assertTrue(entered.wait(1))
                self.assertFalse(finished.wait(0.05))
            thread.join(2)
        self.assertTrue(finished.is_set())
        self.assertIsNone(values[0]["error"])


if __name__ == "__main__":
    unittest.main()
