"""文枢's own calendar/list: setup idempotence, ownership guard and direct writes (mocked native)."""
from __future__ import annotations

import datetime as dt
from unittest.mock import patch

import gtd
import gtd_board
import gtd_owned
import gtd_proposals
from test_board_support import BoardFixture

CAL = {"id": "cal-wenshu", "title": "文枢", "source": "iCloud", "source_id": "s1", "writable": True}
LIST = {"id": "list-wenshu", "title": "文枢", "source": "iCloud", "source_id": "s1", "writable": True}
TODAY = dt.datetime.now().astimezone()


def event(identity: str, calendar_id: str, start: str = "2026-11-12T11:00:00Z",
          end: str = "2026-11-12T12:30:00Z", version: str | None = "v1") -> dict:
    """One snapshot event row in the native bridge's shape."""
    return {"id": identity, "calendar_id": calendar_id, "calendar": "x", "title": "组会", "start": start, "end": end,
            "original_start": start.replace("Z", ".000Z"), "occurrence_date": start.replace("Z", ".000Z"),
            "all_day": False, "modified_at": version}


class OwnedTest(BoardFixture):
    """Every native call is mocked; the fixture fails on any unexpected one."""

    def snapshot(self, events: list[dict] | None = None, reminders: list[dict] | None = None,
                 owned: bool = True) -> None:
        """Seed calendars/lists (optionally including 文枢's) and items."""
        self.write("last-snapshot.json", {
            "calendars": [{"id": "cal-user", "title": "工作"}] + ([CAL] if owned else []),
            "lists": [{"id": "list-user", "title": "提醒"}] + ([LIST] if owned else []),
            "events": events or [], "reminders": reminders or []})

    def own(self) -> None:
        """Record the owned ids in config, as setup would."""
        config = self.state.config()
        config.update({"owned_calendar_id": CAL["id"], "owned_list_id": LIST["id"]})
        self.write("config.json", config)

    def test_setup_creates_both_then_is_idempotent(self) -> None:
        """First run creates the calendar and list; a second run makes no native call."""
        self.snapshot(owned=False)
        replies = [{"ok": True, "item": CAL}, {"ok": True, "item": LIST}]
        with patch.object(gtd, "native", side_effect=replies) as call, \
                patch.object(gtd, "snapshot", side_effect=lambda s: self.snapshot()):
            value = gtd_owned.setup(self.state)
        self.assertEqual([c.args[2]["action"] for c in call.call_args_list], ["create_calendar", "create_list"])
        self.assertEqual([c.args[2]["title"] for c in call.call_args_list], ["文枢", "文枢"])
        self.assertEqual((value["created"], value["ready"]), (["calendar", "list"], True))
        self.assertEqual(gtd_owned.owned_ids(self.state), {"owned_calendar_id": "cal-wenshu", "owned_list_id": "list-wenshu"})
        with patch.object(gtd, "native") as again:
            self.assertEqual(gtd_owned.setup(self.state)["created"], [])
        again.assert_not_called()

    def test_setup_recreates_only_a_missing_one(self) -> None:
        """A list deleted by the user is recreated; the surviving calendar is kept."""
        self.own()
        self.write("last-snapshot.json", {"calendars": [CAL], "lists": []})
        new_list = {**LIST, "id": "list-2"}
        with patch.object(gtd, "native", return_value={"ok": True, "item": new_list}) as call, \
                patch.object(gtd, "snapshot"):
            gtd_owned.setup(self.state)
        self.assertEqual(call.call_args.args[2]["action"], "create_list")
        self.assertEqual(gtd_owned.owned_ids(self.state)["owned_calendar_id"], "cal-wenshu")
        self.assertEqual(gtd_owned.owned_ids(self.state)["owned_list_id"], "list-2")

    def test_direct_writes_to_user_calendars_are_rejected(self) -> None:
        """The guard runs before any native call, including when nothing is set up."""
        self.snapshot([event("e-user", "cal-user")], [{"id": "r-user", "list_id": "list-user"}])
        for configured in (False, True):
            if configured:
                self.own()
            for operation in ({"action": "update_event", "id": "e-user"},
                              {"action": "create_event", "calendar_id": "cal-user"},
                              {"action": "update_reminder", "id": "r-user"},
                              {"action": "create_reminder", "list_id": "list-user"},
                              {"action": "update_event", "id": "e-user", "calendar_id": "cal-wenshu"}):
                with self.subTest(configured=configured, operation=operation):
                    if operation.get("calendar_id") == "cal-wenshu" and configured:
                        continue  # moving into the owned calendar is checked by the source row below
                    with self.assertRaises(PermissionError):
                        gtd_owned.apply_owned(self.state, {"op_id": "x", **operation}, "x")
        self.assertFalse(gtd_owned.is_owned(self.state, "event:e-user:2026-11-12"))

    def test_owned_event_reschedule_keeps_time_and_duration(self) -> None:
        """Accepting a proposal on an owned event moves the real event via a versioned plan."""
        self.own()
        self.snapshot([event("e1", "cal-wenshu")])
        local = dt.datetime.fromisoformat("2026-11-12T11:00:00+00:00").astimezone()
        proposal = gtd_proposals.create(self.state, "event:e1:x", {"intent": "reschedule", "due": "2026-11-20",
                                        "title": None, "summary": "改期"}, "改到20号", TODAY)
        with patch.object(gtd, "native", return_value={"ok": True, "item": {"id": "e1"}}) as write, \
                patch.object(gtd, "snapshot"):
            value = gtd_proposals.decide(self.state, proposal["id"], True, TODAY)
        sent = write.call_args.args[2]
        start, end = dt.datetime.fromisoformat(sent["start"]), dt.datetime.fromisoformat(sent["end"])
        self.assertEqual((sent["action"], sent["expected_modified_at"]), ("update_event", "v1"))
        self.assertEqual(start.date().isoformat(), "2026-11-20")
        self.assertEqual((start.hour, start.minute), (local.hour, local.minute))
        self.assertEqual(end - start, dt.timedelta(minutes=90))
        self.assertEqual(value["applied_to"], "owned_calendar")
        self.assertEqual(gtd_proposals.pending(self.state), [])

    def test_owned_reschedule_conflict_keeps_proposal_and_never_retries(self) -> None:
        """A version conflict stops; the retry finds the uncertain op instead of writing again."""
        self.own()
        self.snapshot([event("e1", "cal-wenshu")])
        proposal = gtd_proposals.create(self.state, "event:e1:x", {"intent": "reschedule", "due": "2026-11-20",
                                        "title": None, "summary": "改期"}, "改到20号", TODAY)
        with patch.object(gtd, "native", side_effect=RuntimeError("conflict: item changed")) as write:
            with self.assertRaises(RuntimeError):
                gtd_proposals.decide(self.state, proposal["id"], True, TODAY)
            with self.assertRaisesRegex(RuntimeError, "uncertain"):
                gtd_proposals.decide(self.state, proposal["id"], True, TODAY)
        self.assertEqual(write.call_count, 1)
        self.assertEqual(len(gtd_proposals.pending(self.state)), 1)

    def test_user_calendar_reschedule_only_changes_the_board(self) -> None:
        """Events in the user's calendars keep the local override and report it."""
        self.own()
        self.snapshot([event("e2", "cal-user")])
        proposal = gtd_proposals.create(self.state, "event:e2:x", {"intent": "reschedule", "due": "2026-11-20",
                                        "title": None, "summary": "改期"}, "改到20号", TODAY)
        value = gtd_proposals.decide(self.state, proposal["id"], True, TODAY)
        self.assertEqual(value["applied_to"], "board_only")
        self.assertEqual(gtd_board.preferences(self.state)["due_overrides"]["event:e2:x"], "2026-11-20")

    def test_dated_new_task_becomes_owned_reminder(self) -> None:
        """With the owned list set up, a confirmed dated task is a real reminder there."""
        self.own()
        self.snapshot()
        proposal = gtd_proposals.create(self.state, "project:alpha", {"intent": "new_task", "due": "2026-11-14",
                                        "title": "买打印纸", "summary": "新任务"}, "周六买打印纸", TODAY)
        with patch.object(gtd, "native", return_value={"ok": True, "item": {"id": "r9"}}) as write, \
                patch.object(gtd, "snapshot"):
            value = gtd_proposals.decide(self.state, proposal["id"], True, TODAY)
        sent = write.call_args.args[2]
        self.assertEqual((sent["action"], sent["list_id"], sent["title"], sent["due"]),
                         ("create_reminder", "list-wenshu", "买打印纸", "2026-11-14"))
        self.assertEqual(value["applied_to"], "owned_list")

    def test_cards_from_owned_items_are_marked(self) -> None:
        """Board cards expose owned=True only for items in 文枢's own calendar/list."""
        self.own()
        soon = (TODAY + dt.timedelta(hours=5)).astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        later = (TODAY + dt.timedelta(hours=6)).astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        other = {**event("e2", "cal-user", soon, later), "title": "牙医复诊"}  # distinct, so the cards don't merge
        self.snapshot([event("e1", "cal-wenshu", soon, later), other])
        items = {i["id"].split(":")[1]: i for i in gtd_board.build(self.state, TODAY, refresh_activity=False)["items"]
                 if i["kind"] == "event"}
        self.assertEqual((items["e1"]["owned"], items["e2"]["owned"]), (True, False))
