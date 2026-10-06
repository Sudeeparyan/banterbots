import asyncio
from unittest.mock import AsyncMock

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from backend.contracts import CommentaryTurn, SessionCreate
from backend.engine import Engine


def engine():
    journal = AsyncMock()
    return Engine(journal, InMemorySaver())


@pytest.mark.asyncio
async def test_alternation_and_immutable_snapshot(monkeypatch):
    host = engine()
    observed = []

    async def fake_exchange(url, payload, emit, cancel):
        observed.append((url, payload.snapshot.hash, payload.snapshot.event.home_score))
        for agent in ("a", "b"):
            turn = CommentaryTurn(exchange_id=payload.exchange_id, agent_id=agent, persona=agent,
                team_id=payload.snapshot.event.home_team if agent == "a" else payload.snapshot.event.away_team,
                event_id=payload.snapshot.event.id, snapshot_hash=payload.snapshot.hash, text="A grounded call.")
            await emit({"type": "turn", "data": {"turn": turn.model_dump()}})
        return [CommentaryTurn.model_validate(t) for t in host.sessions[payload.session_id].turns[-2:]]

    monkeypatch.setattr("backend.a2a_client.exchange", fake_exchange)
    session = await host.create(SessionCreate(voice_enabled=False))
    await host.one(session, session.replay[0])
    snapshot_a = session.snapshot
    await host.one(session, session.replay[1])
    assert observed[0][0].endswith("8001")
    assert observed[1][0].endswith("8002")
    assert len(session.turns) == 4
    assert snapshot_a["event"]["home_score"] == observed[0][2] == 0
    assert snapshot_a["hash"] == observed[0][1]
    assert all(t["snapshot_hash"] == observed[0][1] for t in session.turns[:2])
    await host.provider.aclose()


@pytest.mark.asyncio
async def test_stale_epoch_cannot_publish():
    host = engine()
    session = await host.create(SessionCreate())
    old_epoch = session.epoch
    await host.halt(session)
    count = len(session.events)
    await host.emit(session, "turn", {"turn": {"text": "late"}}, old_epoch)
    assert len(session.events) == count
    assert session.epoch == old_epoch + 1
    await host.provider.aclose()


@pytest.mark.asyncio
async def test_single_step_is_bounded_and_restart_resets(monkeypatch):
    host = engine()
    monkeypatch.setattr(host, "one", AsyncMock())
    session = await host.create(SessionCreate(voice_enabled=False))
    await host.control(session, "step")
    await session.task
    assert session.status == "paused" and session.index == 1
    assert host.one.await_count == 1
    await host.control(session, "restart")
    assert session.index == 0 and session.exchanges == 0
    await host.provider.aclose()


@pytest.mark.asyncio
async def test_switch_cancels_previous_run():
    host = engine()
    first = await host.create(SessionCreate())
    started = asyncio.Event()

    async def wait_forever():
        started.set()
        await asyncio.Event().wait()

    first.task = asyncio.create_task(wait_forever())
    await started.wait()
    second = await host.create(SessionCreate(game_id="2024_22_KC_PHI"))
    assert first.cancel.is_set() and first.status == "stopped"
    assert first.task is None
    assert host.active == second.id
    await host.provider.aclose()


@pytest.mark.asyncio
async def test_parallel_session_selection_stops_superseded_broadcast(monkeypatch):
    host = engine()
    await host.create(SessionCreate())
    original_halt = host.halt

    async def yielding_halt(*args, **kwargs):
        await asyncio.sleep(0)
        return await original_halt(*args, **kwargs)

    monkeypatch.setattr(host, "halt", yielding_halt)
    try:
        first, second = await asyncio.gather(host.create(SessionCreate()),
                                            host.create(SessionCreate(game_id="2024_22_KC_PHI")))
        assert first.status == "stopped"
        assert second.status == "paused" and host.active == second.id
    finally:
        await host.provider.aclose()


@pytest.mark.asyncio
async def test_parallel_play_requests_keep_one_graph_running(monkeypatch):
    host = engine()
    first = await host.create(SessionCreate(voice_enabled=False))
    second = await host.create(SessionCreate(game_id="2024_22_KC_PHI", voice_enabled=False))
    original_halt = host.halt

    async def yielding_halt(*args, **kwargs):
        await asyncio.sleep(0)
        return await original_halt(*args, **kwargs)

    async def blocked(*args):
        await asyncio.Event().wait()

    monkeypatch.setattr(host, "halt", yielding_halt)
    monkeypatch.setattr(host, "one", blocked)
    try:
        await asyncio.gather(host.control(first, "play"), host.control(second, "play"))
        await asyncio.sleep(0)
        playing = [session for session in (first, second) if session.status == "playing"]
        running = [session for session in (first, second) if session.task and not session.task.done()]
        assert len(playing) == len(running) == 1
        assert playing[0].id == running[0].id == host.active
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_concurrent_source_and_graph_events_cannot_overtake_slow_journal_write(monkeypatch):
    host = engine()
    session = await host.create(SessionCreate())
    queue = asyncio.Queue()
    session.listeners["viewer"] = queue
    first_write = asyncio.Event()
    release = asyncio.Event()

    async def slow_append(event):
        if event.type == "snapshot":
            first_write.set()
            await release.wait()

    monkeypatch.setattr(host.journal, "append", slow_append)
    try:
        facts = asyncio.create_task(host.emit(session, "snapshot", {"snapshot": session.snapshot}))
        await first_write.wait()
        caption = asyncio.create_task(host.emit(session, "caption.delta", {"text": "Call based on those facts"}))
        await asyncio.sleep(0)
        assert queue.empty()
        release.set()
        await asyncio.gather(facts, caption)
        ordered = [queue.get_nowait(), queue.get_nowait()]
        assert [event["type"] for event in ordered] == ["snapshot", "caption.delta"]
        assert ordered[0]["seq"] < ordered[1]["seq"]
    finally:
        await host.provider.aclose()


@pytest.mark.asyncio
async def test_waiting_old_epoch_emit_is_discarded_after_pause():
    host = engine()
    session = await host.create(SessionCreate())
    old_epoch = session.epoch
    try:
        async with session.emit_lock:
            delayed = asyncio.create_task(host.emit(session, "turn", {"turn": {"text": "late call"}}, old_epoch))
            await asyncio.sleep(0)
            session.epoch += 1
        await delayed
        assert not any(event["type"] == "turn" for event in session.events)
    finally:
        await host.provider.aclose()
