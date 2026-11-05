"""Render a promo page (index.html by default) to an MP4 file.

Each frame is produced deterministically through ``window.__seek(t)``, captured
with Playwright, and encoded with ffmpeg. Needs the dev dependencies from
``requirements-dev.txt`` plus an ``ffmpeg`` binary on PATH. Temporary frames
take roughly 200-300 MB at 1080p/30fps.

Usage:
    .venv/bin/python docs/promo/render.py --out dist/promo.mp4
    .venv/bin/python docs/promo/render.py --page forgetful.html --out dist/forgetful.mp4
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

logger = logging.getLogger(__name__)
PROMO_DIR = Path(__file__).parent


def render(page_path: Path, out: Path, fps: int, browser: str | None, subs: Path | None = None) -> None:
    """Capture every frame of the promo and encode them into ``out``.

    Args:
        page_path: Promo HTML page to capture.
        out: Destination MP4 path.
        fps: Frames per second.
        browser: Optional Chromium-compatible executable path.
        subs: Optional cue file from subtitles.py, burned into every frame.

    Raises:
        RuntimeError: When ffmpeg is not available.
    """
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found on PATH")
    with tempfile.TemporaryDirectory(prefix="gtd-promo-") as tmp, sync_playwright() as pw:
        chromium = pw.chromium.launch(executable_path=browser)
        page = chromium.new_page(viewport={"width": 1920, "height": 1080}, device_scale_factor=1)
        page.goto(page_path.resolve().as_uri() + "?record")
        page.wait_for_function("window.__ready === true")
        if subs is not None:
            data = json.loads(subs.read_text(encoding="utf-8"))
            page.evaluate("([c, r]) => window.__setSubs(c, r)", [data["cues"], data["raise_after"]])
        total = int(page.evaluate("window.__DURATION") * fps)
        for i in range(total + 1):
            page.evaluate("t => window.__seek(t)", i / fps)
            page.screenshot(path=f"{tmp}/f{i:05d}.jpg", type="jpeg", quality=95)
            if i % (fps * 5) == 0:
                logger.info("frame %d/%d", i, total)
        chromium.close()
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(fps), "-i", f"{tmp}/f%05d.jpg",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", "-movflags", "+faststart", str(out)],
            check=True,
        )
    logger.info("wrote %s (%d frames)", out, total + 1)


def main() -> None:
    """Parse CLI arguments and render the promo."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--page", default="index.html", help="HTML file inside docs/promo")
    parser.add_argument("--out", type=Path, default=Path("dist/promo.mp4"))
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--browser", default=None, help="Chromium-compatible executable path")
    parser.add_argument("--subs", type=Path, default=None, help="Subtitle cue JSON from subtitles.py")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    render(PROMO_DIR / args.page, args.out, args.fps, args.browser, args.subs)


if __name__ == "__main__":
    main()
