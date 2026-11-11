"""Shared synthetic fixtures for board tests; never touches live state."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import gtd
import gtd_board as board
import gtd_memory
import gtd_provider
import gtd_threads

NOW = dt.datetime(2026, 11, 11, 12).astimezone()


class BoardFixture(unittest.TestCase):
    """Build isolated state fixtures and guard all external activity/model/native calls."""

    def setUp(self) -> None:
        """Create a temporary state and fail on any accidental expensive source access."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = gtd.State(Path(self.temp.name) / "state")
        self.addCleanup(self.state.db.close)
        self.scanned: dict = {}
        self.cache()
        self.write("config.json", {"quiet_hours": False})
        for module, name in ((gtd_threads, "infer_all"), (gtd_threads, "cached_inference"),
                             (gtd_provider, "chat_json"), (gtd, "native"), (gtd_threads.gtd_local, "window")):
            guard = patch.object(module, name, side_effect=AssertionError("unexpected source/model/native call"))
            guard.start()
            self.addCleanup(guard.stop)

    def write(self, name: str, value: dict) -> None:
        """Seed a private fixture file under the synthetic state."""
        gtd.atomic_json(self.state.base / name, value)

    def cache(self, stamp: str | None = None) -> None:
        """Persist realistic activity-cache metadata with the current fixture projects."""
        self.write("experiments/activity-cache.json", {"stamp": stamp or NOW.strftime("%Y-%m-%dT%H"),
                   "days": gtd_threads.SCAN_DAYS, "version": gtd_threads.CACHE_VERSION, "projects": self.scanned})

    def project(self, name: str = "Alpha", stage: str | None = "active", recent: bool = False,
                **values: str) -> str:
        """Seed own activity and optional project memory; return its normalized card ID."""
        last = NOW - dt.timedelta(hours=1) if recent else NOW - dt.timedelta(days=3)
        self.scanned[name] = {"last": last.isoformat(), "dates": [(NOW - dt.timedelta(days=i)).date().isoformat()
                             for i in range(5, 2, -1)], "messages": [[last.isoformat(), "继续推进"]],
                             "replies": [], "commits": 1, "commit_subjects": [], "mentioned_by": {}}
        self.cache()
        if stage:
            gtd_memory.record(self.state, name, stage, **values)
        return "project:" + gtd_threads.norm(name)

    def declare(self, *names: str) -> None:
        """Declare project priorities without altering other synthetic configuration."""
        config = self.state.config()
        config["project_briefs"] = {str(i): {"title": name} for i, name in enumerate(names)}
        self.write("config.json", config)

    def candidate(self, key: str = "task", project: str | None = "Alpha", due: str | None = None,
                  status: str = "inbox", target: str = "", title: str | None = None,
                  evidence: list[dict] | None = None, updated: dt.datetime = NOW) -> str:
        """Insert a local candidate with a controlled workflow state and explicit due date."""
        with self.state.db:
            self.state.db.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?,?)",
                                  (key, title or "任务 " + key, "next", project, due, json.dumps(evidence or []),
                                   status, target, NOW.isoformat(), updated.isoformat()))
        return "candidate:" + key

    def build(self, now: dt.datetime = NOW, refresh: bool = False) -> dict:
        """Build a deterministic board and index its cards by stable ID."""
        return {item["id"]: item for item in board.build(self.state, now, refresh)["items"]}

    def feedback(self, identity: str, action: str, value: str | int, now: dt.datetime = NOW) -> dict:
        """Submit one real feedback operation against the temporary state."""
        return board.record_feedback(self.state, identity, action, value, now)

    def select(self, now: dt.datetime = NOW) -> list[dict]:
        """Preview notifications using a newly built, synthetic board."""
        return board.select_notifications(self.state, board.build(self.state, now, False), now)

