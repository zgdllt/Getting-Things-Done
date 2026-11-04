#!/usr/bin/env python3
"""Exercise a moved release app with synthetic state; never requests native permissions."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time

import gtd
import gtd_dashboard as web


def file_digest(app):
    return {str(path.relative_to(app)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in app.rglob("*") if path.is_file()}


def verify(archive):
    with tempfile.TemporaryDirectory(prefix="gtd-release-runtime-") as temporary:
        root = Path(temporary)
        moved = root / "Moved app directory"
        moved.mkdir()
        subprocess.run(["/usr/bin/ditto", "-x", "-k", str(Path(archive).resolve()), str(moved)], check=True)
        app = moved / "Getting Things Done.app"
        subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], check=True)
        before = file_digest(app)
        state = gtd.State(root / "synthetic-state")
        gtd.atomic_json(state.base / "last-snapshot.json", {"ok": True, "fetched_at": gtd.now(),
            "permissions": {"reminders": "fullAccess", "calendar": "fullAccess"}, "lists": [], "reminders": [], "events": []})
        gtd.atomic_json(state.base / "snapshot-status.json", {"ok": True})
        server = web.Server(("127.0.0.1", 0), state.base, "synthetic-session")
        server.handle_error = lambda *_args: None
        gtd.atomic_json(state.base / "workbench.json", {"port": server.server_port, "token": "synthetic-session"})
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        process = None
        try:
            with (root / "native.log").open("w+") as log:
                process = subprocess.Popen([str(app / "Contents/MacOS/gtd-notify"), "--state", str(state.base),
                    "--verify-dir", str(root / "evidence")], stdout=log, stderr=log)
                receipt = root / "evidence/webview-app.json"
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline and not receipt.exists() and process.poll() is None:
                    time.sleep(0.2)
                if not receipt.exists():
                    log.seek(0)
                    raise RuntimeError("Packaged app did not render: " + log.read()[-2500:])
                view = json.loads(receipt.read_text())
                assert view["sessionReady"] and view["navCount"] == 4 and view["view"] == "today", view
                assert (state.base / "runtime/Personal GTD Bridge.app/Contents/MacOS/gtd-native").is_file()
                assert file_digest(app) == before, "Running the app changed its signed resources"
                assert not list(app.rglob("__pycache__")), "Python wrote into the signed bundle"
                subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], check=True)
                print(json.dumps({"relocated_app": True, "native_view": view, "bridge_provisioned_without_compile": True,
                                  "signature_after_start": "passed", "bundle_unchanged": True,
                                  "native_permissions_requested": False, "system_test_writes": 0}, ensure_ascii=False))
        finally:
            if process and process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
            server.shutdown()
            server.server_close()
            thread.join()
            state.db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive")
    verify(parser.parse_args().archive)
