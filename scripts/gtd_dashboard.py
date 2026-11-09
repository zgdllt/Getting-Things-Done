#!/usr/bin/env python3
"""Private loopback workbench for the existing personal GTD store."""
from __future__ import annotations

import argparse
import datetime as dt
import hmac
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import plistlib
import secrets
import socket
import subprocess
import sys
import time
import uuid
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import gtd
import gtd_clarify
import gtd_models
import gtd_options
import gtd_sources
import gtd_files

LABEL = "local.codex.personal-gtd.workbench"
WEB = gtd.SKILL / "web"


def read_json(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def configuration(base):
    return read_json(Path(base) / "workbench.json", {})


def open_url(base, view="today"):
    conf = configuration(base)
    return f"http://127.0.0.1:{conf['port']}/#access={conf['token']}&view={view}"


def dashboard_state(state):
    native = read_json(state.base / "last-snapshot.json", {})
    config = state.config()
    candidates = state.rows("SELECT * FROM candidates ORDER BY created_at DESC")
    for item in candidates:
        evidence = json.loads(item.pop("evidence"))
        # Full observations can contain entire app trees. The UI gets source excerpts only.
        item["sources"] = [{"timestamp": entry.get("timestamp"), "app": entry.get("app", {}).get("name", ""),
                            "source_type": entry.get("source_type", "computer_history_event"),
                            "memory_window": entry.get("memory_window", ""),
                            "retrieval_terms": entry.get("retrieval_terms", []),
                            "title": entry.get("window", {}).get("title", ""),
                            "url": entry.get("window", {}).get("url", ""),
                            "text": entry.get("text", "")[:1600]} for entry in evidence[-3:]]
        item["source_count"] = len(evidence)
    hook = read_json(state.base / "hook-status.json", {"state": "not_installed"})
    pid = hook.get("pid")
    try:
        if pid:
            os.kill(pid, 0)
            hook["alive"] = True
        else:
            hook["alive"] = False
    except (ProcessLookupError, PermissionError):
        hook["alive"] = False
    jobs = state.rows("SELECT * FROM answer_jobs ORDER BY created_at DESC,rowid DESC")
    for job in jobs:
        job["candidate_keys"] = json.loads(job["candidate_keys"])
        job["question_keys"] = json.loads(job["question_keys"])
        job.pop("fingerprint")
        result = json.loads(job.pop("result") or "{}")
        job["summary"] = result.get("summary", "")
    resolutions = {row["candidate_key"]: row for row in state.rows("SELECT * FROM candidate_resolutions")}
    questions = state.rows("SELECT * FROM questions ORDER BY created_at")
    # Quick-fill answer options come from a cache. Missing ones are generated in a detached
    # worker only when enabled (`quick_answers` in config.json), so fixtures never call a model.
    if gtd_options.attach(state, questions) and state.config().get("quick_answers"):
        try:
            gtd_options.start(state)
        except OSError:
            pass
    for candidate in candidates:
        candidate["resolution"] = resolutions.get(candidate["key"])
    return {"ok": True, "at": gtd.now(), "timezone": gtd_sources.timezone_name(config),
            "model_settings": gtd_models.selection(state), "answer_jobs": jobs,
            "preferences": {"task_display": config.get("workbench_preferences", {}).get("task_display", "cards")},
            "source_status": gtd_sources.status(config),
            "snapshot": native,
            "snapshot_status": read_json(state.base / "snapshot-status.json", {"ok": False, "error": "尚未读取系统清单"}),
            "hook": hook,
            "notifications": read_json(state.base / "notification-status.json", {}),
            "candidates": candidates, "questions": questions,
            "batches": state.rows("SELECT id,created_at,state,summary FROM batches WHERE state='done' ORDER BY created_at DESC LIMIT 12"),
            "pending_batches": state.db.execute("SELECT COUNT(*) FROM batches WHERE state='pending'").fetchone()[0],
            "uncertain_writes": state.db.execute("SELECT COUNT(*) FROM operations WHERE state='pending'").fetchone()[0]}


def nonempty(data, key):
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("请填写" + {"text": "内容", "title": "标题", "list_id": "目标提醒事项列表"}.get(key, key))
    return value.strip()


def save_answers(state, items):
    if not isinstance(items, list) or not items:
        raise ValueError("请先填写答复")
    with state.lock("operations.lock"), state.db:
        for item in items:
            key, answer = nonempty(item, "key"), nonempty(item, "text")
            row = state.db.execute("SELECT * FROM questions WHERE key=?", (key,)).fetchone()
            if not row:
                raise ValueError("这条问题已不存在，请刷新")
            if row["state"] != "open" and row["answer"] != answer:
                raise ValueError("这条问题已在其他位置处理，请刷新查看")
            state.db.execute("UPDATE questions SET state='answered',answer=? WHERE key=?", (answer, key))
        ident = gtd_clarify.enqueue(state, [item["key"].strip() for item in items])
    return {"ok": True, "saved": len(items), "job_id": ident}


def prepare(state, data):
    # Freeze exactly the user-reviewed fields before a separate explicit commit.
    items = data.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("请先选择需要加入正式清单的事项")
    snapshot = read_json(state.base / "last-snapshot.json", {})
    lists = {item["id"]: item for item in snapshot.get("lists", []) if item.get("writable")}
    operations, seen = [], set()
    for item in items:
        key = nonempty(item, "candidate_key")
        if key in seen:
            raise ValueError("同一事项只能选择一次")
        seen.add(key)
        candidate = state.db.execute("SELECT * FROM candidates WHERE key=?", (key,)).fetchone()
        if not candidate or candidate["state"] != "inbox" or candidate["target_id"]:
            raise ValueError("事项已经处理，请刷新查看")
        if gtd_clarify.pending_for(state, key):
            raise ValueError("正在根据答复整理这条事项，请等结果更新后再确认")
        if state.db.execute("SELECT 1 FROM questions WHERE candidate_key=? AND state='open'", (key,)).fetchone():
            raise ValueError("请先答复这条事项的待澄清问题")
        list_id = nonempty(item, "list_id")
        if list_id not in lists:
            raise ValueError("请选择一个可写的提醒事项列表")
        due = item.get("due") or None
        if due:
            if not isinstance(due, str) or dt.date.fromisoformat(due).isoformat() != due:
                raise ValueError("截止日期应为有效的 YYYY-MM-DD")
        answers = state.rows("SELECT question,answer FROM questions WHERE candidate_key=? AND state='answered'", (key,))
        notes = ("项目：" + candidate["project"] + "\n" if candidate["project"] else "")
        notes += "\n".join("问：" + row["question"] + "\n答：" + row["answer"] for row in answers)
        operations.append({"action": "create_reminder", "op_id": str(uuid.uuid4()),
                           "candidate_key": key, "expected_candidate_updated_at": candidate["updated_at"],
                           "list_id": list_id, "title": nonempty(item, "title"), "due": due, "notes": notes})
    plan_id = str(uuid.uuid4())
    path = state.base / "workbench-plans" / (plan_id + ".json")
    gtd.atomic_json(path, {"operations": operations})
    return {"ok": True, "plan_id": plan_id,
            "preview": [{"title": op["title"], "due": op["due"], "list": lists[op["list_id"]]["title"],
                         "source": lists[op["list_id"]].get("source", "")} for op in operations]}


def action(state, route, data):
    if route == "/api/files/register":
        if "recursive" in data:
            raise gtd_files.DeprecatedFilesProtocol("递归扫描接口已废弃，请更新 App；添加文件夹只整理文件夹本身")
        paths = data.get("paths")
        if not isinstance(paths, list) or not paths:
            raise ValueError("请选择文件或文件夹")
        return gtd_files.request(state, paths)
    if route == "/api/files/projects":
        return gtd_files.sync_projects(state, resume=data.get("resume", False))
    if route == "/api/files/pause":
        return gtd_files.stop(state)
    if route == "/api/files/resume":
        return gtd_files.resume_worker(state)
    if route == "/api/files/cache/clear":
        return gtd_files.clear_cache(state, data.get("archives", []), data.get("reset_index", False))
    if route == "/api/files/scan":
        raise gtd_files.DeprecatedFilesProtocol("旧扫描接口已废弃，请更新 App 并使用刷新建议")
    if route == "/api/files/refresh":
        return gtd_files.request(state)
    if route == "/api/files/edit":
        return gtd_files.edit(state, data)
    if route == "/api/files/prepare":
        return gtd_files.prepare(state, data.get("item_ids", []))
    if route == "/api/files/commit":
        return gtd_files.commit(state, data["plan_id"])
    if route == "/api/files/undo":
        return gtd_files.commit(state, data["operation_id"], undo=True)
    if route == "/api/files/open":
        return gtd_files.open_item(state, data.get("item_id"), data.get("reveal", False))
    if route == "/api/files/remove":
        return gtd_files.remove_scope(state, data.get("scope_id"))
    if route == "/api/capture":
        return gtd.capture(state, nonempty(data, "text"))
    if route == "/api/answers":
        result = save_answers(state, data.get("items"))
        return gtd_clarify.launch(state, result)
    if route == "/api/clarify":
        return gtd_clarify.retry(state, job_id=nonempty(data, "job_id")) if data.get("job_id") else gtd_clarify.retry(state, nonempty(data, "candidate_key"))
    if route == "/api/model":
        return gtd_models.save(state, data.get("model"), data.get("reasoning_effort"), gtd.atomic_json)
    if route == "/api/preferences":
        style = data.get("task_display")
        if style not in ("cards", "list"):
            raise ValueError("请选择卡片或条目展示")
        with state.lock("configuration.lock", blocking=True):
            config = state.config()
            preferences = config.setdefault("workbench_preferences", {})
            preferences["task_display"] = style
            gtd.atomic_json(state.base / "config.json", config)
        return {"ok": True, "preferences": {"task_display": style}}
    if route == "/api/dismiss":
        key = nonempty(data, "candidate_key")
        with state.lock("operations.lock"), state.db:
            row = state.db.execute("SELECT state FROM candidates WHERE key=?", (key,)).fetchone()
            if not row or row["state"] not in ("inbox", "resolved", "dismissed"):
                raise ValueError("这条事项已进入正式清单，请在提醒事项中处理")
            state.db.execute("UPDATE candidates SET state='dismissed',updated_at=? WHERE key=?", (gtd.now(), key))
            state.db.execute("UPDATE questions SET state='answered',answer='用户忽略了此候选事项' WHERE candidate_key=? AND state='open'", (key,))
        return {"ok": True}
    if route == "/api/refresh":
        result = gtd.snapshot(state)
        return {"ok": True, "fetched_at": result["fetched_at"]}
    if route == "/api/prepare":
        return prepare(state, data)
    if route == "/api/commit":
        plan_id = str(uuid.UUID(nonempty(data, "plan_id")))
        path = state.base / "workbench-plans" / (plan_id + ".json")
        if not path.exists():
            raise ValueError("预览已不存在，请重新预览")
        result = gtd.apply_plan(state, path)
        try:
            gtd.snapshot(state)
        except Exception:
            result["refresh_pending"] = True
        return result
    if route == "/api/complete":
        item_id = nonempty(data, "id")
        op_id = str(uuid.UUID(nonempty(data, "op_id")))
        path = state.base / "workbench-plans" / (op_id + ".json")
        if path.exists():
            old = read_json(path)
            op = old["operations"][0]
            if op.get("action") != "update_reminder" or op.get("id") != item_id or op.get("expected_modified_at") != data.get("expected_modified_at"):
                raise ValueError("操作已变化，请刷新核对")
            return gtd.apply_plan(state, path)
        # This endpoint only completes the exact existing reminder shown to the user.
        native = read_json(state.base / "last-snapshot.json", {})
        item = next((i for i in native.get("reminders", []) if i["id"] == item_id), None)
        if not item or not item.get("modified_at"):
            raise ValueError("请刷新后再处理这条提醒事项")
        if data.get("expected_modified_at") != item["modified_at"]:
            raise ValueError("事项已更新，请刷新后再处理")
        plan = {"operations": [{"op_id": op_id, "action": "update_reminder", "id": item_id,
                                "expected_modified_at": item["modified_at"], "completed": True}]}
        if path.exists() and read_json(path) != plan:
            raise ValueError("操作已变化，请刷新核对")
        gtd.atomic_json(path, plan)
        result = gtd.apply_plan(state, path)
        try:
            gtd.snapshot(state)
        except Exception:
            result["refresh_pending"] = True
        return result
    if route == "/api/open":
        apps = {"reminders": "com.apple.reminders", "calendar": "com.apple.iCal"}
        app = apps.get(data.get("app"))
        if not app:
            raise ValueError("未知的系统应用")
        subprocess.run(["/usr/bin/open", "-b", app], check=True, capture_output=True, timeout=10)
        return {"ok": True}
    raise ValueError("未知操作")


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, base, token):
        self.base, self.token = Path(base), token
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    @property
    def origin(self):
        return "http://127.0.0.1:" + str(self.server.server_port)

    def valid_host(self):
        return self.headers.get("Host") == "127.0.0.1:" + str(self.server.server_port)

    def authenticated(self):
        jar = cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except cookies.CookieError:
            return False
        supplied = self.headers.get("X-GTD-Token", "")
        if not supplied and "gtd_session" in jar:
            supplied = jar["gtd_session"].value
        return hmac.compare_digest(supplied, self.server.token)

    def send(self, status, value, content_type="application/json; charset=utf-8", headers=None):
        body = value if isinstance(value, bytes) else gtd.encode(value).encode()
        self.send_response(status)
        for key, val in {"Content-Type": content_type, "Content-Length": str(len(body)),
                         "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                         "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
                         "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
                         **(headers or {})}.items():
            self.send_header(key, val)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.valid_host():
            return self.send(403, {"error": "不允许的访问来源"})
        route = self.path.split("?", 1)[0]
        assets = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                  "/deck.js": ("deck.js", "text/javascript; charset=utf-8"),
                  "/files.js": ("files.js", "text/javascript; charset=utf-8"),
                  "/files.css": ("files.css", "text/css; charset=utf-8"),
                  "/style.css": ("style.css", "text/css; charset=utf-8"),
                  "/icon.svg": ("icon.svg", "image/svg+xml")}
        if route in assets:
            path, kind = assets[route]
            return self.send(200, (WEB / path).read_bytes(), kind)
        if not self.authenticated():
            return self.send(401, {"error": "请从桌面「Getting Things Done」入口打开工作台"})
        if route not in ("/api/state", "/api/models", "/api/files"):
            return self.send(404, {"error": "页面不存在"})
        state = gtd.State(self.server.base)
        try:
            query = {key: values[-1] for key, values in parse_qs(urlsplit(self.path).query).items()}
            self.send(200, gtd_models.catalog(state) if route == "/api/models"
                      else gtd_files.snapshot(state, query) if route == "/api/files" else dashboard_state(state))
        except ValueError as error:
            self.send(400, {"error": str(error)})
        finally:
            state.db.close()

    def do_POST(self):
        if not self.valid_host() or self.headers.get("Origin") != self.origin:
            return self.send(403, {"error": "请从本机工作台提交操作"})
        if not self.authenticated():
            return self.send(401, {"error": "请从桌面「Getting Things Done」入口重新打开"})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size <= 0 or size > 1024 * 1024:
                raise ValueError("提交的内容为空或过长")
            if self.headers.get_content_type() != "application/json":
                raise ValueError("请提交 JSON 内容")
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError("提交的内容应为对象")
        except (ValueError, UnicodeError) as error:
            self.close_connection = True
            return self.send(400, {"error": str(error)})
        if self.path == "/api/session":
            return self.send(200, {"ok": True}, headers={"Set-Cookie": "gtd_session=" + self.server.token + "; HttpOnly; SameSite=Strict; Path=/; Max-Age=31536000"})
        state = gtd.State(self.server.base)
        try:
            self.send(200, action(state, self.path, data))
        except gtd_files.DeprecatedFilesProtocol as error:
            self.send(410, {"error": str(error)})
        except gtd.ProcessorBusy:
            self.send(409, {"error": "另一项系统写入正在进行，稍后可重试本次操作"})
        except (ValueError, KeyError) as error:
            self.send(409, {"error": str(error)})
        except Exception as error:
            self.send(500, {"error": str(error)})
        finally:
            state.db.close()

    def do_OPTIONS(self):
        self.send(403, {"error": "不支持跨站访问"})


def is_loaded():
    return subprocess.run(["/bin/launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], capture_output=True).returncode == 0


def wait_for_server(base):
    conf = configuration(base)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", conf["port"]), timeout=0.3):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("工作台尚未开始接收连接，请在 Codex 检查 GTD 状态")


def install(state):
    conf = configuration(state.base)
    if not conf:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        conf = {"port": port, "token": secrets.token_urlsafe(32)}
        gtd.atomic_json(state.base / "workbench.json", conf)
    target = f"gui/{os.getuid()}"
    plist = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
    plist.parent.mkdir(parents=True, exist_ok=True)
    entry = {"Label": LABEL, "ProgramArguments": [gtd.stable_python(), "-B", str(Path(__file__).resolve()),
             "--state", str(state.base), "serve"], "RunAtLoad": True,
             "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 10,
             "WorkingDirectory": str(state.base), "StandardOutPath": str(state.base / "workbench.stdout.log"),
             "StandardErrorPath": str(state.base / "workbench.stderr.log")}
    subprocess.run(["/bin/launchctl", "bootout", target + "/" + LABEL], capture_output=True)
    plist.write_bytes(plistlib.dumps(entry))
    os.chmod(plist, 0o600)
    result = gtd.bootstrap_job(target, plist)
    if result.returncode:
        raise RuntimeError("工作台启动失败：" + result.stderr)
    from gtd_notifications import bundle
    app = bundle(state)
    launcher = Path.home() / "Desktop" / "Getting Things Done.app"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    if not launcher.exists() and not launcher.is_symlink():
        launcher.symlink_to(app)
    previous = launcher.with_name("Getting Things Done.webloc")
    if previous.exists() and plistlib.loads(previous.read_bytes()).get("URL") == open_url(state.base):
        previous.unlink()
    wait_for_server(state.base)
    return {"ok": True, "url": f"http://127.0.0.1:{conf['port']}/", "launcher": str(launcher)}


def manage(state, command):
    if command == "workbench-status":
        conf = configuration(state.base)
        return {"loaded": is_loaded(), "url": f"http://127.0.0.1:{conf['port']}/" if conf else None}
    if command == "workbench-stop":
        result = subprocess.run(["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
        return {"ok": result.returncode == 0}
    result = install(state) if command == "workbench-start" or not is_loaded() else {"ok": True}
    if command == "workbench":
        from gtd_notifications import bundle
        subprocess.run(["/usr/bin/open", "-a", str(bundle(state)), "--args", "--state", str(state.base)], check=True)
    return result


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default=str(gtd.DEFAULT_STATE))
    parser.add_argument("command", choices=["serve", "install", "ensure"])
    args = parser.parse_args()
    state = gtd.State(args.state)
    if args.command == "ensure":
        # Provision the signed, bundled bridge without launching it or prompting.
        # The separately installed Codex skill can then reuse this private cache.
        prebuilt = gtd.SKILL.parent / "Personal GTD Bridge.app/Contents/MacOS/gtd-native"
        if gtd.SKILL.parent.name == "Resources" and prebuilt.is_file():
            gtd.native_binary(state)
            from gtd_notifications import bundle
            bundle(state)
        conf = configuration(state.base)
        try:
            with socket.create_connection(("127.0.0.1", conf["port"]), timeout=1):
                pass
        except (KeyError, OSError):
            install(state)
        print(gtd.encode({"ok": True}))
        return
    if args.command == "install":
        print(gtd.encode(install(state)))
        return
    conf = configuration(state.base)
    if state.db.execute("SELECT 1 FROM answer_jobs WHERE state IN ('pending','processing')").fetchone():
        gtd_clarify.start(state)
    if gtd_files.read(state)["scopes"]:
        gtd_files.launch(state)
    state.db.close()
    with Server(("127.0.0.1", conf["port"]), args.state, conf["token"]) as server:
        server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    main()
