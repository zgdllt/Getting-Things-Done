#!/usr/bin/env python3
"""Build a self-contained Toy from public UI code and synthetic demo data."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = {
    "workbench.css": ROOT / "web/style.css",
    "deck.js": ROOT / "web/deck.js",
    "icon.svg": ROOT / "web/icon.svg",
    "demo.js": ROOT / "demos/toy/demo.js",
    "demo.css": ROOT / "demos/toy/demo.css",
}


def build(destination):
    destination = Path(destination).resolve()
    if destination == ROOT or destination in ROOT.parents or destination == ROOT / "demos/toy":
        raise ValueError("Choose a separate build output directory")
    destination.mkdir(parents=True, exist_ok=True)
    html = (ROOT / "demos/toy/index.html").read_text()
    hashes = {}
    for name, source in ASSETS.items():
        content = source.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        path = Path(name)
        target_name = path.stem + "-" + digest[:10] + path.suffix
        (destination / target_name).write_bytes(content)
        html = html.replace("./" + name, "./" + target_name)
        hashes[target_name] = digest
    # Replace only files from a prior build, leaving unrelated output intact.
    manifest = destination / "build-manifest.json"
    if manifest.exists():
        for name in json.loads(manifest.read_text()).get("assets", {}):
            if name not in hashes and Path(name).name == name:
                (destination / name).unlink(missing_ok=True)
    (destination / "index.html").write_text(html)
    manifest.write_text(json.dumps({"assets": hashes, "data": "synthetic", "model_calls": False}, indent=2) + "\n")
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(ROOT / ".test-artifacts/toy-demo"))
    args = parser.parse_args()
    print(build(args.output))
