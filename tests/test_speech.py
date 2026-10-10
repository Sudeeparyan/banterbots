import asyncio
import json
import re
import base64
import struct
from types import SimpleNamespace

import pytest

from backend.contracts import AgentRequest, CommentaryTurn, GameSnapshot, PlayEvent
from backend.speech import (RATE, SILENCE_FRAME, TAIL_MS, SpeechFailure, SpeechObserver, VoiceBooth, cue_text,
                            pcm_rms, script_covered, speak, voice_session)


def payload_and_turn():
    event = PlayEvent(id="p1", game_id="g1", source="nflverse", sequence=1, quarter=1, clock="12:01",
                      home_team="KC", away_team="BAL", home_score=0, away_score=0,
                      description="L.Jackson pass complete for 12 yards.", yards_gained=12)
    payload = AgentRequest(session_id="session1", epoch=1, exchange_id="exchange1", snapshot=GameSnapshot(event=event).seal(),
                           provider="azure")
    turn = CommentaryTurn(exchange_id=payload.exchange_id, agent_id="a", persona="Max Carter", team_id="KC",
                          event_id=event.id, snapshot_hash=payload.snapshot.hash, text="Twelve yards on that pass.",
                          voice="meridian", mode="azure")
    return payload, turn


def test_pcm_boundary_requires_actual_silence_and_inactive_transcript():
    observer = SpeechObserver(0, 0)
    observer.audio(struct.pack("<h", 4000) * 480)
    observer.caption("Good gain.", 1)
    assert observer.boundary(3) is None  # A transport gap is not audio silence.
    observer.audio(b"\0" * RATE * 2)
    assert observer.boundary(1.5) is None  # Captions still active.
    assert observer.boundary(2.3) == "observed_silence"
    observer.audio(struct.pack("<h", 4000) * 480)
    assert observer.boundary(3) is None
    assert observer.boundary(10.01) is None  # speech was first observed at 3
    assert observer.boundary(13.01) == "duration_cap"


def test_duration_cap_measures_the_line_not_a_slow_voice_start():
    observer = SpeechObserver(0, 0)
    assert observer.boundary(9) is None  # a fresh session can take a while to answer
    observer.audio(struct.pack("<h", 4000) * 480)
    observer.caption("Hill, eighteen yards.", 9)
    assert observer.boundary(9) is None
    assert observer.boundary(18) is None  # the late line still gets its full time
    assert observer.boundary(19.01) == "duration_cap"
    silent = SpeechObserver(0, 0)
    assert silent.boundary(21.9) is None and silent.boundary(22.01) == "duration_cap"  # 10 s + start grace


def test_pcm_order_alignment_and_silence_input():
    observer = SpeechObserver(0, 0)
    index, first = observer.audio(b"\x01")
    assert first == b"" and index == 0
    index, second = observer.audio(b"\x02\x03\x04")
    assert second == b"\x01\x02\x03\x04" and index == 0
    assert observer.samples == 2 and observer.chunks == 1
    assert len(base64.b64decode(SILENCE_FRAME)) == RATE * 2 * 20 // 1000
    assert pcm_rms(base64.b64decode(SILENCE_FRAME)) == 0
    with pytest.raises(ValueError, match="complete"):
        pcm_rms(b"\0")


def test_live_config_contains_facts_current_call_and_distinct_voice():
    payload, turn = payload_and_turn()
    config = voice_session(payload, turn)
    assert config["model"] == "gpt-live-1"
    assert config["audio"] == {"format": {"type": "audio/pcm", "rate": 24000}, "output": {"voice": "meridian"}}
    assert config["delegation"] == {"type": "client"}
    assert "Twelve yards on that pass." in config["input"][0]["content"][0]["text"]
    assert payload.snapshot.hash in config["input"][0]["content"][0]["text"]


def test_live_direction_is_trusted_performance_separate_from_exact_script(monkeypatch):
    payload, turn = payload_and_turn()
    event = payload.snapshot.event.model_copy(update={"flags": ["touchdown", "scoring"],
                                                    "scoring_team": "KC", "home_score": 6})
    payload = payload.model_copy(update={"snapshot": GameSnapshot(event=event).seal()})
    turn = turn.model_copy(update={"text": "Touchdown Chiefs!", "emotion": "excited"})
    config = voice_session(payload, turn)
    instructions = config["instructions"]
    assert "Keep every player name, team, number, score, down and result exactly as written" in instructions
    assert "Backchannel policy:" in instructions and "Delegation policy:" in instructions
    assert '"validated_call": "Touchdown Chiefs!"' in config["input"][0]["content"][0]["text"]
    assert '"sound_cue": "crowd_roar"' in config["input"][0]["content"][0]["text"]
    cue = cue_text(payload, turn)
    assert '"Touchdown Chiefs!"' in cue and "Explode" in cue and "laugh" in cue
    assert "exactly once" not in cue
    monkeypatch.setenv("VOICE_PERFORMANCE", "strict")
    assert "Speak only this script exactly once" in cue_text(payload, turn)


def test_trailing_and_leading_silence_is_held_back_but_pauses_inside_speech_survive():
    observer = SpeechObserver(0, 0, script="Gone. Touchdown.")
    voiced = struct.pack("<h", 4000) * 480
    quiet = b"\0" * (RATE // 2 * 2)  # 500 ms
    assert observer.audio(quiet) == (0, b"")  # startup silence is not played
    index, ready = observer.audio(voiced)
    assert index == 0 and len(ready) == 60 * RATE // 1000 * 2 + len(voiced)  # 60 ms lead-in kept
    assert observer.audio(quiet) == (1, b"")
    index, ready = observer.audio(voiced)
    assert index == 1 and len(ready) == len(quiet) + len(voiced)  # a dramatic pause is kept
    observer.audio(b"\0" * RATE * 2)
    index, tail = observer.finish()
    assert index == 2 and len(tail) == TAIL_MS * RATE // 1000 * 2
    assert observer.samples * 2 == 60 * RATE // 1000 * 2 + len(voiced) * 2 + len(quiet) + len(tail)
    assert observer.trimmed_samples > RATE


def test_script_coverage_ends_lines_quickly_and_tolerates_reactions_and_digits():
    assert script_covered("Oh! Henry, five yards, touchdown Ravens! Wow.", "Henry, 5 yards, touchdown Ravens!")
    assert script_covered("Twenty-one yards for Worthy", "21 yards for Worthy")
    assert not script_covered("Henry, five yards", "Henry, 5 yards, touchdown Ravens!")
    observer = SpeechObserver(0, 0, script="Touchdown Ravens!")
    observer.audio(struct.pack("<h", 4000) * 480)
    observer.caption("Touchdown, Ravens!", 1)
    observer.audio(b"\0" * (RATE * 2 * 3 // 10))
    assert observer.boundary(1.3) == "script_complete"
    unfinished = SpeechObserver(0, 0, script="Touchdown Ravens! What a drive.")
    unfinished.audio(struct.pack("<h", 4000) * 480)
    unfinished.caption("Touchdown, Ravens!", 1)
    unfinished.audio(b"\0" * (RATE * 2 * 3 // 10))
    assert unfinished.boundary(1.3) is None  # a dramatic pause mid-line is not the end


class FakeConnection:
    def __init__(self, captions=True, finalize=True):
        self.queue = asyncio.Queue()
        self.calls = []
        self.captions = captions
        self.finalize = finalize
        self.session = SimpleNamespace(start=self.start, close=self.close,
                                       instructions=SimpleNamespace(append=self.instructions),
                                       input_audio=SimpleNamespace(append=self.input_audio))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.queue.get()

    async def start(self, **kwargs):
        self.calls.append(("start", kwargs))
        await self.queue.put(SimpleNamespace(type="session.started", session=SimpleNamespace(id="live-test")))

    async def instructions(self, **kwargs):
        self.calls.append(("instructions", kwargs))
        if self.captions:
            await self.queue.put(SimpleNamespace(type="session.output_transcript.delta", delta="Observed spoken call."))
        voiced = base64.b64encode(struct.pack("<h", 4000) * 480).decode()
        quiet = base64.b64encode(b"\0" * RATE * 2).decode()
        await self.queue.put(SimpleNamespace(type="session.output_audio.delta", delta=voiced))
        await self.queue.put(SimpleNamespace(type="session.output_audio.delta", delta=quiet))

    async def input_audio(self, **kwargs):
        self.calls.append(("input_audio", kwargs))

    async def close(self):
        self.calls.append(("close", {}))
        if self.finalize:
            await self.queue.put(SimpleNamespace(type="session.closed", usage=SimpleNamespace(model_dump=lambda: {"seconds": 1}), reason="close_requested"))


class FakeClient:
    def __init__(self, connection, **kwargs):
        self.live = SimpleNamespace(connect=lambda: connection)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


@pytest.mark.asyncio
async def test_voice_stream_finalizes_observed_text_and_ordered_segment():
    payload, turn = payload_and_turn()
    connection = FakeConnection()
    events = []

    async def emit(event):
        events.append(event)

    spoken = await speak(payload, turn, emit, client_factory=lambda **kwargs: FakeClient(connection), max_seconds=0.15)
    assert spoken.spoken_text == "Observed spoken call."
    assert spoken.text == "Twelve yards on that pass."
    chunks = [value["data"] for value in events if value["type"] == "audio"]
    assert [value["chunk_index"] for value in chunks] == [0, 1]
    assert all(value["segment_id"] == turn.id and value["rate"] == 24000 for value in chunks)
    seal = next(value["data"] for value in events if value["type"] == "segment.sealed")
    assert seal["total_samples"] == 480 + TAIL_MS * RATE // 1000 and seal["finalized"] is True
    assert seal["trimmed_samples"] == RATE - TAIL_MS * RATE // 1000
    assert seal["transcript"] == spoken.spoken_text
    assert connection.calls[0][0] == "start"
    assert any(call[0] == "input_audio" for call in connection.calls)


@pytest.mark.asyncio
async def test_voice_finalization_failure_is_explicit():
    payload, turn = payload_and_turn()
    connection = FakeConnection(finalize=False)

    async def emit(event):
        pass

    with pytest.raises(SpeechFailure, match="unconfirmed") as failure:
        await speak(payload, turn, emit, client_factory=lambda **kwargs: FakeClient(connection),
                    max_seconds=0.1, finalization_seconds=0.1)
    assert failure.value.observed_text == "Observed spoken call."
    assert failure.value.audio_chunks == 2


@pytest.mark.asyncio
async def test_audio_without_observed_transcript_is_not_passed_as_spoken_text():
    payload, turn = payload_and_turn()
    connection = FakeConnection(captions=False)

    async def emit(event):
        pass

    with pytest.raises(RuntimeError, match="observed"):
        await speak(payload, turn, emit, client_factory=lambda **kwargs: FakeClient(connection), max_seconds=0.1)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["start", "instructions", "input_audio", "close"])
async def test_stalled_voice_writes_are_bounded_and_close_the_session(operation):
    payload, turn = payload_and_turn()
    connection = FakeConnection()
    close_called = asyncio.Event()
    original_close = connection.close

    async def stalled(**kwargs):
        await asyncio.Event().wait()

    async def close():
        close_called.set()
        await original_close()

    connection.session.close = close
    if operation == "start":
        connection.session.start = stalled
        expected = "session start"
    elif operation == "instructions":
        connection.session.instructions.append = stalled
        expected = "speech instructions"
    elif operation == "input_audio":
        connection.session.input_audio.append = stalled
        expected = "silent input"
    else:
        attempts = 0

        async def stalled_close():
            nonlocal attempts
            attempts += 1
            close_called.set()
            if attempts == 1:
                await asyncio.Event().wait()
            else:
                await original_close()

        connection.session.close = stalled_close
        expected = "session close"

    async def emit(event):
        pass

    with pytest.raises(SpeechFailure, match=f"{expected} timed out"):
        await asyncio.wait_for(speak(payload, turn, emit, client_factory=lambda **kwargs: FakeClient(connection),
                                     max_seconds=0.1, io_timeout=0.02), 1)
    assert close_called.is_set()


@pytest.mark.asyncio
async def test_cancelling_voice_closes_upstream_and_never_seals_partial_audio():
    payload, turn = payload_and_turn()
    connection = FakeConnection()
    events = []
    heard = asyncio.Event()

    async def emit(event):
        events.append(event)
        if event["type"] == "audio":
            heard.set()

    task = asyncio.create_task(speak(payload, turn, emit, client_factory=lambda **kwargs: FakeClient(connection)))
    await asyncio.wait_for(heard.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    before = len(events)
    await asyncio.sleep(0.03)
    assert len(events) == before
    assert any(call[0] == "close" for call in connection.calls)
    assert not any(event["type"] == "segment.sealed" for event in events)


@pytest.mark.asyncio
async def test_bad_output_audio_preserves_only_observed_text_and_never_seals():
    payload, turn = payload_and_turn()
    connection = FakeConnection()

    async def bad_instructions(**kwargs):
        await connection.queue.put(SimpleNamespace(type="session.output_transcript.delta", delta="Observed words."))
        await connection.queue.put(SimpleNamespace(type="session.output_audio.delta", delta="%%%invalid%%%"))

    connection.session.instructions.append = bad_instructions
    events = []

    async def emit(event):
        events.append(event)

    with pytest.raises(SpeechFailure) as failure:
        await speak(payload, turn, emit, client_factory=lambda **kwargs: FakeClient(connection))
    assert failure.value.observed_text == "Observed words."
    assert failure.value.samples == 0
    assert not any(event["type"] == "segment.sealed" for event in events)


class LiveFake:
    """A multi-turn GPT-Live session: speaks each cued line, then streams silence."""

    def __init__(self, finish=True):
        self.queue = asyncio.Queue()
        self.calls = []
        self.finish = finish
        self.usage = 0.0
        self.session = SimpleNamespace(start=self.start, close=self.close,
                                       instructions=SimpleNamespace(append=self.instructions),
                                       thinking=SimpleNamespace(append=self.thinking),
                                       input_audio=SimpleNamespace(append=self.input_audio))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.queue.get()

    async def start(self, **kwargs):
        self.calls.append(("start", kwargs))
        await self.queue.put(SimpleNamespace(type="session.started", session=SimpleNamespace(id="live-booth")))

    async def thinking(self, **kwargs):
        self.calls.append(("thinking", kwargs))

    async def instructions(self, **kwargs):
        self.calls.append(("instructions", kwargs))
        match = re.search(r'go silent: (".*?")\. It replaces', kwargs["content"])
        if not match:
            return
        await self.queue.put(SimpleNamespace(type="session.output_transcript.delta", delta=json.loads(match.group(1))))
        await self.queue.put(SimpleNamespace(type="session.output_audio.delta",
                                             delta=base64.b64encode(struct.pack("<h", 4000) * 4800).decode()))
        if self.finish:
            self.usage += 2
            await self.queue.put(SimpleNamespace(type="session.output_audio.delta",
                                                 delta=base64.b64encode(b"\0" * (RATE * 2 * 4 // 10)).decode()))
            await self.queue.put(SimpleNamespace(type="session.usage.updated", usage=SimpleNamespace(seconds=self.usage)))

    async def input_audio(self, **kwargs):
        pass

    async def close(self):
        self.calls.append(("close", {}))
        await self.queue.put(SimpleNamespace(type="session.closed", usage=SimpleNamespace(model_dump=lambda: {"seconds": self.usage}),
                                             reason="close_requested"))


def booth_for(live, mode="persistent", idle_seconds=5.0):
    return VoiceBooth("Max Carter", "a", "meridian", mode=mode, idle_seconds=idle_seconds,
                      client_factory=lambda **kwargs: FakeClient(live))


@pytest.mark.asyncio
async def test_persistent_booth_reuses_one_session_reports_usage_and_closes_when_idle():
    payload, turn = payload_and_turn()
    live = LiveFake()
    booth = booth_for(live, idle_seconds=0.05)
    events = []

    async def emit(event):
        events.append(event)

    first = await booth.speak(payload, turn, emit)
    second = await booth.speak(payload, turn.model_copy(update={"id": "turn-2", "text": "Second and four."}), emit)
    names = [call[0] for call in live.calls]
    assert names.count("start") == 1 and names.count("thinking") == 2
    seals = [event["data"] for event in events if event["type"] == "segment.sealed"]
    assert [seal["boundary"] for seal in seals] == ["script_complete", "script_complete"]
    assert first.spoken_text == turn.text and second.spoken_text == "Second and four." and second.speech_complete
    context = json.loads(live.calls[1][1]["content"])
    assert context["your_team"] == "KC" and "play" in context
    usage = [event["data"]["seconds"] for event in events
             if event["type"] == "trace" and event["data"].get("node") == "voice_session"]
    assert usage == [2.0, 2.0]
    await asyncio.sleep(0.2)
    assert ("close", {}) in live.calls and booth.line is None


@pytest.mark.asyncio
async def test_cancelled_persistent_line_stops_the_voice_and_never_leaks_late_audio():
    payload, turn = payload_and_turn()
    live = LiveFake(finish=False)
    booth = booth_for(live)
    events = []
    heard = asyncio.Event()

    async def emit(event):
        events.append(event)
        if event["type"] == "audio":
            heard.set()

    task = asyncio.create_task(booth.speak(payload, turn, emit))
    await asyncio.wait_for(heard.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert any(call[0] == "instructions" and call[1]["content"].startswith("Stop speaking") for call in live.calls)
    before = len(events)
    await live.queue.put(SimpleNamespace(type="session.output_audio.delta",
                                         delta=base64.b64encode(struct.pack("<h", 4000) * 480).decode()))
    await asyncio.sleep(0.05)
    assert len(events) == before and booth.line.stray_audio == 1
    assert not any(event["type"] == "segment.sealed" for event in events)
    live.finish = True
    spoken = await booth.speak(payload, turn.model_copy(update={"id": "turn-3"}), emit)
    assert spoken.speech_complete and [call[0] for call in live.calls].count("start") == 1
    await booth.close()


@pytest.mark.asyncio
async def test_warm_booth_opens_while_drafting_and_closes_after_the_line():
    payload, turn = payload_and_turn()
    live = LiveFake()
    booth = booth_for(live, mode="warm")
    booth.prewarm()
    await asyncio.sleep(0.05)
    assert live.calls[0][0] == "start"
    events = []

    async def emit(event):
        events.append(event)

    spoken = await booth.speak(payload, turn, emit)
    assert spoken.speech_complete and booth.line is None
    finalized = next(event["data"] for event in events if event["type"] == "trace" and event["data"].get("phase") == "finalized")
    assert finalized["usage"] == {"seconds": 2.0} and [call[0] for call in live.calls].count("start") == 1


class AdlibFake(LiveFake):
    """Speaks the cued line, then keeps talking: the extra sentence must never reach the listener."""

    def __init__(self, extra=" He just blew past him.", inserted=None):
        super().__init__()
        self.extra, self.inserted = extra, inserted

    async def instructions(self, **kwargs):
        self.calls.append(("instructions", kwargs))
        match = re.search(r'go silent: (".*?")\. It replaces', kwargs["content"])
        if not match:
            return
        line = self.inserted or json.loads(match.group(1))
        voiced = struct.pack("<h", 4000) * 4800  # 200 ms of the line
        rambling = struct.pack("<h", 3000) * 7200  # 300 ms of ad-lib
        await self.queue.put(SimpleNamespace(type="session.output_transcript.delta", delta=line, start_ms=0))
        await self.queue.put(SimpleNamespace(type="session.output_audio.delta", delta=base64.b64encode(voiced).decode()))
        await self.queue.put(SimpleNamespace(type="session.output_audio.delta", delta=base64.b64encode(rambling).decode()))
        if self.extra:
            await self.queue.put(SimpleNamespace(type="session.output_transcript.delta", delta=self.extra, start_ms=200))
        await self.queue.put(SimpleNamespace(type="session.output_audio.delta",
                                             delta=base64.b64encode(b"\0" * (RATE * 2 * 4 // 10)).decode()))


@pytest.mark.asyncio
async def test_trailing_adlib_is_cut_at_the_sample_where_it_begins():
    payload, turn = payload_and_turn()
    live = AdlibFake()
    booth = booth_for(live)
    events = []

    async def emit(event):
        events.append(event)

    spoken = await booth.speak(payload, turn, emit)
    seal = next(event["data"] for event in events if event["type"] == "segment.sealed")
    played = b"".join(base64.b64decode(event["data"]["pcm_base64"]) for event in events if event["type"] == "audio")
    assert seal["total_samples"] == 4800 and len(played) == 4800 * 2  # the line, not a sample of the ad-lib
    assert spoken.spoken_text == turn.text and seal["adlib_cut"] == "He just blew past him."
    assert any(event["type"] == "trace" and event["data"].get("phase") == "adlib" for event in events)
    assert any(call[0] == "instructions" and call[1]["content"].startswith("Stop speaking") for call in live.calls)
    assert not any(event["type"] == "caption.delta" and "blew" in event["data"]["delta"] for event in events)
    await booth.close()


@pytest.mark.asyncio
async def test_short_reaction_after_the_line_is_kept_and_insertions_are_reported():
    payload, turn = payload_and_turn()
    booth = booth_for(AdlibFake(extra=" Wow!"))
    events = []

    async def emit(event):
        events.append(event)

    spoken = await booth.speak(payload, turn, emit)
    assert spoken.spoken_text.endswith("Wow!")
    await booth.close()
    laughing = booth_for(AdlibFake(extra=" [laugh] Ha!"))
    events.clear()
    assert (await laughing.speak(payload, turn, emit)).spoken_text.endswith("[laugh] Ha!")  # laughter is a reaction
    await laughing.close()
    seal = next(event["data"] for event in events if event["type"] == "segment.sealed")
    assert seal["adlib_cut"] is None and seal["total_samples"] > 4800
    await booth.close()
    inserted = booth_for(AdlibFake(extra="", inserted="Twelve yards, he just flew, on that pass."))
    events.clear()
    await inserted.speak(payload, turn, emit)
    adlib = next(event["data"] for event in events if event["type"] == "trace" and event["data"].get("phase") == "adlib")
    assert adlib["cut"] is None and {"he", "just", "flew"} <= set(adlib["inserted"])
    await inserted.close()


class ModeratedFake(LiveFake):
    """The service stops the acted cue for moderation; the plain re-read goes through."""

    async def instructions(self, **kwargs):
        plain = re.search(r'broadcast energy: (".*?")\. Then', kwargs["content"])
        if plain:
            return await super().instructions(content=f"go silent: {plain.group(1)}. It replaces")
        self.calls.append(("instructions", kwargs))
        if kwargs["content"].startswith("Cue"):
            await self.queue.put(SimpleNamespace(type="error", error=SimpleNamespace(
                message="The generation was stopped due to moderation.")))


@pytest.mark.asyncio
async def test_false_moderation_stop_is_read_again_before_anything_is_heard():
    payload, turn = payload_and_turn()
    live = ModeratedFake()
    booth = booth_for(live)
    events = []

    async def emit(event):
        events.append(event)

    spoken = await booth.speak(payload, turn, emit)
    assert spoken.spoken_text == turn.text
    assert any(event["type"] == "trace" and event["data"].get("phase") == "retry" for event in events)
    assert [call[0] for call in live.calls].count("start") == 2  # the stopped line was closed, a fresh one read it
    await booth.close()
