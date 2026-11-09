"""Indexed, private file suggestions. Never reads a legacy large scan into memory."""
import contextlib
import json
from pathlib import Path
import sqlite3
import time
import uuid

import gtd


def folder(state):
    path = state.base / "file-organizer"
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def defaults():
    return {"version": 2, "scopes": [], "tags": [], "mapping": {}, "job": {"state": "idle"},
            "revision": 0, "requested": 0, "scanned": -1, "scan_errors": [],
            "initialized": False, "idle_days": 30, "excluded_projects": [], "worker_pid": None}


class Store:
    def __init__(self, state):
        self.root = folder(state)
        path = self.root / "library.sqlite3"
        # Initialization is serialized across independent HTTP connections and workers.
        with state.lock("files-store-init.lock", blocking=True):
            fresh = not path.exists()
            self.db = sqlite3.connect(path, timeout=10)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA busy_timeout=10000")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata(id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS items(
                    id TEXT PRIMARY KEY, path TEXT UNIQUE NOT NULL, status TEXT NOT NULL,
                    category TEXT NOT NULL, present INTEGER NOT NULL, search TEXT NOT NULL,
                    value TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS file_status ON items(present,status,id);
                CREATE INDEX IF NOT EXISTS file_category ON items(present,category,id);
                CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY, created_at TEXT NOT NULL, value TEXT NOT NULL);
            """)
            if fresh or not self.db.execute("SELECT 1 FROM metadata").fetchone():
                data = defaults()
                legacy = self.root / "library.json"
                old = {}
                if legacy.exists() and legacy.stat().st_size > 4 * 1024 * 1024:
                    archived = self.root / ("library-paused-" + str(time.time_ns()) + ".json")
                    legacy.rename(archived)
                    (self.root / "paused").write_text("Legacy recursive scan requires explicit restart\n")
                    data["paused_archive"] = archived.name
                elif legacy.exists():
                    old = json.loads(legacy.read_text())
                    data.update({k: v for k, v in old.items() if k not in ("items", "operations")})
                data["version"] = 2
                # The new unit of organization is exactly a selected root, never its descendants.
                data["scopes"] = [{key: value for key, value in scope.items() if key != "recursive"}
                                  for scope in data["scopes"]]
                self.save(data)
                roots = {scope["path"] for scope in data["scopes"]}
                for item in old.get("items", []):
                    if item["path"] in roots:
                        self.put(item)
                for operation in old.get("operations", []):
                    self.operation(operation)
                # Applied plans are independent receipts, including after a huge library
                # was quarantined without parsing it. Retain their undo entries.
                for receipt in self.root.glob("*.json"):
                    try:
                        if uuid.UUID(receipt.stem).hex != receipt.stem:
                            continue
                    except ValueError:
                        continue
                    operation = json.loads(receipt.read_text())
                    if operation.get("state") != "prepared":
                        self.operation(operation)
                self.db.commit()
                gtd.atomic_json(legacy, {"version": 2, "storage": "library.sqlite3"})

    def close(self):
        self.db.close()

    def meta(self):
        return {**defaults(), **json.loads(self.db.execute("SELECT value FROM metadata WHERE id=1").fetchone()[0])}

    def save(self, data):
        self.db.execute("INSERT OR REPLACE INTO metadata VALUES(1,?)", (gtd.encode(data),))

    def put(self, item):
        search = " ".join(str(item.get(key, "")) for key in ("name", "path", "reason"))
        search += " " + " ".join(item.get("tags", []))
        self.db.execute("""INSERT INTO items VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET path=excluded.path,status=excluded.status,
            category=excluded.category,present=excluded.present,search=excluded.search,value=excluded.value""",
            (item["id"], item["path"], item["status"], item["category"], int(item["present"]),
             search.casefold(), gtd.encode(item)))

    def get(self, ident):
        row = self.db.execute("SELECT value,present FROM items WHERE id=?", (ident,)).fetchone()
        return {**json.loads(row[0]), "present": bool(row[1])} if row else None

    def items(self, where="1", parameters=(), limit=None, offset=0):
        sql = "SELECT value,present FROM items WHERE " + where + " ORDER BY id"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            parameters = (*parameters, limit, offset)
        return [{**json.loads(row[0]), "present": bool(row[1])} for row in self.db.execute(sql, parameters)]

    def operation(self, plan):
        self.db.execute("INSERT OR REPLACE INTO operations VALUES(?,?,?)",
                        (plan["id"], plan["created_at"], gtd.encode(plan)))

    def recent_operations(self):
        result = []
        for row in self.db.execute("SELECT value FROM operations ORDER BY created_at DESC LIMIT 10"):
            plan = json.loads(row[0])
            # Full receipts remain in their plan file; polling never downloads all paths.
            result.append({**{key: plan[key] for key in ("id", "created_at", "state")},
                           "success_count": sum(item["ok"] for item in plan["results"]),
                           "results": [item for item in plan["results"] if not item["ok"]][:5],
                           "undo_results": [item for item in plan.get("undo_results", []) if not item["ok"]][:5]})
        return result


@contextlib.contextmanager
def open_store(state):
    store = Store(state)
    try:
        with store.db:
            yield store
    finally:
        store.close()


def cache_info(state):
    root = folder(state)
    files = [{"name": p.name, "bytes": p.stat().st_size}
             for p in sorted(root.glob("library-paused-*.json")) if p.is_file() and not p.is_symlink()]
    index_bytes = sum(p.stat().st_size for p in root.glob("library.sqlite3*") if p.is_file())
    return {"path": str(root), "archives": files, "archive_bytes": sum(p["bytes"] for p in files),
            "index_bytes": index_bytes}
