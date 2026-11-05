"""Scores for the two promo films, and a CLI that renders and muxes them.

Cue times mirror the animation timelines in ``index.html`` and
``forgetful.html``; if a scene moves there, move its cues here too.

Usage:
    python docs/promo/audio_scores.py --film index --video in.mp4 --out with_audio.mp4
    python docs/promo/audio_scores.py --film forgetful --voice-dir /tmp/vo/forgetful --video in.mp4 --out out.mp4
    python docs/promo/audio_scores.py --film forgetful --wav forgetful.wav
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

import audio_synth as s
import numpy as np

logger = logging.getLogger(__name__)
Chord = tuple[str, list[str]]


def _up(note: str) -> str:
    return note[:-1] + str(int(note[-1]) + 1)


def _clicks(sfx: s.Bus, times: Sequence[float]) -> None:
    for t in times:
        sfx.add(s.click(), t, 0.35, pan=0.3)


def _typing(sfx: s.Bus, rng: np.random.Generator, text: str, t0: float, t1: float) -> None:
    for k in range(1, len(text) + 1):
        sfx.add(s.key(rng), t0 + (t1 - t0) * k / len(text) + rng.uniform(-0.015, 0.015), 0.16, pan=0.25)


def _silence(bus: s.Bus, t0: float, t1: float, ramp: float = 0.04) -> None:
    """Gate a bus to silence between ``t0`` and ``t1`` with short ramps."""
    a, b, r = int(t0 * s.SR), int(t1 * s.SR), int(ramp * s.SR)
    bus.data[:, a:b] = 0
    bus.data[:, max(a - r, 0):a] *= np.linspace(1, 0, min(r, a))
    end = min(b + r, bus.data.shape[1])
    bus.data[:, b:end] *= np.linspace(0, 1, end - b)


# ------------------------------------------------------------- film 1: index
DRIVE_CHORDS: list[Chord] = [("D2", ["D4", "F#4", "A4"]), ("A2", ["C#4", "E4", "A4"]),
                             ("B2", ["D4", "F#4", "B4"]), ("G2", ["D4", "G4", "B4"])]
DRIVE_HOOK: list[list[str | None]] = [["A4", None, "D5", None, "F#5", None, "E5", "D5"],
                                      ["E5", None, "C#5", None, "A4", None, "B4", "C#5"],
                                      ["D5", None, "F#5", None, "B5", None, "A5", "F#5"],
                                      ["G5", None, "F#5", None, "E5", None, "D5", None]]


def _drive_bar(music: s.Bus, rng: np.random.Generator, t0: float, k: int, *, kick: bool, clap: bool,
               stabs: bool, arp: bool, hook: bool, pad: bool = False) -> None:
    """One 2-second bar (120 BPM) of the upbeat electronic bed; ``k`` picks the chord."""
    beat = 0.5
    root, chord = DRIVE_CHORDS[k % 4]
    for i in range(8):  # octave-pumping bass eighths
        music.add(s.bass(root if i % 2 == 0 else _up(root), 0.3), t0 + i * beat / 2, 0.42 if i % 2 == 0 else 0.3)
    for i in range(16):
        music.add(s.hat(rng), t0 + i * beat / 4, 0.05 if i % 2 else 0.03, pan=0.3)
    if kick:
        for i in range(4):
            music.add(s.kick(), t0 + i * beat, 0.6)
            music.add(s.open_hat(rng), t0 + (i + 0.5) * beat, 0.12, pan=-0.25)
    if clap:
        for i in (1, 3):
            music.add(s.clap(rng), t0 + i * beat, 0.34)
    if stabs:
        for off in (0.5, 1.5, 2.5, 3.5):
            music.add(s.stab(chord), t0 + off * beat, 0.2, pan=-0.15)
    if pad:
        music.add(s.pad(chord, 2.6), t0, 0.5)
    if arp:
        tones = chord + [_up(n) for n in chord]
        for i in range(16):
            music.add(s.pluck(_up(tones[i % 6]), 0.25), t0 + i * beat / 4, 0.055, pan=0.4 if i % 2 else -0.4)
    if hook:
        for i, n in enumerate(DRIVE_HOOK[k % 4]):
            if n:
                music.add(s.lead(n, 0.32), t0 + i * beat / 2, 0.15, pan=0.1)


def score_index(rng: np.random.Generator) -> tuple[s.Bus, s.Bus, float]:
    """Upbeat 120 BPM electronic bed in D for the product walkthrough (57 s).

    Bar ``k`` starts at ``2k - 1`` s so drops land on scene changes at 7, 15, 23 and 47 s.
    """
    dur = 57.0
    music, sfx = s.Bus(dur), s.Bus(dur)
    for k in range(1, 26):
        t0 = 2 * k - 1
        if k <= 3:  # intro: pulse building to the GTD scene
            _drive_bar(music, rng, t0, k, kick=k >= 2, clap=False, stabs=k == 3, arp=False, hook=False, pad=True)
        elif 20 <= k <= 23:  # breakdown under the principles
            _drive_bar(music, rng, t0, k, kick=False, clap=k == 23, stabs=False, arp=True, hook=False, pad=True)
        else:
            _drive_bar(music, rng, t0, k, kick=True, clap=True, stabs=True, arp=k >= 8,
                       hook=8 <= k <= 11 or k >= 24)
    for t in (5.0, 13.0, 21.0, 37.0, 45.0):  # risers into each drop
        music.add(s.riser(rng, 2.0), t, 0.55)
    for t in (7.0, 15.0, 23.0, 39.0, 47.0, 51.0):
        music.add(s.crash(rng), t, 0.32)
    # End card: a full bar on D, then the chord rings out.
    _drive_bar(music, rng, 51.0, 0, kick=True, clap=True, stabs=True, arp=True, hook=True)
    music.add(s.kick(), 53.0, 0.7)
    music.add(s.stab(DRIVE_CHORDS[0][1], 1.2), 53.0, 0.35)
    music.add(s.pad(DRIVE_CHORDS[0][1] + ["D5"], 4.0, attack=0.05, release=2.5), 53.0, 0.7)
    music.add(s.bass("D2", 3.0), 53.0, 0.5)
    music.add(s.lead("D5", 2.6), 53.0, 0.15)

    # Scene 1: counters and the punch line.
    for a, b, n, note in ((0.8, 2.2, 10, "A6"), (1.9, 2.9, 7, "B6"), (2.3, 3.0, 3, "D7")):
        for k in range(n):
            sfx.add(s.tick(note), a + (b - a) * (k / n) ** 0.8, 0.12)
    sfx.add(s.chime(["D6", "A6"]), 3.4, 0.18)
    # GTD method.
    sfx.add(s.whoosh(rng, 0.8), 6.5, 0.25)
    for i, note in enumerate(["D5", "E5", "F#5", "A5", "B5"]):
        sfx.add(s.marimba(note, 0.8), 8.9 + i * 0.3, 0.22, pan=-0.4 + 0.2 * i)
    sfx.add(s.chime(["A5", "E6"]), 12.2, 0.2)
    # Scattered clues gather into one question.
    sfx.add(s.whoosh(rng, 0.8), 14.4, 0.25)
    for i in range(9):
        sfx.add(s.pop(up=bool(i % 2)), 15.4 + i * 0.22, 0.12, pan=float(rng.uniform(-0.6, 0.6)))
    sfx.add(s.chime(["D6"]), 19.0, 0.15)
    sfx.add(s.whoosh(rng, 0.6), 21.3, 0.3)
    sfx.add(s.chime(["A5", "D6"], gap=0.07), 21.9, 0.22)
    # Clarify, organize, confirm.
    sfx.add(s.whoosh(rng, 0.7, rising=False), 22.8, 0.2)
    _clicks(sfx, [25.3, 29.2, 33.4, 34.0, 36.5, 49.0])
    _typing(sfx, rng, "周四评审前发给王老师，先讲清 5 页核心流程。", 25.6, 28.4)
    sfx.add(s.success(), 30.7, 0.25)
    sfx.add(s.whoosh(rng, 0.35), 34.05, 0.15)
    sfx.add(s.success(), 36.9, 0.35)
    # Principles, today, end card.
    sfx.add(s.whoosh(rng, 0.7), 39.4, 0.22)
    for i, note in enumerate(["D4", "F#4", "A4"]):
        sfx.add(s.marimba(note, 0.8), 41.3 + i * 0.45, 0.2)
    sfx.add(s.whoosh(rng, 0.7, rising=False), 46.2, 0.2)
    sfx.add(s.chime(["D6", "F#6"], gap=0.06), 49.15, 0.25)
    sfx.add(s.chime(["D6", "A6", "D7"], gap=0.12), 51.8, 0.2)
    return music, sfx, dur


# --------------------------------------------------------- film 2: forgetful
HOOK1 = ["C5", None, "A4", "C5", "D5", None, "F5", "D5"]
HOOK2 = ["C5", None, "A4", "G4", "F4", None, None, None]
PROG_A: list[Chord] = [("F2", ["A3", "C4", "F4"]), ("A2", ["A3", "C4", "E4"]),
                       ("Bb2", ["Bb3", "D4", "F4"]), ("C3", ["G3", "C4", "E4"])]
PROG_B: list[Chord] = [("Bb2", ["Bb3", "D4", "F4"]), ("C3", ["G3", "C4", "E4"]),
                       ("A2", ["A3", "C4", "E4"]), ("D3", ["A3", "D4", "F4"])]


def _vlog_bar(music: s.Bus, rng: np.random.Generator, t0: float, chord: Chord, *, kick: bool, clap: bool,
              hats: bool, hook: list[str | None] | None, bells: bool = False, plucks: bool = True) -> None:
    beat = 0.6
    root, notes = chord
    music.add(s.pad(notes, 3.2), t0, 0.3)
    music.add(s.bass(root, 0.9), t0, 0.45)
    music.add(s.bass(root, 0.6), t0 + 2 * beat, 0.35)
    if plucks:
        for k in (1, 3, 5, 7):
            for j, n in enumerate(notes):
                music.add(s.pluck(_up(n), 0.5), t0 + k * beat / 2 + j * 0.008, 0.07, pan=0.3)
    if kick:
        music.add(s.kick(), t0, 0.5)
        music.add(s.kick(), t0 + 2 * beat, 0.4)
    if clap:
        music.add(s.clap(rng), t0 + beat, 0.28, pan=-0.1)
        music.add(s.clap(rng), t0 + 3 * beat, 0.28, pan=-0.1)
    if hats:
        for k in range(8):
            music.add(s.hat(rng), t0 + k * beat / 2, 0.07 if k % 2 else 0.04, pan=0.3)
    for k, h in enumerate(hook or []):
        if h:
            music.add(s.marimba(h, 0.7), t0 + k * beat / 2, 0.24, pan=-0.2)
            if bells:
                music.add(s.bell(_up(h), 1.0), t0 + k * beat / 2, 0.08, pan=0.2)


def score_forgetful(rng: np.random.Generator) -> tuple[s.Bus, s.Bus, float]:
    """Playful 100 BPM marimba groove in F for the testimonial film (58 s)."""
    dur, bar = 58.0, 2.4
    music, sfx = s.Bus(dur), s.Bus(dur)
    for b in range(8):  # 0–19.2 s, cut at 18.0 by the tape stop
        _vlog_bar(music, rng, b * bar, PROG_A[b % 4], kick=b >= 1, clap=b >= 2, hats=b >= 4,
                  hook=(HOOK1 if b % 4 == 0 else HOOK2) if b % 2 == 0 else None, plucks=b >= 1)
    _silence(music, 17.98, 21.55)
    for i, b in enumerate(range(9, 19)):  # 21.6–45.6 s
        t0 = 21.6 + i * bar
        if b <= 15:
            _vlog_bar(music, rng, t0, PROG_B[i % 4], kick=True, clap=b >= 12, hats=b >= 12,
                      hook=HOOK1 if b in (13, 15) else None)
        else:
            _vlog_bar(music, rng, t0, PROG_A[(b - 16) % 4], kick=True, clap=True, hats=True,
                      hook=HOOK1 if b != 18 else HOOK2, bells=True)
    _silence(music, 46.2, 49.0, ramp=0.6)
    for i, n in enumerate(["F3", "A3", "C4", "G4"]):
        music.add(s.epiano(n, 3.4), 49.0 + i * 0.05, 0.3)
    music.add(s.bell("C6", 2.0), 50.5, 0.12)
    _vlog_bar(music, rng, 52.2, PROG_A[0], kick=False, clap=False, hats=False, hook=HOOK1, bells=True)
    _vlog_bar(music, rng, 54.6, ("Bb2", ["Bb3", "D4", "F4", "A4"]), kick=False, clap=False, hats=False,
              hook=HOOK2, bells=True, plucks=False)

    # Title.
    sfx.add(s.whoosh(rng, 0.5), 0.3, 0.15)
    sfx.add(s.marker(rng, 0.75), 1.9, 0.5)
    sfx.add(s.scribble(rng, 0.55), 2.9, 0.5)
    # The forgotten promise.
    sfx.add(s.whoosh(rng, 0.6, rising=False), 5.3, 0.2)
    sfx.add(s.pop(up=False), 6.3, 0.35, pan=-0.2)
    sfx.add(s.pop(up=True), 7.4, 0.35, pan=0.2)
    sfx.add(s.pop(up=False), 9.2, 0.35, pan=-0.2)
    sfx.add(s.scribble(rng, 0.5), 9.9, 0.5, pan=0.2)
    sfx.add(s.marimba("E4", 0.6), 10.9, 0.3)
    sfx.add(s.marimba("C4", 0.9), 11.15, 0.3)
    for k in range(6):
        sfx.add(s.key(rng), 10.7 + k * 0.18, 0.15, pan=0.2)
    sfx.add(s.pop(up=True), 11.85, 0.4, pan=0.2)
    # Abandoned tools, then the record stops.
    for i in range(3):
        sfx.add(s.whoosh(rng, 0.35), 14.75 + i * 0.35, 0.12)
        sfx.add(s.stamp(rng), 16.25 + i * 0.5, 0.55, pan=-0.3 + 0.3 * i)
    sfx.add(s.tape_stop(), 17.9, 0.45)
    # Computer History.
    sfx.add(s.whoosh(rng, 0.8), 21.0, 0.25)
    for i in range(3):
        sfx.add(s.tick("E6"), 22.3 + i * 0.4, 0.2)
    sfx.add(s.chime(["C6", "G6"]), 25.4, 0.22)
    sfx.add(s.pop(up=True), 26.2, 0.2)
    # One question, then confirm.
    sfx.add(s.chime(["A5", "E6", "A6"], gap=0.08), 29.0, 0.3)
    sfx.add(s.whoosh(rng, 0.6, rising=False), 30.55, 0.2)
    _clicks(sfx, [30.4, 31.6, 34.2, 36.2, 37.5])
    _typing(sfx, rng, "5 页核心流程截图就够，周三晚上发。", 31.8, 33.8)
    sfx.add(s.success(), 35.3, 0.22)
    sfx.add(s.whoosh(rng, 0.3), 36.3, 0.15)
    sfx.add(s.success(), 38.0, 0.35)
    # Next morning.
    sfx.add(s.whoosh(rng, 0.6), 39.0, 0.2)
    sfx.add(s.chime(["G5", "C6", "E6"], gap=0.12), 40.0, 0.32)
    sfx.add(s.whoosh(rng, 0.6, rising=False), 41.4, 0.2)
    sfx.add(s.whoosh(rng, 0.4), 42.2, 0.2)
    sfx.add(s.pop(up=True), 42.3, 0.35, pan=0.2)
    sfx.add(s.pop(up=True), 42.8, 0.35, pan=0.2)
    sfx.add(s.pop(up=False), 44.0, 0.35, pan=-0.2)
    sfx.add(s.success(), 44.15, 0.25)
    # Twist and end card.
    sfx.add(s.whoosh(rng, 1.0, rising=False), 45.8, 0.25)
    sfx.add(s.boom(rng), 47.8, 0.7)
    sfx.add(s.chime(["F6", "C7"], gap=0.1), 52.3, 0.18)
    return music, sfx, dur


# The energetic film keeps the bed louder and ducks it less under the narration.
MIX: dict[str, dict[str, float]] = {"index": {"bed_rms": 0.09, "duck": 0.58}}
FILMS: dict[str, Callable[[np.random.Generator], tuple[s.Bus, s.Bus, float]]] = {
    "index": score_index, "forgetful": score_forgetful}


def voice_bus(voice_dir: Path, duration: float) -> s.Bus:
    """Place narration clips listed in ``voice_dir/manifest.json`` (from narration.py)."""
    bus = s.Bus(duration)
    for item in json.loads((voice_dir / "manifest.json").read_text(encoding="utf-8")):
        clip = s.read_wav(voice_dir / item["file"])
        bus.add(clip / max(np.abs(clip).max(), 1e-9) * 0.85, item["start"])
    return bus


def render_audio(film: str, wav: Path, voice_dir: Path | None = None) -> None:
    """Synthesize the soundtrack for ``film`` (optionally with narration) into ``wav``."""
    music, sfx, dur = FILMS[film](np.random.default_rng(20261105))
    voice = voice_bus(voice_dir, dur) if voice_dir else None
    s.write_wav(wav, s.master(music, sfx, music_gain=0.85, fade_in=0.05, fade_out=1.6, voice=voice,
                              **MIX.get(film, {})))


LOUDNESS = "I=-14:TP=-1.5:LRA=11"


def mux(video: Path, wav: Path, out: Path) -> None:
    """Attach ``wav`` to ``video`` as AAC, normalised to -14 LUFS with two-pass loudnorm.

    Raises:
        RuntimeError: When ffmpeg is not available or the measurement cannot be parsed.
    """
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found on PATH")
    probe = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(wav), "-af",
                            f"loudnorm={LOUDNESS}:print_format=json", "-f", "null", "-"],
                           check=True, capture_output=True, text=True).stderr
    try:
        m = json.loads(probe[probe.rindex("{"):probe.rindex("}") + 1])
    except ValueError as e:
        raise RuntimeError("could not parse loudnorm measurement") from e
    second = (f"loudnorm={LOUDNESS}:linear=true:measured_I={m['input_i']}:measured_TP={m['input_tp']}"
              f":measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}")
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-i", str(wav),
                    "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-af", second, "-ar", "44100",
                    "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(out)], check=True)
    logger.info("wrote %s (input %s LUFS)", out, m["input_i"])


def main() -> None:
    """Parse CLI arguments, synthesize the soundtrack and optionally mux it."""
    parser = argparse.ArgumentParser(description="Synthesize promo soundtracks.")
    parser.add_argument("--film", choices=sorted(FILMS), required=True)
    parser.add_argument("--wav", type=Path, default=None, help="Keep the WAV at this path")
    parser.add_argument("--video", type=Path, default=None, help="Silent MP4 to attach audio to")
    parser.add_argument("--out", type=Path, default=None, help="Output MP4 (requires --video)")
    parser.add_argument("--voice-dir", type=Path, default=None, help="Narration folder from narration.py")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.video and not args.out:
        parser.error("--video requires --out")
    with tempfile.TemporaryDirectory(prefix="gtd-audio-") as tmp:
        wav = args.wav or Path(tmp) / f"{args.film}.wav"
        render_audio(args.film, wav, args.voice_dir)
        if args.video:
            mux(args.video, wav, args.out)


if __name__ == "__main__":
    main()
