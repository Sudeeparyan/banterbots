"""Recovery regressions: interrupted work must remain observable and resumable."""
import asyncio
import contextlib
from unittest.mock import AsyncMock

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from backend.contracts import CommentaryTurn, Game, GameSnapshot, RunEvent, SessionCreate
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
async def test_replay_highlight_and_pause_keep_causal_drive_context():
    host = engine()
    session = await host.create(SessionCreate(start_index=98, voice_enabled=False))
    try:
        await host.halt(session)
        result = await host.make_snapshot({"session_id": session.id, "epoch": session.epoch,
                                           "play": session.replay[98].model_dump()})
        history = result["snapshot"]["drive_history"]
        assert [play["id"] for play in history] == [play.id for play in session.replay[93:98]]
        assert result["snapshot"]["event"]["away_score"] == session.replay[98].away_score
        assert not {play.id for play in session.replay[98:]} & {play["id"] for play in history}
    finally:
        await host.provider.aclose()


@pytest.mark.asyncio
async def test_paused_live_resume_reseeds_latest_play_instead_of_stalling(monkeypatch):
    host = engine()
    session = await host.create(SessionCreate())
    session.config.mode = "live"
    plays = [play.model_copy(update={"source": "espn"}) for play in session.replay[:4]]
    session.seen = {play.id: play for play in plays}
    session.pending = plays[-2:]
    original_sleep = asyncio.sleep

    async def feed(*args):
        session.cancel.set()
        return plays

    async def no_wait(*args):
        await original_sleep(0)

    monkeypatch.setattr(host.provider, "events", feed)
    monkeypatch.setattr("backend.engine.asyncio.sleep", no_wait)
    try:
        await host.halt(session)
        session.cancel = asyncio.Event()
        await host.live_poll(session)
        assert [play.id for play in session.pending] == [plays[-1].id]
        assert len(session.seen) == len(plays)
        assert session.feed["status"] == "connected"
    finally:
        await host.provider.aclose()


@pytest.mark.asyncio
async def test_correction_keeps_queued_live_plays_in_provider_sequence(monkeypatch):
    host = engine()
    session = await host.create(SessionCreate())
    session.config.mode = "live"
    plays = [play.model_copy(update={"source": "espn"}) for play in session.replay[:5]]
    session.seen = {play.id: play for play in plays}
    session.pending = plays[1:]
    revised = plays[2].model_copy(update={"revision": 2, "description": "Corrected play"})
    original_sleep = asyncio.sleep

    async def feed(*args):
        session.cancel.set()
        return [*plays[:2], revised, *plays[3:]]

    async def no_wait(*args):
        await original_sleep(0)

    monkeypatch.setattr(host.provider, "events", feed)
    monkeypatch.setattr("backend.engine.asyncio.sleep", no_wait)
    try:
        await host.live_poll(session)
        assert [play.sequence for play in session.pending] == [1, 2, 3, 4]
        assert session.pending[1].revision == 2 and "correction" in session.pending[1].flags
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


@pytest.mark.asyncio
async def test_saved_live_run_preserves_scoreboard_date_for_explicit_regeneration(monkeypatch):
    host = engine()
    game = Game(id="401671789", home_team="KC", away_team="BAL", mode="live",
                start_time="2024-09-06T00:20:00Z")

    async def dated_games(date=None):
        return [game] if date == "20240905" else []

    monkeypatch.setattr(host.provider, "games", dated_games)
    try:
        session = await host.create(SessionCreate(game_id=game.id, mode="live", date="20240905"))
        saved = host.journal.save.call_args.args[0]
        assert session.view()["date"] == saved["date"] == "20240905"
        host.journal.get.return_value = saved
        host.journal.events.return_value = []
        host.sessions.clear()
        restored = await host.get(session.id)
        assert restored.config.date == restored.view()["date"] == "20240905"
        assert restored.task is None and restored.turns == []
        regenerated = await host.create(SessionCreate(game_id=game.id, mode="live", date=restored.view()["date"]))
        assert regenerated.id != restored.id and regenerated.config.date == "20240905"
    finally:
        await host.provider.aclose()


@pytest.mark.asyncio
async def test_saved_runs_without_date_remain_readable():
    host = engine()
    try:
        session = await host.create(SessionCreate())
        saved = session.view()
        saved.pop("date")
        host.journal.get.return_value = saved
        host.journal.events.return_value = []
        host.sessions.clear()
        restored = await host.get(session.id)
        assert restored.view()["date"] is None and restored.task is None
    finally:
        await host.provider.aclose()


@pytest.mark.asyncio
async def test_simultaneous_saved_run_gets_share_listener_and_control_state(monkeypatch):
    host = engine()
    session = await host.create(SessionCreate())
    saved = session.view()
    host.sessions.clear()
    host.journal.events.return_value = []
    reads = 0

    async def delayed_read(sid):
        nonlocal reads
        reads += 1
        await asyncio.sleep(0)
        return saved

    monkeypatch.setattr(host.journal, "get", delayed_read)
    try:
        http_view, socket_view = await asyncio.gather(host.get(session.id), host.get(session.id))
        assert http_view is socket_view and reads == 1
        socket_view.listeners["connected-browser"] = asyncio.Queue()
        assert "connected-browser" in http_view.listeners
    finally:
        await host.provider.aclose()


@pytest.mark.asyncio
async def test_live_baseline_seeds_context_before_slow_source_persistence(monkeypatch):
    host = engine()
    session = await host.create(SessionCreate())
    session.config.mode = "live"
    plays = [play.model_copy(update={"source": "espn"}) for play in session.replay[:8]]
    writing = asyncio.Event()
    release = asyncio.Event()
    original_append = host.journal.append

    async def source_write(event):
        if event.type == "source.event" and not writing.is_set():
            writing.set()
            await release.wait()
        await original_append(event)

    monkeypatch.setattr(host.journal, "append", source_write)
    monkeypatch.setattr(host.provider, "events", AsyncMock(return_value=plays))
    poller = asyncio.create_task(host.live_poll(session))
    try:
        await writing.wait()
        assert session.pending[-1].id == plays[-1].id
        assert len(session.seen) == len(plays)
        session.cancel.set()
        release.set()
        poller.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poller
        snapshot = await host.make_snapshot({"session_id": session.id, "epoch": session.epoch,
                                             "play": plays[-1].model_dump()})
        assert [item["id"] for item in snapshot["snapshot"]["drive_history"]] == [play.id for play in plays[2:7]]
    finally:
        release.set()
        if not poller.done():
            poller.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poller
        await host.provider.aclose()


@pytest.mark.asyncio
async def test_late_live_poll_response_cannot_repopulate_paused_session(monkeypatch):
    host = engine()
    session = await host.create(SessionCreate())
    session.config.mode = "live"
    play = session.replay[4].model_copy(update={"source": "espn"})
    requested = asyncio.Event()
    release = asyncio.Event()

    async def delayed_feed(game_id):
        requested.set()
        await release.wait()
        return [play]

    monkeypatch.setattr(host.provider, "events", delayed_feed)
    poller = asyncio.create_task(host.live_poll(session))
    try:
        await requested.wait()
        await host.halt(session)
        before = session.seq
        release.set()
        await poller
        assert session.seq == before and not session.seen and not session.pending
        assert session.status == "paused"
    finally:
        await host.provider.aclose()


@pytest.mark.asyncio
async def test_old_live_correction_does_not_receive_later_commentary(monkeypatch):
    host = engine()
    session = await host.create(SessionCreate())
    session.config.mode = "live"
    plays = [play.model_copy(update={"source": "espn"}) for play in session.replay[:20]]
    session.seen = {play.id: play for play in plays}
    for index in (4, 18):
        snapshot = GameSnapshot(event=plays[index]).seal()
        session.turns.append(CommentaryTurn(exchange_id=str(index), agent_id="a", persona="Max", team_id="KC",
            event_id=plays[index].id, snapshot_hash=snapshot.hash, text=f"Call from play {index}").model_dump())
    try:
        revised = plays[6].model_copy(update={"revision": 2, "flags": ["correction"]})
        snapshot = await host.make_snapshot({"session_id": session.id, "epoch": session.epoch,
                                             "play": revised.model_dump()})
        assert [turn["text"] for turn in snapshot["snapshot"]["recent_turns"]] == ["Call from play 4"]
    finally:
        await host.provider.aclose()


@pytest.mark.asyncio
async def test_resume_does_not_repeat_already_completed_latest_live_play(monkeypatch):
    host = engine()
    session = await host.create(SessionCreate(voice_enabled=False))
    session.config.mode = "live"
    play = session.replay[4].model_copy(update={"source": "espn"})
    monkeypatch.setattr(host.provider, "events", AsyncMock(return_value=[play]))
    monkeypatch.setattr(host, "one", AsyncMock())
    try:
        await host.control(session, "step")
        await asyncio.wait_for(session.task, 2)
        assert session.commented_revisions[play.id] == play.revision
        await host.halt(session)
        session.cancel = asyncio.Event()

        async def same_feed(game_id):
            session.cancel.set()
            return [play]

        monkeypatch.setattr(host.provider, "events", same_feed)
        original_sleep = asyncio.sleep

        async def no_wait(seconds):
            await original_sleep(0)

        monkeypatch.setattr("backend.engine.asyncio.sleep", no_wait)
        await host.live_poll(session)
        assert not session.pending
        assert host.one.await_count == 1
    finally:
        await host.provider.aclose()


@pytest.mark.asyncio
async def test_final_live_game_finishes_after_latest_play_instead_of_polling_forever(monkeypatch):
    host = engine()
    session = await host.create(SessionCreate(voice_enabled=False))
    session.config.mode = "live"
    play = session.replay[-1].model_copy(update={"source": "espn"})
    host.provider._games[session.game.id] = session.game.model_copy(update={"mode": "live", "status": "final"})
    monkeypatch.setattr(host.provider, "events", AsyncMock(return_value=[play]))
    monkeypatch.setattr(host, "one", AsyncMock())
    original_sleep = asyncio.sleep

    async def no_wait(seconds):
        await original_sleep(0)

    monkeypatch.setattr("backend.engine.asyncio.sleep", no_wait)
    try:
        await host.control(session, "play")
        await asyncio.wait_for(session.task, 2)
        assert session.status == "completed" and host.one.await_count == 1
        assert session.feed["game_status"] == "final"
    finally:
        await host.provider.aclose()


@pytest.mark.asyncio
async def test_last_replay_single_step_completes_and_remains_complete_after_reopen(monkeypatch):
    host = engine()
    session = await host.create(SessionCreate(start_index=177, voice_enabled=False))
    monkeypatch.setattr(host, "one", AsyncMock())
    try:
        await host.control(session, "step")
        await session.task
        assert session.status == "completed" and session.index == session.game.play_count
        await host.close()
        saved = host.journal.save.call_args.args[0]
        assert saved["status"] == "completed"
        host.journal.get.return_value = saved
        host.journal.events.return_value = []
        host.sessions.clear()
        restored = await host.get(session.id)
        assert restored.status == "completed" and restored.task is None
    finally:
        await host.provider.aclose()


@pytest.mark.asyncio
async def test_crash_recovery_consumes_spoken_live_revision_but_keeps_unspoken_correction():
    host = engine()
    session = await host.create(SessionCreate(voice_enabled=False))
    session.config.mode = "live"
    saved = session.view()
    play = session.replay[4].model_copy(update={"source": "espn"})
    spoken = GameSnapshot(event=play).seal()
    correction = GameSnapshot(event=play.model_copy(update={"revision": 2, "flags": ["correction"]})).seal()
    turn = CommentaryTurn(exchange_id="crashed", agent_id="a", persona="Max", team_id="KC",
        event_id=play.id, snapshot_hash=spoken.hash, text="A recorded live call.")
    records = [("snapshot", {"snapshot": spoken.model_dump()}), ("turn", {"turn": turn.model_dump()}),
               ("snapshot", {"snapshot": correction.model_dump()})]
    host.journal.get.return_value = saved
    host.journal.events.return_value = [RunEvent(seq=saved["seq"] + index + 1, session_id=session.id,
        epoch=session.epoch, type=kind, data=data).model_dump() for index, (kind, data) in enumerate(records)]
    host.sessions.clear()
    try:
        restored = await host.get(session.id)
        assert restored.commented_revisions[play.id] == 1
        assert restored.snapshot["event"]["revision"] == 2
        assert restored.task is None and len(restored.turns) == 1
    finally:
        await host.provider.aclose()
