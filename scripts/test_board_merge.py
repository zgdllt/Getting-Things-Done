"""Merge regressions loaded by test_board, using only synthetic state."""
from __future__ import annotations

import datetime as dt

import gtd_board as board
import gtd_memory
from test_board_support import BoardFixture, NOW


class MergeTests(BoardFixture):
    """Exercise cross-source identity, precedence, alias feedback and sorting."""

    def test_same_date_normalized_title_threshold(self) -> None:
        """Merge normalized titles at 0.6 on the same explicit date, including timestamps."""
        self.candidate("a", None, "2026-11-11", title="ABC-DE!")
        self.candidate("b", None, NOW.isoformat(), title="abc-xy")
        self.candidate("c", None, "2026-11-11", title="abwzz")
        self.candidate("d", None, "2026-11-12", title="abcde")
        cards = self.build()
        self.assertEqual(set(cards), {"candidate:a", "candidate:c", "candidate:d"})
        self.assertEqual(cards["candidate:a"]["merged_ids"], ["candidate:b"])
        self.assertEqual(board.read_json(self.state.base / "board.json")["merged"], 1)

    def test_missing_due_dates_are_not_equal_deadlines(self) -> None:
        """Undated equal titles in different projects remain distinct commitments."""
        for key, project in (("a", "Alpha"), ("b", "Beta"), ("c", None), ("d", None)):
            self.candidate(key, project, status="accepted", title="提交报告")
        self.assertEqual(len(self.build()), 4)

    def test_project_next_step_containment_without_matching_date(self) -> None:
        """Fold a candidate into its own project's step even with a different deadline."""
        identity = self.project("Alpha", "paused", next_at="2026-11-11", next_step="部署真机")
        self.candidate("step", "AL-PHA", "2026-11-20", title="明天继续部署真机并验收")
        other = self.candidate("other", "Beta", status="accepted", title="明天继续部署真机并验收")
        cards = self.build()
        self.assertEqual(set(cards), {identity, other})
        self.assertEqual(cards[identity]["merged_ids"], ["candidate:step"])
        self.assertEqual(cards[identity]["quadrant"], 1)

    def test_close_candidates_same_project_ignore_due_date(self) -> None:
        """Merge sibling candidates at 0.85, but keep lower similarity and other projects."""
        self.candidate("a", "Alpha", "2026-11-10", title="abcdefghijklmnopqrst")
        self.candidate("b", "AL-PHA", "2026-11-20", title="abcdefghijklmnopqxyz")
        self.candidate("c", "Alpha", "2026-11-21", title="abcdefghijklmnopwxyz")
        self.candidate("d", "Beta", "2026-11-22", title="abcdefghijklmnopqrst")
        cards = self.build()
        # b/c also match, so transitive merging keeps the complete source group.
        self.assertEqual(set(cards), {"candidate:a", "candidate:d"})
        self.assertEqual(cards["candidate:a"]["merged_ids"], ["candidate:b", "candidate:c"])
        self.candidate("e", "Alpha", "2026-11-23", title="abcdefghijklzzzzzzzz")
        self.assertIn("candidate:e", self.build())

    def test_survivor_priority_strongest_scores_and_parent_remapping(self) -> None:
        """Prefer reminder > event > project > candidate, retaining maximal scores."""
        identity = self.project(stage="paused", next_at="2026-11-11", next_step="提交报告")
        self.declare("Alpha")
        task = self.candidate("step", "Alpha", (NOW - dt.timedelta(hours=1)).isoformat(), title="提交报告")
        child = self.candidate("child", "Alpha", "2026-12-20", title="校对另一份素材")
        self.assertIn(task, self.build()[identity]["merged_ids"])
        event = {"id": "e", "title": "提交报告", "start": (NOW + dt.timedelta(hours=1)).isoformat()}
        self.write("last-snapshot.json", {"events": [event]})
        event_id = f"event:e:{event['start']}"
        self.assertEqual(self.build()[event_id]["merged_ids"], sorted([identity, task]))
        self.write("last-snapshot.json", {"events": [event], "reminders": [
            {"id": "r", "title": "提交报告", "due": "2026-11-11"}]})
        cards = self.build()
        survivor = cards["reminder:r"]
        self.assertEqual(survivor["merged_ids"], sorted([identity, task, event_id]))
        self.assertEqual((survivor["importance"], survivor["urgency"], survivor["quadrant"]), (1.0, 1.0, 1))
        self.assertIn("已逾期（今天）", survivor["urgency_reasons"])
        self.assertIsNone(survivor["parent"])
        self.assertEqual(cards[child]["parent"], "reminder:r")
        self.assertEqual(board.read_json(self.state.base / "board.json")["merged"], 3)

    def test_feedback_on_alias_updates_survivor(self) -> None:
        """Move, snooze and complete a reminder through a merged candidate identity."""
        alias = self.candidate("step", None, "2026-11-11", title="提交报告")
        self.write("last-snapshot.json", {"reminders": [{"id": "r", "title": "提交报告", "due": "2026-11-11"}]})
        result = self.feedback(alias, "move", 4)
        self.assertEqual(result["item_id"], "reminder:r")
        self.assertEqual(self.build()["reminder:r"]["quadrant"], 4)
        self.feedback(alias, "notification", "later")
        self.assertIn("reminder:r", board.preferences(self.state)["snoozed"])
        self.feedback(alias, "status", "done")
        self.assertEqual(self.build(), {})
        self.write("last-snapshot.json", {})
        self.assertEqual(self.build(), {})

    def test_terminal_project_aliases_stay_hidden(self) -> None:
        """Completing a merged project must not resurrect its dated candidate alias."""
        for status in ("done", "tool"):
            with self.subTest(status=status):
                identity = self.project(status, "paused", next_at="2026-11-11", next_step="验收" + status)
                alias = self.candidate(status, status, "2026-11-20", title="验收" + status)
                self.assertIn(alias, self.build()[identity]["merged_ids"])
                self.feedback(alias, "status", status)
                self.assertNotIn(alias, self.build())
                self.assertNotIn(identity, self.build())
                gtd_memory.record(self.state, status, "active", next_step="验收" + status)
                self.assertIn(identity, self.build())
                self.assertNotIn(alias, self.build())

    def test_alias_pin_survives_priority_change_and_expires_at_source_change(self) -> None:
        """Keep a valid candidate pin when a matching native reminder appears."""
        alias = self.candidate("step", None, "2026-11-11", title="提交报告")
        self.feedback(alias, "move", 2)
        self.write("last-snapshot.json", {"reminders": [{"id": "r", "title": "提交报告", "due": "2026-11-11"}]})
        self.assertTrue(self.build()["reminder:r"]["pinned"])
        self.assertEqual(self.build()["reminder:r"]["quadrant"], 2)
        with self.state.db:
            self.state.db.execute("UPDATE candidates SET suggested_due='2026-11-12' WHERE key='step'")
        self.assertFalse(self.build()["reminder:r"]["pinned"])

    def test_parent_does_not_point_to_hidden_project(self) -> None:
        """Clear a child's parent if its project is hidden by earlier feedback."""
        self.project(stage="paused", next_at="2026-11-11", next_step="部署真机")
        alias = self.candidate("step", "Alpha", "2026-11-20", title="部署真机")
        child = self.candidate("child", "Alpha", "2026-11-25", title="买新电脑")
        prefs = board.preferences(self.state)
        prefs["hidden"]["project:alpha"] = None
        prefs["hidden"][alias] = None
        self.write("memory/preferences.json", prefs)
        cards = self.build()
        self.assertIsNone(cards[child]["parent"])
        self.assertNotIn("project:alpha", cards)

    def test_quadrant_order_and_counts_retain_all_q4_items(self) -> None:
        """Order by urgency, importance and actual recency without dropping Q4 cards."""
        for key, age in (("older", 3), ("newer", 1)):
            self.candidate(key, None, status="accepted", title=key,
                           updated=NOW - dt.timedelta(days=age))
        self.candidate("urgent", None, "2026-11-10", title="付房租")
        self.candidate("far", None, "2026-11-16", title="买食材")
        self.project("Beta", stage=None)
        self.candidate("weighted", "Beta", status="accepted", title="处理其余事项")
        self.project("Alpha")
        memory = gtd_memory.projects(self.state)
        memory["Alpha"]["updated_at"] = (NOW - dt.timedelta(days=10)).isoformat()
        self.write("memory/projects.json", memory)
        value = board.build(self.state, NOW, False)
        q4 = [item["id"] for item in value["items"] if item["quadrant"] == 4]
        self.assertEqual(q4, ["candidate:far", "candidate:newer", "candidate:older"])
        self.assertEqual(value["quadrant_counts"], value["counts"])
        self.assertEqual(sum(value["counts"].values()), len(value["items"]))
        self.assertEqual(value["quadrant_counts"][4], 3)
        q2 = [item for item in value["items"] if item["quadrant"] == 2]
        self.assertEqual([item["id"] for item in q2], ["project:alpha", "candidate:weighted", "project:beta"])
        self.assertLess(q2[0]["activity_at"], q2[1]["activity_at"])
