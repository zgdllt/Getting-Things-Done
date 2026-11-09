"""Selected workspaces, Finder tag suggestions and confirmed, reversible writes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time
import uuid

import gtd
import gtd_models
import gtd_projects
from gtd_file_store import folder, open_store, cache_info

CATEGORIES = {
    "important_urgent": "重要且紧急", "important": "重要不紧急",
    "urgent": "紧急但较不重要", "later": "可暂缓", "done": "已完成", "undecided": "待判断",
}
DEFAULT_COLORS = {"important_urgent": 6, "important": 5, "urgent": 7, "later": 1, "done": 2}

class DeprecatedFilesProtocol(ValueError):
    pass


def read(state):
    with open_store(state) as store:
        return store.meta()


def write(state, data):
    with open_store(state) as store:
        store.save(data)


def snapshot(state, query=None):
    query = query or {}
    page = max(0, int(query.get("page", 0)))
    limit = min(100, max(1, int(query.get("limit", 40))))
    view = query.get("filter", "suggested")
    with open_store(state) as store:
        data = store.meta()
        where, parameters = ["present=1"], []
        if view == "suggested":
            where.append("status IN ('pending','suggested')")
        elif view in ("applied", "dismissed"):
            where.append("status=?"); parameters.append(view)
        term = query.get("q", "").strip().casefold()
        if term:
            where.append("instr(search,?)>0"); parameters.append(term)
        tag = query.get("tag", "")
        if tag:
            where.append("""(EXISTS(SELECT 1 FROM json_each(json_extract(value,'$.tags')) WHERE value=?)
                OR category=?)""")
            parameters.extend((tag, next((key for key, name in data["mapping"].items() if name == tag), "")))
        clause = " AND ".join(where)
        total = store.db.execute("SELECT COUNT(*) FROM items WHERE " + clause, parameters).fetchone()[0]
        page = min(page, (max(1, total) - 1) // limit)
        items = store.items(clause, tuple(parameters), limit, page * limit)
        operations = store.recent_operations()
        count = store.db.execute("SELECT COUNT(*) FROM items WHERE present=1").fetchone()[0]
    alive = worker_alive(data.get("worker_pid"), state)
    paused = (folder(state) / "paused").exists()
    if paused:
        data["job"] = {"state": "paused", "summary": "资料整理已暂停，不会扫描或调用模型", "error": ""}
    return {"ok": True, **data, "items": items, "total": total, "item_count": count,
            "page": page, "limit": limit, "operations": operations, "paused": paused,
            "observer_alive": alive, "categories": CATEGORIES, "cache": cache_info(state)}


def worker_alive(pid, state):
    if not pid:
        return False
    try:
        if os.getpgid(pid) != pid:
            return False
        command = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "args="],
                                 capture_output=True, text=True, timeout=2).stdout
        return "gtd_files.py" in command and str(state.base) in command
    except (OSError, subprocess.TimeoutExpired):
        return False


def launch(state):
    with state.lock("files-launch.lock", blocking=True):
        if (folder(state) / "paused").exists() or worker_alive(read(state).get("worker_pid"), state):
            return
        environment = os.environ.copy()
        environment["PATH"] = os.pathsep.join(dict.fromkeys(os.get_exec_path(environment) + [
            str(Path.home() / ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin",
        ]))
        with (state.base / "file-organizer.log").open("a") as log:
            process = subprocess.Popen([gtd.stable_python(), "-B", str(Path(__file__).resolve()),
                                        "--state", str(state.base)], stdin=subprocess.DEVNULL, stdout=log,
                                       stderr=log, start_new_session=True, env=environment)
        with state.lock("files.lock", blocking=True), open_store(state) as store:
            data = store.meta(); data["worker_pid"] = process.pid; store.save(data)


def request(state, paths=None):
    if (folder(state) / "paused").exists():
        raise ValueError("资料整理已按你的要求暂停，恢复前不会扫描或调用模型")
    with state.lock("files.lock", blocking=True), open_store(state) as store:
        data = store.meta()
        for value in paths or []:
            if not isinstance(value, str) or not value.strip():
                raise ValueError("请选择有效的文件或文件夹")
            path = Path(value).expanduser()
            if not path.is_absolute() or path.is_symlink() or not path.exists():
                raise ValueError("路径不存在或是符号链接，请重新选择")
            path = path.resolve()
            old = next((scope for scope in data["scopes"] if scope["path"] == str(path)), None)
            if not old:
                data["scopes"].append({"id": uuid.uuid4().hex, "path": str(path), "name": path.name,
                                       "source": "manual"})
        if not data["scopes"]:
            raise ValueError("请先选择整理范围")
        if paths is None:
            # Stream updates through SQLite; never materialize the entire collection.
            for row in store.db.execute("SELECT value FROM items WHERE present=1 AND status IN ('pending','suggested')"):
                item = json.loads(row[0])
                if not item.get("manual"):
                    item.update(status="pending", reason="", decision=item["decision"] + 1)
                    store.put(item)
        data["requested"] += 1; data["initialized"] = True
        data["job"] = {"state": "pending", "summary": "正在读取工作区并准备标签建议", "error": ""}
        store.save(data)
    launch(state)
    return {"ok": True}


def in_scope(path, scopes):
    return any(path == scope["path"] for scope in scopes)


def sync_projects(state, resume=False):
    result = gtd_projects.discover()
    with state.lock("files.lock", blocking=True), open_store(state) as store:
        data = store.meta()
        by_path = {scope["path"]: scope for scope in data["scopes"]}
        for project in result["projects"]:
            if project["path"] in data["excluded_projects"]:
                continue
            existing = by_path.get(project["path"])
            if existing:
                existing.update(project)
            else:
                scope = {**project, "id": uuid.uuid4().hex, "source": "codex"}
                data["scopes"].append(scope)
        for scope in data["scopes"]:
            scope.pop("recursive", None)
        data["initialized"] = True
        data["project_errors"] = result["errors"]
        data["requested"] += 1
        store.save(data)
    if resume:
        return resume_worker(state)
    if not (folder(state) / "paused").exists():
        launch(state)
    return {"ok": True, "projects": len(result["projects"]), "errors": result["errors"]}


def remove_scope(state, ident):
    with state.lock("files.lock", blocking=True), open_store(state) as store:
        data = store.meta()
        removed = next((scope for scope in data["scopes"] if scope["id"] == ident), None)
        if not removed:
            raise ValueError("整理范围已不存在")
        data["scopes"].remove(removed)
        if removed.get("source") == "codex":
            data["excluded_projects"] = sorted(set([*data["excluded_projects"], removed["path"]]))
        store.db.execute("UPDATE items SET present=0 WHERE path=?", (removed["path"],))
        data["revision"] += 1; data["requested"] += 1
        store.save(data)
    return {"ok": True}


def stop(state):
    (folder(state) / "paused").write_text("Paused by user\n")
    with state.lock("files-launch.lock", blocking=True):
        pid = read(state).get("worker_pid")
        if worker_alive(pid, state):
            os.killpg(pid, signal.SIGTERM)
            deadline = time.monotonic() + 3
            def group_alive():
                try:
                    os.killpg(pid, 0)
                    return True
                except ProcessLookupError:
                    return False
            while time.monotonic() < deadline and group_alive():
                time.sleep(0.05)
            if group_alive():
                os.killpg(pid, signal.SIGKILL)
        with state.lock("files.lock", blocking=True), open_store(state) as store:
            data = store.meta()
            data.update(worker_pid=None, job={"state": "paused", "summary": "资料整理已暂停", "error": ""})
            store.save(data)
    return {"ok": True}


def resume_worker(state):
    with state.lock("files.lock", blocking=True), open_store(state) as store:
        data = store.meta()
        data["requested"] += 1
        data["job"] = {"state": "pending", "summary": "正在读取项目根目录，不递归扫描", "error": ""}
        store.save(data)
        (folder(state) / "paused").unlink(missing_ok=True)
    launch(state)
    return {"ok": True}


def clear_cache(state, names, reset_index=False):
    available = {item["name"] for item in cache_info(state)["archives"]}
    if not isinstance(names, list) or any(name not in available for name in names):
        raise ValueError("缓存列表已变化，请重新打开清理窗口")
    if reset_index:
        stop(state)
        # Keep scopes, settings and the receipt journal needed to undo real tag changes.
        with state.lock("files-write.lock", blocking=True), state.lock("files.lock", blocking=True), open_store(state) as store:
            store.db.execute("DELETE FROM items")
            data = store.meta(); data["revision"] += 1; store.save(data)
        with open_store(state) as store:
            store.db.execute("VACUUM")
    freed = 0
    for name in names:
        path = folder(state) / name
        freed += path.stat().st_size
        path.unlink()
    return {"ok": True, "freed_bytes": freed, "cache": cache_info(state)}


def merge_scan(state, result, scanned_request=None):
    with state.lock("files.lock", blocking=True), open_store(state) as store:
        data = store.meta()
        store.db.execute("UPDATE items SET present=0")
        scopes = {scope["path"]: scope for scope in data["scopes"]}
        tags = result["tags"]
        mapping = {key: next((tag["name"] for tag in tags if tag["color"] == color), "")
                   for key, color in DEFAULT_COLORS.items()}
        mapping.update(data["mapping"])
        colors = {tag["name"]: tag["color"] for tag in tags}
        mapping = {key: name if name in colors and (colors[name] == 2 if key == "done" else colors[name] != 2) else ""
                   for key, name in mapping.items()}
        if mapping != data["mapping"] or tags != data["tags"]:
            data["revision"] += 1
        for record in result["items"]:
            if record["path"] not in scopes:
                continue
            ident = hashlib.sha256(record["path"].encode()).hexdigest()[:24]
            previous = store.get(ident)
            same = previous and previous["signature"] == record["signature"] and previous["tags"] == record["tags"] and previous.get("tag_colors") == record.get("tag_colors")
            item = {**record, "id": ident, "status": "pending", "category": "undecided", "reason": "",
                    "decision": 0, "present": True, "manual": False}
            if same:
                item.update({key: previous.get(key, item.get(key)) for key in (
                    "status", "category", "reason", "decision", "manual", "model", "reasoning_effort", "policy_gray")})
            elif previous and previous.get("manual") and previous["identity"] == record["identity"]:
                item.update({key: previous[key] for key in ("status", "category", "reason", "decision", "manual")})
                item["decision"] += 1
            scope = scopes[record["path"]]
            if record.get("modified_at", 0) > scope.get("last_file_activity", 0):
                scope["last_file_activity"] = record["modified_at"]
            item["activity"] = activity(scope, data["idle_days"])
            item["source"] = scope.get("source", "manual")
            if any(item.get("tag_colors", {}).get(tag) == 2 for tag in item["tags"]):
                item.update(category="done", reason="已有绿色标签，沿用你的已完成标记。", policy_gray=False)
                if item["status"] == "pending":
                    item["status"] = "suggested"
            elif item["status"] not in ("applied", "dismissed") and not item["manual"]:
                if item["activity"]["gray"] and mapping.get("later"):
                    reason = (f"已连续观察 {data['idle_days']} 天，无对话与文件变动，建议暂缓。"
                              if item["activity"]["idle"] else "活动记录不足，按你选择的默认规则建议灰色，暂缓处理。")
                    item.update(status="suggested", category="later", reason=reason, policy_gray=True)
                elif previous and previous.get("policy_gray"):
                    item.update(status="pending", category="undecided", reason="", policy_gray=False)
            store.put(item)
        data.update(tags=tags, mapping=mapping, scan_errors=result.get("errors", []),
                    scanned=data["requested"] if scanned_request is None else scanned_request)
        store.save(data)
        return data


def activity(scope, days, now=None):
    now = time.time() if now is None else now
    cutoff = now - days * 86400
    observed = scope.get("observed_since")
    changed = scope.get("last_file_activity", observed)
    conversation = scope.get("last_conversation")
    recent = bool((conversation or 0) > cutoff or (scope.get("last_file_activity") or 0) > cutoff)
    idle = bool(scope.get("conversation_known") and observed and changed and
                observed <= cutoff and changed <= cutoff and (conversation or 0) <= cutoff)
    return {"last_conversation": conversation, "last_file_activity": scope.get("last_file_activity"),
            "observed_since": observed, "idle": idle, "gray": idle or not recent,
            "note": ("对话与文件活动均达到闲置期限" if idle else
                     "文件活动记录从加入后开始积累；未观察的历史不能判定为无改动")}


def context(state, data, items):
    snapshot_path = state.base / "last-snapshot.json"
    native = json.loads(snapshot_path.read_text()) if snapshot_path.exists() else {}
    tasks = [{"title": item["title"], "due": item.get("due"), "completed": item.get("completed", False),
              "notes": item.get("notes", "")[:600]} for item in native.get("reminders", [])]
    return {
        "now": gtd.now(), "priority_mapping": data["mapping"], "confirmed_tasks": tasks,
        "items": [{"item_id": item["id"], "signature": item["signature"], "path": item["path"],
                   "directory": item["directory"], "tags": item["tags"],
                   "activity": item.get("activity", {})} for item in items],
    }


def validate(result, supplied, mapping):
    expected = {item["item_id"]: item["signature"] for item in supplied["items"]}
    if not isinstance(result, dict) or not isinstance(result.get("summary"), str) or not isinstance(result.get("suggestions"), list):
        raise ValueError("模型未返回有效的标签建议")
    seen = set()
    for item in result["suggestions"]:
        ident = item.get("item_id")
        if ident not in expected or ident in seen or item.get("signature") != expected[ident]:
            raise ValueError("模型建议引用了本次范围之外或已变化的文件")
        category = item.get("category")
        if category not in CATEGORIES or not isinstance(item.get("reason"), str) or not item["reason"].strip():
            raise ValueError("模型建议缺少有效分类或理由")
        if category != "undecided" and not mapping.get(category):
            raise ValueError("模型使用了尚未映射的标签")
        seen.add(ident)
    if seen != set(expected):
        raise ValueError("模型遗漏了部分文件，原文件未改变")
    return result


def suggest(state):
    while True:
        if (folder(state) / "paused").exists():
            return
        data = read(state)
        with open_store(state) as store:
            batch = store.items("present=1 AND status='pending'", limit=40)
            pending_count = store.db.execute("SELECT COUNT(*) FROM items WHERE present=1 AND status='pending'").fetchone()[0]
        if not batch:
            with state.lock("files.lock", blocking=True):
                latest = read(state)
                if latest["job"]["state"] != "done":
                    latest["job"] = {"state": "done", "summary": "标签建议已准备好，请核对后应用", "error": ""}
                    write(state, latest)
            return
        choice = gtd_models.selection(state)
        with state.lock("files.lock", blocking=True):
            latest = read(state)
            latest["job"] = {"state": "processing", "summary": f"正在为 {pending_count} 个项目或所选项准备建议",
                             "error": "", **choice}
            write(state, latest)
        supplied = context(state, data, batch)
        prompt = (gtd.SKILL / "references/files-extractor.md").read_text() + "\n\nINPUT_JSON:\n" + gtd.encode(supplied)
        result = validate(gtd.run_extractor(
            state, gtd.extractor_command(state, schema=gtd.SKILL / "references/files.schema.json"),
            prompt, "last-file-suggestions.json"), supplied, data["mapping"])
        originals = {item["id"]: item for item in batch}
        with state.lock("files.lock", blocking=True), open_store(state) as store:
            latest = store.meta()
            for proposal in result["suggestions"]:
                item = store.get(proposal["item_id"])
                before = originals[proposal["item_id"]]
                if not item or item["signature"] != before["signature"] or item["decision"] != before["decision"] or not item["present"]:
                    continue
                if latest["mapping"] != data["mapping"]:
                    continue
                item.update(status="suggested", category=proposal["category"], reason=proposal["reason"], **choice)
                store.put(item)
            latest["job"]["summary"] = result["summary"]
            store.save(latest)


def edit(state, data):
    with state.lock("files.lock", blocking=True), open_store(state) as store:
        library = store.meta()
        if "idle_days" in data:
            days = int(data["idle_days"])
            if not 1 <= days <= 3650:
                raise ValueError("闲置期限请填写 1 到 3650 天")
            library["idle_days"] = days
        elif "mapping" in data:
            mapping = data["mapping"]
            names = {tag["name"]: tag["color"] for tag in library["tags"]}
            if not isinstance(mapping, dict) or set(mapping) != set(DEFAULT_COLORS) or any(
                value and value not in names for value in mapping.values()
            ):
                raise ValueError("请选择现有 Finder 标签")
            if mapping["done"] and names[mapping["done"]] != 2:
                raise ValueError("已完成必须使用绿色标签")
            if any(value and names[value] == 2 for key, value in mapping.items() if key != "done"):
                raise ValueError("绿色保留给已完成，不用于重要／紧急程度")
            used = [value for value in mapping.values() if value]
            if len(used) != len(set(used)):
                raise ValueError("每种程度请使用不同的标签")
            library["mapping"] = mapping
        else:
            item = store.get(data.get("item_id"))
            if not item or not item["present"]:
                raise ValueError("文件记录已不存在")
            if data.get("dismiss"):
                item["status"] = "dismissed"
            else:
                category = data.get("category")
                if category not in CATEGORIES or category != "undecided" and not library["mapping"].get(category):
                    raise ValueError("请选择已映射的分类")
                if category != "done" and 2 in item.get("tag_colors", {}).values():
                    raise ValueError("这项已有绿色完成标签；请先在 Finder 中调整完成标记，再选择其他程度")
                item.update(category=category, reason="你已手动选择此标签。", status="suggested", manual=True)
            item["decision"] += 1
            store.put(item)
        library["revision"] += 1
        store.save(library)
    return {"ok": True}


def prepare(state, item_ids):
    if not isinstance(item_ids, list) or any(not isinstance(value, str) for value in item_ids):
        raise ValueError("请选择有效的资料")
    with state.lock("files.lock", blocking=True), open_store(state) as store:
        data = store.meta()
        chosen = [item for ident in set(item_ids) if (item := store.get(ident)) and item["present"]]
        if len(chosen) != len(set(item_ids)) or not chosen:
            raise ValueError("请选择仍在整理范围内的资料")
        items = []
        for item in chosen:
            name = data["mapping"].get(item["category"])
            if item["status"] != "suggested" or not name:
                raise ValueError("请先为每项选择有效标签")
            color = next((tag["color"] for tag in data["tags"] if tag["name"] == name), None)
            if color is None or (color != 2 if item["category"] == "done" else color == 2):
                raise ValueError("标签颜色已变化，请重新选择映射并预览")
            if name in item["tags"] or item["category"] == "done" and 2 in item.get("tag_colors", {}).values():
                continue
            items.append({key: item[key] for key in ("path", "identity", "signature", "id", "decision")})
            items[-1].update(before=item["tags"], before_colors=item.get("tag_colors", {}),
                             colors={tag["name"]: tag["color"] for tag in data["tags"]}, add=[name], remove=[])
        if not items:
            raise ValueError("所选资料已经具有这些标签")
        plan = {"id": uuid.uuid4().hex, "state": "prepared", "created_at": gtd.now(),
                "revision": data["revision"], "items": items, "results": []}
        gtd.atomic_json(folder(state) / (plan["id"] + ".json"), plan)
        return {"ok": True, "plan": plan}


def commit(state, plan_id, undo=False):
    ident = uuid.UUID(plan_id).hex
    path = folder(state) / (ident + ".json")
    with state.lock("files-write.lock", blocking=True):
        if not path.exists():
            raise ValueError("预览已不存在，请重新预览")
        plan = json.loads(path.read_text())
        if not undo and plan["state"] in ("done", "partial"):
            return {"ok": True, "operation": plan}
        if plan["state"] in ("applying", "undoing"):
            raise ValueError("这次写入的结果尚未核实，请先刷新核对文件；不会重复应用")
        if not undo and plan["state"] != "prepared":
            raise ValueError("这批操作已经撤销，请重新预览后再应用")
        with state.lock("files.lock", blocking=True), open_store(state) as store:
            data = store.meta()
            if not undo and plan["state"] == "prepared":
                if data["revision"] != plan["revision"]:
                    raise ValueError("建议或标签设置已变化，请重新预览")
                for entry in plan["items"]:
                    item = store.get(entry["id"])
                    if not item or not item["present"] or item["decision"] != entry["decision"] or item["signature"] != entry["signature"]:
                        raise ValueError("资料已变化，请重新预览")
            items = plan["items"]
            if undo:
                if plan["state"] == "undone":
                    return {"ok": True, "operation": plan}
                successful = {item["path"] for item in plan["results"] if item["ok"]}
                undone = {item["path"] for item in plan.get("undo_results", []) if item["ok"]}
                items = [{**item, "add": [], "remove": item["add"],
                          "remove_colors": {tag: item["colors"][tag] for tag in item["add"]}}
                         for item in items if item["path"] in successful - undone]
                if not items:
                    raise ValueError("这次没有成功添加的标签可撤销")
            plan["state"] = "undoing" if undo else "applying"
            gtd.atomic_json(path, plan)
            store.operation(plan)
        result = gtd.native(state, "files-apply", {"items": items, "undo": undo})
        with state.lock("files.lock", blocking=True), open_store(state) as store:
            for receipt in result["items"]:
                item = store.get(hashlib.sha256(receipt["path"].encode()).hexdigest()[:24])
                if item and receipt["ok"]:
                    item.update(tags=receipt["after"], tag_colors=receipt.get("tag_colors", {}),
                                status="suggested" if undo else "applied")
                    item["decision"] += 1
                    store.put(item)
            all_ok = all(item["ok"] for item in result["items"])
            if undo:
                merged = {item["path"]: item for item in plan.get("undo_results", [])}
                merged.update({item["path"]: item for item in result["items"]})
                plan.update(state="undone" if all_ok else "undo_partial", undo_results=list(merged.values()))
            else:
                plan.update(state="done" if all_ok else "partial", results=result["items"])
            gtd.atomic_json(path, plan)
            store.operation(plan)
        return {"ok": True, "operation": plan}


def open_item(state, item_id, reveal=False):
    data = read(state)
    with open_store(state) as store:
        item = store.get(item_id)
    if not item or not item["present"] or not in_scope(item["path"], data["scopes"]):
        raise ValueError("文件已不在整理范围中")
    subprocess.run(["/usr/bin/open", *(["-R"] if reveal else []), "--", item["path"]],
                   check=True, capture_output=True, timeout=10)
    return {"ok": True}


def worker(state):
    if (folder(state) / "paused").exists():
        return
    with state.lock("files-worker.lock"):
        watcher = None
        roots = []
        failed_request = None
        last_sync = 0
        last_scan = 0
        selector = selectors.DefaultSelector()
        try:
            with state.lock("files.lock", blocking=True):
                data = read(state)
                data["worker_pid"] = os.getpid()
                for scope in data["scopes"]:
                    # A gap in observation cannot prove there were no nested modifications.
                    scope["observed_since"] = time.time()
                write(state, data)
            while not (folder(state) / "paused").exists() and read(state)["scopes"]:
                data = read(state)
                attempted_request = data["requested"]
                selected = sorted(scope["path"] for scope in data["scopes"])
                if roots != selected or watcher is None or watcher.poll() is not None:
                    if watcher:
                        selector.unregister(watcher.stdout); watcher.terminate(); watcher.wait(timeout=5)
                        watcher.stdout.close()
                    watcher = subprocess.Popen([str(gtd.native_binary(state)), "files-watch"],
                                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                               stderr=subprocess.DEVNULL, text=True)
                    watcher.stdin.write(gtd.encode({"scopes": data["scopes"], "exclude": [str(state.base)]}))
                    watcher.stdin.close()
                    selector.register(watcher.stdout, selectors.EVENT_READ); roots = selected
                    if not selector.select(timeout=5):
                        raise RuntimeError("资料活动观察未能启动，请检查原生组件后重试")
                    ready = json.loads(watcher.stdout.readline() or "{}")
                    if ready.get("kind") != "watching" or not ready.get("ok"):
                        raise RuntimeError("资料活动观察启动失败：" + ready.get("error", "原生组件已退出"))
                if time.monotonic() - last_sync > 60:
                    result = gtd_projects.discover()
                    by_path = {project["path"]: project for project in result["projects"]}
                    with state.lock("files.lock", blocking=True):
                        latest = read(state)
                        for scope in latest["scopes"]:
                            if scope["path"] in by_path:
                                scope.update(by_path[scope["path"]])
                        latest["project_errors"] = result["errors"]
                        write(state, latest)
                    last_sync = time.monotonic()
                try:
                    if failed_request != attempted_request and (data["scanned"] != attempted_request or time.monotonic() - last_scan >= 60):
                        # This protocol accepts explicit items only; the old scanner is retired.
                        current = read(state)
                        response = subprocess.run([str(gtd.native_binary(state)), "files-inspect"],
                            input=gtd.encode({"paths": [scope["path"] for scope in current["scopes"]],
                                              "exclude": [str(state.base)]}),
                            capture_output=True, text=True, check=True, timeout=30)
                        data = merge_scan(state, json.loads(response.stdout), attempted_request)
                        last_scan = time.monotonic()
                        if data["job"]["state"] != "error" or data["scanned"] != data["requested"]:
                            suggest(state)
                except Exception as error:
                    failed_request = attempted_request
                    with state.lock("files.lock", blocking=True):
                        data = read(state)
                        data["job"] = {"state": "error", "summary": "整理建议未完成，文件标签未自动改变", "error": str(error)}
                        write(state, data)
                latest = read(state)
                if not latest["scopes"]:
                    return
                if latest["requested"] != attempted_request:
                    continue
                if not selector.select(timeout=2):
                    continue
                line = watcher.stdout.readline()
                if not line:
                    raise RuntimeError("工作区观察进程已退出")
                event = json.loads(line)
                changed = set(event.get("roots", []))
                with state.lock("files.lock", blocking=True):
                    latest = read(state)
                    for scope in latest["scopes"]:
                        if scope["path"] in changed:
                            scope["last_file_activity"] = time.time()
                            if event.get("uncertain"):
                                scope["observed_since"] = time.time()
                    write(state, latest)
                # Events update activity evidence only; builds do not launch a model per file.
        finally:
            if watcher and watcher.poll() is None:
                watcher.terminate(); watcher.wait(timeout=5)
            if watcher:
                watcher.stdout.close()
            selector.close()


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default=str(gtd.DEFAULT_STATE))
    parser.add_argument("--cache-info", action="store_true")
    parser.add_argument("--clear-archived-cache", action="store_true")
    parser.add_argument("--pause", action="store_true")
    parser.add_argument("--resume-projects", action="store_true")
    args = parser.parse_args()
    instance = gtd.State(args.state)
    try:
        if args.cache_info:
            print(gtd.encode(cache_info(instance)))
        elif args.clear_archived_cache:
            print(gtd.encode(clear_cache(instance, [item["name"] for item in cache_info(instance)["archives"]])))
        elif args.pause:
            print(gtd.encode(stop(instance)))
        elif args.resume_projects:
            print(gtd.encode(sync_projects(instance, resume=True)))
        else:
            worker(instance)
    except gtd.ProcessorBusy:
        pass
    except Exception as error:
        with instance.lock("files.lock", blocking=True):
            data = read(instance)
            data.update(worker_pid=None, job={"state": "error", "summary": "资料后台未能启动，请修复后重试", "error": str(error)})
            write(instance, data)
        raise
    finally:
        instance.db.close()
