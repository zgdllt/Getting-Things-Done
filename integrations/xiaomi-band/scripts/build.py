#!/usr/bin/env python3
"""Build the GTD Band Bridge AstroBox plugin (.abp) and band quick app (.rpk).

Usage:
    python3 integrations/xiaomi-band/scripts/build.py [--skip-watch] [--skip-plugin]

Outputs go to integrations/xiaomi-band/dist/. Requires the pinned Rust nightly
from plugin/rust-toolchain.toml (installed via rustup) and Node with
aiot-toolkit installed in watch/ (`npm install`).
"""
import argparse
import hashlib
import json
import logging
import os
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger("band-build")

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugin"
WATCH = ROOT / "watch"
DIST = ROOT / "dist"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rustup_env() -> Dict[str, str]:
    """Prefer a rustup-managed cargo so rust-toolchain.toml is honoured."""
    env = dict(os.environ)
    try:
        prefix = subprocess.run(["brew", "--prefix", "rustup"], check=True,
                                capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return env
    env["PATH"] = str(Path(prefix) / "bin") + os.pathsep + env.get("PATH", "")
    return env


def build_plugin() -> Path:
    manifest = json.loads((PLUGIN / "manifest.json").read_text(encoding="utf-8"))
    cargo_version = next(line.split('"')[1] for line in (PLUGIN / "Cargo.toml").read_text().splitlines()
                         if line.startswith("version"))
    if cargo_version != manifest["version"]:
        raise RuntimeError(f"Cargo version {cargo_version} != manifest version {manifest['version']}")
    subprocess.run(["cargo", "build", "--release"], cwd=PLUGIN, env=rustup_env(), check=True)
    wasm = PLUGIN / "target" / "wasm32-wasip3" / "release" / "gtd_band_bridge.wasm"
    stage = DIST / "plugin"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    shutil.copy2(wasm, stage / manifest["entry"])
    shutil.copy2(PLUGIN / "manifest.json", stage / "manifest.json")
    shutil.copy2(PLUGIN / manifest["icon"], stage / manifest["icon"])
    package = DIST / f"GTD-Band-Bridge-{manifest['version']}.abp"
    with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in ("manifest.json", manifest["entry"], manifest["icon"]):
            archive.write(stage / name, name)
    return package


def build_watch() -> Path:
    subprocess.run(["npm", "run", "build"], cwd=WATCH, check=True,
                   stdout=subprocess.DEVNULL)
    candidates: List[Path] = sorted((WATCH / "dist").glob("*.rpk"), key=lambda p: p.stat().st_mtime)
    if not candidates:
        raise RuntimeError("aiot build produced no .rpk")
    target = DIST / candidates[-1].name
    shutil.copy2(candidates[-1], target)
    return target


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--skip-plugin", action="store_true")
    parser.add_argument("--skip-watch", action="store_true")
    args = parser.parse_args()
    DIST.mkdir(exist_ok=True)
    outputs: List[Path] = []
    if not args.skip_plugin:
        outputs.append(build_plugin())
    if not args.skip_watch:
        outputs.append(build_watch())
    for path in outputs:
        logger.info("%s  %s  %d bytes", sha256(path)[:16], path.relative_to(ROOT), path.stat().st_size)


if __name__ == "__main__":
    main()
