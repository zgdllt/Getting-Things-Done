"""Check Finder metadata and FSEvents using temporary files only."""
import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import selectors
import subprocess
import tempfile

import gtd


def run(binary=None):
    with tempfile.TemporaryDirectory(prefix="gtd-native-file-tags-") as temporary:
        root = Path(temporary).resolve()
        state = gtd.State(root / "state")
        binary = Path(binary) if binary else gtd.native_binary(state)
        workspace = root / "workspace"
        workspace.mkdir()
        file = workspace / "fixture.txt"
        file.write_text("Synthetic unchanged content\n")
        original = hashlib.sha256(file.read_bytes()).hexdigest()
        attribute = "com.apple.metadata:_kMDItemUserTags"
        def raw(path):
            value = subprocess.check_output(["/usr/bin/xattr", "-px", attribute, str(path)], text=True)
            return plistlib.loads(bytes.fromhex(value))
        def set_raw(path, values):
            subprocess.run(["/usr/bin/xattr", "-wx", attribute,
                            plistlib.dumps(values, fmt=plistlib.FMT_BINARY).hex(), str(path)], check=True)
        def call(command, payload):
            result = subprocess.run([str(binary), command], input=json.dumps(payload),
                                    capture_output=True, text=True, check=True, timeout=20)
            return json.loads(result.stdout)
        set_raw(file, ["synthetic-blue\n4"])
        retired = subprocess.run([str(binary),"files-scan"],
            input=json.dumps({"scopes":[{"path":str(workspace),"recursive":True}]}),capture_output=True,text=True)
        assert retired.returncode != 0 and "废弃" in retired.stdout, retired
        retired_shape = subprocess.run([str(binary),"files-inspect"],
            input=json.dumps({"paths":[str(workspace)],"recursive":False}),capture_output=True,text=True)
        assert retired_shape.returncode != 0 and "废弃" in retired_shape.stdout, retired_shape
        scan = call("files-inspect", {"paths": [str(workspace)]})
        assert [item["path"] for item in scan["items"]] == [str(workspace)], scan
        scan = call("files-inspect", {"paths": [str(file)]})
        record = scan["items"][0]
        assert record["tag_colors"] == {"synthetic-blue": 4}, record
        assert {"name": "synthetic-blue", "color": 4} in scan["tags"]
        entry = {**record, "before": record["tags"], "before_colors": record["tag_colors"],
                 "add": ["synthetic-red"], "remove": [], "colors": {"synthetic-red": 6}}
        applied = call("files-apply", {"items": [entry]})["items"][0]
        assert applied["ok"], applied
        assert raw(file) == ["synthetic-blue\n4", "synthetic-red\n6"]
        # A later unrelated tag is not removed by undo.
        set_raw(file, raw(file) + ["synthetic-later\n3"])
        undo = {**entry, "add": [], "remove": ["synthetic-red"], "remove_colors": {"synthetic-red": 6}}
        undone = call("files-apply", {"items": [undo], "undo": True})["items"][0]
        assert undone["ok"], undone
        assert raw(file) == ["synthetic-blue\n4", "synthetic-later\n3"]
        assert hashlib.sha256(file.read_bytes()).hexdigest() == original
        # The first commit must not claim a user's independent same-tag addition.
        fresh = next(item for item in call("files-inspect", {"paths": [str(file)]})["items"])
        stale = {**fresh, "before": fresh["tags"], "before_colors": fresh["tag_colors"],
                 "add": ["synthetic-red"], "remove": [], "colors": {"synthetic-red": 6}}
        set_raw(file, raw(file) + ["synthetic-red\n6"])
        conflict = call("files-apply", {"items": [stale]})["items"][0]
        assert not conflict["ok"], conflict
        # New content after preview also invalidates a write.
        set_raw(file, ["synthetic-blue\n4"])
        file.write_text("Changed after preview\n")
        assert not call("files-apply", {"items": [entry]})["items"][0]["ok"]
        # A symlink is not used to tag a different target.
        link = workspace / "link.txt"
        link.symlink_to(file)
        assert not call("files-apply", {"items": [{**entry, "path": str(link)}]})["items"][0]["ok"]
        watcher = subprocess.Popen([str(binary), "files-watch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            watcher.stdin.write(json.dumps({"scopes":[{"path":str(workspace)}]}));watcher.stdin.close()
            assert json.loads(watcher.stdout.readline())["kind"] == "watching"
            with selectors.DefaultSelector() as selector:
                selector.register(watcher.stdout, selectors.EVENT_READ)
                (workspace / "new.txt").write_text("filesystem event fixture")
                assert selector.select(timeout=10), "No file-change event"
                event = json.loads(watcher.stdout.readline())
                assert event["kind"] == "files_changed" and str(workspace) in event["roots"],event
        finally:
            watcher.terminate()
            watcher.wait(timeout=5)
        state.db.close()
        return {"ok": True, "custom_colors_preserved": True, "undo_preserves_later_tags": True,
                "preview_conflicts_rejected": True, "file_events": True, "root_only_scan": True,
                "old_recursive_protocol_rejected": True, "user_files_modified": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary")
    arguments = parser.parse_args()
    print(json.dumps(run(arguments.binary), ensure_ascii=False))
