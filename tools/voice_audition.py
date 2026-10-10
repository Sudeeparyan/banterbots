"""Audition GPT-Live performance: does it shout, groan and laugh, and how fast does a warm line answer?

    .venv\\Scripts\\python.exe tools\\voice_audition.py [--mode persistent|warm|fresh] [--strict]

Speaks a fixed set of lines (routine, own-team touchdown, rival touchdown, needling reply) through
the same VoiceBooth the agents use, then writes one WAV per line and report.json under
.runtime/listening/audition-<mode>[-strict]/: cue-to-first-audio latency, audible duration,
loudness (RMS and peak dBFS), the observed transcript and whether every script fact was spoken.
Needs Azure credentials in .env; costs a few cents of GPT-Live time.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import os
import struct
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.commentary import fact_issue, load_persona  # noqa: E402
from backend.contracts import AgentRequest, CommentaryTurn, GameSnapshot  # noqa: E402
from backend.delivery import delivery_for  # noqa: E402
from backend.providers import get_replay_events  # noqa: E402
from backend.security import safe_error  # noqa: E402
from backend.speech import RATE, VoiceBooth  # noqa: E402

LINES = [
    # (label, event id, speaker, mode, text)
    ("routine", "183", "a", "exchange", "Henry, one yard. Second and nine."),
    ("own_touchdown", "391", "b", "exchange", "Touchdown Ravens! Henry from five! Yes! Max, take the headset off."),
    ("rival_touchdown", "391", "a", "exchange", "Oh no. Henry walks in from five. Baltimore strikes first, and I hate it."),
    ("needle_reply", "391", "b", "reply", "Ha! The headset strikes again, Max. Noted, for the record."),
    ("home_touchdown", "575", "a", "exchange", "Worthy! Twenty-one yards! Touchdown Chiefs! Somebody check on Riley!"),
]


def levels(pcm: bytes) -> tuple[float, float]:
    values = struct.unpack(f"<{len(pcm) // 2}h", pcm) if pcm else (0,)
    rms = math.sqrt(sum(value * value for value in values) / len(values)) or 1
    peak = max(abs(value) for value in values) or 1
    return round(20 * math.log10(rms / 32768), 1), round(20 * math.log10(peak / 32768), 1)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="persistent", choices=["persistent", "warm", "fresh"])
    parser.add_argument("--strict", action="store_true", help="Use the exact-read instruction instead of performance")
    args = parser.parse_args()
    if args.strict:
        os.environ["VOICE_PERFORMANCE"] = "strict"
    output = Path(__file__).resolve().parents[1] / ".runtime" / "listening" / f"audition-{args.mode}{'-strict' if args.strict else ''}"
    output.mkdir(parents=True, exist_ok=True)
    events = {event.id: event for game in ("2024_01_BAL_KC",) for event in get_replay_events(game)}
    booths = {agent: VoiceBooth(load_persona(agent).name, agent, load_persona(agent).voice, mode=args.mode)
              for agent in ("a", "b")}
    reports = []
    try:
        for booth in booths.values():
            booth.prewarm()
        for label, event_id, agent, mode, text in LINES:
            event = events[event_id]
            persona = load_persona(agent)
            team = event.home_team if persona.allegiance == "home" else event.away_team
            request = AgentRequest(session_id="audition", epoch=1, exchange_id=label,
                                   snapshot=GameSnapshot(event=event).seal(), provider="azure")
            peer = None
            if mode == "reply":
                peer = CommentaryTurn(exchange_id=label, agent_id="a", persona="Max Carter", team_id=event.home_team,
                                      event_id=event.id, snapshot_hash=request.snapshot.hash,
                                      text="Oh no. Henry walks in from five.",
                                      spoken_text="Oh no. Henry walks in from five.")
                request = request.model_copy(update={"mode": "reply", "hop_budget": 0, "peer_utterance": peer})
            turn = CommentaryTurn(exchange_id=label, agent_id=agent, persona=persona.name, team_id=team,
                                  event_id=event.id, snapshot_hash=request.snapshot.hash, text=text, voice=persona.voice,
                                  mode="azure", kind="reply" if peer else "call",
                                  delivery=delivery_for(request, team, agent, text))
            audio = bytearray()
            marks: dict[str, float] = {}

            async def emit(value, audio=audio, marks=marks):
                if value["type"] == "audio":
                    marks.setdefault("first_audio", time.perf_counter())
                    audio.extend(base64.b64decode(value["data"]["pcm_base64"]))
                elif value["type"] == "segment.sealed":
                    marks["sealed"] = time.perf_counter()
                    marks.update({key: value["data"].get(key) for key in (
                        "boundary", "adlib_cut", "adlib_inserted", "first_speech_sample", "first_caption_ms",
                        "trimmed_samples")})

            started = time.perf_counter()
            try:
                spoken = await asyncio.wait_for(booths[agent].speak(request, turn, emit), 60)
                rms, peak = levels(bytes(audio))
                with wave.open(str(output / f"{label}.wav"), "wb") as recording:
                    recording.setnchannels(1)
                    recording.setsampwidth(2)
                    recording.setframerate(RATE)
                    recording.writeframes(bytes(audio))
                report = {"line": label, "speaker": persona.name, "energy": turn.delivery.energy,
                          "contour": turn.delivery.contour, "script": text, "spoken": spoken.spoken_text,
                          "fact_issue": fact_issue(spoken.spoken_text or "", request),
                          "cue_to_first_audio_ms": round((marks.get("first_audio", started) - started) * 1000),
                          "cue_to_sealed_ms": round((marks.get("sealed", started) - started) * 1000),
                          "boundary": marks.get("boundary"), "audio_seconds": round(len(audio) / 2 / RATE, 2),
                          "adlib_cut": marks.get("adlib_cut"), "adlib_inserted": marks.get("adlib_inserted"),
                          # Caption timing against the first audible sample: how far transcripts trail the voice.
                          "first_speech_ms": round((marks.get("first_speech_sample") or 0) * 1000 / RATE),
                          "first_caption_ms": marks.get("first_caption_ms"),
                          "trimmed_ms": round((marks.get("trimmed_samples") or 0) * 1000 / RATE),
                          "rms_dbfs": rms, "peak_dbfs": peak, "file": f"{label}.wav"}
            except Exception as exc:
                report = {"line": label, "error": safe_error(exc)}
            reports.append(report)
            print(json.dumps(report, ensure_ascii=False), flush=True)
    finally:
        for booth in booths.values():
            await booth.close()
    (output / "report.json").write_text(json.dumps(reports, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
