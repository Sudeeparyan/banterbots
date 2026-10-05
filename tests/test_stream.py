"""Exercise the real dashboard WebSocket route with an isolated SQLite journal."""
import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from starlette.websockets import WebSocketDisconnect

import backend.api as api_module
import backend.journal as journal_module
from backend.contracts import RunEvent, SessionCreate
from backend.engine import Broadcast, Engine
from backend.journal import Journal
from backend.providers import get_replay_games


async def until(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(poll(), timeout=2)


@pytest.fixture
def stream_client(tmp_path, monkeypatch):
    monkeypatch.setattr(journal_module, "RUNTIME", tmp_path)
    journal = Journal()
    asyncio.run(journal.open())
    engine = Engine(journal, InMemorySaver())
    session = Broadcast(id="stream-test", config=SessionCreate(voice_enabled=False), game=get_replay_games()[0])
    engine.sessions[session.id] = session
    application = FastAPI()
    application.state.engine = engine
    application.add_api_websocket_route("/api/sessions/{sid}/stream", api_module.stream)
    monkeypatch.setattr(api_module, "app", application)
    try:
        with TestClient(application) as client:
            yield client, engine, session
            client.portal.call(engine.close)
    finally:
        asyncio.run(journal.close())


def append_history(client, engine, session, kinds):
    for sequence, kind in enumerate(kinds, 1):
        event = RunEvent(seq=sequence, session_id=session.id, epoch=session.epoch, type=kind,
                         data={"marker": kind, "segment_id": "old-audio", "turn_id": "old-turn"})
        client.portal.call(engine.journal.append, event)
    session.seq = len(kinds)


def test_reconnect_replays_history_with_flags_and_never_replays_audio(stream_client):
    client, engine, session = stream_client
    append_history(client, engine, session, ["snapshot", "turn.start", "audio", "segment.sealed", "turn", "trace"])
    with client.websocket_connect(f"/api/sessions/{session.id}/stream?since=0") as socket:
        received = [socket.receive_json() for _ in range(3)]
        assert [event["type"] for event in received] == ["snapshot", "turn", "trace"]
        assert all(event["historical"] is True for event in received)
        assert [event["seq"] for event in received] == [1, 5, 6]
        client.portal.call(engine.emit, session, "feed", {"status": "connected"})
        live = socket.receive_json()
        assert live["type"] == "feed" and not live.get("historical", False)
        assert live["seq"] == 7

    with client.websocket_connect(f"/api/sessions/{session.id}/stream?since=5") as socket:
        received = [socket.receive_json() for _ in range(2)]
        assert [event["seq"] for event in received] == [6, 7]
        assert all(event["historical"] is True for event in received)
        assert all(event["type"] not in {"audio", "segment.sealed", "turn.start"} for event in received)


def test_only_playback_owner_acknowledgement_releases_segment(stream_client):
    client, engine, session = stream_client
    append_history(client, engine, session, ["trace"])
    with client.websocket_connect(f"/api/sessions/{session.id}/stream") as owner:
        owner.receive_json()  # History frame proves registration is complete.
        owner_id = session.player
        with client.websocket_connect(f"/api/sessions/{session.id}/stream") as spectator:
            spectator.receive_json()
            assert len(session.listeners) == 2 and session.player == owner_id
            spectator.send_json({"type": "playback.ack", "segment_id": "spectator-forgery", "played_samples": 2400})
            owner.send_json({"type": "playback.ack", "segment_id": "owner-segment", "played_samples": 4800})
            owner_trace, spectator_trace = owner.receive_json(), spectator.receive_json()
            assert owner_trace["data"]["segment_id"] == spectator_trace["data"]["segment_id"] == "owner-segment"
            assert owner_trace["data"]["node"] == "playback" and owner_trace["data"]["phase"] == "complete"
            assert session.playback["owner-segment"].is_set()
            assert "spectator-forgery" not in session.playback


def test_owner_disconnect_releases_waits_and_transfers_to_remaining_listener(stream_client):
    client, engine, session = stream_client
    append_history(client, engine, session, ["trace"])
    with client.websocket_connect(f"/api/sessions/{session.id}/stream") as owner:
        owner.receive_json()
        original_player = session.player
        with client.websocket_connect(f"/api/sessions/{session.id}/stream") as spectator:
            spectator.receive_json()
            pending = asyncio.Event()
            session.playback["unheard-segment"] = pending
            owner.close()
            client.portal.call(until, lambda: session.player != original_player)
            assert original_player not in session.listeners
            assert len(session.listeners) == 1 and session.player in session.listeners
            assert pending.is_set()
            spectator.send_json({"type": "playback.ack", "segment_id": "new-owner-segment", "cancelled": True})
            acknowledgement = spectator.receive_json()
            assert acknowledgement["data"]["phase"] == "cancelled"
            assert session.playback["new-owner-segment"].is_set()
    client.portal.call(until, lambda: not session.listeners)
    assert session.player is None


def test_unknown_run_rejects_websocket_before_accept(stream_client):
    client, _, _ = stream_client
    with pytest.raises(WebSocketDisconnect) as rejected:
        with client.websocket_connect("/api/sessions/nonexistent/stream"):
            pass
    assert rejected.value.code == 1008


@pytest.mark.parametrize("missing_prefix", [True, False], ids=["missing-leading-chunks", "seal-without-audio"])
def test_mid_segment_reconnect_discards_incomplete_audio_then_delivers_peer(stream_client, missing_prefix):
    client, engine, session = stream_client
    append_history(client, engine, session, ["trace"])
    with client.websocket_connect(f"/api/sessions/{session.id}/stream") as socket:
        socket.receive_json()
        if missing_prefix:
            # Connection arrived after chunks 0–2: this packet cannot be played.
            client.portal.call(engine.emit, session, "audio", {"segment_id": "lost-prefix", "agent_id": "a",
                "chunk_index": 3, "audio": "AAAA", "rate": 24000})
        client.portal.call(engine.emit, session, "segment.sealed", {"segment_id": "lost-prefix", "agent_id": "a",
            "total_samples": 2400, "chunks": 4, "browser_speech": False})
        client.portal.call(engine.emit, session, "audio", {"segment_id": "whole-peer", "agent_id": "b",
            "chunk_index": 0, "audio": "AAAA", "rate": 24000})
        client.portal.call(engine.emit, session, "segment.sealed", {"segment_id": "whole-peer", "agent_id": "b",
            "total_samples": 2, "chunks": 1, "browser_speech": False})
        client.portal.call(engine.emit, session, "feed", {"status": "test-complete"})
        received = []
        while True:
            event = socket.receive_json()
            received.append(event)
            if event["type"] == "feed":
                break
        assert not any(event["type"] == "audio" and event["data"].get("segment_id") == "lost-prefix" for event in received)
        discarded = next(event for event in received if event["type"] == "segment.sealed"
                         and event["data"].get("segment_id") == "lost-prefix")
        assert discarded["data"]["discard_audio"] is True
        peer_audio = [event for event in received if event["type"] == "audio"]
        assert len(peer_audio) == 1 and peer_audio[0]["data"]["segment_id"] == "whole-peer"
        assert peer_audio[0]["data"]["chunk_index"] == 0
        peer_seal = next(event for event in received if event["type"] == "segment.sealed"
                         and event["data"].get("segment_id") == "whole-peer")
        assert not peer_seal["data"].get("discard_audio", False)
        assert session.playback["lost-prefix"].is_set()


def test_reconnect_after_demo_turn_releases_unheard_speech(stream_client):
    client, engine, session = stream_client
    event = RunEvent(seq=1, session_id=session.id, epoch=session.epoch, type="turn",
                     data={"turn": {"id": "old-demo"}})
    client.portal.call(engine.journal.append, event)
    session.seq = 1
    with client.websocket_connect(f"/api/sessions/{session.id}/stream") as socket:
        assert socket.receive_json()["historical"]
        client.portal.call(engine.emit, session, "segment.sealed", {"segment_id": "old-demo",
            "browser_speech": True, "total_samples": 0})
        lost = socket.receive_json()
        assert lost["data"]["discard_audio"] and not lost["data"]["browser_speech"]
        assert session.playback["old-demo"].is_set()
        client.portal.call(engine.emit, session, "turn", {"turn": {"id": "new-demo"}})
        assert not socket.receive_json().get("historical")
        client.portal.call(engine.emit, session, "segment.sealed", {"segment_id": "new-demo",
            "browser_speech": True, "total_samples": 0})
        fresh = socket.receive_json()
        assert fresh["data"]["browser_speech"] and not fresh["data"].get("discard_audio")
