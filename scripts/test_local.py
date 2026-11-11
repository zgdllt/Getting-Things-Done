"""Tests for local history, the custom provider client and the replay experiment. Synthetic data only."""
import datetime as dt
import io
import json
import os
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import gtd
import gtd_compare
import gtd_sources
import gtd_options
import gtd_local
import gtd_provider
import gtd_threads

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 11, 4, 10, 0, tzinfo=UTC)


def iso(minutes):
    return (T0 + dt.timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n" for row in rows))


class RedactionTest(unittest.TestCase):
    def test_masks_tokens_and_keys(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlLXNhbXBsZQ"
        text = gtd_local.redact(f"key {jwt} and sk-abcdefghijklmnopqrstu and ghp_{'a' * 30} and Bearer {'x' * 30}")
        for secret in (jwt, "sk-abcdefghijklmnopqrstu", "ghp_", "x" * 30):
            self.assertNotIn(secret, text)
        self.assertIn("Bearer [已隐去密钥]", text)

    def test_clean_strips_injected_blocks_and_clips(self):
        text = gtd_local.clean("<system-reminder>secret context</system-reminder>真正的问题" + "字" * 2000)
        self.assertTrue(text.startswith("真正的问题"))
        self.assertLessEqual(len(text), gtd_local.MAX_TEXT + 1)


class CodexTest(unittest.TestCase):
    def test_keeps_user_messages_and_final_answers_only(self):
        with tempfile.TemporaryDirectory() as root:
            def message(minute, role, text, phase=None, kind="input_text"):
                payload = {"type": "message", "role": role, "content": [{"type": kind, "text": text}]}
                if phase:
                    payload["phase"] = phase
                return {"timestamp": iso(minute), "type": "response_item", "payload": payload}
            write_jsonl(Path(root) / "2026/11/04/rollout.jsonl", [
                {"timestamp": iso(0), "type": "session_meta", "payload": {"cwd": "/Users/x/workspace/robocup"}},
                message(1, "user", "<environment_context>cwd</environment_context>"),
                message(2, "user", "修一下射门的时序"),
                message(3, "assistant", "我先看看", "commentary", "output_text"),
                message(4, "assistant", "已修好并通过测试", "final_answer", "output_text"),
                message(200, "user", "窗口之外"),
            ])
            events = gtd_local.codex_events(T0, T0 + dt.timedelta(hours=1), Path(root))
        self.assertEqual([e["kind"] for e in events], ["agent.user_message", "agent.final_answer"])
        self.assertEqual(events[0]["window"]["title"], "robocup")
        self.assertEqual(events[0]["source_type"], "local_codex_session")


class ClaudeTest(unittest.TestCase):
    def test_keeps_human_messages_and_end_of_turn_text(self):
        with tempfile.TemporaryDirectory() as root:
            write_jsonl(Path(root) / "proj/session.jsonl", [
                {"type": "user", "timestamp": iso(1), "cwd": "/w/gtd", "origin": {"kind": "human"},
                 "message": {"role": "user", "content": [{"type": "text",
                                                          "text": "<system-reminder>x</system-reminder>写测试"}]}},
                {"type": "user", "timestamp": iso(2), "cwd": "/w/gtd",
                 "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}},
                {"type": "assistant", "timestamp": iso(3), "cwd": "/w/gtd",
                 "message": {"stop_reason": "tool_use", "content": [{"type": "text", "text": "调用工具"}]}},
                {"type": "assistant", "timestamp": iso(4), "cwd": "/w/gtd",
                 "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "测试已通过"}]}},
                {"type": "user", "timestamp": iso(5), "isSidechain": True, "origin": {"kind": "human"},
                 "message": {"role": "user", "content": [{"type": "text", "text": "子任务"}]}},
            ])
            events = gtd_local.claude_events(T0, T0 + dt.timedelta(hours=1), Path(root))
        self.assertEqual([(e["kind"], e["text"]) for e in events],
                         [("agent.user_message", "写测试"), ("agent.final_answer", "测试已通过")])
        self.assertEqual(events[0]["window"]["title"], "gtd")


class ProviderTest(unittest.TestCase):
    def provider_file(self, mode):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"base_url": "https://example.invalid/v1", "api_key": "k", "model": "m"}, handle)
        handle.close()
        os.chmod(handle.name, mode)
        self.addCleanup(os.unlink, handle.name)
        return Path(handle.name)

    def test_load_rejects_readable_credentials(self):
        with self.assertRaises(gtd_provider.ProviderError):
            gtd_provider.load(self.provider_file(0o644))
        self.assertEqual(gtd_provider.load(self.provider_file(0o600))["model"], "m")

    def test_parses_fenced_json_and_keeps_usage(self):
        body = {"choices": [{"message": {"content": "```json\n{\"options\": [\"a\", \"b\"]}\n```"}}],
                "usage": {"total_tokens": 9}}
        with patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(body).encode())):
            value = gtd_provider.chat_json({"base_url": "https://example.invalid/v1", "api_key": "k", "model": "m"}, "s", "u")
        self.assertEqual(value["options"], ["a", "b"])
        self.assertEqual(value["_usage"]["total_tokens"], 9)

    def test_extra_body_is_merged_into_the_request(self):
        body = {"choices": [{"message": {"content": "{}"}}]}
        provider = {"base_url": "https://example.invalid/v1", "api_key": "k", "model": "m",
                    "extra_body": {"max_tokens": 8000, "chat_template_kwargs": {"enable_thinking": False}}}
        with patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(body).encode())) as call:
            gtd_provider.chat_json(provider, "s", "u")
        sent = json.loads(call.call_args.args[0].data)
        self.assertEqual((sent["max_tokens"], sent["chat_template_kwargs"]), (8000, {"enable_thinking": False}))
        self.assertEqual((sent["model"], len(sent["messages"])), ("m", 2))

    def test_extra_body_cannot_replace_messages(self):
        provider = {"base_url": "https://example.invalid/v1", "api_key": "k", "model": "m",
                    "extra_body": {"messages": []}}
        with self.assertRaises(gtd_provider.ProviderError):
            gtd_provider.chat_json(provider, "s", "u")

    def test_client_errors_are_not_retried(self):
        error = urllib.error.HTTPError("u", 401, "no", {}, io.BytesIO(b"bad key"))
        with patch("urllib.request.urlopen", side_effect=error) as call, patch("time.sleep"):
            with self.assertRaises(gtd_provider.ProviderError):
                gtd_provider.chat_json({"base_url": "https://example.invalid/v1", "api_key": "k", "model": "m"}, "s", "u")
        self.assertEqual(call.call_count, 1)


class CompareTest(unittest.TestCase):
    def test_chunks_respect_budget_and_order(self):
        events = [{"text": "x" * 100} for _ in range(10)]
        groups = gtd_compare.chunks(events, 500)
        self.assertEqual(sum(len(g) for g in groups), 10)
        self.assertTrue(all(len(g) == 2 for g in groups))

    def test_score_counts_recovered_positives_and_repeated_negatives(self):
        existing = [{"key": "a", "state": "accepted"}, {"key": "b", "state": "resolved"},
                    {"key": "c", "state": "dismissed"}, {"key": "d", "state": "inbox"}]
        proposals = [{"id": f"p{i}"} for i in range(4)]
        metrics = gtd_compare.score(existing, proposals, {"p0": "a", "p1": "c", "p2": None, "p3": "d"})
        self.assertEqual((metrics["positives"], metrics["negatives"]), (2, 1))
        self.assertEqual(metrics["positives_recovered"], 1)
        self.assertEqual(metrics["positive_recall"], 0.5)
        self.assertEqual(metrics["negatives_repeated"], 1)
        self.assertEqual(metrics["local_labeled_precision"], 0.5)
        self.assertEqual((metrics["matched_unlabeled"], metrics["novel_proposals"]), (1, 1))
        partial = gtd_compare.score(existing, proposals, {"p0": "a"})
        self.assertEqual((partial["novel_proposals"], partial["unjudged_proposals"]), (0, 3))
        self.assertEqual(metrics["computer_history_labeled_precision"], 0.667)


class ExclusionTest(unittest.TestCase):
    def test_sensitive_apps_never_reach_analysis(self):
        events = [{"event_key": "a", "timestamp": iso(1), "app": {"bundleIdentifier": "com.1password.1password"}},
                  {"event_key": "b", "timestamp": iso(2), "app": {"name": "WeChat"}},
                  {"event_key": "c", "timestamp": iso(3), "app": {"name": "Codex"}}]
        with patch.object(gtd_sources, "memory_summaries", return_value=[]), \
             patch.object(gtd_sources, "retrospective_summaries", return_value=[]):
            kept = gtd_sources.analysis_records(events, {"excluded_apps": ["WeChat"]})
        self.assertEqual([e["event_key"] for e in kept], ["c"])


class ProviderBackendTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = gtd.State(Path(self.temp.name))
        secret = Path(self.temp.name) / "secrets" / "provider.json"
        secret.parent.mkdir()
        secret.write_text(json.dumps({"base_url": "https://example.invalid/v1", "api_key": "k", "model": "deepseek-x"}))
        os.chmod(secret, 0o600)
        (Path(self.temp.name) / "config.json").write_text(json.dumps({"extractor_backend": "provider"}))

    def tearDown(self):
        self.state.db.close()
        self.temp.cleanup()

    def test_extractor_dispatches_to_configured_provider(self):
        command = gtd.extractor_command(self.state)
        self.assertEqual(command[0], gtd.PROVIDER_COMMAND)
        reply = {"summary": "ok", "candidates": [], "questions": [], "_usage": {"total_tokens": 5}}
        with patch.object(gtd_provider, "chat_json", return_value=dict(reply)) as call:
            value = gtd.run_extractor(self.state, command, "PROMPT")
        self.assertEqual(value, {"summary": "ok", "candidates": [], "questions": []})
        self.assertIn("JSON Schema", call.call_args.args[1])
        record = json.loads((Path(self.temp.name) / "last-extractor.json").read_text())
        self.assertEqual((record["model"], record["backend"]), ("deepseek-x", "provider"))

    def test_provider_failure_surfaces_as_runtime_error(self):
        with patch.object(gtd_provider, "chat_json", side_effect=gtd_provider.ProviderError("HTTP 503")):
            with self.assertRaises(RuntimeError):
                gtd.run_extractor(self.state, gtd.extractor_command(self.state), "P")

    def test_options_command_works_with_provider_backend(self):
        self.assertEqual(gtd_options.command(self.state)[0], gtd.PROVIDER_COMMAND)

    def test_codex_remains_default(self):
        (Path(self.temp.name) / "config.json").write_text("{}")
        self.assertIsNone(gtd.provider_file(self.state))


class BackfillTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = gtd.State(Path(self.temp.name) / "state")
        # Keep the test away from any real memory summaries in the home directory.
        gtd.atomic_json(self.state.base / "config.json", {"memory_source_enabled": False})
        self.events = [gtd_local._record(f"/s/a.jsonl:{i}", iso(i), "agent.user_message", "local_codex_session",
                                         "Codex", "thesis", f"周五前把第 {i} 稿发给导师") for i in range(3)]

    def tearDown(self):
        self.state.db.close()
        self.temp.cleanup()

    def extract(self, _state, _command, prompt):
        context = json.loads(prompt.split("OBSERVED_DATA_JSON:\n", 1)[1])
        keys = [event["event_key"] for event in context["events"]]
        return {"summary": "合成", "questions": [],
                "candidates": [{"key": "send-draft", "title": "把稿子发给导师", "bucket": "next",
                                "project": "thesis", "suggested_due": None, "event_keys": keys}]}

    def run_backfill(self, **kwargs):
        with patch.object(gtd.gtd_local, "window", return_value=self.events) as window, \
             patch.object(gtd, "extractor_command", return_value=["fixture"]), \
             patch.object(gtd, "run_extractor", side_effect=self.extract):
            return gtd.backfill(self.state, 3, **kwargs), window

    def test_backfill_queues_local_history_and_extracts_candidates(self):
        value, window = self.run_backfill(git_roots=["~/code"])
        self.assertTrue(value["ok"])
        self.assertEqual((value["events"], value["batches"], value["processed_batches"]), (3, 1, 1))
        self.assertEqual(value["by_source"], {"local_codex_session": 3})
        start, end, roots = window.call_args.args
        self.assertAlmostEqual((end - start).total_seconds(), 3 * 86400, delta=1)
        self.assertEqual(roots, [Path("~/code").expanduser()])
        self.assertEqual(window.call_args.kwargs, {"agent_replies": False})
        candidate = self.state.rows("SELECT key, state, evidence FROM candidates")[0]
        self.assertEqual((candidate["key"], candidate["state"]), ("send-draft", "inbox"))
        self.assertEqual(len(json.loads(candidate["evidence"])), 3)
        self.assertFalse(self.state.rows("SELECT * FROM batches WHERE state='pending'"))

    def test_backfill_runs_once_unless_forced(self):
        self.run_backfill()
        with self.assertRaises(ValueError):
            self.run_backfill()
        value, _ = self.run_backfill(force=True)
        self.assertTrue(value["ok"])

    def test_extractor_failure_keeps_batches_for_resume(self):
        with patch.object(gtd.gtd_local, "window", return_value=self.events), \
             patch.object(gtd, "extractor_command", return_value=["fixture"]), \
             patch.object(gtd, "run_extractor", side_effect=RuntimeError("synthetic outage")):
            value = gtd.backfill(self.state, 3)
        self.assertFalse(value["ok"])
        self.assertEqual(value["pending_batches"], 1)
        self.assertIn("synthetic outage", value["error"])

    def test_window_keeps_newest_events_past_the_cap(self) -> None:
        """Filter both session sources before capping, while preserving default replies."""
        events = [gtd_local._record(f"k{i}", iso(i), "git.commit", "local_git_commit", "git", "r", "c")
                  for i in range(5)]
        reply = gtd_local._record("reply", iso(6), "agent.final_answer", "local_codex_session", "Codex", "r", "done")
        claude = {**reply, "event_key": "claude", "source_type": "local_claude_session"}
        with patch.object(gtd_local, "MAX_EVENTS", 2), \
             patch.object(gtd_local, "codex_events", return_value=events + [reply]), \
             patch.object(gtd_local, "claude_events", return_value=[claude]), \
             patch.object(gtd_local, "git_events", return_value=[]):
            kept = gtd_local.window(T0, T0 + dt.timedelta(hours=1), agent_replies=False)
            unfiltered = gtd_local.window(T0, T0 + dt.timedelta(hours=1))
        self.assertEqual([event["event_key"] for event in kept], ["k3", "k4"])
        self.assertEqual([event["event_key"] for event in unfiltered], ["reply", "claude"])


class LocalLiveSourceTest(unittest.TestCase):
    """The local adapter feeds the daemon through spool without Computer History."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = gtd.State(Path(self.temp.name) / "state")
        gtd.atomic_json(self.state.base / "config.json", {"history_source": "local_history", "memory_source_enabled": False})
        self.now = dt.datetime.now(dt.timezone.utc)

    def tearDown(self):
        self.state.db.close()
        self.temp.cleanup()

    def event(self, key, minutes_ago):
        stamp = (self.now - dt.timedelta(minutes=minutes_ago)).isoformat()
        return gtd_local._record(key, stamp, "agent.user_message", "local_codex_session", "Codex", "p", "做 " + key)

    def spool(self, events, baseline=False):
        with patch.object(gtd_local, "window", return_value=events) as window:
            result = gtd.spool(self.state, "", baseline=baseline)
        return result, window

    def queued_keys(self):
        return [e["event_key"] for row in self.state.rows("SELECT payload FROM batches") for e in json.loads(row["payload"])]

    def test_registry_returns_local_adapter(self):
        adapter = gtd_sources.history_adapter({"history_source": "local_history", "local_sources": ["git", "bogus"]})
        self.assertIsInstance(adapter, gtd_local.LocalHistoryAdapter)
        self.assertEqual(adapter.sources, ["git"])

    def test_overlapping_polls_queue_each_event_once(self):
        first, window = self.spool([self.event("a", 5), self.event("b", 2)])
        self.assertEqual((first["records"], first["source_adapter"]), (2, "local_history"))
        start, end = window.call_args.args[:2]
        self.assertAlmostEqual((end - start).total_seconds(), (gtd_local.FIRST_LOOKBACK + gtd_local.OVERLAP).total_seconds(), delta=5)
        second, window = self.spool([self.event("a", 5), self.event("b", 2), self.event("c", 1)])
        self.assertEqual(second["records"], 1)
        self.assertEqual(self.queued_keys(), ["a", "b", "c"])
        cursor = self.state.rows("SELECT offset, signature FROM cursors WHERE path=?", (gtd_local.CURSOR_PATH,))[0]
        self.assertEqual(sorted(json.loads(cursor["signature"])), ["a", "b", "c"])

    def test_baseline_skips_existing_activity(self):
        result, _ = self.spool([self.event("old", 3)], baseline=True)
        self.assertEqual(result["records"], 0)
        self.assertFalse(self.state.rows("SELECT * FROM batches"))
        later, _ = self.spool([self.event("old", 3), self.event("new", 1)])
        self.assertEqual(self.queued_keys(), ["new"])

    def test_cursor_and_batches_share_a_transaction(self):
        with patch.object(gtd_local, "window", return_value=[self.event("x", 1)]), \
             patch.object(gtd, "encode", side_effect=[json.dumps({"x": 1}), RuntimeError("disk full")]):
            with self.assertRaises(RuntimeError):
                gtd.spool(self.state, "")
        self.assertFalse(self.state.rows("SELECT * FROM cursors"))
        self.assertFalse(self.state.rows("SELECT * FROM batches"))

    def test_install_hook_for_local_source_needs_no_root(self):
        with patch.object(gtd_local, "window", return_value=[self.event("before", 1)]), \
             patch.object(gtd, "native_binary", side_effect=AssertionError("CH watcher not needed")), \
             patch.object(gtd, "bootstrap_job", return_value=subprocess.CompletedProcess([], 0, "", "")), \
             patch.object(gtd.subprocess, "run"), patch.object(Path, "home", return_value=Path(self.temp.name)):
            result = gtd.install_hook(self.state, source="local_history")
        self.assertTrue(result["ok"])
        self.assertEqual(result["baseline"]["records"], 0)
        self.assertEqual(self.state.config()["history_source"], "local_history")
        with self.assertRaises(ValueError):
            gtd.install_hook(self.state, source="computer_history")

    def test_status_reports_missing_sources(self):
        empty = Path(self.temp.name) / "none"
        with patch.object(gtd_local, "CODEX_ROOT", empty), patch.object(gtd_local, "CLAUDE_ROOT", empty):
            status = gtd_local.LocalHistoryAdapter({"local_git_roots": [str(empty)]}).status()
        self.assertEqual((status["state"], status["adapter"]), ("unavailable", "local_history"))

    def test_reply_filtering_in_live_window_and_backfill(self) -> None:
        """Default intake excludes replies; explicit opt-in affects all local intake paths."""
        user = self.event("user", 1)
        reply = {**user, "event_key": "reply", "kind": "agent.final_answer"}
        commit = {**user, "event_key": "commit", "kind": "git.commit", "source_type": "local_git_commit"}
        for include in (False, True):
            config = self.state.config()
            if include:
                config["local_include_agent_replies"] = True
            gtd.atomic_json(self.state.base / "config.json", config)
            with self.state.db:
                self.state.db.execute("DELETE FROM cursors")
                self.state.db.execute("DELETE FROM batches")
            with patch.object(gtd_local, "codex_events", return_value=[user, reply]), \
                 patch.object(gtd_local, "claude_events", return_value=[]), \
                 patch.object(gtd_local, "git_events", return_value=[commit]), \
                 patch.object(gtd, "process", return_value={}):
                live = gtd.spool(self.state, "")
                self.assertEqual(live["records"], 3 if include else 2)
                adapter = gtd_local.LocalHistoryAdapter(config)
                events, _ = adapter.window("", self.now - dt.timedelta(hours=1), self.now)
                self.assertEqual(len(events), 3 if include else 2)
                value = gtd.backfill(self.state, 1, force=True)
                self.assertEqual(value["events"], 3 if include else 2)
                payloads = [e for row in self.state.rows("SELECT payload FROM batches") for e in json.loads(row["payload"])]
                self.assertEqual(any(e["kind"] == "agent.final_answer" for e in payloads), include)

    def test_activity_scan_retains_replies_without_own_activity_credit(self) -> None:
        """Thread context still reads replies, but only human events set last activity."""
        user = self.event("user", 5)
        reply = {**self.event("reply", 0), "kind": "agent.final_answer"}
        with patch.object(gtd_local, "codex_events", return_value=[user, reply]), \
             patch.object(gtd_local, "claude_events", return_value=[]), \
             patch.object(gtd_local, "git_events", return_value=[]):
            scanned = gtd_threads.scan_activity(self.now, 1, [], self.state.base / "activity.json")
        self.assertEqual(scanned["p"]["last"], user["timestamp"])
        self.assertEqual(scanned["p"]["replies"], [[reply["timestamp"], reply["text"]]])


if __name__ == "__main__":
    unittest.main()
