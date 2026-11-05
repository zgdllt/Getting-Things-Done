"""Subtitle cues for the promo films, built from a narration manifest.

Each narration line (from ``narration.py``) is split at punctuation into cues of
at most ``MAX_CHARS`` characters, timed in proportion to their length inside the
clip's real start and duration. Output is a JSON file for ``render.py --subs``
(burned into the frames) and an SRT file for platforms that take sidecar subtitles.

Usage:
    python docs/promo/subtitles.py --film forgetful --manifest /tmp/vo/forgetful/manifest.json \\
        --json /tmp/subs/forgetful.json --srt /tmp/subs/forgetful.srt
"""
from __future__ import annotations

import argparse
import json
import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_CHARS = 16
# Time after which subtitles move up so they clear the end-card disclaimer.
END_CARD = {"index": 51.8, "forgetful": 52.3}
_TAG = re.compile(r"\[[^\]]*\]\s*")
_BREAK = re.compile(r"(?<=[，。！？；：…—])")
_SOFT = str.maketrans({"，": " ", "。": " ", "；": " ", "：": " ", "—": " "})


@dataclass(frozen=True)
class Cue:
    """One on-screen subtitle."""

    start: float
    end: float
    text: str


def _clean(text: str) -> str:
    """Subtitle style: commas and full stops become spaces, ``！？…`` are kept."""
    return re.sub(r"\s+", " ", text.translate(_SOFT)).strip().rstrip("、")


def _phrases(text: str) -> list[str]:
    """Punctuation-delimited phrases; overlong ones are split again at ``、``."""
    out: list[str] = []
    for phrase in (p for p in _BREAK.split(_TAG.sub("", text).strip()) if p.strip()):
        out.extend(re.split(r"(?<=、)", phrase) if len(_clean(phrase)) > MAX_CHARS else [phrase])
    return [p for p in out if p.strip()]


def split_line(text: str) -> list[str]:
    """Split one narration line into cue texts of at most ``MAX_CHARS`` visible characters.

    A cue never continues past a sentence end or a hard break (``。！？：——``).
    """
    chunks: list[str] = []
    for phrase in _phrases(text):
        sentence_ended = bool(chunks) and chunks[-1].rstrip()[-1:] in "。！？：—"
        if chunks and not sentence_ended and len(_clean(chunks[-1] + phrase)) <= MAX_CHARS:
            chunks[-1] += phrase
        else:
            chunks.append(phrase)
    return [c for c in (_clean(c) for c in chunks) if c]


def build_cues(manifest: list[dict]) -> list[Cue]:
    """Turn manifest entries (``start``, ``seconds``, ``text``) into timed cues."""
    cues: list[Cue] = []
    for item in manifest:
        parts = split_line(item["text"])
        weights = [max(len(p.replace(" ", "")), 1) for p in parts]
        t, total = float(item["start"]), float(item["seconds"])
        for part, w in zip(parts, weights, strict=True):
            span = total * w / sum(weights)
            cues.append(Cue(round(t, 3), round(t + span, 3), part))
            t += span
    return cues


def _srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    return f"{ms // 3_600_000:02d}:{ms // 60_000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def write_srt(cues: list[Cue], path: Path) -> None:
    """Write cues as SubRip text."""
    blocks = [f"{i}\n{_srt_time(c.start)} --> {_srt_time(c.end)}\n{c.text}\n" for i, c in enumerate(cues, 1)]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(blocks), encoding="utf-8")


def main() -> None:
    """Parse CLI arguments and write the JSON and SRT subtitle files."""
    parser = argparse.ArgumentParser(description="Build promo subtitles from a narration manifest.")
    parser.add_argument("--film", choices=sorted(END_CARD), required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--json", type=Path, required=True, help="Cue file for render.py --subs")
    parser.add_argument("--srt", type=Path, default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cues = build_cues(json.loads(args.manifest.read_text(encoding="utf-8")))
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps({"raise_after": END_CARD[args.film], "cues": [asdict(c) for c in cues]},
                                    ensure_ascii=False, indent=1), encoding="utf-8")
    if args.srt:
        write_srt(cues, args.srt)
    for c in cues:
        logger.info("%6.2f-%6.2f  %s", c.start, c.end, c.text)


if __name__ == "__main__":
    main()
