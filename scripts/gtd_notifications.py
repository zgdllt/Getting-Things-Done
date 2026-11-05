"""Deliver actionable notifications without changing the EventKit app identity."""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import time
import uuid

import gtd


def bundle(state):
    with state.lock("app-build.lock", blocking=True):
        return build_bundle(state)


def build_bundle(state):
    # A distributed app already contains the runtime and its native executable.
    # Reuse it instead of rebuilding a second app on the destination machine.
    if gtd.SKILL.parent.name == "Resources" and gtd.SKILL.parent.parent.name == "Contents":
        hosted = gtd.SKILL.parent.parent.parent
        if hosted.suffix == ".app" and (hosted / "Contents/MacOS/gtd-notify").is_file():
            # Let the separately installed skill reuse this app without Xcode.
            stamp = state.base / "runtime/gtd-notify.source.sha256"
            stamp.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            stamp.write_text(hashlib.sha256((gtd.HERE / "gtd_notify.swift").read_bytes()).hexdigest())
            return hosted
    source = gtd.HERE / "gtd_notify.swift"
    app = ((Path.home() / "Applications" / "Getting Things Done.app") if state.base == (Path.home() / ".codex/personal-gtd").resolve()
           else state.base / "runtime" / "Getting Things Done.app")
    binary = app / "Contents/MacOS/gtd-notify"
    stamp = state.base / "runtime" / "gtd-notify.source.sha256"
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if binary.exists() and stamp.exists() and stamp.read_text() == digest:
        return app
    binary.parent.mkdir(parents=True, exist_ok=True)
    (app / "source.sha256").unlink(missing_ok=True)
    info = {"CFBundleIdentifier": "local.codex.personal-gtd.notifications",
            "CFBundleName": "Getting Things Done", "CFBundleDisplayName": "Getting Things Done",
            "CFBundleExecutable": "gtd-notify", "CFBundlePackageType": "APPL",
            "CFBundleVersion": "5", "CFBundleShortVersionString": gtd.VERSION, "LSUIElement": True,
            "CFBundleIconFile": "GTD", "NSHighResolutionCapable": True,
            "GTDStateRoot": str(state.base), "GTDPythonPath": gtd.stable_python(),
            "GTDServiceScript": str(gtd.HERE / "gtd_dashboard.py"),
            "CFBundleURLTypes": [{"CFBundleURLName": "Getting Things Done", "CFBundleURLSchemes": ["personalgtd"]}]}
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
    result = subprocess.run(["/usr/bin/swiftc", "-O", str(source), "-framework", "AppKit",
                             "-framework", "UserNotifications", "-framework", "WebKit", "-o", str(binary)], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("通知组件编译失败：" + result.stderr[-3000:])
    iconset = state.base / "runtime" / "GTD.iconset"
    subprocess.run([str(binary), "--make-iconset", str(iconset)], check=True, capture_output=True, timeout=30)
    resources = app / "Contents" / "Resources"
    resources.mkdir(parents=True, exist_ok=True)
    subprocess.run(["/usr/bin/iconutil", "-c", "icns", "-o", str(resources / "GTD.icns"), str(iconset)], check=True, capture_output=True)
    subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(app)], check=True, capture_output=True)
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(digest)
    return app


def is_running(app):
    return subprocess.run(["/usr/bin/pgrep", "-f", re.escape(str(app / "Contents/MacOS/gtd-notify"))],
                          capture_output=True).returncode == 0


def deliver(state, title, body, identifier=None):
    from gtd_dashboard import open_url
    request = receipt = claimed = None
    def acknowledged():
        if not receipt or not receipt.exists():
            return None
        result = json.loads(receipt.read_text())
        if result.get("ok"):
            return {"channel": "native", "clickable": True, "submitted": True, "submitted_at": gtd.now()}
        if result.get("uncertain"):
            return {"channel": "native", "clickable": True, "submitted": False, "ack_pending": True,
                    "detail": "通知发送结果待核对，待澄清事项仍保留。", "at": gtd.now()}
        raise RuntimeError(result.get("error", "通知未获授权"))
    try:
        app = bundle(state)
        queue = state.base / "notifications"
        queue.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = hashlib.sha256(identifier.encode()).hexdigest()[:32] if identifier else uuid.uuid4().hex
        request, receipt = queue / (key + ".request.json"), queue / (key + ".receipt.json")
        claimed = queue / (key + ".sending.json")
        result = acknowledged()
        if result:
            gtd.atomic_json(state.base / "notification-status.json", result)
            return result
        if not request.exists() and not claimed.exists():
            gtd.atomic_json(request, {"id": key, "title": title, "body": body,
                                     "url": open_url(state.base, "inbox")})
        if not is_running(app):
            subprocess.run(["/usr/bin/open", "-g", "-a", str(app), "--args", "--background", "--state", str(state.base)],
                           check=True, capture_output=True, timeout=10)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            result = acknowledged()
            if result:
                gtd.atomic_json(state.base / "notification-status.json", result)
                return result
            time.sleep(0.1)
        raise TimeoutError("通知组件等待授权或响应超时")
    except Exception as error:
        # All failures after enqueue share the same claim-aware reconciliation.
        try:
            result = acknowledged()
        except RuntimeError:
            result = None  # Explicit native failure: safe to use the existing system channel.
        if result:
            gtd.atomic_json(state.base / "notification-status.json", result)
            return result
        try:
            if request:
                cancelled = request.with_suffix(".cancelled")
                request.rename(cancelled)
                cancelled.unlink()
        except FileNotFoundError:
            if claimed and claimed.exists():
                status = {"channel": "native", "clickable": True, "submitted": False,
                          "ack_pending": True, "detail": "通知发送结果待核对，待澄清事项仍保留。", "at": gtd.now()}
                gtd.atomic_json(state.base / "notification-status.json", status)
                return status
        status = {"channel": "system_script", "clickable": False, "detail": str(error), "at": gtd.now()}
        gtd.atomic_json(state.base / "notification-status.json", status)
        return status
