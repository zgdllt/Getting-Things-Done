"""Detached daily stage inference using the user's profile, with private run status."""
from __future__ import annotations

import datetime as dt
import logging
import subprocess
from pathlib import Path
from typing import Any

import gtd
import gtd_board
import gtd_memory

LOG = logging.getLogger(__name__)


def status(state: gtd.State) -> dict[str, Any]:
    """Return the latest inference attempt without starting any work."""
    return gtd_board.read_json(state.base / "memory/inference-status.json")


def start(state: gtd.State, now: dt.datetime | None = None) -> bool:
    """Reserve at most one launch per local day, after 09:00 with a configured provider."""
    now = gtd_board.local_time(now)
    if now.hour < 9 or gtd.provider_file(state) is None:
        return False
    try:
        with state.lock("inference.lock"):
            previous = gtd_board.timestamp(status(state).get("started_at"))
            if previous and previous.date() >= now.date():
                return False
            value = {"started_at": now.isoformat(), "finished_at": None,
                     "projects_inferred": 0, "projects": [], "error": None}
            path = gtd_memory.folder(state) / "inference-status.json"
            gtd.atomic_json(path, value)
            script = Path(__file__).resolve().parent / "gtd_threads.py"
            try:
                subprocess.Popen([gtd.stable_python(), "-B", str(script), "--state", str(state.base), "--infer"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True, cwd=str(state.base))
            except OSError:
                gtd.atomic_json(path, {**value, "finished_at": now.isoformat(), "error": "无法启动阶段推断"})
                LOG.warning("Could not launch daily stage inference")
                return False
    except gtd.ProcessorBusy:
        return False
    return True


def run(state: gtd.State, now: dt.datetime | None = None) -> dict[str, Any]:
    """Run cached inference only, preserving confirmations and recording partial failures."""
    import gtd_threads
    now = gtd_board.local_time(now)
    with state.lock("inference.lock", blocking=True):
        previous = status(state)
        value = {"started_at": previous.get("started_at") if previous and not previous.get("finished_at")
                 else now.isoformat(), "finished_at": None, "projects_inferred": 0, "projects": [], "error": None}
        path = gtd_memory.folder(state) / "inference-status.json"
        gtd.atomic_json(path, value)
        try:
            inferred = gtd_threads.cached_inference(state, now)
            fresh = {name: entry for name, entry in inferred.items() if entry.get("at") == now.isoformat()}
            value["projects"] = [name for name, entry in fresh.items() if entry.get("stage")]
            value["projects_inferred"] = len(value["projects"])
            errors = [entry["error"] for entry in fresh.values() if entry.get("error")]
            value["error"] = "; ".join(errors)[:300] or None
        except Exception as error:
            value["error"] = f"{type(error).__name__}: {str(error)[:250]}"
            LOG.warning("Daily stage inference failed")
        finally:
            value["finished_at"] = gtd_board.local_time().isoformat()
            gtd.atomic_json(path, value)
    return value
