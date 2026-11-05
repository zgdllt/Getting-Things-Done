"""Narration for the promo films, synthesized with a Fish Speech (S2 Pro) server.

Each line has a start time and a deadline taken from the animation timeline.
Voice consistency: unless ``--ref-audio`` is given, the first line is generated
without a reference using ``--seed``, and that clip becomes the reference voice
for every following line. Lines that overrun their slot by up to 20 % are sped up
with ffmpeg ``atempo``; longer overruns are reported so the text can be trimmed.

The S2 Pro weights are under the Fish Audio Research License (non-commercial);
anything distributed with this audio must credit "Built with Fish Audio".

Usage (server reachable at the URL, e.g. through ``ssh -L 18080:127.0.0.1:8080 asta``):
    python docs/promo/narration.py --film forgetful --url http://127.0.0.1:18080/v1/tts --out-dir /tmp/vo
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import ormsgpack
import requests

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Line:
    """One narration line: spoken between ``start`` and ``end`` seconds."""

    start: float
    end: float
    text: str


SCRIPTS: dict[str, list[Line]] = {
    "index": [
        Line(0.3, 6.6, "[excited tone] 一天几十个网页、几十条消息、好几场会——你答应别人的事，还记得吗？"),
        Line(7.3, 12.3, "[confident] GTD 只说一件事：大脑是用来想事情的，[emphasis] 不是用来记事情的！"),
        Line(12.4, 15.2, "[excited] 而最难的，就是第一步：收集！"),
        Line(15.3, 19.4, "打开 Computer History，每一条线索，[emphasis] 自动留下！"),
        Line(19.5, 23.6, "零散的线索，自动归拢，只剩一个问题，等你拍板！"),
        Line(24.6, 30.6, "回答它，就这么简单！答案立刻保存，下一步马上成型！"),
        Line(33.2, 37.0, "[confident] 你点头，它才写进清单。[emphasis] 绝不擅自做主！"),
        Line(40.3, 46.2, "线索不是承诺！工作台留在本机，提醒事项和日历，才是正式清单！"),
        Line(46.8, 51.4, "[excited] 现在，你只需要决定——先做哪一件！"),
        Line(52.0, 56.8, "[excited tone] Getting Things Done！留一点心力，给正在做的事！"),
    ],
    "forgetful": [
        Line(0.4, 5.0, "[excited] 这个东西，治好了我的健忘。[short pause] 真的！"),
        Line(5.5, 10.7, "我是那种答应完就忘的人。不是不在乎，是真的会忘。"),
        Line(10.8, 14.0, "[sigh] 这个月，已经第三次了。"),
        Line(14.3, 17.9, "清单软件、手账、备忘录，我全都试过。"),
        Line(18.1, 21.3, "问题是…… [pause] 我连记下来都会忘。"),
        Line(21.4, 25.5, "后来我开了 Computer History，不用记，也不用写。"),
        Line(25.6, 29.0, "GTD 从里面，认出了我随口答应的一句话。"),
        Line(30.8, 34.9, "它只问了我一个问题：做到什么程度，算完成？"),
        Line(35.0, 39.0, "我回答完，点一下确认，它才写进提醒事项。"),
        Line(39.5, 42.6, "第二天一早，它比我先想起来。"),
        Line(42.7, 45.9, "当天晚上就发了，[laughing tone] 还提前了一天！"),
        Line(46.2, 48.1, "所以，健忘治好了吗？"),
        Line(48.2, 49.6, "没有。"),
        Line(49.7, 52.6, "只是答应过的事，不用再靠我记了。"),
        Line(53.0, 57.7, "Getting Things Done。把记挂的事交给清单，把注意力留给眼前。"),
    ],
}
MAX_STRETCH = 1.2
_TAG = re.compile(r"\[[^\]]*\]\s*")


def wav_seconds(path: Path) -> float:
    """Return the duration of a WAV file in seconds."""
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


def synthesize(url: str, text: str, ref: tuple[bytes, str] | None, seed: int, timeout: float) -> bytes:
    """Request one WAV clip from a Fish Speech ``/v1/tts`` endpoint.

    Raises:
        requests.HTTPError: When the server rejects the request.
    """
    payload = {"text": text, "format": "wav", "seed": seed, "normalize": True,
               "references": [{"audio": ref[0], "text": ref[1]}] if ref else []}
    resp = requests.post(url, data=ormsgpack.packb(payload), timeout=timeout,
                         headers={"Content-Type": "application/msgpack"})
    resp.raise_for_status()
    return resp.content


def trim_silence(path: Path, threshold: float = 0.012, keep: float = 0.05) -> None:
    """Cut leading and trailing silence, keeping ``keep`` seconds of margin."""
    with wave.open(str(path), "rb") as w:
        params, raw = w.getparams(), w.readframes(w.getnframes())
    data = np.frombuffer(raw, dtype="<i2").reshape(-1, params.nchannels)
    loud = np.flatnonzero(np.abs(data).max(axis=1) > threshold * 32767)
    if loud.size == 0:
        return
    margin = int(keep * params.framerate)
    data = data[max(loud[0] - margin, 0):loud[-1] + margin]
    with wave.open(str(path), "wb") as w:
        w.setparams(params)
        w.writeframes(data.astype("<i2").tobytes())


def fit_to_slot(path: Path, slot: float) -> float:
    """Speed a clip up (pitch preserved) when it overruns its slot by at most ``MAX_STRETCH``."""
    length = wav_seconds(path)
    if length <= slot:
        return length
    ratio = length / slot
    if ratio > MAX_STRETCH:
        logger.warning("%s is %.2fs for a %.2fs slot (x%.2f): trim the text", path.name, length, slot, ratio)
        return length
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found on PATH")
    tmp = path.with_suffix(".fit.wav")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(path),
                    "-filter:a", f"atempo={ratio:.4f}", str(tmp)], check=True)
    tmp.replace(path)
    logger.info("%s sped up x%.2f to fit %.2fs", path.name, ratio, slot)
    return wav_seconds(path)


def generate(film: str, url: str, out_dir: Path, seed: int, ref_audio: Path | None, ref_text: str | None,
             timeout: float, only: set[int] | None = None) -> None:
    """Synthesize lines of ``film`` into ``out_dir`` and update ``manifest.json``.

    ``only`` regenerates the listed line indices and keeps the others; without
    ``--ref-audio`` the existing first clip then stays the reference voice.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = SCRIPTS[film]
    first = out_dir / f"{film}_00.wav"
    ref = (ref_audio.read_bytes(), ref_text or "") if ref_audio else None
    if ref is None and only and 0 not in only:
        ref = (first.read_bytes(), _TAG.sub("", lines[0].text))
    manifest_path = out_dir / "manifest.json"
    old: dict[str, dict] = {}
    if manifest_path.exists():
        old = {m["file"]: m for m in json.loads(manifest_path.read_text(encoding="utf-8"))}
    manifest = []
    for i, line in enumerate(lines):
        path = out_dir / f"{film}_{i:02d}.wav"
        if only is not None and i not in only and path.name in old:
            manifest.append(old[path.name])
            continue
        path.write_bytes(synthesize(url, line.text, ref, seed, timeout))
        trim_silence(path)
        if ref is None:  # first clip defines the voice for the rest of the film
            ref = (path.read_bytes(), _TAG.sub("", line.text))
        length = fit_to_slot(path, line.end - line.start)
        manifest.append({"file": path.name, "start": line.start, "end": line.end, "seconds": round(length, 3),
                         "text": line.text, "seed": seed})
        logger.info("[%02d] %.2fs / slot %.2fs  %s", i, length, line.end - line.start, line.text)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    """Parse CLI arguments and synthesize narration for one film."""
    parser = argparse.ArgumentParser(description="Synthesize promo narration with Fish Speech.")
    parser.add_argument("--film", choices=sorted(SCRIPTS), required=True)
    parser.add_argument("--url", default="http://127.0.0.1:18080/v1/tts")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=7, help="Voice seed when no reference is given")
    parser.add_argument("--ref-audio", type=Path, default=None, help="Reference voice clip (with consent)")
    parser.add_argument("--ref-text", default=None, help="Transcript of the reference clip")
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--only", default=None, help="Comma-separated line indices to regenerate")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    only = {int(i) for i in args.only.split(",")} if args.only else None
    generate(args.film, args.url, args.out_dir, args.seed, args.ref_audio, args.ref_text, args.timeout, only)


if __name__ == "__main__":
    main()
