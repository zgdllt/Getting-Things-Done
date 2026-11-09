"""Adaptive answer options for GTD clarification questions.

For each open question the GTD model (the user's GTD model/provider settings,
default gpt-6.1-terra) proposes 2-3 short answers. The workbench shows them as
quick-fill chips under the answer box; the Xiaomi band integration shows them
as buttons. Choosing an option never bypasses confirmation in the workbench:
it only fills the answer, which the user saves as usual.

Results and failures are cached per question in `band_options`. Generation
runs in a detached worker (`start`) so the workbench never waits on the model.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import gtd
import gtd_models

logger = logging.getLogger("gtd.options")

PROMPT = gtd.SKILL / "references" / "options.md"
SCHEMA = gtd.SKILL / "references" / "options.schema.json"
OPTIONS_EFFORT = "low"
MAX_WIDTH = 12.0          # CJK characters; ASCII counts as half
MAX_OPTIONS = 3
RETRY_FAILED_MINUTES = 30
START_THROTTLE_SECONDS = 60
BATCH_LIMIT = 6
RESERVED = ("否掉", "稍后", "到电脑", "电脑处理", "其他", "其它")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS band_options(
  question_key TEXT PRIMARY KEY,
  options TEXT NOT NULL,
  model TEXT,
  created_at TEXT NOT NULL,
  error TEXT
);
"""

Runner = Callable[[Any, List[str], str, str], Dict[str, Any]]


def ensure_schema(state: gtd.State) -> None:
    state.db.executescript(SCHEMA_SQL)
    state.db.commit()


def width(text: str) -> float:
    return sum(0.5 if ord(char) < 128 else 1.0 for char in text)


def clean(options: Any) -> List[str]:
    """Keep 2-3 distinct, short options that do not duplicate the fixed actions."""
    if not isinstance(options, list):
        return []
    kept: List[str] = []
    for option in options:
        if not isinstance(option, str):
            continue
        text = option.strip().rstrip("。.!！")
        if not text or width(text) > MAX_WIDTH or any(word in text for word in RESERVED) or text in kept:
            continue
        kept.append(text)
    kept = kept[:MAX_OPTIONS]
    return kept if len(kept) >= 2 else []


def context(state: gtd.State, question_key: str) -> Optional[Dict[str, Any]]:
    question = state.db.execute("SELECT key,question,candidate_key FROM questions WHERE key=?",
                                (question_key,)).fetchone()
    if question is None:
        return None
    candidate = state.db.execute("SELECT key,title,bucket,project,suggested_due,evidence FROM candidates WHERE key=?",
                                 (question["candidate_key"],)).fetchone()
    sources: List[Dict[str, Any]] = []
    answered: List[Dict[str, Any]] = []
    if candidate is not None:
        try:
            evidence = json.loads(candidate["evidence"] or "[]")
        except ValueError:
            evidence = []
        sources = [{"timestamp": item.get("timestamp"), "text": str(item.get("text", ""))[:800]}
                   for item in evidence[-2:] if isinstance(item, dict)]
        answered = state.rows("SELECT question,answer FROM questions WHERE candidate_key=? AND state='answered'",
                              (candidate["key"],))
    return {
        "now": gtd.now(),
        "candidate": {key: candidate[key] for key in ("title", "bucket", "project", "suggested_due")} if candidate else None,
        "question": question["question"],
        "answered_questions": answered,
        "sources": sources,
    }


def command(state: gtd.State) -> List[str]:
    """The GTD extractor command with only the reasoning effort lowered."""
    cmd = gtd.extractor_command(state, schema=SCHEMA)
    at = cmd.index("--cd")
    cmd[at:at] = ["-c", "model_reasoning_effort=" + json.dumps(OPTIONS_EFFORT)]
    return cmd


def cached(state: gtd.State, question_key: str) -> Optional[List[str]]:
    """Cached options, or None when they should be (re)generated."""
    row = state.db.execute("SELECT options,error,created_at FROM band_options WHERE question_key=?",
                           (question_key,)).fetchone()
    if row is None:
        return None
    if row["error"]:
        cutoff = (gtd.dt.datetime.now(gtd.dt.timezone.utc)
                  - gtd.dt.timedelta(minutes=RETRY_FAILED_MINUTES)).isoformat()
        return None if row["created_at"] < cutoff else []
    return json.loads(row["options"])


def generate(state: gtd.State, question_key: str, runner: Optional[Runner] = None) -> List[str]:
    run = runner or gtd.run_extractor
    info = context(state, question_key)
    if info is None:
        return []
    prompt = PROMPT.read_text(encoding="utf-8") + "\n\nQUESTION_JSON:\n" + gtd.encode(info)
    options: List[str] = []
    error = ""
    try:
        options = clean(run(state, command(state), prompt, "last-options.json").get("options"))
        if not options:
            error = "model returned no usable options"
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as e:
        error = str(e)[:500]
        logger.warning("option generation failed for %s: %s", question_key, error)
    with state.db:
        state.db.execute("INSERT OR REPLACE INTO band_options(question_key,options,model,created_at,error) "
                         "VALUES(?,?,?,?,?)",
                         (question_key, json.dumps(options, ensure_ascii=False),
                          gtd_models.selection(state)["model"], gtd.now(), error or None))
    return options


def get_or_generate(state: gtd.State, question_key: str, runner: Optional[Runner] = None) -> List[str]:
    options = cached(state, question_key)
    return options if options is not None else generate(state, question_key, runner)


# ---------------------------------------------------------------- workbench

def open_keys(state: gtd.State) -> List[str]:
    return [row["key"] for row in state.rows(
        "SELECT q.key FROM questions q JOIN candidates c ON c.key=q.candidate_key "
        "WHERE q.state='open' AND c.state IN ('inbox','resolved') ORDER BY q.created_at, q.key")]


def attach(state: gtd.State, questions: List[Dict[str, Any]]) -> bool:
    """Add cached `options` to question rows; return True when some open ones still lack them."""
    ensure_schema(state)
    ready = {row["question_key"]: json.loads(row["options"])
             for row in state.rows("SELECT question_key, options FROM band_options WHERE error IS NULL")}
    for question in questions:
        question["options"] = ready.get(question["key"], [])
    return any(cached(state, key) is None for key in open_keys(state))


def start(state: gtd.State) -> bool:
    """Spawn one detached generation worker, at most once per throttle window."""
    stamp = state.base / "options-start.json"
    try:
        if time.time() - stamp.stat().st_mtime < START_THROTTLE_SECONDS:
            return False
    except FileNotFoundError:
        pass
    gtd.atomic_json(stamp, {"started_at": gtd.now()})
    environment = os.environ.copy()
    paths = os.get_exec_path(environment) + [str(Path.home() / ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin"]
    environment["PATH"] = os.pathsep.join(dict.fromkeys(paths))
    log = state.base / "options.log"
    with log.open("a") as handle:
        os.chmod(log, 0o600)
        subprocess.Popen([gtd.stable_python(), "-B", str(Path(__file__).resolve()), "--state", str(state.base)],
                         stdin=subprocess.DEVNULL, stdout=handle, stderr=handle,
                         start_new_session=True, env=environment)
    return True


def process(state: gtd.State, limit: int = BATCH_LIMIT) -> Dict[str, Any]:
    ensure_schema(state)
    try:
        with state.lock("options.lock"):
            done = 0
            for key in open_keys(state):
                if done >= limit:
                    break
                if cached(state, key) is None:
                    generate(state, key)
                    done += 1
            return {"ok": True, "generated": done}
    except gtd.ProcessorBusy:
        return {"ok": True, "generated": 0, "busy": True}


if __name__ == "__main__":
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state", default=str(gtd.DEFAULT_STATE))
    parser.add_argument("--limit", type=int, default=BATCH_LIMIT)
    arguments = parser.parse_args()
    instance = gtd.State(arguments.state)
    try:
        print(gtd.encode(process(instance, arguments.limit)))
    finally:
        instance.db.close()
        sys.stdout.flush()
