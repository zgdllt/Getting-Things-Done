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

    def test_same_date_bigrams_match_reordered_titles(self) -> None:
        """Match character bigrams even when title order defeats sequence matching."""
        first = self.candidate("a", None, "2026-11-12", title="abcdefghij")
        alias = self.candidate("b", None, (NOW + dt.timedelta(days=1)).isoformat(), title="fghijabcde")
        other = self.candidate("c", None, "2026-11-13", title="fghijabcde")
        cards = self.build()
        self.assertEqual(set(cards), {first, other})
        self.assertEqual(cards[first]["merged_ids"], [alias])

    def test_same_date_jaccard_boundary_and_project_title_fallback(self) -> None:
        """Include Jaccard 0.3, exclude 3/11, and fall back to a project without a step."""
        first = self.candidate("a", None, "2026-11-12", title="abcdefg")
        alias = self.candidate("b", None, "2026-11-12", title="efgabxyz")
        below = self.candidate("c", None, "2026-11-13", title="abcdefg")
        other = self.candidate("d", None, "2026-11-13", title="efgabxyzw")
        project = self.project("abcdefg", "paused", next_at="2026-11-14")
        fallback = self.candidate("e", None, "2026-11-14", title="efgabxyz")
        cards = self.build()
        self.assertEqual(set(cards), {first, below, other, project})
        self.assertEqual(cards[first]["merged_ids"], [alias])
        self.assertEqual(cards[project]["merged_ids"], [fallback])

    def test_same_date_project_step_bigram_coverage(self) -> None:
        """Cover exactly 60 percent of a plan even if Jaccard is below 0.3."""
        alpha = self.project("Alpha", "paused", next_at="2026-11-12", next_step="abcdef")
        beta = self.project("Beta", "paused", next_at="2026-11-13", next_step="abcdef")
        alias = self.candidate("a", None, "2026-11-12", title="abcduvwxyz0123456789")
        other = self.candidate("b", None, "2026-11-13", title="abuvwxyz0123456789")
        cards = self.build()
        self.assertEqual(set(cards), {alpha, beta, other})
        self.assertEqual(cards[alpha]["merged_ids"], [alias])
        self.assertEqual(cards[beta]["merged_ids"], [])

    def test_real_deployment_titles_merge_into_paused_plan(self) -> None:
        """Keep the paused plan card for the three supplied deployment descriptions."""
        project = self.project("booster_lab", "paused", next_at="2026-11-12", next_step="上课时尝试真机部署")
        first = self.candidate("course", "booster_lab", "2026-11-12",
                               title="在 11 月 12 日课程中尝试 Karpathy Loop 运控真机部署")
        second = self.candidate("tomorrow", "booster_lab", "2026-11-12",
                                title="明天（11 月 12 日）上课时尝试真机部署运控（用户自述运控质量已达标…）")
        cards = self.build()
        self.assertEqual(set(cards), {project})
        self.assertEqual(cards[project]["title"], "booster_lab：上课时尝试真机部署")
        self.assertEqual(cards[project]["due_source"], "plan")
        self.assertEqual(cards[project]["merged_ids"], sorted([first, second]))

    def test_plan_suffix_requires_same_date_and_matched_project(self) -> None:
        """The real-case suffix fallback cannot merge a different project or deadline."""
        project = self.project("booster_lab", "paused", next_at="2026-11-12", next_step="上课时尝试真机部署")
        title = "在 11 月 12 日课程中尝试 Karpathy Loop 运控真机部署"
        other = self.candidate("other", "Beta", "2026-11-12", title=title)
        undated = self.candidate("undated", "booster_lab", status="accepted", title=title)
        self.assertEqual(set(self.build()), {project, other, undated})
        self.assertEqual(self.build()[project]["merged_ids"], [])
        with self.state.db:
            self.state.db.execute("UPDATE candidates SET suggested_due='2026-11-13' WHERE key='undated'")
        self.assertEqual(set(self.build()), {project, other, undated})

    def test_unrelated_homework_event_and_paper_review_stay_separate(self) -> None:
        """A shared deadline cannot merge unrelated homework and paper commitments."""
        task = self.candidate("paper", None, "2026-11-12", title="评审具身智能论文")
        event = {"id": "homework", "title": "提交概率论作业", "start": (NOW + dt.timedelta(days=1)).isoformat()}
        self.write("last-snapshot.json", {"events": [event]})
        cards = self.build()
        self.assertEqual(set(cards), {task, f"event:homework:{event['start']}"})
        self.assertTrue(all(not item["merged_ids"] for item in cards.values()))

    def test_nested_metadata_counts_and_children_follow_board_order(self) -> None:
        """Count only top-level cards, keeping dated children separate and undated IDs reachable."""
        project = self.project()
        older = self.candidate("older", "Alpha", title="整理传感器线束", updated=NOW - dt.timedelta(days=1))
        newer = self.candidate("newer", "Alpha", title="校准镜头焦距")
        dated = self.candidate("dated", "Alpha", "2026-11-12", title="缴纳场地租金")
        standalone = self.candidate("standalone", None, status="accepted", title="订购纸张")
        value = board.build(self.state, NOW, False)
        cards = {item["id"]: item for item in value["items"]}
        self.assertTrue(all(isinstance(item["top_level"], bool) for item in cards.values()))
        for identity in (newer, older):
            self.assertFalse(cards[identity]["top_level"])
            self.assertEqual(cards[identity]["parent"], project)
        for identity in (project, dated, standalone):
            self.assertTrue(cards[identity]["top_level"])
        self.assertEqual(cards[dated]["parent"], project)
        self.assertEqual(cards[project]["children"], [newer, older])
        self.assertEqual(cards[project]["children_count"], 2)
        self.assertEqual(value["nested_count"], 2)
        self.assertEqual(value["quadrant_counts"], {1: 1, 2: 1, 3: 0, 4: 1})
        stored = board.read_json(self.state.base / "board.json")
        self.assertEqual(stored["nested_count"], 2)
        self.assertEqual(stored["counts"], stored["quadrant_counts"])

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
        nested = self.candidate("nested", "Alpha", title="清点仪器库存")
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
        self.assertTrue(cards[child]["top_level"])
        self.assertEqual(cards[nested]["parent"], "reminder:r")
        self.assertFalse(cards[nested]["top_level"])
        self.assertEqual(survivor["children"], [nested])
        self.assertEqual(survivor["children_count"], 1)
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
        nested = self.candidate("nested", "Alpha", status="accepted", title="清点仪器库存")
        prefs = board.preferences(self.state)
        prefs["hidden"]["project:alpha"] = None
        prefs["hidden"][alias] = None
        self.write("memory/preferences.json", prefs)
        cards = self.build()
        self.assertIsNone(cards[child]["parent"])
        self.assertIsNone(cards[nested]["parent"])
        self.assertTrue(cards[nested]["top_level"])
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
        self.assertEqual(q4, ["candidate:newer", "candidate:older"])
        self.assertEqual(value["quadrant_counts"], value["counts"])
        self.assertEqual(sum(value["counts"].values()) + value["nested_count"], len(value["items"]))
        self.assertEqual(value["quadrant_counts"][4], 2)
        q2 = [item for item in value["items"] if item["quadrant"] == 2]
        self.assertEqual([item["id"] for item in q2], ["candidate:far", "project:alpha", "candidate:weighted", "project:beta"])
        self.assertLess(q2[1]["activity_at"], q2[2]["activity_at"])
