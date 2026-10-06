import asyncio
import base64
import struct
from types import SimpleNamespace

import pytest

from backend.contracts import AgentRequest, CommentaryTurn, GameSnapshot, PlayEvent
from backend.speech import RATE, SILENCE_FRAME, SpeechFailure, SpeechObserver, pcm_rms, speak, voice_session


def payload_and_turn():
    event = PlayEvent(id="p1", game_id="g1", source="nflverse", sequence=1, quarter=1, clock="12:01",
                      home_team="KC", away_team="BAL", home_score=0, away_score=0,
                      description="L.Jackson pass complete for 12 yards.", yards_gained=12)
    payload = AgentRequest(session_id="session1", epoch=1, exchange_id="exchange1", snapshot=GameSnapshot(event=event).seal(),
                           provider="openai")
    turn = CommentaryTurn(exchange_id=payload.exchange_id, agent_id="a", persona="Max Carter", team_id="KC",
                          event_id=event.id, snapshot_hash=payload.snapshot.hash, text="Twelve yards on that pass.",
                          voice="meridian", mode="openai")
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
    assert observer.boundary(10.01) == "duration_cap"


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
    assert seal["total_samples"] == RATE + 480 and seal["finalized"] is True
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
