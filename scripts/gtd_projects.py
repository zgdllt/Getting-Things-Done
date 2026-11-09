"""Read local Codex project/activity metadata, without opening conversation bodies."""
import contextlib
import json
import os
from pathlib import Path
import sqlite3


def canonical(value):
    # Older desktop workspace roots were serialized with Windows separators on macOS.
    if value.startswith("\\Users\\"):
        value = value.replace("\\", "/")
    return os.path.realpath(os.path.expanduser(value))


def discover(home=None):
    home = Path(home or os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    path = home / ".codex-global-state.json"
    global_state = json.loads(path.read_text()) if path.exists() else {}
    projects = {}
    for ident, project in global_state.get("local-projects", {}).items():
        for value in project.get("rootPaths", []):
            if value.startswith(("/", "\\Users\\")):
                projects[(ident, canonical(value))] = project.get("name", Path(value).name)
    activities = []
    database = home / "state_5.sqlite"
    errors = []
    if database.exists():
        try:
            with contextlib.closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
                db.row_factory = sqlite3.Row
                for row in db.execute("""SELECT p.id,p.name,p.metadata,r.path FROM projects p
                                        JOIN project_roots r ON p.id=r.project_id"""):
                    metadata = json.loads(row["metadata"] or "{}")
                    host = metadata.get("hostId", metadata.get("host_id", "local"))
                    if host not in ("local", None) or metadata.get("projectKind") in ("remote", "chatgpt"):
                        continue
                    if row["path"].startswith("/"):
                        projects[(row["id"], canonical(row["path"]))] = row["name"]
                # Only identity, location and timestamps; no title, preview or messages.
                for row in db.execute("""SELECT id,cwd,project_id,
                    MAX(COALESCE(updated_at_ms,updated_at*1000),COALESCE(recency_at_ms,recency_at*1000,0)) AS recent
                    FROM threads"""):
                    assignment = global_state.get("thread-project-assignments", {}).get(row["id"], {})
                    if assignment.get("projectKind") in ("remote", "chatgpt"):
                        continue
                    hints = global_state.get("thread-workspace-root-hints", {})
                    activities.append((row["project_id"] or assignment.get("projectId"),
                                       canonical(hints.get(row["id"]) or row["cwd"]), row["recent"] / 1000))
        except (sqlite3.Error, ValueError, TypeError) as error:
            errors.append("Codex 对话活动暂不可读：" + str(error))
    else:
        errors.append("尚未找到 Codex 对话活动数据库")
    roots = {}
    for (ident, path), name in projects.items():
        # Remote project IDs and paths on other hosts never become local Finder targets.
        if not Path(path).exists() or not Path(path).is_dir():
            continue
        root = roots.setdefault(path, {"path": path, "name": name, "project_ids": [],
                                       "last_conversation": None, "conversation_known": not errors})
        root["project_ids"].append(ident)
    for root in roots.values():
        times = [recent for ident, cwd, recent in activities
                 if ident in root["project_ids"] or cwd == root["path"] or cwd.startswith(root["path"] + "/")]
        root["last_conversation"] = max(times) if times else None
    return {"projects": list(roots.values()), "errors": errors}
