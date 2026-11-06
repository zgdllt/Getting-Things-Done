#!/usr/bin/env python3
"""Local GTD evidence inbox, macOS bridge and Computer History event subscriber."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import selectors
import signal
import shutil
import sqlite3
import ssl
import subprocess
import sys
import time
import tomllib
import uuid

import gtd_models
import gtd_sources

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
VERSION = "0.5.1"
DEFAULT_STATE = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "personal-gtd"
LABEL = "local.codex.personal-gtd.history-hook"
BUCKETS = {"inbox", "next", "waiting", "someday", "project", "reference"}
class ProcessorBusy(RuntimeError):
    pass
class ServiceStopped(BaseException):
    pass


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def stable_python():
    # GUI-launched apps may have only /usr/bin in PATH (Python 3.9 on this Mac).
    current = str(Path(sys.executable).resolve())
    for prefix in ("/opt/homebrew", "/usr/local"):
        stable = Path(prefix) / "bin/python3"
        if current.startswith(prefix + "/") and stable.is_file():
            return str(stable)
    return sys.executable


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("x") as f:
            os.chmod(temp, 0o600)
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


class State:
    def __init__(self, base):
        self.base = Path(base).expanduser().resolve()
        self.base.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.base / "inbox.sqlite3", timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS cursors(path TEXT PRIMARY KEY, inode INTEGER, offset INTEGER);
          CREATE TABLE IF NOT EXISTS batches(
            id TEXT PRIMARY KEY, created_at TEXT, state TEXT, payload TEXT, summary TEXT, error TEXT);
          CREATE TABLE IF NOT EXISTS candidates(
            key TEXT PRIMARY KEY, title TEXT, bucket TEXT, project TEXT, suggested_due TEXT,
            evidence TEXT, state TEXT, target_id TEXT, created_at TEXT, updated_at TEXT);
          CREATE TABLE IF NOT EXISTS questions(
            key TEXT PRIMARY KEY, candidate_key TEXT, question TEXT, state TEXT,
            answer TEXT, notified_at TEXT, created_at TEXT);
          CREATE TABLE IF NOT EXISTS operations(
            op_id TEXT PRIMARY KEY, digest TEXT, state TEXT, receipt TEXT);
          CREATE TABLE IF NOT EXISTS notification_batches(
            id TEXT PRIMARY KEY, question_keys TEXT, title TEXT, body TEXT, state TEXT, created_at TEXT);
          CREATE TABLE IF NOT EXISTS answer_jobs(
            id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE, question_keys TEXT, candidate_keys TEXT,
            state TEXT, created_at TEXT, updated_at TEXT, result TEXT, error TEXT, model TEXT, reasoning_effort TEXT);
          CREATE TABLE IF NOT EXISTS candidate_resolutions(
            candidate_key TEXT PRIMARY KEY, job_id TEXT, disposition TEXT, explanation TEXT, updated_at TEXT);
        """)
        if "signature" not in {row["name"] for row in self.db.execute("PRAGMA table_info(cursors)")}:
            self.db.execute("ALTER TABLE cursors ADD COLUMN signature TEXT")
            self.db.commit()
        if "candidate_key" not in {row["name"] for row in self.db.execute("PRAGMA table_info(operations)")}:
            self.db.execute("ALTER TABLE operations ADD COLUMN candidate_key TEXT")
            self.db.commit()

    def config(self):
        path = self.base / "config.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def rows(self, sql, parameters=()):
        return [dict(row) for row in self.db.execute(sql, parameters)]

    @contextlib.contextmanager
    def lock(self, name="processor.lock", blocking=False):
        with (self.base / name).open("a+") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                raise ProcessorBusy("Another GTD processor is handling the inbox")
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


def native_binary(state):
    override = os.environ.get("PERSONAL_GTD_NATIVE")
    if override:
        return Path(override).resolve()
    with state.lock("native-build.lock", blocking=True):
        return build_native_binary(state)


def build_native_binary(state):
    source = HERE / "gtd_native.swift"
    bundle = state.base / "runtime" / "Personal GTD Bridge.app"
    binary = bundle / "Contents" / "MacOS" / "gtd-native"
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    stamp = bundle.parent / "gtd-native.source.sha256"
    if binary.exists() and stamp.exists() and stamp.read_text() == digest:
        return binary
    prebuilt = SKILL.parent / "Personal GTD Bridge.app"
    if SKILL.parent.name == "Resources" and SKILL.parent.parent.name == "Contents" and (prebuilt / "Contents/MacOS/gtd-native").is_file():
        shutil.copytree(prebuilt, bundle, dirs_exist_ok=True)
        (bundle / "source.sha256").unlink(missing_ok=True)
        stamp.write_text(digest)
        return binary
    binary.parent.mkdir(parents=True, exist_ok=True)
    (bundle / "source.sha256").unlink(missing_ok=True)
    info = {
        "CFBundleIdentifier": "local.codex.personal-gtd.bridge",
        "CFBundleExecutable": "gtd-native", "CFBundleName": "Personal GTD",
        "CFBundleDisplayName": "Getting Things Done", "CFBundlePackageType": "APPL",
        "CFBundleVersion": "1", "LSUIElement": True,
        "NSCalendarsFullAccessUsageDescription": "读取已有日程用于 GTD 回顾与去重，并写入你在 Codex 中确认的时间安排。",
        "NSCalendarsUsageDescription": "读取日程并管理你确认的 GTD 安排。",
        "NSRemindersFullAccessUsageDescription": "读取已有待办用于 GTD 回顾与去重，并管理你在 Codex 中确认的任务。",
        "NSRemindersUsageDescription": "读取和管理你确认的 GTD 待办。",
    }
    (bundle / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
    result = subprocess.run(["/usr/bin/swiftc", "-O", str(source), "-framework", "EventKit",
                             "-framework", "CoreServices", "-o", str(binary)],
                            capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("Native bridge compilation failed: " + result.stderr[-6000:])
    subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(bundle)],
                   check=True, capture_output=True)
    stamp.write_text(digest)
    return binary


def native(state, command, payload=None, timeout=150):
    binary = native_binary(state)
    if os.environ.get("PERSONAL_GTD_NATIVE"):
        result = subprocess.run([str(binary), command], input=encode(payload or {}),
                                capture_output=True, text=True, timeout=timeout)
        code, stdout, stderr = result.returncode, result.stdout, result.stderr
    else:
        # LaunchServices gives foreground and login-agent calls the same TCC app identity.
        directory = state.base/"runtime"/"requests"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = uuid.uuid4().hex
        source, output, errors = [directory/(key+extension) for extension in (".input.json",".output.json",".stderr")]
        arguments = ["/usr/bin/open", "-W", "-n"]
        if command != "authorize":
            arguments += ["-g"]
        arguments += ["--stdin", str(source), "--stdout", str(output), "--stderr", str(errors),
                      "-a", str(binary.parents[2]), "--args", command]
        try:
            atomic_json(source, payload or {})
            for path in (output, errors):
                with path.open("x"):
                    os.chmod(path, 0o600)
            started = time.monotonic()
            result = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout)
            code, stdout = result.returncode, output.read_text()
            # Very short-lived apps can exit before open attaches its PID wait.
            if "Unable to block on application" in result.stderr:
                while time.monotonic()-started < timeout:
                    try:
                        json.loads(stdout)
                        break
                    except ValueError:
                        time.sleep(0.05)
                        stdout = output.read_text()
            stderr = errors.read_text() + result.stderr
        finally:
            for path in (source, output, errors):
                path.unlink(missing_ok=True)
    try:
        value = json.loads(stdout)
    except ValueError as error:
        raise RuntimeError("Native bridge returned no valid result: " + stderr[-1500:]) from error
    if not value.get("ok"):
        raise RuntimeError(value.get("error", "Native operation failed"))
    if code:
        value["launch_wait_exit_status"] = code
    return value


def snapshot(state, past=7, future=14):
    clock = dt.datetime.now(dt.timezone.utc)
    try:
        result = native(state, "snapshot", {
            "start": (clock - dt.timedelta(days=past)).isoformat(),
            "end": (clock + dt.timedelta(days=future)).isoformat(),
            "completed_since": (clock - dt.timedelta(days=past)).isoformat(),
        })
    except Exception as error:
        atomic_json(state.base / "snapshot-status.json",
                    {"ok": False, "attempted_at": now(), "error": str(error)})
        raise
    result["fetched_at"] = now()
    atomic_json(state.base / "last-snapshot.json", result)
    atomic_json(state.base / "snapshot-status.json",
                {"ok": True, "fetched_at": result["fetched_at"], "permissions": result["permissions"]})
    return result


def project_event(event, path, offset):
    return gtd_sources.project_event(event, path, offset)


def spool(state, root, baseline=False):
    adapter = gtd_sources.history_adapter(state.config())
    collected = adapter.collect(state, root, baseline)
    records, cursors = collected["records"], collected["cursors"]
    batches, chunk, size = [], [], 0
    for record in records:
        record_size = len(encode(record))
        if chunk and size + record_size > 60000:
            batches.append(chunk)
            chunk, size = [], 0
        chunk.append(record)
        size += record_size
    if chunk:
        batches.append(chunk)
    # The durable payload and cursor advance share a transaction; rolling source files may disappear.
    with state.db:
        for batch in batches:
            state.db.execute("INSERT INTO batches VALUES(?,?,?,?,?,?)",
                             (uuid.uuid4().hex, now(), "pending", encode(batch), "", ""))
        state.db.executemany("INSERT OR REPLACE INTO cursors(path,inode,offset,signature) VALUES(?,?,?,?)", cursors)
    return {"batches": len(batches), "records": len(records),
            "malformed_lines": collected["malformed_lines"],
            "first_event_at": collected["first_event_at"],
            "latest_event_at": collected["latest_event_at"],
            "source_adapter": adapter.name, "source_schema": adapter.schema}


def validate_analysis(value, events):
    if not isinstance(value, dict) or not isinstance(value.get("summary"), str):
        raise ValueError("Analysis must contain a summary")
    allowed = {event["event_key"] for event in events}
    for group in ("candidates", "questions"):
        if not isinstance(value.get(group), list):
            raise ValueError("Missing analysis list: " + group)
    keys = set()
    for candidate in value["candidates"]:
        if not isinstance(candidate, dict) or not all(isinstance(candidate.get(k), str)
                and candidate[k].strip() for k in ("key", "title", "bucket")):
            raise ValueError("Invalid candidate")
        if candidate["bucket"] not in BUCKETS or candidate["key"] in keys:
            raise ValueError("Invalid or duplicate candidate key")
        refs = candidate.get("event_keys")
        if not isinstance(refs, list) or not refs or any(ref not in allowed for ref in refs):
            raise ValueError("Candidate cites an event outside this batch")
        keys.add(candidate["key"])
    for question in value["questions"]:
        if not isinstance(question, dict) or not all(isinstance(question.get(k), str)
                and question[k].strip() for k in ("key", "question")):
            raise ValueError("Invalid clarification question")
        if question.get("candidate_key") is not None and not isinstance(question["candidate_key"], str):
            raise ValueError("Invalid question candidate reference")
    return value


def ingest(state, batch_id, value, events):
    value = validate_analysis(value, events)
    lookup = {event["event_key"]: event for event in events}
    with state.lock("operations.lock", blocking=True), state.db:
        versions = value.get("_candidate_versions", {})
        suppressed = {row["key"] for row in state.rows("SELECT key,updated_at FROM candidates")
                      if row["key"] in versions and row["updated_at"] != versions[row["key"]]}
        for item in value["candidates"]:
            evidence = [lookup[key] for key in item["event_keys"]]
            old = state.db.execute("SELECT * FROM candidates WHERE key=?", (item["key"],)).fetchone()
            merged = {entry["event_key"]: entry for entry in (json.loads(old["evidence"]) if old else []) + evidence}
            clarified = old and state.db.execute("SELECT updated_at FROM candidate_resolutions WHERE candidate_key=?", (item["key"],)).fetchone()
            fresh = False
            if clarified:
                cutoff = dt.datetime.fromisoformat(clarified["updated_at"])
                for source in evidence:
                    try:
                        observed = dt.datetime.fromisoformat(source.get("timestamp", "").replace("Z", "+00:00"))
                        fresh = fresh or observed > cutoff
                    except (ValueError, TypeError):
                        pass
            if old and (item["key"] in suppressed or (clarified and not fresh)):
                # Late history cannot undo an answer or its freshly generated response.
                suppressed.add(item["key"])
                state.db.execute("UPDATE candidates SET evidence=? WHERE key=?",
                                 (encode(list(merged.values())), item["key"]))
                continue
            if old and (old["state"] != "inbox" or clarified):
                state.db.execute("UPDATE candidates SET evidence=?,updated_at=? WHERE key=?",
                                 (encode(list(merged.values())), now(), item["key"]))
                changed = any((item.get(field) or "") != (old[field] or "")
                              for field in ("title", "bucket", "project", "suggested_due"))
                if changed and (old["state"] == "accepted" or clarified):
                    changes = {field: {"before": old[field] or None, "after": item.get(field) or None}
                               for field in ("title", "bucket", "project", "suggested_due")
                               if (item.get(field) or "") != (old[field] or "")}
                    key = "changed:" + item["key"] + ":" + hashlib.sha256(
                        encode({field:item.get(field) for field in ("title","bucket","project","suggested_due")}).encode()
                    ).hexdigest()[:16]
                    state.db.execute("INSERT OR IGNORE INTO questions VALUES(?,?,?,?,?,?,?)",
                        (key, item["key"], "已有事项“" + old["title"] +
                         "”出现修改建议，需要调整吗？变更：" + encode(changes),
                         "open", "", None, now()))
                    if old["state"] == "resolved":
                        state.db.execute("UPDATE candidates SET state='inbox' WHERE key=?", (item["key"],))
                continue
            state.db.execute("""INSERT INTO candidates VALUES(?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(key) DO UPDATE SET
                title=excluded.title,bucket=excluded.bucket,project=excluded.project,
                suggested_due=excluded.suggested_due,evidence=excluded.evidence,updated_at=excluded.updated_at""",
                (item["key"], item["title"], item["bucket"], item.get("project") or "",
                 item.get("suggested_due") or "", encode(list(merged.values())), "inbox", "", now(), now()))
        for item in value["questions"]:
            ref = item.get("candidate_key")
            if ref in suppressed:
                continue
            if ref and not state.db.execute("SELECT 1 FROM candidates WHERE key=?", (ref,)).fetchone():
                raise ValueError("Question refers to a nonexistent candidate")
            state.db.execute("INSERT OR IGNORE INTO questions VALUES(?,?,?,?,?,?,?)",
                             (item["key"], ref, item["question"], "open", "", None, now()))
        # Successful batches retain only their summary and the evidence referenced by candidates.
        state.db.execute("UPDATE batches SET state='done',payload=NULL,summary=?,error='' WHERE id=?",
                         (value["summary"], batch_id))


def extractor_command(state, config_home=None, schema=None):
    binary = shutil.which("codex")
    if not binary:
        raise RuntimeError("Codex CLI not found")
    # Keep the user's exact provider/auth route; only disable MCP capabilities.
    home = Path(config_home) if config_home is not None else Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    config_path = home / "config.toml"
    configuration = tomllib.loads(config_path.read_text()) if config_path.exists() else {}
    command = [binary, "exec", "--ephemeral", "--skip-git-repo-check", "--sandbox", "read-only",
               "--color", "never", "--json", "--output-schema", str(schema or SKILL / "references" / "analysis.schema.json")]
    command += gtd_models.command_args(state)
    for feature in ("shell_tool", "unified_exec", "apps", "plugins", "computer_use", "browser_use",
                    "browser_use_external", "in_app_browser", "image_generation", "multi_agent",
                    "hooks", "memories", "chronicle", "view_image", "goals",
                    "default_mode_request_user_input", "collaboration_modes"):
        command += ["--disable", feature]
    command += ["-c", 'approval_policy="never"', "-c", 'web_search="disabled"']
    servers = configuration.get("mcp_servers", {})
    if servers:
        # Quote names inside a TOML value, not in the CLI's dotted-key path parser.
        disabled = "{" + ", ".join(json.dumps(name, ensure_ascii=False) + "={enabled=false}" for name in servers) + "}"
        command += ["-c", "mcp_servers=" + disabled]
    return command + ["--cd", str(state.base), "-"]


def analyze(state, events, *, analysis_events=None):
    current = state.rows("SELECT key,title,bucket,project,suggested_due,state,target_id,updated_at FROM candidates")
    questions = state.rows("SELECT key,candidate_key,question,state,answer FROM questions")
    native_path = state.base / "last-snapshot.json"
    system = json.loads(native_path.read_text()) if native_path.exists() else {}
    config = state.config()
    timezone = gtd_sources.timezone_name(config)
    if analysis_events is None:
        analysis_events = gtd_sources.analysis_records(events, config)
    context = {
        "now": dt.datetime.now(gtd_sources.ZoneInfo(timezone)).isoformat(),
        "timezone": timezone,
        "existing_candidates": current, "existing_questions": questions,
        "system_permissions": system.get("permissions", {}),
        "snapshot_generated_at": system.get("generated_at"),
        "system_snapshot_status": json.loads((state.base/"snapshot-status.json").read_text())
            if (state.base/"snapshot-status.json").exists() else {"ok": False, "error": "No system snapshot yet"},
        "existing_reminders": [{key: item.get(key) for key in ("id", "title", "list", "completed", "due")}
                               for item in system.get("reminders", [])],
        "existing_calendar_events": [{key: item.get(key) for key in ("id","event_id","title","calendar","start","end","all_day")}
                                     for item in system.get("events", [])],
        "source_status": gtd_sources.status(config),
        "raw_event_count": len(events),
        "events": analysis_events,
    }
    prompt = (SKILL / "references" / "extractor.md").read_text() + "\n\nOBSERVED_DATA_JSON:\n" + encode(context)
    command = extractor_command(state)
    value = run_extractor(state, command, prompt)
    # An answer saved while history was being analyzed invalidates its old suggestions.
    value["_candidate_versions"] = {item["key"]: item["updated_at"] for item in current}
    return validate_analysis(value, analysis_events)


def run_extractor(state, command, prompt, record="last-extractor.json"):
    started = time.time()
    environment = os.environ.copy()
    certificate = state.config().get("ca_certificate")
    if certificate and not environment.get("CODEX_CA_CERTIFICATE"):
        environment["CODEX_CA_CERTIFICATE"] = certificate
    result = subprocess.run(command, input=prompt, capture_output=True, text=True,
                            timeout=240, env=environment)
    final, action_items, diagnostics = None, set(), 0
    for line in result.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        item = event.get("item", {})
        if event.get("type") == "item.completed" and item.get("type") == "agent_message":
            final = item.get("text")
        kind = item.get("type")
        if kind == "error":
            diagnostics += 1
        elif kind and kind not in ("agent_message", "reasoning"):
            action_items.add(item.get("id", encode(item)))
    if result.returncode or not final:
        raise RuntimeError("Codex extractor failed: " + result.stderr[-2000:])
    if action_items:
        raise RuntimeError("Extractor unexpectedly used tools; no output was ingested")
    atomic_json(state.base / record, {"finished_at": now(), "seconds": round(time.time()-started, 2),
                "model": command[command.index("--model") + 1],
                "action_items": len(action_items), "diagnostics": diagnostics})
    return json.loads(final)


def notify(state):
    with state.lock("notification.lock", blocking=True):
        return notify_locked(state)


def notify_locked(state):
    pending = state.rows("""SELECT q.key,q.question,c.title,c.project FROM questions q
        LEFT JOIN candidates c ON c.key=q.candidate_key
        WHERE q.state='open' AND q.notified_at IS NULL ORDER BY q.created_at,q.rowid""")
    if not pending:
        return {"submitted": False, "new_questions": 0}
    def compact(value, length):
        value = " ".join((value or "").split())
        return value if len(value) <= length else value[:length-1] + "…"
    batches = state.rows("SELECT * FROM notification_batches WHERE state='pending' ORDER BY created_at")
    assigned = {key for batch in batches for key in json.loads(batch["question_keys"])}
    fresh = [row for row in pending if row["key"] not in assigned]
    if fresh:
        first = fresh[0]
        title = "GTD · " + compact(first["title"] or first["question"], 46)
        body = compact(first["question"], 145)
        if len(fresh) > 1:
            others = list(dict.fromkeys(row["title"] or row["question"] for row in fresh[1:]))
            body += "\n另有 " + str(len(fresh)-1) + " 个问题：" + compact("；".join(others), 65)
        batch = {"id": uuid.uuid4().hex, "question_keys": encode([row["key"] for row in fresh]),
                 "title": title, "body": body, "state": "pending", "created_at": now()}
        with state.db:
            state.db.execute("INSERT INTO notification_batches VALUES(?,?,?,?,?,?)", tuple(batch.values()))
        batches.append(batch)
    submitted, acknowledged = [], False
    live_keys = {row["key"] for row in pending}
    for batch in batches:
        keys = set(json.loads(batch["question_keys"])) & live_keys
        if not keys:
            with state.db:
                state.db.execute("UPDATE notification_batches SET state='cancelled' WHERE id=?", (batch["id"],))
            continue
        title, body = batch["title"], batch["body"]
        if (state.base / "workbench.json").exists():
            from gtd_notifications import deliver
            delivery = deliver(state, title, body, identifier=batch["id"])
            if delivery.get("submitted") is False:
                acknowledged = True
                continue
        else:
            delivery = {"channel": "system_script", "clickable": False}
        if delivery.get("channel") == "system_script":
            body += "\n打开Getting Things Done，或在 Codex 说“澄清 GTD”。"
            script = "display notification " + json.dumps(body, ensure_ascii=False) + " with title " + json.dumps(title, ensure_ascii=False)
            result = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True, timeout=15)
            if result.returncode:
                raise RuntimeError("Notification submission failed: " + result.stderr[-1000:])
        with state.db:
            state.db.executemany("UPDATE questions SET notified_at=? WHERE key=? AND notified_at IS NULL", [(now(), key) for key in keys])
            state.db.execute("UPDATE notification_batches SET state='submitted' WHERE id=?", (batch["id"],))
        submitted.append({"title": title, "body": body, **delivery})
    return {"submitted": bool(submitted), "new_questions": len(pending), "ack_pending": acknowledged,
            "deliveries": submitted}


def process(state, limit=None, notifications=True):
    completed = 0
    with state.lock():
        pending = state.rows("SELECT id,payload FROM batches WHERE state='pending' ORDER BY created_at,rowid")
        for batch in pending[:limit] if limit is not None else pending:
            events = json.loads(batch["payload"])
            try:
                # Summaries and recalled memories have their own source IDs.
                # Resolve them once so inference, validation and persistence all
                # use exactly the same evidence, even if a memory changes.
                analysis_events = gtd_sources.analysis_records(events, state.config())
                value = analyze(state, events, analysis_events=analysis_events)
                ingest(state, batch["id"], value, analysis_events)
            except Exception as error:
                with state.db:
                    state.db.execute("UPDATE batches SET error=? WHERE id=?", (str(error), batch["id"]))
                raise
            completed += 1
    return {"processed_batches": completed, "notification": notify(state) if notifications else None}


def apply_plan(state, path):
    plan = json.loads(Path(path).read_text())
    if not isinstance(plan, dict) or not isinstance(plan.get("operations"), list):
        raise ValueError("Plan must contain operations")
    ids = [item.get("op_id") for item in plan["operations"]]
    if any(not isinstance(key, str) or not key for key in ids) or len(ids) != len(set(ids)):
        raise ValueError("Every operation needs a distinct op_id")
    receipts = []
    with state.lock("operations.lock"):
        for item in plan["operations"]:
            op_id = item["op_id"]
            payload = {key: value for key, value in item.items() if key not in ("candidate_key", "expected_candidate_updated_at")}
            digest = hashlib.sha256(encode(payload).encode()).hexdigest()
            prior = state.db.execute("SELECT * FROM operations WHERE op_id=?", (op_id,)).fetchone()
            if prior:
                if prior["digest"] != digest:
                    raise ValueError("An op_id was reused with different contents")
                if prior["state"] != "applied":
                    raise RuntimeError("Prior write has an uncertain outcome. Inspect system state before retrying: " + op_id)
                receipt = json.loads(prior["receipt"])
            else:
                if item.get("candidate_key"):
                    candidate = state.db.execute("SELECT * FROM candidates WHERE key=?", (item["candidate_key"],)).fetchone()
                    if not candidate or candidate["state"] == "dismissed":
                        raise ValueError("Candidate is missing or was dismissed; read it again")
                    if payload.get("action", "").startswith("create_") and candidate["target_id"]:
                        raise ValueError("Candidate already has a system item; read it again")
                    if payload.get("action", "").startswith("create_") and state.db.execute(
                            "SELECT 1 FROM operations WHERE candidate_key=?", (item["candidate_key"],)).fetchone():
                        raise RuntimeError("This candidate has an existing system write. Inspect its receipt before creating another item")
                    expected = item.get("expected_candidate_updated_at")
                    if expected and expected != candidate["updated_at"]:
                        raise ValueError("Candidate changed after preview; read it again")
                with state.db:
                    state.db.execute("INSERT INTO operations(op_id,digest,state,receipt,candidate_key) VALUES(?,?,?,?,?)",
                                     (op_id, digest, "pending", "", item.get("candidate_key")))
                receipt = native(state, "apply", payload)
                with state.db:
                    state.db.execute("UPDATE operations SET state='applied',receipt=? WHERE op_id=?", (encode(receipt), op_id))
            if item.get("candidate_key"):
                target = receipt.get("item", {}).get("id", "")
                with state.db:
                    state.db.execute("UPDATE candidates SET state='accepted',target_id=?,updated_at=? WHERE key=?",
                                     (target, now(), item["candidate_key"]))
            receipts.append(receipt)
    return {"ok": True, "receipts": receipts}


def capture(state, title, bucket="inbox", project=""):
    if not title.strip():
        raise ValueError("Capture text must not be empty")
    key = "manual:" + uuid.uuid4().hex
    evidence = [{"event_key": key, "kind": "user.capture", "timestamp": now(),
                 "app": {"name": "Codex"}, "window": {}, "text": title}]
    with state.db:
        state.db.execute("INSERT INTO candidates VALUES(?,?,?,?,?,?,?,?,?,?)",
            (key, title, bucket, project, "", encode(evidence), "inbox", "", now(), now()))
    return {"ok": True, "candidate_key": key}


def history_window(state, root, since, until):
    def parsed(value):
        result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("History bounds need an explicit timezone")
        return result
    start, end = parsed(since), parsed(until)
    if end <= start:
        raise ValueError("History end must be after start")
    adapter = gtd_sources.history_adapter(state.config())
    events, available = adapter.window(root, start, end)
    output_path = state.base/"history-window.json"
    atomic_json(output_path, {"requested_start": since, "requested_end": until,
        "available_start": min(available).isoformat() if available else None,
        "available_end": max(available).isoformat() if available else None, "events": events})
    return {"ok": True, "path": str(output_path), "events": len(events),
            "available_start": min(available).isoformat() if available else None,
            "available_end": max(available).isoformat() if available else None}


def daemon(state):
    config = state.config()
    root = config.get("history_root")
    if not root:
        raise RuntimeError("Configure the Computer History root first")
    atomic_json(state.base / "hook-status.json", {"state": "starting", "at": now(), "pid": os.getpid()})
    watcher = subprocess.Popen([str(native_binary(state)), "watch", root], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, bufsize=1)
    selector = selectors.DefaultSelector()
    selector.register(watcher.stdout, selectors.EVENT_READ)
    dirty, last_run = True, 0.0
    retry_at, failures, needs_attention = None, 0, False
    retry_delays = [30, 120, 600]
    try:
        while watcher.poll() is None:
            for key, _ in selector.select(timeout=1):
                line = key.fileobj.readline()
                if line:
                    dirty = True
            due_retry = retry_at is not None and time.monotonic() >= retry_at
            due_change = dirty and retry_at is None and time.monotonic() - last_run >= config.get("coalesce_seconds", 60)
            if due_retry or due_change:
                last_run, dirty = time.monotonic(), False
                retry_at = None
                try:
                    with state.lock():
                        captured = spool(state, root)
                    # Refresh the native snapshot when granted; unavailable permissions remain explicit.
                    try:
                        snapshot(state)
                    except Exception:
                        pass
                    if needs_attention:
                        status_path = state.base/"hook-status.json"
                        previous = json.loads(status_path.read_text()) if status_path.exists() else {}
                        atomic_json(status_path, {**previous, "state": "needs_attention", "at": now(),
                            "pid": os.getpid(), **captured})
                        continue
                    snapshot_path = state.base/"snapshot-status.json"
                    system_status = json.loads(snapshot_path.read_text()) if snapshot_path.exists() else {"ok": False}
                    atomic_json(state.base/"hook-status.json", {"state": "processing", "at": now(),
                        "pid": os.getpid(), "system_snapshot": system_status, **captured})
                    processed = process(state, limit=1)
                    failures = 0
                    with state.db:
                        state.db.execute("UPDATE questions SET state='answered',answer='后台处理已恢复' WHERE key='system:processor-error' AND state='open'")
                    remaining = state.db.execute("SELECT COUNT(*) FROM batches WHERE state='pending'").fetchone()[0]
                    if remaining:
                        retry_at = time.monotonic() + 1
                    elif processed.get("notification", {}).get("ack_pending"):
                        retry_at = time.monotonic() + 30
                    atomic_json(state.base / "hook-status.json", {"state": "processing_backlog" if remaining else "watching",
                        "at": now(), "pid": os.getpid(), "pending_batches": remaining,
                        "system_snapshot": system_status, **captured, **processed})
                except ProcessorBusy:
                    retry_at = time.monotonic() + 1
                except Exception as error:
                    failures += 1
                    if failures <= len(retry_delays):
                        retry_at = time.monotonic() + retry_delays[failures-1]
                    else:
                        needs_attention = True
                        with state.db:
                            state.db.execute("""INSERT INTO questions VALUES(?,?,?,?,?,?,?)
                                ON CONFLICT(key) DO UPDATE SET state='open',answer='',notified_at=NULL""",
                                ("system:processor-error", None,
                                 "后台整理持续失败，线索已保存在本机。请在 Codex 查看 GTD 状态并决定重试。",
                                 "open", "", None, now()))
                        try:
                            notify(state)
                        except Exception:
                            pass
                    atomic_json(state.base / "hook-status.json", {"state": "needs_attention" if needs_attention else "retry_wait",
                        "at": now(), "pid": os.getpid(), "error": str(error), "failures": failures,
                        "retry_in_seconds": retry_delays[failures-1] if retry_at is not None else None})
        raise RuntimeError("Filesystem watcher exited: " + watcher.stderr.read()[-1500:])
    finally:
        selector.close()
        watcher.terminate()
        try:
            watcher.wait(timeout=5)
        except subprocess.TimeoutExpired:
            watcher.kill()


def bootstrap_job(target, plist):
    # launchd can still be tearing down a just-unloaded job (observed error 5).
    for delay in (0, 0.2, 0.7):
        if delay:
            time.sleep(delay)
        result = subprocess.run(["/bin/launchctl", "bootstrap", target, str(plist)], capture_output=True, text=True)
        if result.returncode != 5:
            break
    return result


def install_hook(state, root):
    root = str(Path(root).expanduser().resolve())
    native_binary(state)
    with state.lock():
        config = state.config()
        first_enable = config.get("history_root") != root
        baseline = spool(state, root, baseline=first_enable)
        legacy_timezone = config.get("timezone") == "Asia/Shanghai" and "timezone_source" not in config
        timezone_source = config.get("timezone_source")
        if timezone_source is None:
            timezone_source = "system" if legacy_timezone or not config.get("timezone") else "user"
        config.update({"history_root": root,
                       "history_source": config.get("history_source", "computer_history"),
                       "coalesce_seconds": config.get("coalesce_seconds", 60),
                       "timezone": gtd_sources.local_timezone_name() if legacy_timezone or not config.get("timezone")
                       else config["timezone"],
                       "timezone_source": timezone_source,
                       "memory_source_enabled": config.get("memory_source_enabled", True)})
        atomic_json(state.base / "config.json", config)
    plist = Path.home() / "Library" / "LaunchAgents" / (LABEL + ".plist")
    plist.parent.mkdir(parents=True, exist_ok=True)
    python = stable_python()
    agent = {
        "Label": LABEL, "ProgramArguments": [python, "-B", str(Path(__file__).resolve()), "--state", str(state.base), "daemon"],
        "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 30,
        "WorkingDirectory": str(state.base), "StandardOutPath": str(state.base / "hook.stdout.log"),
        "StandardErrorPath": str(state.base / "hook.stderr.log"),
        "EnvironmentVariables": {"PATH": ":".join([str(Path.home()/".local/bin"), "/opt/homebrew/bin",
                                                   "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"])},
    }
    target = "gui/" + str(os.getuid())
    subprocess.run(["/bin/launchctl", "bootout", target + "/" + LABEL], capture_output=True)
    plist.write_bytes(plistlib.dumps(agent))
    os.chmod(plist, 0o600)
    result = bootstrap_job(target, plist)
    if result.returncode:
        raise RuntimeError("Cannot load the history hook: " + result.stderr)
    return {"ok": True, "service": LABEL, "plist": str(plist), "baseline": baseline}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default=str(DEFAULT_STATE))
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "authorize", "snapshot", "inbox", "questions", "review", "process", "daemon", "hook-status", "hook-stop", "workbench", "workbench-start", "workbench-status", "workbench-stop"):
        sub.add_parser(name)
    collect = sub.add_parser("collect")
    collect.add_argument("--root")
    collect.add_argument("--baseline", action="store_true")
    history = sub.add_parser("history")
    history.add_argument("--root")
    history.add_argument("--since", required=True)
    history.add_argument("--until", required=True)
    quick = sub.add_parser("capture")
    quick.add_argument("title")
    quick.add_argument("--bucket", choices=sorted(BUCKETS), default="inbox")
    quick.add_argument("--project", default="")
    configure = sub.add_parser("configure")
    configure.add_argument("--ca-certificate", required=True)
    start = sub.add_parser("hook-start")
    start.add_argument("--root", required=True)
    apply = sub.add_parser("apply")
    apply.add_argument("--plan", required=True)
    answer = sub.add_parser("answer")
    answer.add_argument("key")
    answer.add_argument("text")
    dismiss = sub.add_parser("dismiss")
    dismiss.add_argument("key")
    args = parser.parse_args()
    state = State(args.state)
    if args.command.startswith("workbench"):
        from gtd_dashboard import manage
        value = manage(state, args.command)
    elif args.command == "doctor":
        value = {"ok": True, "native": native(state, "status"), "codex_cli": shutil.which("codex"),
                 "state": str(state.base), "config": state.config()}
    elif args.command == "authorize":
        value = native(state, "authorize")
    elif args.command == "snapshot":
        data = snapshot(state)
        value = {"ok": True, "path": str(state.base/"last-snapshot.json"), "permissions": data["permissions"],
                 "reminders": len(data["reminders"]) if "reminders" in data else None,
                 "events": len(data["events"]) if "events" in data else None,
                 "lists": data.get("lists"), "calendars": data.get("calendars")}
    elif args.command == "collect":
        with state.lock():
            value = spool(state, args.root or state.config().get("history_root", ""), args.baseline)
    elif args.command == "history":
        value = history_window(state, args.root or state.config().get("history_root", ""), args.since, args.until)
    elif args.command == "capture":
        value = capture(state, args.title, args.bucket, args.project)
    elif args.command == "configure":
        certificate = Path(args.ca_certificate).expanduser().resolve()
        ssl.create_default_context(cafile=str(certificate))
        with state.lock():
            config = state.config()
            config["ca_certificate"] = str(certificate)
            atomic_json(state.base/"config.json", config)
        value = {"ok": True, "ca_certificate": str(certificate)}
    elif args.command == "process":
        value = process(state)
    elif args.command == "inbox":
        value = {"candidates": state.rows("SELECT * FROM candidates WHERE state='inbox' ORDER BY created_at")}
    elif args.command == "questions":
        value = {"questions": state.rows("SELECT * FROM questions WHERE state='open' ORDER BY created_at")}
    elif args.command == "answer":
        with state.lock("operations.lock"), state.db:
            count = state.db.execute("UPDATE questions SET state='answered',answer=? WHERE key=? AND state='open'",
                                     (args.text, args.key)).rowcount
        if not count:
            raise ValueError("Open question not found")
        value = {"ok": True}
    elif args.command == "dismiss":
        with state.lock("operations.lock"), state.db:
            count = state.db.execute("UPDATE candidates SET state='dismissed',updated_at=? WHERE key=?",
                                     (now(), args.key)).rowcount
        if not count:
            raise ValueError("Candidate not found")
        value = {"ok": True}
    elif args.command == "apply":
        value = apply_plan(state, args.plan)
    elif args.command == "review":
        data = snapshot(state)
        value = {"snapshot": str(state.base/"last-snapshot.json"), "permissions": data["permissions"],
                 "inbox": state.rows("SELECT key,title,bucket,project,suggested_due FROM candidates WHERE state='inbox'"),
                 "questions": state.rows("SELECT key,candidate_key,question FROM questions WHERE state='open'"),
                 "pending_batches": state.db.execute("SELECT COUNT(*) FROM batches WHERE state='pending'").fetchone()[0]}
    elif args.command == "hook-start":
        value = install_hook(state, args.root)
    elif args.command == "hook-stop":
        result = subprocess.run(["/bin/launchctl", "bootout", "gui/"+str(os.getuid())+"/"+LABEL], capture_output=True)
        atomic_json(state.base/"hook-status.json", {"state": "stopped", "at": now()})
        value = {"ok": result.returncode == 0, "service": LABEL}
    elif args.command == "hook-status":
        path = state.base/"hook-status.json"
        value = json.loads(path.read_text()) if path.exists() else {"state": "not_installed"}
        result = subprocess.run(["/bin/launchctl", "print", "gui/"+str(os.getuid())+"/"+LABEL], capture_output=True)
        value["loaded"] = result.returncode == 0
    elif args.command == "daemon":
        previous = signal.getsignal(signal.SIGTERM)
        def stopped(_signum, _frame):
            raise ServiceStopped()
        signal.signal(signal.SIGTERM, stopped)
        try:
            daemon(state)
        except (ServiceStopped, KeyboardInterrupt):
            atomic_json(state.base/"hook-status.json", {"state": "stopped", "at": now()})
        finally:
            signal.signal(signal.SIGTERM, previous)
        return
    print(encode(value))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(encode({"ok": False, "error": str(error)}))
        sys.exit(1)
