"""Synthesize the broadcast stingers: designed sound, not recordings.

    .venv\\Scripts\\python.exe tools\\make_stingers.py

Writes intro, quarter, halftime, final and score WAVs (32 kHz mono PCM16) to
frontend/public/audio/stingers/. Everything is generated here from sine partials,
a seeded noise burst and envelopes, so the files are original and reproducible;
LICENSES.md lists them as designed sound. Run tools/measure_loudness.mjs --manifest
afterwards to refresh each file's gain in manifest.json.
"""
from __future__ import annotations

import math
import random
import struct
import wave
from pathlib import Path

RATE = 32000
OUTPUT = Path(__file__).resolve().parents[1] / "frontend" / "public" / "audio" / "stingers"


def note(name: str) -> float:
    """Equal-tempered frequency for names like A4 or Eb3."""
    steps = {"C": -9, "D": -7, "E": -5, "F": -4, "G": -2, "A": 0, "B": 2}
    base, accidental, octave = name[0], name[1:-1], int(name[-1])
    semitone = steps[base] + {"": 0, "#": 1, "b": -1}[accidental] + 12 * (octave - 4)
    return 440.0 * 2 ** (semitone / 12)


def blank(seconds: float) -> list[float]:
    return [0.0] * int(seconds * RATE)


def mix(track: list[float], sound: list[float], at: float, gain: float = 1.0) -> None:
    start = int(at * RATE)
    for i, value in enumerate(sound):
        if start + i < len(track):
            track[start + i] += value * gain


def brass(frequency: float, seconds: float, brightness: float = 1.0) -> list[float]:
    """A warm horn-like tone: harmonics that open on the attack and mellow as it holds."""
    out = []
    for i in range(int(seconds * RATE)):
        t = i / RATE
        attack = min(1.0, t / 0.04)
        release = min(1.0, (seconds - t) / 0.12)
        vibrato = 1 + 0.004 * math.sin(2 * math.pi * 5.2 * t) * min(1.0, t / 0.3)
        value = 0.0
        for k in range(1, 9):
            fade = math.exp(-t * (0.6 + 0.9 * k) / brightness)
            value += math.sin(2 * math.pi * frequency * k * vibrato * t) * fade / k
        out.append(value * attack * max(0.0, release) * 0.32)
    return out


def chord(names: list[str], seconds: float, brightness: float = 1.0) -> list[float]:
    voices = [brass(note(name), seconds, brightness) for name in names]
    return [sum(values) / len(voices) for values in zip(*voices)]


def timpani(seconds: float = 1.4, pitch: float = 82.0, seed: int = 7) -> list[float]:
    noise = random.Random(seed)
    out, phase = [], 0.0
    for i in range(int(seconds * RATE)):
        t = i / RATE
        frequency = pitch * (1 + 0.35 * math.exp(-t * 18))
        phase += 2 * math.pi * frequency / RATE
        body = math.sin(phase) * math.exp(-t * 3.2)
        skin = (noise.random() * 2 - 1) * math.exp(-t * 40) * 0.5
        out.append((body + skin) * 0.9)
    return out


def bell(frequency: float, seconds: float = 1.2) -> list[float]:
    out = []
    for i in range(int(seconds * RATE)):
        t = i / RATE
        value = (math.sin(2 * math.pi * frequency * t) * math.exp(-t * 2.4)
                 + 0.5 * math.sin(2 * math.pi * frequency * 2.76 * t) * math.exp(-t * 4.5)
                 + 0.25 * math.sin(2 * math.pi * frequency * 5.4 * t) * math.exp(-t * 8))
        out.append(value * min(1.0, t / 0.004) * 0.45)
    return out


def finish(track: list[float]) -> list[float]:
    """Peak at -3 dBFS with a short fade so the tail never clicks."""
    peak = max(abs(value) for value in track) or 1.0
    scale = 10 ** (-3 / 20) / peak
    fade = int(0.08 * RATE)
    for i in range(fade):
        track[-1 - i] *= i / fade
    return [value * scale for value in track]


def intro() -> list[float]:
    track = blank(2.7)
    mix(track, chord(["C4", "E4", "G4"], 0.32), 0.0)
    mix(track, chord(["F4", "A4", "C5"], 0.32), 0.3)
    mix(track, chord(["G4", "B4", "D5"], 0.32), 0.6)
    mix(track, chord(["C4", "G4", "C5", "E5"], 1.9, brightness=1.6), 0.9, 1.2)
    mix(track, timpani(), 0.9)
    return finish(track)


def quarter() -> list[float]:
    track = blank(1.5)
    mix(track, bell(note("G5")), 0.0)
    mix(track, bell(note("C5")), 0.28)
    return finish(track)


def halftime() -> list[float]:
    track = blank(2.5)
    mix(track, chord(["D4", "F#4", "A4"], 0.28), 0.0)
    mix(track, chord(["D4", "F#4", "A4"], 0.28), 0.22)
    mix(track, chord(["G4", "B4", "D5"], 1.9, brightness=1.4), 0.5, 1.1)
    mix(track, timpani(pitch=74.0, seed=11), 0.5)
    return finish(track)


def final() -> list[float]:
    track = blank(3.0)
    mix(track, chord(["A3", "C4", "E4"], 0.45), 0.0)
    mix(track, chord(["F3", "A3", "C4", "F4"], 0.45), 0.42)
    mix(track, chord(["C4", "E4", "G4", "C5"], 2.1, brightness=1.8), 0.84, 1.2)
    mix(track, timpani(pitch=65.0, seed=3), 0.84)
    mix(track, timpani(seconds=1.6, pitch=65.0, seed=5), 1.1, 0.6)
    return finish(track)


def score() -> list[float]:
    track = blank(1.3)
    for index, name in enumerate(["C5", "E5", "G5", "C6"]):
        mix(track, bell(note(name), 0.9), index * 0.07, 0.8)
    return finish(track)


def write(name: str, samples: list[float]) -> Path:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    path = OUTPUT / f"{name}.wav"
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(RATE)
        output.writeframes(b"".join(struct.pack("<h", max(-32767, min(32767, round(value * 32767))))
                                    for value in samples))
    return path


if __name__ == "__main__":
    for name, build in [("intro", intro), ("quarter", quarter), ("halftime", halftime),
                        ("final", final), ("score", score)]:
        print(write(name, build()))
