"""Tests for local history, the custom provider client and the replay experiment. Synthetic data only."""
import datetime as dt
import io
import json
import os
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


if __name__ == "__main__":
    unittest.main()
