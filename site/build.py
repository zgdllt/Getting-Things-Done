"""Assemble the GitHub Pages site into a deployable folder.

Copies ``site/`` and pulls shared assets from the repository so nothing is
duplicated by hand: screenshots and icon from ``docs/images`` and ``web``,
and the interactive promo films from ``docs/promo``.

Usage:
    python3 site/build.py --out _site
    python3 -m http.server -d _site 8000   # local preview
"""
from __future__ import annotations

import argparse
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
# (source relative to repo root, destination relative to the output folder)
SHARED: list[tuple[str, str]] = [
    ("docs/images/workbench.png", "assets/workbench.png"),
    ("docs/images/clarify.png", "assets/clarify.png"),
    ("docs/images/app-icon.png", "assets/app-icon.png"),
    ("web/icon.svg", "assets/icon.svg"),
    ("docs/promo/index.html", "films/index.html"),
    ("docs/promo/forgetful.html", "films/forgetful.html"),
]


def build(out: Path) -> None:
    """Write the complete static site to ``out`` (replacing it).

    Raises:
        FileNotFoundError: When a shared asset is missing from the repository.
    """
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(SITE, out, ignore=shutil.ignore_patterns("build.py", "__pycache__", ".DS_Store"))
    for src, dst in SHARED:
        target = out / dst
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / src, target)
    (out / ".nojekyll").write_text("", encoding="utf-8")
    files = [p for p in out.rglob("*") if p.is_file()]
    logger.info("built %s: %d files, %.1f MB", out, len(files), sum(p.stat().st_size for p in files) / 1e6)


def main() -> None:
    """Parse CLI arguments and build the site."""
    parser = argparse.ArgumentParser(description="Assemble the GitHub Pages site.")
    parser.add_argument("--out", type=Path, default=ROOT / "_site")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    build(args.out.resolve())


if __name__ == "__main__":
    main()
