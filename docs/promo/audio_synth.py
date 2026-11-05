"""Deterministic numpy synthesizer for the promo soundtracks.

Everything here is generated from math and a fixed random seed, so the music
and sound effects are original, license-free, and identical on every render.
"""
from __future__ import annotations

import logging
import wave
from collections.abc import Sequence
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

SR = 44_100
_NOTE = {"C": 0, "C#": 1, "Db": 1, "D": 2, "D#": 3, "Eb": 3, "E": 4, "F": 5, "F#": 6,
         "Gb": 6, "G": 7, "G#": 8, "Ab": 8, "A": 9, "A#": 10, "Bb": 10, "B": 11}
TAU = 2 * np.pi


def hz(note: str) -> float:
    """Convert a note name such as ``"F#4"`` to its frequency in Hz."""
    midi = 12 * (int(note[-1]) + 1) + _NOTE[note[:-1]]
    return 440.0 * 2 ** ((midi - 69) / 12)


def _t(dur: float) -> np.ndarray:
    return np.arange(int(dur * SR)) / SR


def _attack(t: np.ndarray, seconds: float) -> np.ndarray:
    return np.minimum(1.0, t / max(seconds, 1e-4))


def lowpass(x: np.ndarray, width: int) -> np.ndarray:
    """Box-filter low-pass; larger ``width`` means a darker sound."""
    return x if width <= 1 else np.convolve(x, np.ones(width) / width, mode="same")


def highpass(x: np.ndarray, width: int) -> np.ndarray:
    """Complement of :func:`lowpass`."""
    return x - lowpass(x, width)


# ---------------------------------------------------------------- instruments
def epiano(note: str, dur: float = 1.8) -> np.ndarray:
    """Soft FM electric piano."""
    f, t = hz(note), _t(dur)
    mod = 1.6 * np.exp(-t * 3.0) * np.sin(TAU * f * t)
    tone = np.sin(TAU * f * t + mod) + 0.2 * np.sin(TAU * 2 * f * t) * np.exp(-t * 5)
    return 0.45 * tone * _attack(t, 0.006) * np.exp(-t * 1.9)


def pad(notes: Sequence[str], dur: float, attack: float = 0.7, release: float = 1.0) -> np.ndarray:
    """Warm sustained chord with slow attack and release."""
    t = _t(dur)
    tone = np.zeros_like(t)
    for n in notes:
        f = hz(n)
        tone += np.sin(TAU * f * t) + 0.5 * np.sin(TAU * f * 1.0035 * t) + 0.15 * np.sin(TAU * 2 * f * t)
    env = _attack(t, attack) * np.clip((dur - t) / release, 0, 1)
    return 0.25 * tone / max(len(notes), 1) * env * (1 + 0.05 * np.sin(TAU * 0.3 * t))


def bass(note: str, dur: float = 1.2) -> np.ndarray:
    """Round sine bass."""
    f, t = hz(note), _t(dur)
    return 0.7 * (np.sin(TAU * f * t) + 0.25 * np.sin(TAU * 2 * f * t)) * _attack(t, 0.01) * np.exp(-t * 2.2)


def marimba(note: str, dur: float = 0.9) -> np.ndarray:
    """Wooden mallet tone."""
    f, t = hz(note), _t(dur)
    tone = np.sin(TAU * f * t) * np.exp(-t * 6) + 0.3 * np.sin(TAU * 3.93 * f * t) * np.exp(-t * 24)
    return 0.6 * tone * _attack(t, 0.002)


def pluck(note: str, dur: float = 0.8) -> np.ndarray:
    """Bright additive pluck, roughly ukulele-like."""
    f, t = hz(note), _t(dur)
    tone = sum(np.sin(TAU * k * f * t) * np.exp(-t * (3 + 2.5 * k)) / k for k in range(1, 7))
    return 0.5 * tone * _attack(t, 0.003)


def bell(note: str, dur: float = 1.6) -> np.ndarray:
    """Glockenspiel-style bell with inharmonic partials."""
    f, t = hz(note), _t(dur)
    parts = [(1.0, 1.0, 2.5), (2.76, 0.4, 5.0), (5.40, 0.2, 9.0), (8.93, 0.1, 14.0)]
    tone = sum(a * np.sin(TAU * f * r * t) * np.exp(-t * d) for r, a, d in parts)
    return 0.35 * tone * _attack(t, 0.002)


def kick() -> np.ndarray:
    """Soft kick drum: falling sine sweep."""
    t = _t(0.4)
    freq = 45 + 85 * np.exp(-t * 28)
    return 0.9 * np.sin(TAU * np.cumsum(freq) / SR) * np.exp(-t * 8)


def hat(rng: np.random.Generator) -> np.ndarray:
    """Closed hi-hat."""
    t = _t(0.07)
    return 0.4 * highpass(rng.standard_normal(t.size), 3) * np.exp(-t * 70)


def clap(rng: np.random.Generator) -> np.ndarray:
    """Three-burst hand clap."""
    t = _t(0.25)
    noise = lowpass(highpass(rng.standard_normal(t.size), 6), 3)
    env = sum(np.exp(-np.clip(t - o, 0, None) * 45) * (t >= o) for o in (0.0, 0.011, 0.022))
    return 0.5 * noise * env


def saw(note: str, dur: float, bright: float = 1.0, detune: float = 0.006) -> np.ndarray:
    """Band-limited detuned saw whose upper harmonics fade like a closing filter."""
    f, t = hz(note), _t(dur)
    tone = np.zeros_like(t)
    for d in (-detune, 0.0, detune):
        for k in range(1, 13):
            tone += np.sin(TAU * k * f * (1 + d) * t) / k * np.exp(-t * (1 + k * 1.6) / bright)
    return 0.22 * tone * _attack(t, 0.004)


def stab(notes: Sequence[str], dur: float = 0.35) -> np.ndarray:
    """Short bright chord stab."""
    t = _t(dur)
    return sum(saw(n, dur, bright=0.5) for n in notes) / len(notes) * np.exp(-t * 6)


def lead(note: str, dur: float = 0.4) -> np.ndarray:
    """Square-ish lead with light vibrato."""
    f, t = hz(note), _t(dur)
    phase = TAU * f * t + 0.12 * np.sin(TAU * 5.5 * t) * np.minimum(1, t / 0.15)
    tone = sum(np.sin((2 * k + 1) * phase) / (2 * k + 1) for k in range(6))
    return 0.4 * tone * _attack(t, 0.01) * np.clip((dur - t) / 0.06, 0, 1) * np.exp(-t * 1.2)


def open_hat(rng: np.random.Generator) -> np.ndarray:
    """Open hi-hat."""
    t = _t(0.28)
    return 0.35 * highpass(rng.standard_normal(t.size), 2) * np.exp(-t * 14)


def crash(rng: np.random.Generator) -> np.ndarray:
    """Crash cymbal for downbeats after a build."""
    t = _t(2.5)
    return 0.5 * highpass(rng.standard_normal(t.size), 3) * np.exp(-t * 1.8) * _attack(t, 0.003)


def riser(rng: np.random.Generator, dur: float = 2.0) -> np.ndarray:
    """Noise sweep plus rising tone that builds into a drop."""
    t = _t(dur)
    w = t / dur
    noise = rng.standard_normal(t.size)
    swept = (1 - w) * lowpass(noise, 30) + w * highpass(noise, 3)
    tone = np.sin(TAU * np.cumsum(220 * 2 ** (2.5 * w)) / SR)
    return (0.5 * swept + 0.15 * tone) * w ** 2


# -------------------------------------------------------------- sound effects
def click() -> np.ndarray:
    """Mouse click."""
    t = _t(0.05)
    return 0.6 * (np.sin(TAU * 2200 * t) * np.exp(-t * 400) + 0.5 * np.sin(TAU * 900 * t) * np.exp(-t * 180))


def key(rng: np.random.Generator) -> np.ndarray:
    """Single soft keyboard tap with random pitch."""
    t = _t(0.04)
    f = rng.uniform(1500, 2600)
    tone = np.sin(TAU * f * t) * np.exp(-t * 320)
    noise = highpass(rng.standard_normal(t.size), 2) * np.exp(-t * 500)
    return 0.4 * (tone + 0.3 * noise)


def pop(up: bool = True) -> np.ndarray:
    """Message bubble pop; ``up`` for sent, down for received."""
    t = _t(0.12)
    f0, f1 = (520, 980) if up else (900, 560)
    freq = f0 + (f1 - f0) * np.minimum(1, t / 0.07)
    return 0.5 * np.sin(TAU * np.cumsum(freq) / SR) * _attack(t, 0.003) * np.exp(-t * 30)


def whoosh(rng: np.random.Generator, dur: float = 0.6, rising: bool = True) -> np.ndarray:
    """Air whoosh for scene changes."""
    t = _t(dur)
    noise = rng.standard_normal(t.size)
    dark, bright = lowpass(noise, 40), lowpass(noise, 5)
    w = t / dur if rising else 1 - t / dur
    return 0.9 * ((1 - w) * dark + w * bright) * np.sin(np.pi * t / dur) ** 2


def chime(notes: Sequence[str], gap: float = 0.09) -> np.ndarray:
    """Notification chime: bell notes in quick succession."""
    out = np.zeros(int((gap * len(notes) + 1.6) * SR))
    for i, n in enumerate(notes):
        b, s = bell(n), int(i * gap * SR)
        out[s:s + b.size] += b
    return out


def success() -> np.ndarray:
    """Short rising marimba arpeggio for a completed action."""
    out = chime(["C6", "E6", "G6"], gap=0.06) * 0.5
    body = marimba("C5", 0.6) * 0.3
    out[:body.size] += body
    return out


def stamp(rng: np.random.Generator) -> np.ndarray:
    """Rubber stamp thud."""
    t = _t(0.35)
    thud = np.sin(TAU * np.cumsum(70 + 60 * np.exp(-t * 40)) / SR) * np.exp(-t * 14)
    crunch = lowpass(rng.standard_normal(t.size), 4) * np.exp(-t * 45)
    return 0.9 * thud + 0.5 * crunch


def scribble(rng: np.random.Generator, dur: float = 0.5) -> np.ndarray:
    """Pen scratching on paper."""
    t = _t(dur)
    noise = lowpass(highpass(rng.standard_normal(t.size), 4), 2)
    strokes = 0.5 + 0.5 * np.sin(TAU * 11 * t + 2 * np.sin(TAU * 3 * t))
    return 0.35 * noise * strokes * np.sin(np.pi * t / dur)


def marker(rng: np.random.Generator, dur: float = 0.7) -> np.ndarray:
    """Highlighter swipe: smooth band-limited noise."""
    t = _t(dur)
    return 0.4 * lowpass(highpass(rng.standard_normal(t.size), 10), 3) * np.sin(np.pi * t / dur) ** 1.5


def tape_stop() -> np.ndarray:
    """Pitch-falling 'record stops' effect."""
    t = _t(0.55)
    freq = 330 * np.exp(-t * 4.5)
    tone = np.sin(TAU * np.cumsum(freq) / SR) + 0.4 * np.sin(TAU * np.cumsum(freq * 1.5) / SR)
    return 0.45 * tone * np.clip(1 - t / 0.55, 0, 1)


def boom(rng: np.random.Generator) -> np.ndarray:
    """Low cinematic impact."""
    t = _t(2.2)
    body = np.sin(TAU * np.cumsum(38 + 40 * np.exp(-t * 6)) / SR) * np.exp(-t * 1.8)
    air = lowpass(rng.standard_normal(t.size), 30) * np.exp(-t * 4)
    return 0.9 * body + 0.4 * air


def tick(note: str = "A6") -> np.ndarray:
    """Tiny wooden tick."""
    return marimba(note, 0.12) * np.exp(-_t(0.12) * 25)


# ------------------------------------------------------------------- mixing
class Bus:
    """Stereo bus that collects timed sounds."""

    def __init__(self, duration: float) -> None:
        self.data = np.zeros((2, int(duration * SR)))

    def add(self, sig: np.ndarray, at: float, gain: float = 1.0, pan: float = 0.0) -> None:
        """Mix ``sig`` in at ``at`` seconds with equal-power ``pan`` in [-1, 1]."""
        start = int(round(at * SR))
        if start >= self.data.shape[1] or start < 0:
            return
        seg = sig[:self.data.shape[1] - start] * gain
        angle = (pan + 1) * np.pi / 4
        self.data[0, start:start + seg.size] += seg * np.cos(angle)
        self.data[1, start:start + seg.size] += seg * np.sin(angle)


def reverb(x: np.ndarray, wet: float, decay: float = 0.45, seed: int = 7) -> np.ndarray:
    """Convolution reverb with a decorrelated stereo noise impulse response."""
    rng = np.random.default_rng(seed)
    t = _t(decay * 4)
    ir = rng.standard_normal((2, t.size)) * np.exp(-t / decay)
    ir = np.stack([lowpass(ch, 6) for ch in ir])
    ir /= np.sqrt((ir ** 2).sum(axis=1, keepdims=True))
    size = 1 << int(np.ceil(np.log2(x.shape[1] + t.size)))
    y = np.fft.irfft(np.fft.rfft(x, size) * np.fft.rfft(ir, size), size)[:, :x.shape[1]]
    return x + wet * y


def _smooth(x: np.ndarray, width: int) -> np.ndarray:
    """Centered moving average via cumulative sums (fast for wide windows)."""
    c = np.concatenate([[0.0], np.cumsum(x)])
    half = width // 2
    hi = np.minimum(np.arange(x.size) + half + 1, x.size)
    lo = np.maximum(np.arange(x.size) - half, 0)
    return (c[hi] - c[lo]) / (hi - lo)


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x ** 2)) + 1e-12)


def master(music: Bus, sfx: Bus, music_gain: float, fade_in: float, fade_out: float,
           voice: Bus | None = None, duck: float = 0.6, bed_rms: float | None = None) -> np.ndarray:
    """Combine buses, duck the bed under ``voice``, apply fades, normalise and soft-limit."""
    music_mix = reverb(music.data, wet=0.32) * music_gain
    sfx_mix = reverb(sfx.data, wet=0.12, seed=11)
    target = bed_rms if bed_rms is not None else (0.07 if voice is not None else 0.12)
    scale = target / _rms(music_mix + sfx_mix)
    music_mix *= scale
    sfx_mix *= scale
    mix = music_mix + sfx_mix
    if voice is not None:
        level = np.minimum(np.abs(voice.data).max(axis=0) / 0.05, 1.0)
        env = np.clip(_smooth(level, int(0.35 * SR)) * 1.6, 0, 1)
        mix = music_mix * (1 - duck * env) + sfx_mix * (1 - 0.25 * env) + reverb(voice.data, wet=0.05, seed=13)
    n = mix.shape[1]
    t = np.arange(n) / SR
    mix *= np.minimum(1, t / max(fade_in, 1e-3)) * np.clip((n / SR - t) / fade_out, 0, 1)
    mix /= max(np.abs(mix).max(), 1e-9) / 0.92
    return np.tanh(mix * 1.15) / np.tanh(1.15)


def read_wav(path: Path) -> np.ndarray:
    """Read a 16-bit WAV as mono float in [-1, 1], resampled to :data:`SR`."""
    with wave.open(str(path), "rb") as w:
        sr, ch, raw = w.getframerate(), w.getnchannels(), w.readframes(w.getnframes())
    x = np.frombuffer(raw, dtype="<i2").reshape(-1, ch).mean(axis=1) / 32768
    if sr != SR:
        x = np.interp(np.linspace(0, x.size - 1, int(x.size * SR / sr)), np.arange(x.size), x)
    return x


def write_wav(path: Path, data: np.ndarray) -> None:
    """Write a stereo float array in [-1, 1] as 16-bit PCM."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(data, -1, 1) * 32767).astype("<i2").T.copy()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    logger.info("wrote %s (%.1fs)", path, data.shape[1] / SR)
