#!/usr/bin/env python3
"""Build a relocatable macOS app, source archive and SHA-256 manifest."""
import argparse
import hashlib
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import tempfile
import zipfile

import gtd
import gtd_notifications
from install import PAYLOAD

ROOT = Path(__file__).resolve().parent.parent
APP_NAME = "Getting Things Done.app"


def run(arguments, **kwargs):
    return subprocess.run(arguments, cwd=ROOT, check=True, **kwargs)


def compile_component(source, output, architecture, frameworks):
    arguments = ["/usr/bin/swiftc", "-O", "-target", architecture + "-apple-macos14.0",
                 "-file-prefix-map", str(ROOT) + "=Getting-Things-Done", source]
    for framework in frameworks:
        arguments += ["-framework", framework]
    run(arguments + ["-o", str(output)])


def build_app(directory, architecture):
    """Use temporary build state only; never installs or requests native access."""
    directory = Path(directory)
    with tempfile.TemporaryDirectory(prefix="gtd-release-build-") as temporary:
        state = gtd.State(Path(temporary) / "state")
        try:
            template = gtd_notifications.bundle(state)
            bridge = gtd.build_native_binary(state).parents[2]
            app = directory / APP_NAME
            shutil.copytree(template, app)
            resources = app / "Contents/Resources"
            skill = resources / "skill"
            for name in PAYLOAD:
                target = skill / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / name, target)
            bundled_bridge = resources / "Personal GTD Bridge.app"
            shutil.copytree(bridge, bundled_bridge)
            compile_component("scripts/gtd_native.swift", bundled_bridge / "Contents/MacOS/gtd-native",
                              architecture, ["EventKit", "CoreServices"])
            compile_component("scripts/gtd_notify.swift", app / "Contents/MacOS/gtd-notify",
                              architecture, ["AppKit", "UserNotifications", "WebKit"])
            for bundle in (bundled_bridge, app):
                path = bundle / "Contents/Info.plist"
                info = plistlib.loads(path.read_bytes())
                for field in ("GTDStateRoot", "GTDPythonPath", "GTDServiceScript"):
                    info.pop(field, None)
                info["LSMinimumSystemVersion"] = "14.0"
                path.write_bytes(plistlib.dumps(info))
                run(["/usr/bin/codesign", "--force", "--sign", "-", str(bundle)], capture_output=True)
                run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(bundle)])
            audit_app(app, architecture)
            return app
        finally:
            state.db.close()


def audit_app(app, architecture):
    forbidden = (str(Path.home()).encode(), b"/Users/", b"/var/folders/", b"PRIVATE KEY-----")
    for path in app.rglob("*"):
        if not path.is_file():
            continue
        data = path.read_bytes()
        if any(value in data for value in forbidden):
            raise RuntimeError("Private build path or secret found in " + str(path.relative_to(app)))
        if path.name in {"config.json", "workbench.json", "last-snapshot.json"} or path.suffix in {".sqlite3", ".jsonl", ".pyc"}:
            raise RuntimeError("Runtime data found in " + str(path.relative_to(app)))
    for relative in ("Contents/MacOS/gtd-notify", "Contents/Resources/Personal GTD Bridge.app/Contents/MacOS/gtd-native"):
        binary = app / relative
        run(["/usr/bin/lipo", str(binary), "-verify_arch", architecture])
        version = run(["/usr/bin/vtool", "-show-build", str(binary)], capture_output=True, text=True).stdout
        if "minos 14.0" not in version:
            raise RuntimeError("Unexpected deployment target in " + relative + ": " + version)


def archive_app(app, output):
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(app.rglob("*")):
            archive.write(path, Path(APP_NAME) / path.relative_to(app))


def package(output, architecture, app_only=False):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if not app_only:
        dirty = run(["git", "status", "--porcelain"], capture_output=True, text=True).stdout
        if dirty:
            raise RuntimeError("Commit the release source before creating matching app/source archives; use --app-only for development builds")
    prefix = "Getting-Things-Done-v" + gtd.VERSION
    assets = []
    with tempfile.TemporaryDirectory(prefix="gtd-release-stage-") as temporary:
        app = build_app(Path(temporary), architecture)
        archive = output / (prefix + "-macos-" + architecture + ".zip")
        archive_app(app, archive)
        assets.append(archive)
    if not app_only:
        source = output / (prefix + "-source.zip")
        run(["git", "archive", "--format=zip", "--prefix=" + prefix + "/", "--output=" + str(source), "HEAD"])
        assets.append(source)
    manifest = "".join(hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.name + "\n" for path in assets)
    (output / "SHA256SUMS").write_text(manifest)
    return assets + [output / "SHA256SUMS"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="dist")
    parser.add_argument("--arch", choices=["arm64", "x86_64"], default=platform.machine())
    parser.add_argument("--app-only", action="store_true", help="Build a development app without archiving uncommitted source")
    args = parser.parse_args()
    if platform.system() != "Darwin":
        parser.error("Native release packaging requires macOS and Xcode Command Line Tools")
    for path in package(args.output, args.arch, args.app_only):
        print(path.name)


if __name__ == "__main__":
    main()
