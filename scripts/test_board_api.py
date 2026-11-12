"""Tests for the board, undo and memory workbench routes. Synthetic data only; no model or native calls."""
import datetime as dt
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import gtd
import gtd_board_api as api
import gtd_board
import gtd_dashboard as web
import gtd_memory
import gtd_proposals
import gtd_provider


class BoardApiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = gtd.State(Path(self.temp.name))
        self.key = gtd.capture(self.state, "合成线索")["candidate_key"]
        gtd.atomic_json(self.state.base / "last-snapshot.json", {"ok": True, "permissions": {}, "lists": [],
                        "reminders": [{"id": "r1", "title": "交报告", "list": "L", "completed": False,
                                       "due": (dt.date.today() + dt.timedelta(days=1)).isoformat()}], "events": []})
        (self.state.base / "experiments").mkdir()
        gtd.atomic_json(self.state.base / "experiments" / "activity-cache.json",
                        {"stamp": "x", "days": 14, "version": 2, "projects": {}})
        self.popen = patch.object(api.subprocess, "Popen").start()
        patch.object(gtd, "native", side_effect=AssertionError("unexpected native call")).start()
        patch.object(gtd_provider, "chat_json", side_effect=AssertionError("unexpected model call")).start()

    def tearDown(self):
        patch.stopall()
        self.state.db.close()
        self.temp.cleanup()

    def test_board_has_cards_and_no_raw_evidence(self):
        value = web.action(self.state, "/api/board/refresh", {})
        payload = json.dumps(value, ensure_ascii=False)
        self.assertIn("交报告", payload)
        self.assertNotIn("evidence", payload)
        self.assertTrue(all(set(card) <= set(api.CARD_FIELDS) for card in value["items"]))
        self.assertIn("count", value["unsorted"])

    def test_feedback_can_be_undone_and_both_are_logged(self):
        card = next(i for i in api.board(self.state)["items"] if i["title"] == "交报告")
        moved = web.action(self.state, "/api/board/feedback",
                           {"item_id": card["id"], "action": "move", "value": 4, "title": "交报告"})
        self.assertEqual(next(i for i in moved["items"] if i["id"] == card["id"])["quadrant"], 4)
        restored = web.action(self.state, "/api/board/undo", {"token": moved["undo"]})
        self.assertEqual(next(i for i in restored["items"] if i["id"] == card["id"])["quadrant"], card["quadrant"])
        log = [json.loads(line) for line in (self.state.base / "memory" / "feedback.jsonl").read_text().splitlines()]
        self.assertEqual([entry["action"] for entry in log], ["move", "undo"])
        self.assertEqual(log[-1]["title"], "交报告")
        with self.assertRaises(ValueError):          # one undo per action
            web.action(self.state, "/api/board/undo", {"token": moved["undo"]})

    def test_undo_expires_and_rejects_foreign_tokens(self):
        card = api.board(self.state)["items"][0]
        token = web.action(self.state, "/api/board/feedback", {"item_id": card["id"], "action": "move", "value": 3})["undo"]
        with self.assertRaises(ValueError):
            web.action(self.state, "/api/board/undo", {"token": "someone-else"})
        later = api.now() + dt.timedelta(minutes=api.UNDO_MINUTES + 1)
        with patch.object(api, "now", return_value=later), self.assertRaises(ValueError):
            web.action(self.state, "/api/board/undo", {"token": token})

    def test_invalid_feedback_leaves_no_undo_snapshot(self):
        with self.assertRaises(ValueError):
            web.action(self.state, "/api/board/feedback", {"item_id": "nope", "action": "move", "value": 2})
        self.assertFalse((self.state.base / "memory" / "undo.json").exists())

    def test_memory_profile_and_project_stage_are_saved_privately(self):
        value = web.action(self.state, "/api/memory/profile", {"text": "我做完能用就转工具。"})
        self.assertEqual(value["profile"], "我做完能用就转工具。")
        self.assertEqual(os.stat(self.state.base / "memory" / "profile.md").st_mode & 0o777, 0o600)
        value = web.action(self.state, "/api/memory/project", {"name": "Alpha", "stage": "paused",
                                                              "next_step": "真机部署", "next_at": "2026-11-20"})
        self.assertEqual(value["projects"]["Alpha"]["stage"], "paused")
        self.assertEqual(gtd_memory.projects(self.state)["Alpha"]["source"], "user:workbench")
        with self.assertRaises(ValueError):
            web.action(self.state, "/api/memory/project", {"name": "Alpha", "stage": "sleeping"})
        with self.assertRaises(ValueError):
            web.action(self.state, "/api/memory/profile", {"text": "x" * (api.PROFILE_LIMIT + 1)})

    def test_background_refresh_is_throttled(self):
        os.utime(self.state.base / "experiments" / "activity-cache.json", (0, 0))     # stale cache
        self.assertTrue(api.start_refresh(self.state))
        self.assertFalse(api.start_refresh(self.state))                              # within throttle
        self.assertEqual(self.popen.call_count, 1)
        args = self.popen.call_args.args[0]
        self.assertEqual(args[-3:], ["--state", str(self.state.base), "build"])

    def propose(self, item_id: str, kind: str = "reschedule", due: str | None = None) -> dict:
        """Persist a synthetic reply proposal without calling a model or a native app."""
        return gtd_proposals.create(self.state, item_id,
                                   {"intent": kind, "due": due, "title": "另写一份报告" if kind == "new_task" else None,
                                    "summary": "用户提出调整"}, "原始答复", api.now())

    def decide(self, proposal: dict, accept: bool) -> dict:
        """Use the actual workbench proposal route to decide a pending proposal."""
        return web.action(self.state, "/api/board/proposal", {"id": proposal["id"], "accept": accept})

    def test_reject_both_proposal_kinds_drops_and_logs(self) -> None:
        """Rejection removes proposals without changing task state or native data."""
        for kind in ("reschedule", "new_task"):
            with self.subTest(kind=kind):
                proposal = self.propose("reminder:r1", kind, "2026-11-20")
                self.assertEqual(api.board(self.state)["items"][0]["proposals"], [proposal])
                board = self.decide(proposal, False)
                self.assertTrue(board["ok"])
                self.assertEqual(gtd_proposals.pending(self.state), [])
                self.assertEqual(api.recent_feedback(self.state)[0]["action"], "proposal_rejected")
        self.assertEqual(len(self.state.rows("SELECT * FROM candidates")), 1)

    def test_accept_new_task_captures_candidate_with_optional_due(self) -> None:
        """Accepted new tasks use capture and retain their proposed suggested_due."""
        for due in (None, (dt.date.today() + dt.timedelta(days=5)).isoformat()):
            with self.subTest(due=due):
                proposal = self.propose("reminder:r1", "new_task", due)
                value = self.decide(proposal, True)
                row = self.state.rows("SELECT * FROM candidates WHERE key=?", (value["candidate_key"],))[0]
                self.assertEqual((row["title"], row["suggested_due"], row["state"]),
                                 ("另写一份报告", due or "", "inbox"))
                self.assertEqual(api.recent_feedback(self.state)[0]["action"], "proposal_accepted")
                with self.assertRaises(ValueError):
                    self.decide(proposal, True)

    def test_due_override_changes_project_due_source_and_expires_pin(self) -> None:
        """A local project date changes its fingerprint, removing its old quadrant pin."""
        gtd_memory.record(self.state, "Alpha", "active")
        gtd_board.record_feedback(self.state, "project:alpha", "move", 4)
        due = (dt.date.today() + dt.timedelta(days=1)).isoformat()
        value = self.decide(self.propose("project:alpha", due=due), True)
        card = next(card for card in value["items"] if card["id"] == "project:alpha")
        self.assertEqual((card["due"], card["due_source"], card["pinned"]), (due, "user", False))
        self.assertEqual(gtd_board.preferences(self.state)["due_overrides"][card["id"]], due)
        self.assertEqual(card["proposals"], [])

    def test_due_override_promotes_undated_candidate_and_updates_event(self) -> None:
        """Due overrides are usable for both unsorted candidates and calendar cards."""
        start = (api.now() + dt.timedelta(days=1)).isoformat()
        snapshot = api.gtd_board.read_json(self.state.base / "last-snapshot.json")
        snapshot["events"] = [{"id": "e1", "title": "项目讨论", "start": start}]
        gtd.atomic_json(self.state.base / "last-snapshot.json", snapshot)
        due = (dt.date.today() + dt.timedelta(days=5)).isoformat()
        identities = ["candidate:" + self.key, "event:e1:" + gtd_board.valid_due(start)]
        for identity in identities:
            with self.subTest(identity=identity):
                value = self.decide(self.propose(identity, due=due), True)
                card = next(card for card in value["items"] if card["id"] == identity)
                self.assertEqual((card["due"], card["due_source"]), (due, "user"))

    def reminder_snapshot(self, version: str | None = "v1") -> dict:
        """Seed one editable reminder and return the isolated authoritative fixture."""
        snapshot = gtd_board.read_json(self.state.base / "last-snapshot.json")
        snapshot["reminders"][0]["modified_at"] = version
        gtd.atomic_json(self.state.base / "last-snapshot.json", snapshot)
        return snapshot

    def test_reminder_acceptance_uses_versioned_apply_plan_and_refreshes(self) -> None:
        """A successful native receipt refreshes the snapshot and rebuilt board date."""
        snapshot = self.reminder_snapshot()
        due = (dt.date.today() + dt.timedelta(days=5)).isoformat()
        proposal = self.propose("reminder:r1", due=due)

        def native(state: gtd.State, command: str, payload: dict) -> dict:
            """Simulate the native version guard and the subsequent authoritative snapshot."""
            if command == "apply":
                self.assertEqual(payload["action"], "update_reminder")
                self.assertEqual(payload["expected_modified_at"], "v1")
                self.assertEqual(payload["id"], "r1")
                snapshot["reminders"][0].update(due=payload["due"], modified_at="v2")
                return {"ok": True, "item": snapshot["reminders"][0]}
            self.assertEqual(command, "snapshot")
            return snapshot

        with patch.object(gtd, "native", side_effect=native) as write:
            value = self.decide(proposal, True)
        self.assertEqual(write.call_count, 2)
        card = next(card for card in value["items"] if card["id"] == "reminder:r1")
        self.assertEqual((card["due"], card["due_source"]), (due, "reminder"))
        operation = self.state.rows("SELECT * FROM operations")[0]
        self.assertEqual((operation["op_id"], operation["state"]), (proposal["id"], "applied"))

    def test_reminder_missing_version_stops_before_native_write(self) -> None:
        """Native dates cannot be changed from an unversioned snapshot."""
        proposal = self.propose("reminder:r1", due="2026-11-20")
        with self.assertRaises(ValueError):
            self.decide(proposal, True)
        self.assertEqual(gtd_proposals.pending(self.state), [proposal])
        self.assertEqual(self.state.rows("SELECT * FROM operations"), [])

    def test_reminder_conflict_and_uncertain_write_stop_retries(self) -> None:
        """A conflict or unknown outcome keeps the proposal and never issues another write."""
        for error in (RuntimeError("conflict: item changed after the supplied snapshot"), OSError("lost receipt")):
            with self.subTest(error=error):
                self.reminder_snapshot()
                proposal = self.propose("reminder:r1", due="2026-11-20")
                with patch.object(gtd, "native", side_effect=error) as write:
                    with self.assertRaises((RuntimeError, OSError)):
                        self.decide(proposal, True)
                    self.assertEqual(write.call_args.args[2]["expected_modified_at"], "v1")
                    self.reminder_snapshot("v2")
                    with self.assertRaisesRegex(RuntimeError, "uncertain"):
                        self.decide(proposal, True)
                self.assertEqual(write.call_count, 1)
                self.assertIn(proposal, gtd_proposals.pending(self.state))

    def test_applied_reminder_receipt_survives_refresh_failure(self) -> None:
        """A successful write is accepted even if reading the new snapshot is unavailable."""
        self.reminder_snapshot()
        proposal = self.propose("reminder:r1", due="2026-11-20")
        with patch.object(gtd, "native", side_effect=[{"ok": True, "item": {"id": "r1"}}, OSError("no snapshot")]):
            value = self.decide(proposal, True)
        self.assertTrue(value["refresh_pending"])
        self.assertNotIn(proposal, gtd_proposals.pending(self.state))

    def test_uncertain_native_proposal_cannot_turn_local_after_merge_changes(self) -> None:
        """Persisted native plans retain their guard when an absorbed candidate reappears."""
        snapshot = self.reminder_snapshot()
        key = gtd.capture(self.state, "交报告")["candidate_key"]
        with self.state.db:
            self.state.db.execute("UPDATE candidates SET suggested_due=? WHERE key=?",
                                  (snapshot["reminders"][0]["due"], key))
        proposal = self.propose("candidate:" + key, due="2026-11-20")
        with patch.object(gtd, "native", side_effect=OSError("lost receipt")) as native:
            with self.assertRaises(OSError):
                self.decide(proposal, True)
            snapshot["reminders"] = []
            gtd.atomic_json(self.state.base / "last-snapshot.json", snapshot)
            with self.assertRaisesRegex(RuntimeError, "uncertain"):
                self.decide(proposal, True)
        self.assertEqual(native.call_count, 1)
        self.assertEqual(gtd_board.preferences(self.state)["due_overrides"], {})
        self.assertEqual(gtd_proposals.pending(self.state), [proposal])

    def test_proposal_on_absorbed_candidate_uses_surviving_reminder(self) -> None:
        """Acceptance resolves proposal aliases before selecting the guarded native path."""
        snapshot = self.reminder_snapshot()
        key = gtd.capture(self.state, "交报告")["candidate_key"]
        with self.state.db:
            self.state.db.execute("UPDATE candidates SET suggested_due=? WHERE key=?",
                                  (snapshot["reminders"][0]["due"], key))
        proposal = self.propose("candidate:" + key, due="2026-11-20")
        with patch.object(gtd, "native", side_effect=[{"ok": True, "item": {"id": "r1"}}, snapshot]) as native:
            self.decide(proposal, True)
        self.assertEqual(native.call_args_list[0].args[2]["id"], "r1")
        self.assertEqual(native.call_args_list[0].args[2]["expected_modified_at"], "v1")

    def test_proposal_decision_requires_boolean_accept(self) -> None:
        """Invalid decisions cannot accidentally approve pending native operations."""
        proposal = self.propose("reminder:r1", due="2026-11-20")
        for accept in (None, "false", 0, 1):
            with self.subTest(accept=accept), self.assertRaises(ValueError):
                api.post(self.state, "/api/board/proposal", {"id": proposal["id"], "accept": accept})
        self.assertEqual(gtd_proposals.pending(self.state), [proposal])


if __name__ == "__main__":
    unittest.main()
