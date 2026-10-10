"""The booth flow: possession leads, pre-snap setups, segments, memory, listener input and lookahead."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

import backend.api as api_module
from backend.contracts import CommentaryTurn, SessionCreate
from backend.engine import Engine


def engine():
    return Engine(AsyncMock(), InMemorySaver())


def index_of(session, event_id):
    return next(i for i, play in enumerate(session.replay) if play.id == event_id)


def recording_exchange(host, observed, bit=None, prediction=None):
    async def fake_exchange(url, payload, emit, cancel):
        observed.append(payload)
        agents = ("a", "b") if payload.mode in {"exchange", "segment"} else ("a" if url.endswith("8001") else "b",)
        turns = []
        previous = None
        for agent in agents:
            event = payload.snapshot.event
            turn = CommentaryTurn(exchange_id=payload.exchange_id, agent_id=agent, persona=agent,
                                  team_id=event.home_team if agent == "a" else event.away_team, event_id=event.id,
                                  snapshot_hash=payload.snapshot.hash, text="A grounded line.",
                                  kind={"setup": "setup", "segment": "segment"}.get(payload.mode, "call"),
                                  bit=bit if payload.mode == "exchange" else None,
                                  prediction=prediction if payload.mode == "setup" else None,
                                  reply_to_turn_id=previous)
            previous = turn.id
            await emit({"type": "turn", "data": {"turn": turn.model_dump()}})
            turns.append(turn)
        return turns
    return fake_exchange


@pytest.mark.asyncio
async def test_possession_decides_play_by_play_and_framing_moments_alternate():
    host = engine()
    session = await host.create(SessionCreate(voice_enabled=False))
    try:
        kc = next(play for play in session.replay if play.possession == "KC" and play.play_type == "pass")
        bal = next(play for play in session.replay if play.possession == "BAL" and play.play_type == "run")
        quarter = next(play for play in session.replay if play.play_type == "period_end")
        assert host.lead_for(session, kc) == "a" and host.lead_for(session, bal) == "b"
        session.exchanges = 1
        assert host.lead_for(session, quarter) == "b"
    finally:
        await host.provider.aclose()


@pytest.mark.asyncio
async def test_third_down_gets_a_presnap_setup_and_poll_before_the_result_is_snapshotted(monkeypatch):
    host = engine()
    observed = []
    monkeypatch.setattr("backend.a2a_client.exchange", recording_exchange(host, observed, prediction="convert"))
    session = await host.create(SessionCreate(voice_enabled=False))
    try:
        index = index_of(session, "148")
        await host.one(session, session.replay[index])
        setup, call = observed[0], observed[1]
        assert setup.mode == "setup" and setup.snapshot.phase == "pre_snap" and setup.hop_budget == 0
        event = setup.snapshot.event
        assert event.description == "" and event.flags == [] and event.yards_gained is None
        assert (event.down, event.distance) == (3, 11)
        assert setup.snapshot.event.home_score == session.replay[index - 1].home_score
        assert call.mode == "exchange" and call.snapshot.phase == "result"
        assert "Hill" in call.snapshot.event.description
        # The setup lead is Baltimore's voice: the visitors have the ball.
        assert session.turns[0]["kind"] == "setup" and session.turns[0]["agent_id"] == "b"
        polls = [event for event in session.events if event["type"] == "poll.open"]
        assert polls and polls[0]["data"]["event_id"] == "148"
        # The setup's prediction is remembered and scored once the conversion is known.
        prediction = next(item for item in session.memory if item.get("prediction"))
        assert prediction["outcome"] == "right"
    finally:
        await host.provider.aclose()


@pytest.mark.asyncio
async def test_listener_poll_vote_is_scored_and_reaches_the_next_snapshot(monkeypatch):
    host = engine()
    observed = []
    monkeypatch.setattr("backend.a2a_client.exchange", recording_exchange(host, observed))
    session = await host.create(SessionCreate(voice_enabled=False))
    try:
        index = index_of(session, "148")
        await host.setup({"session_id": session.id, "epoch": session.epoch, "play": session.replay[index].model_dump(),
                          "lead": "b", "exchange_id": "x", "started": 0})
        assert session.listener["poll"]["event_id"] == "148"
        host.resolve(session, session.replay[index])
        # The studio knows the result, but the listener is still hearing the setup: the poll stays
        # open and never shows the result to the browser.
        assert session.listener["poll"]["event_id"] == "148" and "converted" not in session.view()["listener"]["poll"]
        session.listener["poll"]["pick"] = "stopped"
        snapshot = host.booth(session, session.replay[index + 1], "result")
        assert session.listener["resolved_poll"] == {"question": "Do they convert?", "pick": "stopped", "outcome": "wrong"}
        assert snapshot["listener"]["poll"]["outcome"] == "wrong" and session.listener["poll"] is None
    finally:
        await host.provider.aclose()


@pytest.mark.asyncio
async def test_halftime_and_drive_summary_segments_use_only_completed_plays(monkeypatch):
    host = engine()
    observed = []
    monkeypatch.setattr("backend.a2a_client.exchange", recording_exchange(host, observed))
    session = await host.create(SessionCreate(voice_enabled=False))
    try:
        half = index_of(session, "2145")
        await host.one(session, session.replay[half])
        assert [payload.mode for payload in observed] == ["segment"]
        segment = observed[0]
        assert segment.topic == "halftime" and segment.snapshot.phase == "segment"
        stats = segment.snapshot.booth["stats"]
        assert stats["teams"]["KC"]["plays"] > 0
        observed.clear()
        point = index_of(session, "414")  # the extra point that completes Baltimore's opening touchdown drive
        await host.one(session, session.replay[point])
        topics = [payload.topic for payload in observed if payload.mode == "segment"]
        assert topics == ["drive_summary"]
        drive = observed[-1].snapshot.booth["stats"]["drive"]
        assert drive["team"] == "BAL" and drive["plays"] >= 4
    finally:
        await host.provider.aclose()


@pytest.mark.asyncio
async def test_listener_question_is_answered_at_the_next_break(monkeypatch):
    host = engine()
    observed = []
    monkeypatch.setattr("backend.a2a_client.exchange", recording_exchange(host, observed))
    session = await host.create(SessionCreate(voice_enabled=False))
    try:
        session.listener["side"] = "away"
        session.listener["questions"].append("Who has more yards so far?")
        await host.one(session, session.replay[index_of(session, "183")])
        question = next(payload for payload in observed if payload.topic == "listener_question")
        assert question.snapshot.booth["listener"]["question"] == "Who has more yards so far?"
        assert question.snapshot.booth["listener"]["side"] == "away"
        assert "stats" in question.snapshot.booth and session.listener["questions"] == []
    finally:
        await host.provider.aclose()


@pytest.mark.asyncio
async def test_next_play_is_prepared_during_playback_but_never_two_plays_ahead(monkeypatch):
    host = engine()
    session = await host.create(SessionCreate(voice_enabled=True))
    session.player = "listener"
    calls = []

    async def one(session, event):
        calls.append(event.id)
        session.last_segment = f"segment-{event.id}"

    monkeypatch.setattr(host, "one", one)
    try:
        await host.control(session, "play")
        while len(calls) < 2:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        # Play 2 was prepared while play 1 is heard; play 3 waits for play 1 to finish.
        assert calls == [session.replay[0].id, session.replay[1].id]
        session.playback.setdefault(f"segment-{calls[0]}", asyncio.Event()).set()
        while len(calls) < 3:
            await asyncio.sleep(0.01)
        assert calls[2] == session.replay[2].id
        await host.control(session, "pause")
    finally:
        await host.provider.aclose()


@pytest.mark.asyncio
async def test_pause_before_a_prepared_play_is_heard_regenerates_it_on_resume(monkeypatch):
    host = engine()
    session = await host.create(SessionCreate(voice_enabled=False))
    started = asyncio.Event()

    async def one(session, event):
        session.turns.append({"id": "prepared", "event_id": event.id, "exchange_id": "x", "agent_id": "a"})
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(host, "one", one)
    try:
        await host.control(session, "play")
        await started.wait()
        await host.control(session, "pause")
        assert session.index == 0 and session.turns[0]["interrupted"] is True
        session.turns.clear()
        session.heard.add("heard-line")
        started.clear()

        async def heard(session, event):
            session.turns.append({"id": "heard-line", "event_id": event.id, "exchange_id": "y", "agent_id": "a"})
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(host, "one", heard)
        await host.control(session, "play")
        await started.wait()
        await host.control(session, "pause")
        assert session.index == 1  # a heard partial play is consumed, never repeated
    finally:
        await host.provider.aclose()


@pytest.fixture
def listener_client(monkeypatch):
    host = engine()
    session = asyncio.run(host.create(SessionCreate(voice_enabled=False)))
    application = FastAPI()
    application.state.engine = host
    application.add_api_route("/api/sessions/{sid}/listener", api_module.listener, methods=["POST"])
    monkeypatch.setattr(api_module, "app", application)
    with TestClient(application) as client:
        yield client, host, session
    asyncio.run(host.provider.aclose())


def test_listener_endpoint_filters_rate_limits_and_closes_polls(listener_client):
    client, host, session = listener_client
    url = f"/api/sessions/{session.id}/listener"
    assert client.post(url, json={"side": "home"}).json()["side"] == "home"
    assert client.post(url, json={"reaction": "cheer"}).json()["cheers"] == 1
    assert client.post(url, json={"reaction": "cheer"}).json()["cheers"] == 1  # held button, one voice
    assert client.post(url, json={"question": "Can I bet the over?"}).status_code == 422
    assert client.post(url, json={"question": "Who has the ball?"}).json()["questions_waiting"] == 1
    assert client.post(url, json={"question": "And now?"}).status_code == 429
    assert client.post(url, json={"poll_vote": "convert", "poll_event_id": "148"}).status_code == 409
    session.listener["poll"] = {"event_id": "148", "question": "Do they convert?", "pick": None}
    assert client.post(url, json={"poll_vote": "convert", "poll_event_id": "148"}).status_code == 200
    assert session.listener["poll"]["pick"] == "convert"
    assert client.post(url, json={"question": "x" * 300}).status_code == 422


@pytest.mark.asyncio
async def test_screen_events_name_the_audio_they_follow_but_audio_is_never_held():
    host = engine()
    session = await host.create(SessionCreate(voice_enabled=True))
    stream: asyncio.Queue = asyncio.Queue()
    session.listeners["test"] = stream
    try:
        session.screen_after = "call-1"  # still playing in the browser while the next play is prepared
        await host.emit(session, "snapshot", {"snapshot": {"event": {"id": "next"}}})
        await host.emit(session, "audio", {"segment_id": "next-call", "chunk_index": 0})
        snapshot, audio = stream.get_nowait(), stream.get_nowait()
        assert snapshot["data"]["present_after"] == "call-1"
        assert "present_after" not in audio["data"]
    finally:
        await host.provider.aclose()


def booth_turns(payload, agents):
    event, turns, previous = payload.snapshot.event, [], None
    for agent in agents:
        turn = CommentaryTurn(exchange_id=payload.exchange_id, agent_id=agent, persona=agent,
                              team_id=event.home_team if agent == "a" else event.away_team, event_id=event.id,
                              snapshot_hash=payload.snapshot.hash, text="A grounded line.",
                              kind="setup" if payload.mode == "setup" else "call", reply_to_turn_id=previous)
        previous = turn.id
        turns.append(turn)
    return turns


@pytest.mark.asyncio
async def test_call_is_written_while_the_setup_is_spoken_and_its_result_waits_behind_it(monkeypatch):
    host = engine()
    sent, spoken = [], asyncio.Event()

    async def fake_exchange(url, payload, emit, cancel):
        sent.append(payload.mode)
        if payload.mode == "setup":
            await emit({"type": "turn.start", "data": {"segment_id": "setup-line", "agent_id": "b"}})
        # One voice per host: neither line finishes until the setup has been spoken.
        await spoken.wait()
        turns = booth_turns(payload, ("b",) if payload.mode == "setup" else ("b", "a"))
        for turn in turns:
            await emit({"type": "turn", "data": {"turn": turn.model_dump()}})
        return turns

    monkeypatch.setattr("backend.a2a_client.exchange", fake_exchange)
    session = await host.create(SessionCreate(voice_enabled=False))
    try:
        call = asyncio.create_task(host.one(session, session.replay[index_of(session, "148")]))
        for _ in range(200):
            if "exchange" in sent:
                break
            await asyncio.sleep(0.01)
        # The call was requested while the setup is still on air...
        assert sent == ["setup", "exchange"] and not call.done()
        # ...and the result waits on screen until the setup has been heard.
        snapshot = next(event for event in session.events if event["type"] == "snapshot")
        assert snapshot["data"]["present_after"] == "setup-line"
        spoken.set()
        await call
        assert [turn["kind"] for turn in session.turns] == ["setup", "call", "call"]
    finally:
        await host.provider.aclose()


@pytest.mark.asyncio
async def test_next_setup_starts_while_the_reply_is_on_air_and_is_adopted_once(monkeypatch):
    host = engine()
    sent, during_reply = [], []

    async def fake_exchange(url, payload, emit, cancel):
        event = payload.snapshot.event
        sent.append((payload.mode, event.id))
        if payload.mode == "setup":
            await emit({"type": "turn.start", "data": {"segment_id": f"setup-{event.id}", "agent_id": "b"}})
            return booth_turns(payload, ("b",))
        lead = "a" if url.endswith("8001") else "b"
        partner = "b" if lead == "a" else "a"
        await emit({"type": "audio", "data": {"segment_id": "reply", "agent_id": partner, "chunk_index": 0}})
        await asyncio.sleep(0.2)  # the reply is still speaking
        during_reply.append(list(sent))
        return booth_turns(payload, (lead, partner))

    monkeypatch.setattr("backend.a2a_client.exchange", fake_exchange)
    session = await host.create(SessionCreate(provider="azure", voice_enabled=True))
    session.status = "playing"
    try:
        index = index_of(session, "148")
        upcoming = session.replay[index + 1]
        await host.one(session, session.replay[index])
        # Sent while this play's reply was still on air.
        assert ("setup", upcoming.id) in during_reply[0]
        await host.one(session, upcoming)
        assert sent.count(("setup", upcoming.id)) == 1  # adopted, never sent twice
        session.prefetch = asyncio.create_task(asyncio.Event().wait())
        pending = session.prefetch
        await host.halt(session)
        await asyncio.sleep(0)
        assert pending.cancelled() and session.prefetch is None
    finally:
        await host.provider.aclose()
