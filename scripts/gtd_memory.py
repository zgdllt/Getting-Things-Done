"""文枢's memory: project stages and the user's habits, learned from feedback on nudges.

Stored privately in the GTD state folder:
  memory/projects.json   structured stage per project (what decides whether to nudge)
  memory/profile.md      the user's habits and preferences in plain language (editable)

Stages and what they mean for nudges:
  active        being developed; quiet longer than usual -> may be forgotten
  tool          finished enough and used elsewhere; quiet is expected -> no nudge
  paused        stopped on purpose; remind only about `next_step` on or after `next_at`
  blocked       waiting on a decision; nudge to help decide, at most every BLOCKED_DAYS
  superseded    replaced by another project -> no nudge
  done          finished -> no nudge
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Dict, Optional

import gtd

STAGES = ("active", "tool", "paused", "blocked", "superseded", "done")
QUIET_STAGES = {"tool", "superseded", "done"}
BLOCKED_DAYS = 3


def folder(state: gtd.State) -> Path:
    path = state.base / "memory"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def projects(state: gtd.State) -> Dict[str, Dict[str, Any]]:
    path = folder(state) / "projects.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def record(state: gtd.State, project: str, stage: str, note: str = "", next_step: str = "",
           next_at: Optional[str] = None, related: Optional[Dict[str, str]] = None,
           source: str = "user") -> Dict[str, Any]:
    """Store what the user said about one project; later feedback overwrites earlier."""
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}; use one of {STAGES}")
    if next_at:
        dt.date.fromisoformat(next_at[:10])
    table = projects(state)
    entry = {**table.get(project, {}), "stage": stage, "note": note, "next_step": next_step or None,
             "next_at": next_at, "related": related or table.get(project, {}).get("related") or {},
             "source": source, "updated_at": gtd.now()}
    table[project] = entry
    gtd.atomic_json(folder(state) / "projects.json", table)
    return entry


def nudge_rule(entry: Optional[Dict[str, Any]], now: dt.datetime) -> str:
    """'skip', 'planned' (remind about next_step now), 'decide' (blocked) or 'quiet-check' (default)."""
    if not entry:
        return "quiet-check"
    stage = entry.get("stage")
    if stage in QUIET_STAGES:
        return "skip"
    if stage == "paused":
        due = entry.get("next_at")
        return "planned" if due and now.astimezone().date() >= dt.date.fromisoformat(due[:10]) else "skip"
    if stage == "blocked":
        last = entry.get("last_nudged_at")
        if last and now - dt.datetime.fromisoformat(last) < dt.timedelta(days=BLOCKED_DAYS):
            return "skip"
        return "decide"
    return "quiet-check"


def profile(state: gtd.State) -> str:
    path = folder(state) / "profile.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""
