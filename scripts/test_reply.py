"""Reply understanding against isolated state and mocked model/native boundaries."""
from __future__ import annotations

import datetime as dt
import json
import unittest
from unittest.mock import patch

import gtd
import gtd_board
import gtd_board_notify
import gtd_memory
import gtd_proposals
import gtd_provider
import gtd_reply
from test_board_support import BoardFixture, NOW


def completion(intent: str, **fields: object) -> dict:
    """Return one valid provider response with explicit nullable fields."""
    return {"intent": intent, "due": None, "waiting_on": None, "title": None,
            "summary": "已理解你的回复", **fields}


class ReplyTest(BoardFixture):
    """Exercise actual collection, parsing and local state changes without network calls."""

    def setUp(self) -> None:
        """Configure a fake private provider and a controllable JSON completion."""
        super().setUp()
        self.write("secrets/provider.json", {"base_url": "https://invalid.test", "api_key": "fake", "model": "fake"})
        self.write("config.json", {"extractor_backend": "provider", "quiet_hours": False})
        model = patch.object(gtd_provider, "chat_json", return_value=completion("note"))
        self.model = model.start()
        self.addCleanup(model.stop)
        self.identity = self.project()

    def reply(self, text: str = "先记一下", identity: str | None = None) -> dict:
        """Feed a real response file through the notification collector exactly once."""
        folder = self.state.base / "notifications"
        folder.mkdir(exist_ok=True)
        self.write("notifications/one.response.json", {"action": "reply", "item_id": identity or self.identity, "text": text})
        result = gtd_board_notify.collect_responses(self.state, NOW)[0]
        self.assertFalse(list(folder.glob("*.response.json")))
        return result["interpretation"]

    def events(self) -> list[dict]:
        """Read the chronological feedback evidence from this synthetic state only."""
        return [json.loads(line) for line in (self.state.base / "memory/feedback.jsonl").read_text().splitlines()]

    def test_project_status_intents_act_immediately(self) -> None:
        """Completion, priority, progress, blocked and tool replies reuse board feedback."""
        for intent in ("done", "not_important", "doing", "blocked", "tool"):
            with self.subTest(intent=intent):
                name = "Project" + intent
                identity = self.project(name)
                self.model.return_value = completion(intent)
                self.reply(identity=identity)
                if intent in ("done", "tool", "blocked", "doing"):
                    self.assertEqual(gtd_memory.projects(self.state)[name]["stage"],
                                     "active" if intent == "doing" else intent)
                if intent in ("done", "tool", "not_important"):
                    self.assertNotIn(identity, self.build())
                if intent == "doing":
                    self.assertEqual(self.build()[identity]["status"], "doing")
                self.assertEqual(self.events()[-1]["intent"], intent)

    def test_blocked_and_tool_on_nonprojects_remain_notes(self) -> None:
        """A task cannot acquire a project-only stage."""
        identity = self.candidate(due="2026-11-13", project=None)
        for intent in ("blocked", "tool"):
            self.model.return_value = completion(intent)
            self.reply(identity=identity)
        self.assertEqual(len(gtd_board.preferences(self.state)["notes"][identity]), 2)
        self.assertIn(identity, self.build())

    def test_later_snoozes_for_four_hours(self) -> None:
        """A natural-language snooze has exactly the later button's behavior."""
        self.model.return_value = completion("later")
        self.reply("晚点提醒我")
        self.assertEqual(gtd_board.preferences(self.state)["snoozed"][self.identity],
                         (NOW + dt.timedelta(hours=4)).isoformat())

    def test_waiting_records_who_without_changing_status(self) -> None:
        """Waiting retains both the original reply and its explicit waiting object."""
        self.model.return_value = completion("waiting", waiting_on="小王确认")
        self.reply("等小王确认")
        note = self.build()[self.identity]["notes"][0]
        self.assertEqual((note["text"], note["waiting_on"]), ("等小王确认", "小王确认"))
        self.assertEqual(gtd_memory.projects(self.state)["Alpha"]["stage"], "active")

    def test_relative_friday_and_tomorrow_receive_local_clock_and_context(self) -> None:
        """The provider receives a fixed Wednesday and returns the stated relative dates."""
        for text, due in (("改到周五", "2026-11-13"), ("改到明天", "2026-11-12")):
            with self.subTest(text=text):
                self.model.return_value = completion("reschedule", due=due, _usage={"total_tokens": 1})
                self.reply(text)
                payload = json.loads(self.model.call_args.args[2])
                self.assertEqual(payload["now"], NOW.isoformat())
                self.assertTrue(payload["timezone"])
                self.assertEqual(payload["project_stage"], "active")
                self.assertEqual(set(payload["card"]), {"title", "kind", "project", "status", "due", "quadrant"})
                self.assertIn("不可信", self.model.call_args.args[1])
                self.assertIn("绝不", self.model.call_args.args[1])
                self.assertEqual(gtd_proposals.pending(self.state)[-1]["due"], due)
        self.assertEqual(self.model.call_args.kwargs["retries"], 0)

    def test_new_task_is_pending_until_confirmed(self) -> None:
        """Understanding a new task does not capture it or perform native writes."""
        self.model.return_value = completion("new_task", title="另写一份报告", due="2026-11-13")
        with patch.object(gtd, "capture", side_effect=AssertionError("capture before acceptance")):
            event = self.reply("周五还要另写一份报告")
        proposals = self.build()[self.identity]["proposals"]
        self.assertEqual(proposals[0]["id"], event["proposal_id"])
        self.assertEqual(proposals[0]["title"], "另写一份报告")
        self.assertEqual((self.state.base / "memory/proposals.json").stat().st_mode & 0o777, 0o600)

    def test_notes_keep_five_and_cards_show_three(self) -> None:
        """Storage and card payload have separate bounded note histories."""
        for i in range(7):
            self.model.return_value = completion("note", summary=f"备注{i}")
            gtd_reply.handle(self.state, self.identity, str(i), NOW + dt.timedelta(seconds=i))
        notes = gtd_board.preferences(self.state)["notes"][self.identity]
        self.assertEqual([note["text"] for note in notes], ["2", "3", "4", "5", "6"])
        self.assertEqual([note["text"] for note in self.build()[self.identity]["notes"]], ["6", "5", "4"])

    def test_unconfigured_provider_keeps_note(self) -> None:
        """A reply still becomes visible feedback without any configured model."""
        self.write("config.json", {})
        self.reply("保留这个想法")
        self.model.assert_not_called()
        self.assertEqual(self.build()[self.identity]["notes"][0]["text"], "保留这个想法")
        self.assertEqual([event["action"] for event in self.events()], ["reply", "reply_interpreted"])
        self.assertEqual(self.events()[-1]["title"], "Alpha")

    def test_provider_failure_keeps_note(self) -> None:
        """Transport and missing private provider failures never discard the reply."""
        self.model.side_effect = gtd_provider.ProviderError("offline")
        self.assertEqual(self.reply()["intent"], "note")
        (self.state.base / "secrets/provider.json").unlink()
        self.assertEqual(self.reply()["intent"], "note")

    def test_invalid_completions_become_notes(self) -> None:
        """Every schema violation fails closed to a note without changing project state."""
        invalid = [None, [], "not json", {}, completion("bogus"), completion("done", extra=1),
                   completion("done", summary="长" * 31), completion("done", summary=""),
                   completion("done", summary=" " * 100 + "完成" + " " * 100),
                   completion("doing", due="2026-02-30"), completion("done", due=123),
                   completion("done", due="2026-11-13T12:00:00Z"), completion("reschedule"),
                   completion("new_task"), completion("note", waiting_on=[]), completion("note", title=" ")]
        for value in invalid:
            with self.subTest(value=value):
                self.model.return_value = value
                self.assertEqual(self.reply()["intent"], "note")
                self.assertEqual(gtd_memory.projects(self.state)["Alpha"]["stage"], "active")

    def test_notes_and_proposals_follow_merged_ids(self) -> None:
        """A reminder survivor exposes earlier feedback stored on an absorbed task."""
        identity = self.candidate(project=None, due="2026-11-13", title="提交完整报告")
        self.model.return_value = completion("note")
        self.reply(identity=identity)
        self.model.return_value = completion("reschedule", due="2026-11-20")
        self.reply(identity=identity)
        self.write("last-snapshot.json", {"reminders": [{"id": "r1", "title": "提交完整报告", "due": "2026-11-13"}]})
        card = self.build()["reminder:r1"]
        self.assertIn(identity, card["merged_ids"])
        self.assertEqual(len(card["notes"]), 1)
        self.assertEqual(card["proposals"][0]["item_id"], identity)

    def test_stale_card_reply_is_preserved(self) -> None:
        """Unknown notification identities remain notes without model-driven mutation."""
        event = self.reply(identity="reminder:missing")
        self.model.assert_not_called()
        self.assertEqual(event["intent"], "note")
        self.assertIn("reminder:missing", gtd_board.preferences(self.state)["notes"])

    def test_concurrent_collector_cannot_interpret_the_same_reply_twice(self) -> None:
        """A daemon/API collector arriving during a model call cannot duplicate a proposal."""
        def model(*args: object, **kwargs: object) -> dict:
            """Try a second collection while the first response is still being interpreted."""
            self.assertEqual(gtd_board_notify.collect_responses(self.state, NOW), [])
            return completion("reschedule", due="2026-11-13")

        self.model.side_effect = model
        self.reply("改到周五")
        self.assertEqual(self.model.call_count, 1)
        self.assertEqual(len(gtd_proposals.pending(self.state)), 1)
        self.assertEqual([event["action"] for event in self.events()], ["reply", "reply_interpreted"])


if __name__ == "__main__":
    unittest.main()
