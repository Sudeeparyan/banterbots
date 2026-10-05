"""Recovery regressions: interrupted work must remain observable and resumable."""
import asyncio
import contextlib
from unittest.mock import AsyncMock

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from backend.contracts import CommentaryTurn, GameSnapshot, RunEvent, SessionCreate
from backend.engine import Engine


def engine():
    return Engine(AsyncMock(), InMemorySaver())


@pytest.mark.asyncio
async def test_pause_during_exchange_keeps_replay_cursor_on_unfinished_play(monkeypatch):
    host = engine()
    session = await host.create(SessionCreate(voice_enabled=False))
    started = asyncio.Event()

    async def blocked(*args):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(host, "one", blocked)
    try:
        await host.control(session, "play")
        await started.wait()
        await host.control(session, "pause")
        assert session.index == 0
        assert session.status == "paused" and session.task is None
    finally:
        await host.provider.aclose()


@pytest.mark.asyncio
async def test_failed_peer_does_not_consume_replay_play(monkeypatch):
    host = engine()
    session = await host.create(SessionCreate(voice_enabled=False))

    async def failed(*args):
        raise TimeoutError("Peer agent unavailable")

    monkeypatch.setattr(host, "one", failed)
    try:
        await host.control(session, "step")
        await session.task
        assert session.index == 0 and session.exchanges == 0
        assert session.status == "error"
        assert any(event["type"] == "error" and "Peer agent" in event["data"]["message"] for event in session.events)
    finally:
        await host.provider.aclose()


@pytest.mark.asyncio
async def test_stale_snapshot_node_cannot_mutate_new_epoch():
    host = engine()
    session = await host.create(SessionCreate())
    old_epoch = session.epoch
    before = session.snapshot
    try:
        await host.halt(session)
        with contextlib.suppress(asyncio.CancelledError):
            await host.make_snapshot({"session_id": session.id, "epoch": old_epoch,
                                      "play": session.replay[14].model_dump()})
        assert session.snapshot == before
    finally:
        await host.provider.aclose()


@pytest.mark.asyncio
async def test_live_join_seeds_recent_context_without_future_plays():
    host = engine()
    session = await host.create(SessionCreate())
    session.config.mode = "live"
    plays = [play.model_copy(update={"source": "espn"}) for play in session.replay[:12]]
    plays[6].flags = ["reversed", "incomplete_pass"]
    session.seen = {play.id: play for play in plays}
    try:
        result = await host.make_snapshot({"session_id": session.id, "epoch": session.epoch,
                                           "play": plays[8].model_dump()})
        history = result["snapshot"]["drive_history"]
        assert [play["id"] for play in history] == [play.id for play in plays[3:8]]
        assert history[3]["flags"] == ["reversed", "incomplete_pass"]
        assert not {play.id for play in plays[8:]} & {play["id"] for play in history}
    finally:
        await host.provider.aclose()


@pytest.mark.asyncio
async def test_queued_live_revision_supersedes_unspoken_old_version(monkeypatch):
    host = engine()
    session = await host.create(SessionCreate())
    session.config.mode = "live"
    first = session.replay[0].model_copy(update={"source": "espn"})
    play = session.replay[1].model_copy(update={"source": "espn"})
    correction = play.model_copy(update={"revision": 2, "description": "Corrected live play"})
    calls = 0

    async def feed(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            session.cancel.set()
        return [first, play if calls == 1 else correction]

    original_sleep = asyncio.sleep
    async def no_wait(*args):
        await original_sleep(0)

    monkeypatch.setattr(host.provider, "events", feed)
    monkeypatch.setattr("backend.engine.asyncio.sleep", no_wait)
    try:
        await host.live_poll(session)
        queued = [event for event in session.pending if event.id == play.id]
        assert len(queued) == 1 and queued[0].revision == 2
        assert "correction" in queued[0].flags
    finally:
        await host.provider.aclose()


@pytest.mark.asyncio
async def test_live_backpressure_retains_scoring_turnover_and_newest(monkeypatch):
    host = engine()
    session = await host.create(SessionCreate())
    session.config.mode = "live"
    events = [session.replay[0].model_copy(update={"id": f"p{index}", "sequence": index, "flags": []})
              for index in range(50)]
    events[5].flags = ["scoring", "touchdown"]
    events[10].flags = ["interception", "turnover"]
    events[15].flags = ["turnover"]
    session.seen = {events[0].id: events[0]}

    async def feed(*args):
        session.cancel.set()
        return events

    original_sleep = asyncio.sleep
    async def no_wait(*args):
        await original_sleep(0)

    monkeypatch.setattr(host.provider, "events", feed)
    monkeypatch.setattr("backend.engine.asyncio.sleep", no_wait)
    try:
        await host.live_poll(session)
        retained = {event.id for event in session.pending}
        assert {"p5", "p10", "p15", "p49"} <= retained
        assert len(session.pending) < 49
        assert any(event["type"] == "trace" and event["data"].get("node") == "backpressure"
                   and event["data"]["skipped"] > 0 and "p20" in event["data"]["skipped_event_ids"]
                   for event in session.events)
    finally:
        await host.provider.aclose()


@pytest.mark.asyncio
async def test_live_feed_recovers_without_confusing_play_age_with_poll_health(monkeypatch):
    host = engine()
    session = await host.create(SessionCreate())
    session.config.mode = "live"
    event = session.replay[1].model_copy(update={"source": "espn", "occurred_at": "2024-09-06T00:20:00Z"})
    attempts = 0

    async def feed(*args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("Upstream temporarily unavailable")
        session.cancel.set()
        return [event]

    original_sleep = asyncio.sleep
    async def no_wait(*args):
        await original_sleep(0)

    monkeypatch.setattr(host.provider, "events", feed)
    monkeypatch.setattr("backend.engine.asyncio.sleep", no_wait)
    try:
        await host.live_poll(session)
        feed_events = [item["data"] for item in session.events if item["type"] == "feed"]
        assert feed_events[0]["status"] == "error" and feed_events[-1]["status"] == "connected"
        assert session.feed["latest_play_at"] == event.occurred_at
        assert session.feed["last_success"] != event.occurred_at
        assert len(session.pending) == 1 and session.pending[0].source == "espn"
        assert any(item["type"] == "error" and item["data"].get("recoverable") for item in session.events)
    finally:
        await host.provider.aclose()


@pytest.mark.asyncio
async def test_reopening_recovers_durable_turn_after_last_state_save(monkeypatch):
    host = engine()
    session = await host.create(SessionCreate(voice_enabled=False))
    saved = session.view()
    snapshot = GameSnapshot(event=session.replay[1]).seal()
    turn = CommentaryTurn(exchange_id="interrupted", agent_id="a", persona="Max Carter", team_id="KC",
                          event_id=snapshot.event.id, snapshot_hash=snapshot.hash, text="A recorded call.")
    newer = [RunEvent(seq=saved["seq"] + 1, session_id=session.id, epoch=session.epoch,
                     type="snapshot", data={"snapshot": snapshot.model_dump()}).model_dump(),
             RunEvent(seq=saved["seq"] + 2, session_id=session.id, epoch=session.epoch,
                      type="turn", data={"turn": turn.model_dump()}).model_dump()]
    host.journal.get.return_value = saved
    host.journal.events.return_value = newer
    host.sessions.clear()
    forbidden_regeneration = AsyncMock(side_effect=AssertionError("Viewing history must never regenerate"))
    monkeypatch.setattr(host, "one", forbidden_regeneration)
    try:
        restored = await host.get(session.id)
        assert any(item["id"] == turn.id for item in restored.turns)
        assert restored.snapshot["hash"] == snapshot.hash
        assert restored.seq == newer[-1]["seq"]
        assert restored.task is None and forbidden_regeneration.await_count == 0
    finally:
        await host.provider.aclose()
