"""Durable, immediate clarification processing, separate from the history backlog."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import gtd
import gtd_providers


def enqueue(state, question_keys, candidate_keys=None, force=False):
    """Called within the answer transaction and operations lock."""
    question_keys = sorted(set(question_keys))
    answers = []
    candidates = set(candidate_keys or [])
    for key in question_keys:
        row = state.db.execute("SELECT * FROM questions WHERE key=?", (key,)).fetchone()
        if not row or row["state"] != "answered":
            raise ValueError("请先保存答复")
        answers.append({"key": key, "answer": row["answer"]})
        if row["candidate_key"]:
            candidates.add(row["candidate_key"])
    fingerprint = uuid.uuid4().hex if force else hashlib.sha256(gtd.encode(answers).encode()).hexdigest()
    previous = state.db.execute("SELECT id FROM answer_jobs WHERE fingerprint=?", (fingerprint,)).fetchone()
    if previous:
        return previous["id"]
    ident = uuid.uuid4().hex
    state.db.execute("""INSERT INTO answer_jobs
        (id,fingerprint,question_keys,candidate_keys,state,created_at,updated_at,result,error,model,reasoning_effort)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (ident, fingerprint, gtd.encode(question_keys), gtd.encode(sorted(candidates)),
         "pending", gtd.now(), gtd.now(), "", "", "", ""))
    # A pre-answer preview must not remain valid while its meaning is changing.
    for key in candidates:
        state.db.execute("UPDATE candidates SET updated_at=? WHERE key=?", (gtd.now(), key))
    return ident


def start(state):
    """A detached worker owns its SQLite connection; saved answers survive restarts."""
    path = state.base / "clarification.log"
    environment = os.environ.copy()
    # login-launched WKWebView services normally omit user/Homebrew CLI locations.
    # Keep any explicit caller PATH first, then add the same locations as the hook.
    paths = os.get_exec_path(environment) + [str(Path.home() / ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin"]
    environment["PATH"] = os.pathsep.join(dict.fromkeys(paths))
    with path.open("a") as log:
        os.chmod(path, 0o600)
        subprocess.Popen([gtd.stable_python(), "-B", str(Path(__file__).resolve()),
                          "--state", str(state.base)], stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log, start_new_session=True, env=environment)


def launch(state, result):
    try:
        start(state)
    except OSError as error:
        with state.db:
            state.db.execute("UPDATE answer_jobs SET state='error',error=?,updated_at=? WHERE id=? AND state='pending'",
                             (str(error), gtd.now(), result["job_id"]))
        result["processing_error"] = "答复已保存，整理服务启动失败，请重试"
    return result


def pending_for(state, key):
    for row in state.rows("SELECT candidate_keys FROM answer_jobs WHERE state IN ('pending','processing')"):
        if key in json.loads(row["candidate_keys"]):
            return True
    return False


def retry(state, candidate_key=None, job_id=None):
    with state.lock("operations.lock", blocking=True), state.db:
        if job_id:
            previous = state.db.execute("SELECT * FROM answer_jobs WHERE id=?", (job_id,)).fetchone()
            if not previous:
                raise ValueError("整理记录已不存在，请刷新")
            keys, candidates = json.loads(previous["question_keys"]), json.loads(previous["candidate_keys"])
        else:
            row = state.db.execute("SELECT state FROM candidates WHERE key=?", (candidate_key,)).fetchone()
            if not row or row["state"] == "dismissed":
                raise ValueError("这条事项已不存在或已忽略")
            keys = [q["key"] for q in state.rows("SELECT key FROM questions WHERE candidate_key=? AND state='answered'", (candidate_key,))]
            candidates = [candidate_key]
        if not keys:
            raise ValueError("请先保存这条事项的答复")
        active = next((job for job in state.rows("SELECT id,candidate_keys,question_keys FROM answer_jobs WHERE state IN ('pending','processing')")
                       if (set(candidates) & set(json.loads(job["candidate_keys"]))) or set(keys) & set(json.loads(job["question_keys"]))), None)
        ident = active["id"] if active else enqueue(state, keys, candidates, force=True)
    return launch(state, {"ok": True, "job_id": ident})


def context_for(state, job):
    candidates = []
    for key in json.loads(job["candidate_keys"]):
        row = state.db.execute("SELECT * FROM candidates WHERE key=?", (key,)).fetchone()
        if not row or row["state"] == "dismissed":
            continue
        item = dict(row)
        evidence = json.loads(item.pop("evidence"))
        item["sources"] = [{"timestamp": source.get("timestamp"), "text": source.get("text", "")[:1600]}
                           for source in evidence[-3:]]
        item["questions"] = state.rows("SELECT key,question,state,answer FROM questions WHERE candidate_key=?", (key,))
        candidates.append(item)
    keys = json.loads(job["question_keys"])
    standalone = []
    for key in keys:
        row = state.db.execute("SELECT key,question,answer,candidate_key FROM questions WHERE key=?", (key,)).fetchone()
        if row and not row["candidate_key"]:
            standalone.append(dict(row))
    return {"now": gtd.now(), "candidates": candidates, "standalone_answers": standalone}


def validate(result, context):
    if not isinstance(result, dict) or not isinstance(result.get("summary"), str) or not isinstance(result.get("updates"), list):
        raise ValueError("模型没有返回有效的答复整理结果")
    expected = {c["key"] for c in context["candidates"]}
    seen = set()
    for update in result["updates"]:
        key = update.get("candidate_key")
        if key not in expected or key in seen:
            raise ValueError("模型返回了不属于本次答复的事项")
        seen.add(key)
        if update.get("bucket") not in gtd.BUCKETS or update.get("disposition") not in ("actionable", "resolved", "needs_clarification"):
            raise ValueError("模型返回了无效的事项状态")
        if any(not isinstance(update.get(field), str) or not update[field].strip() for field in ("title", "explanation")):
            raise ValueError("模型没有给出具体的答复与下一步")
        if update["disposition"] == "needs_clarification" and not (isinstance(update.get("follow_up"), str) and update["follow_up"].strip()):
            raise ValueError("模型没有给出需要澄清的问题")
        before = next(c for c in context["candidates"] if c["key"] == key)
        if update["disposition"] == "needs_clarification" and any(
                q["state"] == "answered" and q["question"].strip() == update["follow_up"].strip() for q in before["questions"]):
            raise ValueError("模型重复询问了已经答复的问题，请重试整理；答复完整保留")
        if update["disposition"] == "resolved" and before["state"] in ("inbox", "resolved") and any(q["state"] == "open" for q in before["questions"]):
            raise ValueError("该事项仍有未答复的问题，请答复后再整理；已保存的答复完整保留")
    if seen != expected:
        raise ValueError("模型遗漏了本次答复的事项")
    return result


def apply(state, job, context, result):
    originals = {c["key"]: c for c in context["candidates"]}
    with state.lock("operations.lock", blocking=True), state.db:
        for key, before in originals.items():
            row = state.db.execute("SELECT updated_at,state FROM candidates WHERE key=?", (key,)).fetchone()
            questions = state.rows("SELECT key,question,state,answer FROM questions WHERE candidate_key=?", (key,))
            if not row or row["updated_at"] != before["updated_at"] or questions != before["questions"]:
                raise ValueError("事项在整理期间已更新，答复仍已保存，请重新整理")
        for update in result["updates"]:
            key = update["candidate_key"]
            before = originals[key]
            if before["state"] in ("inbox", "resolved"):
                state.db.execute("""UPDATE candidates SET title=?,bucket=?,project=?,suggested_due=?,state=?,updated_at=? WHERE key=?""",
                    (update["title"], update["bucket"], update.get("project") or "", update.get("suggested_due") or "",
                     "resolved" if update["disposition"] == "resolved" else "inbox", gtd.now(), key))
            else:
                state.db.execute("UPDATE candidates SET updated_at=? WHERE key=?", (gtd.now(), key))
            state.db.execute("""INSERT INTO candidate_resolutions VALUES(?,?,?,?,?) ON CONFLICT(candidate_key)
                DO UPDATE SET job_id=excluded.job_id,disposition=excluded.disposition,
                explanation=excluded.explanation,updated_at=excluded.updated_at""",
                (key, job["id"], update["disposition"], update["explanation"], gtd.now()))
            follow = update.get("follow_up") if update["disposition"] == "needs_clarification" else None
            if follow:
                existing = state.db.execute("SELECT 1 FROM questions WHERE candidate_key=? AND question=?", (key, follow)).fetchone()
                if not existing:
                    question_key = "clarify:" + hashlib.sha256((key + follow).encode()).hexdigest()[:24]
                    state.db.execute("INSERT INTO questions VALUES(?,?,?,?,?,?,?)",
                                     (question_key, key, follow, "open", "", None, gtd.now()))
        state.db.execute("UPDATE answer_jobs SET state='done',result=?,error='',updated_at=? WHERE id=?",
                         (gtd.encode(result), gtd.now(), job["id"]))


def process(state):
    completed = 0
    with state.lock("clarifications.lock", blocking=True):
        # Holding this lock proves a previous processing worker is no longer active.
        with state.db:
            state.db.execute("UPDATE answer_jobs SET state='pending' WHERE state='processing'")
        while True:
            job = state.db.execute("SELECT * FROM answer_jobs WHERE state='pending' ORDER BY created_at,rowid LIMIT 1").fetchone()
            if not job:
                break
            choice = gtd_providers.label(state)
            with state.db:
                state.db.execute("UPDATE answer_jobs SET state='processing',model=?,reasoning_effort=?,updated_at=? WHERE id=?",
                                 (choice["model"], choice["reasoning_effort"], gtd.now(), job["id"]))
            try:
                with state.lock("operations.lock", blocking=True):
                    context = context_for(state, job)
                prompt = (gtd.SKILL / "references/clarification.md").read_text() + "\n\nINPUT_JSON:\n" + gtd.encode(context)
                if context["candidates"] or context["standalone_answers"]:
                    command = gtd.extractor_command(state, schema=gtd.SKILL / "references/clarification.schema.json")
                    result = validate(gtd.run_extractor(state, command, prompt, "last-clarification.json"), context)
                else:
                    result = {"summary": "事项已忽略，无需继续整理。", "updates": []}
                apply(state, job, context, result)
                completed += 1
            except Exception as error:
                with state.db:
                    state.db.execute("UPDATE answer_jobs SET state='error',error=?,updated_at=? WHERE id=?",
                                     (str(error), gtd.now(), job["id"]))
    return {"ok": True, "processed": completed}


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default=str(gtd.DEFAULT_STATE))
    arguments = parser.parse_args()
    instance = gtd.State(arguments.state)
    try:
        print(gtd.encode(process(instance)))
    finally:
        instance.db.close()
