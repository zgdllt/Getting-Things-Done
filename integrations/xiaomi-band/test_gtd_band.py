"""Tests for gtd_band.py against a fake GTD Band Bridge plugin (no band, no real state)."""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gtd_band  # noqa: E402

TOKEN = "t" * 64


class FakeBridge:
    """Implements the plugin HTTP API used by gtd_band, with seq + ack semantics."""

    def __init__(self) -> None:
        self.asks: List[Dict[str, Any]] = []
        self.withdrawn: List[str] = []
        self.events: List[Dict[str, Any]] = []
        self.seq = 0
        self.delivered = True
        self.ignore_ack = False
        self.code = "123456"
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def reply(self, status: int, body: Dict[str, Any]) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def authorized(self) -> bool:
                return self.headers.get("Authorization") == "Bearer " + TOKEN

            def do_GET(self) -> None:
                if not self.authorized():
                    return self.reply(401, {"ok": False})
                if self.path == "/v1/events":
                    return self.reply(200, {"ok": True, "events": list(fake.events)})
                self.reply(404, {"ok": False})

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if self.path == "/v1/pair":
                    if body.get("code") != fake.code:
                        return self.reply(403, {"ok": False})
                    return self.reply(200, {"ok": True, "token": TOKEN, "package": gtd_band.PACKAGE})
                if not self.authorized():
                    return self.reply(401, {"ok": False})
                if self.path == "/v1/ask":
                    fake.asks.append(body)
                    return self.reply(200, {"ok": True, "queued": True, "delivered": fake.delivered})
                if self.path == "/v1/withdraw":
                    fake.withdrawn.append(body["id"])
                    return self.reply(200, {"ok": True})
                if self.path == "/v1/events/ack":
                    if not fake.ignore_ack:
                        fake.events = [e for e in fake.events if e["seq"] > body["upto"]]
                    return self.reply(200, {"ok": True})
                self.reply(404, {"ok": False})

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def answer(self, question_key: str, choice: str, shown: int = 1_000, answered: int = 4_000) -> None:
        self.seq += 1
        self.events.append({"v": 1, "type": "answer", "id": question_key, "choice": choice, "seq": self.seq,
                            "shownAt": shown, "answeredAt": answered, "receivedAt": answered + 50})

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class BandTestBase(unittest.TestCase):
    """Temporary GTD state + fake plugin. The model is always faked: no Codex call ever runs."""

    def setUp(self) -> None:
        self.base_saved = (gtd_band.gtd.run_extractor, gtd_band.band_options.command, gtd_band.gtd_clarify.launch)
        gtd_band.gtd.run_extractor = lambda state, command, prompt, record: {"options": []}
        gtd_band.band_options.command = lambda state: ["codex", "exec"]
        gtd_band.gtd_clarify.launch = lambda state, result: result
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.state = gtd_band.open_state(self.base)
        self.bridge = FakeBridge()
        self.client = gtd_band.BridgeClient(self.bridge.url, TOKEN)
        now = gtd_band.gtd.now()
        with self.state.db:
            for i, cand_state in enumerate(["inbox", "inbox", "accepted"]):
                self.state.db.execute("INSERT INTO candidates VALUES(?,?,?,?,?,?,?,?,?,?)",
                                      (f"c{i}", f"候选{i}", "inbox", "", None, "[]", cand_state, None, now, now))
                self.state.db.execute("INSERT INTO questions VALUES(?,?,?,?,?,?,?)",
                                      (f"q{i}", f"c{i}", f"问题{i}？", "open", None, None, f"2026-11-0{i + 1}"))

    def tearDown(self) -> None:
        self.bridge.close()
        self.state.db.close()
        self.temp.cleanup()
        (gtd_band.gtd.run_extractor, gtd_band.band_options.command, gtd_band.gtd_clarify.launch) = self.base_saved

    def cand_state(self, key: str) -> str:
        return self.state.db.execute("SELECT state FROM candidates WHERE key=?", (key,)).fetchone()["state"]

    def outcome(self) -> str:
        return self.state.db.execute("SELECT json_extract(detail,'$.outcome') o FROM band_events "
                                     "WHERE kind='answer' ORDER BY id DESC").fetchone()["o"]


class GtdBandTest(BandTestBase):

    # -- pushing -------------------------------------------------------------

    def test_pushes_one_question_at_a_time(self) -> None:
        self.assertEqual(gtd_band.push(self.state, self.client), "q0")
        self.assertIsNone(gtd_band.push(self.state, self.client), "second push must wait for an answer")
        self.assertEqual([a["id"] for a in self.bridge.asks], ["q0"])
        self.assertEqual(self.bridge.asks[0]["title"], "候选0")
        self.assertTrue(self.bridge.asks[0]["replace"])

    def test_question_answered_on_mac_is_withdrawn_from_band(self) -> None:
        gtd_band.push(self.state, self.client)
        with self.state.db:
            self.state.db.execute("UPDATE questions SET state='answered', answer='mac' WHERE key='q0'")
        self.assertEqual(gtd_band.push(self.state, self.client), "q1")
        self.assertEqual(self.bridge.withdrawn, ["q0"])

    def test_undelivered_push_blocks_only_briefly_and_rotates(self) -> None:
        self.bridge.delivered = False
        self.assertEqual(gtd_band.push(self.state, self.client), "q0")
        self.assertIsNone(gtd_band.push(self.state, self.client))
        with self.state.db:  # pretend the undelivered push happened 11 minutes ago
            self.state.db.execute("UPDATE band_events SET at=? WHERE kind='pushed'", (gtd_band.ago(minutes=11),))
        self.assertEqual(gtd_band.push(self.state, self.client), "q1", "fewer pushes wins over age")

    # -- answers -------------------------------------------------------------

    def test_dismiss_closes_candidate_and_questions(self) -> None:
        gtd_band.push(self.state, self.client)
        self.bridge.answer("q0", "dismiss")
        self.assertEqual(gtd_band.sync(self.state, self.client), 1)
        self.assertEqual(self.cand_state("c0"), "dismissed")
        row = self.state.db.execute("SELECT state FROM questions WHERE key='q0'").fetchone()
        self.assertEqual(row["state"], "answered")
        self.assertEqual(self.bridge.events, [], "events acked after journaling")
        self.assertEqual(gtd_band.push(self.state, self.client), "q1")

    def test_stale_dismiss_does_not_touch_candidate(self) -> None:
        gtd_band.push(self.state, self.client)
        with self.state.db:
            self.state.db.execute("UPDATE questions SET state='answered', answer='mac' WHERE key='q0'")
        self.bridge.answer("q0", "dismiss")
        gtd_band.sync(self.state, self.client)
        self.assertEqual(self.cand_state("c0"), "inbox")
        self.assertEqual(self.outcome(), "stale")

    def test_later_and_mac_do_not_change_gtd_and_are_not_repushed(self) -> None:
        gtd_band.push(self.state, self.client)
        self.bridge.answer("q0", "later")
        gtd_band.sync(self.state, self.client)
        self.assertEqual(self.cand_state("c0"), "inbox")
        self.assertEqual(gtd_band.push(self.state, self.client), "q1")
        self.bridge.answer("q1", "mac")
        gtd_band.sync(self.state, self.client)
        self.assertIsNone(gtd_band.push(self.state, self.client), "accepted candidates are never pushed")

    def test_never_dismisses_formal_items(self) -> None:
        with self.state.db:
            gtd_band.journal(self.state, "pushed", "q2", applied=1, detail={"delivered": True})
        self.bridge.answer("q2", "dismiss")
        gtd_band.sync(self.state, self.client)
        self.assertEqual(self.cand_state("c2"), "accepted")
        self.assertEqual(self.outcome(), "candidate_not_dismissible")

    def test_redelivered_events_are_journaled_once(self) -> None:
        gtd_band.push(self.state, self.client)
        self.bridge.answer("q0", "later")
        self.bridge.ignore_ack = True  # e.g. the ack response was lost
        gtd_band.sync(self.state, self.client)
        gtd_band.sync(self.state, self.client)
        count = self.state.db.execute("SELECT COUNT(*) n FROM band_events WHERE kind='answer'").fetchone()["n"]
        self.assertEqual(count, 1)

    def test_busy_gtd_retries_dismiss(self) -> None:
        gtd_band.push(self.state, self.client)
        self.bridge.answer("q0", "dismiss")
        with self.state.lock("operations.lock"):
            other = gtd_band.gtd.State(self.base)
            self.assertEqual(gtd_band.sync(other, self.client), 0)
            other.db.close()
        self.assertEqual(self.cand_state("c0"), "inbox")
        self.assertEqual(gtd_band.apply_pending(self.state), 1)
        self.assertEqual(self.cand_state("c0"), "dismissed")

    def test_report_measures_answer_rate_and_latency(self) -> None:
        gtd_band.push(self.state, self.client)
        self.bridge.answer("q0", "later", shown=10_000, answered=13_000)
        gtd_band.sync(self.state, self.client)
        result = gtd_band.report(self.state)
        self.assertEqual(result["pushed"], 1)
        self.assertEqual(result["answer_rate"], 1.0)
        self.assertEqual(result["median_seconds_shown_to_answer"], 3.0)
        self.assertEqual(result["choices"]["later"], 1)

    # -- access --------------------------------------------------------------

    def test_pairing_stores_private_token_and_discovery_uses_it(self) -> None:
        with self.assertRaises(gtd_band.BridgeError):
            gtd_band.pair(self.base, "000000", None, self.bridge.port)
        gtd_band.pair(self.base, "123456", None, self.bridge.port)
        path = self.base / gtd_band.PAIRING_FILE
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        client = gtd_band.BridgeClient.discover(self.base)
        self.assertEqual(client.call("GET", "/v1/events")["ok"], True)

    def test_rejects_wrong_token_and_non_loopback_urls(self) -> None:
        with self.assertRaises(gtd_band.BridgeError):
            gtd_band.BridgeClient(self.bridge.url, "wrong").call("GET", "/v1/events")
        with self.assertRaises(gtd_band.BridgeError):
            gtd_band.BridgeClient("http://10.0.0.1:47913", TOKEN)

    def test_broken_bridge_file_is_a_bridge_error(self) -> None:
        broken = self.base / "half.json"
        broken.write_text('{"url": "http://127.0.0.1:1"')
        with self.assertRaises(gtd_band.BridgeError):
            gtd_band.BridgeClient.discover(self.base, broken)

    def test_find_bridge_picks_newest_record_for_this_package(self) -> None:
        root = self.base / "astrobox"
        for name, started, package in [("a", 1, gtd_band.PACKAGE), ("b", 5, gtd_band.PACKAGE), ("c", 9, "other")]:
            (root / name).mkdir(parents=True)
            (root / name / "bridge.json").write_text(json.dumps(
                {"url": f"http://127.0.0.1:{started}", "token": "x", "package": package, "startedAt": started}))
        self.assertEqual(gtd_band.find_bridge(root)["url"], "http://127.0.0.1:5")


class OptionsTest(BandTestBase):
    """Model-generated answer options; the model and answer worker are faked."""

    def setUp(self) -> None:
        super().setUp()
        self.prompts: List[str] = []
        self.launched: List[str] = []
        self.reply: Any = {"options": ["要跟进，等结果", "已完成", "不用跟进"]}
        band_options = gtd_band.band_options
        self.saved = self.base_saved

        def fake_run(state: Any, command: List[str], prompt: str, record: str) -> Dict[str, Any]:
            self.prompts.append(prompt)
            if isinstance(self.reply, Exception):
                raise self.reply
            return self.reply

        def fake_launch(state: Any, result: Dict[str, Any]) -> Dict[str, Any]:
            self.launched.append(result["job_id"])
            return result

        gtd_band.gtd.run_extractor = fake_run
        band_options.command = lambda state: ["codex", "exec"]
        gtd_band.gtd_clarify.launch = fake_launch

    def answer_option(self, key: str, index: int, text: str) -> None:
        self.bridge.answer(key, "option")
        self.bridge.events[-1].update({"option": index, "text": text})

    def test_clean_keeps_short_distinct_non_reserved_options(self) -> None:
        clean = gtd_band.band_options.clean
        self.assertEqual(clean(["已完成", "已完成。", "到电脑处理", "这是一个远远超过十二个汉字长度限制的选项", "等 Codex"]),
                         ["已完成", "等 Codex"])
        self.assertEqual(clean(["只有一个"]), [], "fewer than two options is not a choice")
        self.assertEqual(clean("not a list"), [])

    def test_push_sends_generated_options_once(self) -> None:
        gtd_band.push(self.state, self.client)
        self.assertEqual(self.bridge.asks[0]["options"], ["要跟进，等结果", "已完成", "不用跟进"])
        self.assertIn("问题0？", self.prompts[0])
        self.assertEqual(gtd_band.band_options.get_or_generate(self.state, "q0")[0], "要跟进，等结果")
        self.assertEqual(len(self.prompts), 1, "options are cached per question")

    def test_option_answer_saves_text_and_starts_answer_job(self) -> None:
        gtd_band.push(self.state, self.client)
        self.answer_option("q0", 1, "已完成")
        gtd_band.sync(self.state, self.client)
        row = self.state.db.execute("SELECT state, answer FROM questions WHERE key='q0'").fetchone()
        self.assertEqual((row["state"], row["answer"]), ("answered", "已完成"))
        job = self.state.db.execute("SELECT id, state FROM answer_jobs").fetchone()
        self.assertEqual(job["state"], "pending")
        self.assertEqual(self.launched, [job["id"]])
        self.assertEqual(self.outcome(), "answered")
        self.assertEqual(gtd_band.report(self.state)["choices"]["option"], 1)

    def test_option_for_question_answered_elsewhere_is_stale(self) -> None:
        gtd_band.push(self.state, self.client)
        with self.state.db:
            self.state.db.execute("UPDATE questions SET state='answered', answer='mac' WHERE key='q0'")
        self.answer_option("q0", 0, "要跟进，等结果")
        gtd_band.sync(self.state, self.client)
        self.assertEqual(self.state.db.execute("SELECT answer FROM questions WHERE key='q0'").fetchone()[0], "mac")
        self.assertEqual(self.outcome(), "stale")
        self.assertEqual(self.launched, [])

    def test_model_failure_falls_back_to_fixed_buttons_and_is_not_retried_at_once(self) -> None:
        self.reply = RuntimeError("Codex extractor failed")
        gtd_band.push(self.state, self.client)
        self.assertEqual(self.bridge.asks[0]["options"], [])
        self.assertEqual(gtd_band.band_options.cached(self.state, "q0"), [])
        self.assertEqual(len(self.prompts), 1)

    def test_prefetch_prepares_the_next_question_not_the_current_one(self) -> None:
        gtd_band.push(self.state, self.client)
        self.assertEqual(gtd_band.prefetch(self.state), "q1")
        self.assertIsNone(gtd_band.prefetch(self.state), "already cached")

    def test_command_lowers_only_reasoning_effort(self) -> None:
        gtd_band.band_options.command = self.saved[1]
        original = gtd_band.gtd.extractor_command
        gtd_band.gtd.extractor_command = lambda state, schema=None: ["codex", "--model", "m", "--cd", "/x", "-"]
        try:
            cmd = gtd_band.band_options.command(self.state)
        finally:
            gtd_band.gtd.extractor_command = original
        self.assertEqual(cmd, ["codex", "--model", "m", "-c", 'model_reasoning_effort="low"', "--cd", "/x", "-"])


if __name__ == "__main__":
    unittest.main()
