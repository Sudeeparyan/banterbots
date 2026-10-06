"""One causal broadcast at a time, with persistent LangGraph orchestration."""
from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, TypedDict

import structlog
from langgraph.graph import END, START, StateGraph

from backend.config import AGENT_A_URL, AGENT_B_URL
from backend.contracts import AgentRequest, Game, GameSnapshot, PlayEvent, RunEvent, SessionCreate, now, uid
from backend.journal import Journal
from backend.providers import ESPNProvider, get_replay_events, get_replay_games
from backend.security import redact

log = structlog.get_logger()


@dataclass
class Broadcast:
    id: str
    config: SessionCreate
    game: Game
    epoch: int = 1
    status: str = "paused"
    index: int = 0
    seq: int = 0
    exchanges: int = 0
    created_at: str = field(default_factory=now)
    snapshot: dict | None = None
    turns: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    replay: list[PlayEvent] = field(default_factory=list)
    feed: dict = field(default_factory=lambda: {"status": "replay", "last_success": None})
    metrics: dict = field(default_factory=dict)
    listeners: dict[str, asyncio.Queue] = field(default_factory=dict)
    player: str | None = None
    task: asyncio.Task | None = None
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    playback: dict[str, asyncio.Event] = field(default_factory=dict)
    last_segment: str | None = None
    history_start_seq: int = 0
    seen: dict[str, PlayEvent] = field(default_factory=dict)
    pending: list[PlayEvent] = field(default_factory=list)

    def view(self):
        return {"id": self.id, "epoch": self.epoch, "game": self.game.model_dump(),
                "mode": self.config.mode, "provider": self.config.provider, "status": self.status,
                "speed": self.config.speed, "index": self.index, "total": len(self.replay),
                "date": self.config.date,
                "voice_enabled": self.config.voice_enabled, "created_at": self.created_at,
                "snapshot": self.snapshot, "turns": self.turns, "events": self.events[-300:],
                "feed": self.feed, "metrics": self.metrics, "exchanges": self.exchanges,
                "start_index": self.config.start_index, "seq": self.seq, "history_start_seq": self.history_start_seq}


class GraphState(TypedDict, total=False):
    session_id: str
    epoch: int
    play: dict[str, Any]
    snapshot: dict[str, Any]
    lead: str
    exchange_id: str
    started: float
    turns: list[dict[str, Any]]


class Engine:
    def __init__(self, journal: Journal, checkpointer):
        self.journal = journal
        self.sessions: dict[str, Broadcast] = {}
        self.provider = ESPNProvider()
        self.active: str | None = None
        self.lifecycle = asyncio.Lock()
        graph = StateGraph(GraphState)
        for name, handler in [("normalize", self.normalize), ("snapshot", self.make_snapshot),
                              ("choose_lead", self.choose_lead), ("a2a_exchange", self.agent_exchange),
                              ("finalize", self.finalize)]:
            graph.add_node(name, handler)
        names = [START, "normalize", "snapshot", "choose_lead", "a2a_exchange", "finalize", END]
        for left, right in zip(names, names[1:]):
            graph.add_edge(left, right)
        self.graph = graph.compile(checkpointer=checkpointer)

    async def emit(self, session: Broadcast, kind: str, data: dict, epoch: int | None = None):
        if epoch is not None and epoch != session.epoch:
            return
        data = redact(data)
        session.seq += 1
        event = RunEvent(seq=session.seq, session_id=session.id, epoch=session.epoch, type=kind, data=data)
        encoded = event.model_dump()
        # Audio is transient; persist metadata and exact transcripts, not large PCM blobs.
        if kind != "audio":
            session.events.append(encoded)
            session.events = session.events[-500:]
            await self.journal.append(event)
        for queue in list(session.listeners.values()):
            if queue.full():
                # Disconnect slow consumers rather than dropping arbitrary audio samples.
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                queue.put_nowait({"type": "disconnect"})
            else:
                queue.put_nowait(encoded)
        if kind in {"trace", "error"}:
            log.info(kind, session_id=session.id, details={k: v for k, v in data.items() if k != "prompt"})

    async def state(self, session):
        await self.journal.save(session.view())
        await self.emit(session, "state", {k: v for k, v in session.view().items() if k != "events"})

    async def create(self, config: SessionCreate):
        if config.mode == "replay":
            game = next((g for g in get_replay_games() if g.id == config.game_id), None)
            if not game:
                raise ValueError("Unknown replay game")
            plays = get_replay_events(config.game_id)
            if config.start_index >= len(plays):
                raise ValueError("Replay start position is outside this game")
        else:
            games = await self.provider.games(config.date)
            game = next((g for g in games if g.id == config.game_id), None)
            if not game:
                raise ValueError("Live game is unavailable; refresh the live scoreboard")
            plays = []
        # Serialize broadcast ownership changes. API requests from rapid clicks
        # or multiple tabs can interleave while the previous graph is cancelled.
        async with self.lifecycle:
            return await self._create(config, game, plays)

    async def _create(self, config, game, plays):
        if self.active and self.active in self.sessions:
            await self.halt(self.sessions[self.active], "stopped")
        session = Broadcast(id=uid(), config=config, game=game, index=config.start_index, replay=plays)
        if plays:
            session.snapshot = GameSnapshot(event=plays[config.start_index]).seal().model_dump()
        else:
            session.feed = {"status": "idle", "last_success": None}
        self.sessions[session.id] = session
        self.active = session.id
        await self.state(session)
        return session

    async def get(self, sid):
        if sid in self.sessions:
            return self.sessions[sid]
        saved = await self.journal.get(sid)
        if not saved:
            raise KeyError(sid)
        config = SessionCreate(game_id=saved["game"]["id"], mode=saved["mode"], provider=saved["provider"],
                               speed=saved["speed"], start_index=saved.get("start_index", 0),
                               voice_enabled=saved.get("voice_enabled", True), date=saved.get("date"))
        session = Broadcast(id=sid, config=config, game=Game.model_validate(saved["game"]),
                            epoch=saved["epoch"] + 1, index=saved["index"],
                            created_at=saved["created_at"], snapshot=saved["snapshot"],
                            turns=saved["turns"], metrics=saved["metrics"],
                            exchanges=saved.get("exchanges", 0), feed=saved["feed"],
                            history_start_seq=saved.get("history_start_seq", 0),
                            replay=get_replay_events(config.game_id) if config.mode == "replay" else [])
        events = await self.journal.events(sid)
        session.events = events[-500:]
        session.seq = max([saved.get("seq", 0)] + [e["seq"] for e in events])
        # An artifact may be durable even if the process died before saving the
        # compact session view. Recover captured output without regenerating it.
        preserved = {turn["id"]: turn for turn in session.turns}
        recovered = {}
        for event in events:
            if event["seq"] >= session.history_start_seq and event["type"] == "turn":
                turn = event["data"]["turn"]
                recovered[turn["id"]] = preserved.get(turn["id"], turn)
        recovered.update({key: value for key, value in preserved.items() if key not in recovered})
        session.turns = list(recovered.values())
        known = set(recovered)
        for event in events:
            if event["seq"] <= saved.get("seq", 0):
                continue
            if event["type"] == "turn" and event["data"]["turn"]["id"] not in known:
                turn = event["data"]["turn"]
                known.add(turn["id"])
                session.turns.append(turn)
            elif event["type"] == "snapshot":
                session.snapshot = event["data"]["snapshot"]
            elif event["type"] == "metrics":
                session.metrics = event["data"]
            elif event["type"] == "state":
                session.index = max(session.index, event["data"].get("index", session.index))
        if session.replay and session.snapshot:
            play_id = session.snapshot["event"]["id"]
            captured = [turn for turn in session.turns if turn["event_id"] == play_id]
            position = next((i for i, play in enumerate(session.replay) if play.id == play_id), None)
            if captured and position is not None and session.index <= position:
                session.index = position + 1
                if len(captured) < 2:
                    for turn in captured:
                        turn["interrupted"] = True
        completed = {}
        for turn in session.turns:
            completed.setdefault(turn["exchange_id"], set()).add(turn["agent_id"])
        session.exchanges = max(session.exchanges, sum(agents == {"a", "b"} for agents in completed.values()))
        self.sessions[sid] = session
        return session

    async def halt(self, session, status="paused"):
        session.cancel.set()
        session.epoch += 1
        session.status = status
        if session.task and not session.task.done():
            session.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await session.task
        session.task = None
        session.pending.clear()
        if session.config.mode == "live":
            # Resume is a fresh live join: seed current context and announce the
            # latest play. Retaining the old dedup baseline after clearing the
            # pending queue could otherwise leave the resumed show silent.
            session.seen.clear()
        session.playback.clear()
        await self.state(session)

    async def control(self, session, action, speed=None):
        async with self.lifecycle:
            await self._control(session, action, speed)

    async def _control(self, session, action, speed=None):
        if action == "speed":
            if speed is None:
                raise ValueError("Provide a replay speed")
            session.config.speed = speed
            await self.state(session)
            return
        if action in {"pause", "stop", "restart"}:
            await self.halt(session, "stopped" if action == "stop" else "paused")
            if action == "restart":
                session.index = session.config.start_index
                session.exchanges = 0
                session.turns.clear()
                session.history_start_seq = session.seq + 1
                session.seen.clear()
                session.metrics.clear()
                session.snapshot = GameSnapshot(event=session.replay[session.index]).seal().model_dump() if session.replay else None
                await self.state(session)
            return
        if session.task and not session.task.done():
            if action == "step":
                raise ValueError("Pause the broadcast before single-stepping")
            return
        if self.active and self.active != session.id and self.active in self.sessions:
            await self.halt(self.sessions[self.active], "paused")
        self.active = session.id
        session.cancel = asyncio.Event()
        session.status = "playing" if action == "play" else "stepping"
        await self.state(session)
        session.task = asyncio.create_task(self.run(session, once=action == "step"))

    async def normalize(self, state):
        session = self.sessions[state["session_id"]]
        event = PlayEvent.model_validate(state["play"])
        await self.emit(session, "trace", {"node": "normalize", "phase": "complete", "event_id": event.id,
                        "revision": event.revision, "source": event.source}, state["epoch"])
        return {"play": event.model_dump()}

    async def make_snapshot(self, state):
        session = self.sessions[state["session_id"]]
        if state["epoch"] != session.epoch:
            raise asyncio.CancelledError
        event = PlayEvent.model_validate(state["play"])
        if session.config.mode == "live":
            # Joining a game seeds observed context, while a queued older play
            # must never see later plays already received by the poller.
            history = [p.model_dump() for p in sorted(session.seen.values(), key=lambda p: p.sequence)
                       if p.sequence < event.sequence][-5:]
        else:
            # Cancellation epochs identify output ownership, not game context.
            # Use earlier causal fixtures so pauses, saved-run reopening and
            # highlight jumps retain the same drive facts without future plays.
            history = [play.model_dump() for play in session.replay
                       if play.sequence < event.sequence][-5:]
        history = [{k: p.get(k) for k in ("id", "quarter", "clock", "description", "home_score", "away_score",
                                         "play_type", "flags")}
                   for p in history]
        snapshot = GameSnapshot(event=event, drive_history=history, recent_turns=session.turns[-8:]).seal()
        session.snapshot = snapshot.model_dump()
        await self.emit(session, "snapshot", {"snapshot": session.snapshot}, state["epoch"])
        await self.emit(session, "trace", {"node": "snapshot", "phase": "complete", "snapshot_hash": snapshot.hash}, state["epoch"])
        return {"snapshot": session.snapshot}

    async def choose_lead(self, state):
        session = self.sessions[state["session_id"]]
        lead = "a" if session.exchanges % 2 == 0 else "b"
        await self.emit(session, "trace", {"node": "choose_lead", "phase": "complete", "lead": lead,
                                          "exchange_id": state["exchange_id"]}, state["epoch"])
        return {"lead": lead}

    async def agent_exchange(self, state):
        from backend.a2a_client import exchange

        session = self.sessions[state["session_id"]]
        epoch = state["epoch"]
        session.last_segment = None

        async def forward(item):
            if epoch != session.epoch:
                return
            kind, data = item["type"], redact(item.get("data", {}))
            if kind == "caption.delta" and (data.get("delta") or data.get("text")):
                session.metrics.setdefault("first_text_ms", round((time.perf_counter() - state["started"]) * 1000))
            if kind == "turn":
                turn = data["turn"]
                if not any(t["id"] == turn["id"] for t in session.turns):
                    session.turns.append(turn)
                    session.last_segment = turn["id"]
                    session.playback.setdefault(turn["id"], asyncio.Event())
                    session.metrics.setdefault("first_text_ms", round((time.perf_counter() - state["started"]) * 1000))
            if kind == "segment.sealed":
                session.last_segment = data["segment_id"]
                session.playback.setdefault(data["segment_id"], asyncio.Event())
            if kind == "audio":
                session.metrics.setdefault("first_audio_ms", round((time.perf_counter() - state["started"]) * 1000))
            await self.emit(session, kind, data, epoch)

        payload = AgentRequest(session_id=session.id, epoch=epoch, exchange_id=state["exchange_id"],
                               snapshot=GameSnapshot.model_validate(state["snapshot"]), provider=session.config.provider,
                               voice_enabled=session.config.voice_enabled)
        await self.emit(session, "trace", {"node": "a2a_exchange", "phase": "start", "from": "coordinator",
                        "to": state["lead"], "snapshot_hash": payload.snapshot.hash}, epoch)
        turns = await asyncio.wait_for(exchange(AGENT_A_URL if state["lead"] == "a" else AGENT_B_URL,
                                    payload, forward, session.cancel), timeout=90)
        await self.emit(session, "trace", {"node": "a2a_exchange", "phase": "complete",
                        "exchange_id": state["exchange_id"], "snapshot_hash": payload.snapshot.hash,
                        "turn_count": len(turns)}, epoch)
        return {"turns": [t.model_dump() for t in turns]}

    async def finalize(self, state):
        session = self.sessions[state["session_id"]]
        if state["epoch"] != session.epoch:
            return {}
        session.exchanges += 1
        session.metrics["exchange_ms"] = round((time.perf_counter() - state["started"]) * 1000)
        session.metrics["snapshot_hash"] = state["snapshot"]["hash"]
        await self.emit(session, "metrics", session.metrics, state["epoch"])
        await self.emit(session, "trace", {"node": "finalize", "phase": "complete",
                        "exchange_id": state["exchange_id"], "turn_count": len(state["turns"])}, state["epoch"])
        await self.journal.save(session.view())
        return {}

    async def one(self, session, event):
        session.metrics = {}
        if event.occurred_at and session.config.mode == "live":
            with contextlib.suppress(ValueError, TypeError):
                age = datetime.fromisoformat(now()) - datetime.fromisoformat(event.occurred_at.replace("Z", "+00:00"))
                session.metrics["feed_delay_ms"] = max(0, round(age.total_seconds() * 1000))
        state = {"session_id": session.id, "epoch": session.epoch, "play": event.model_dump(),
                 "exchange_id": uid(), "started": time.perf_counter()}
        await self.graph.ainvoke(state, {"configurable": {"thread_id": f"{session.id}:{session.epoch}"}})

    async def wait_playback(self, session):
        if session.player and session.config.voice_enabled and session.last_segment:
            event = session.playback.setdefault(session.last_segment, asyncio.Event())
            try:
                await asyncio.wait_for(event.wait(), timeout=30)
            except TimeoutError:
                await self.emit(session, "trace", {"node": "playback", "phase": "timeout", "segment_id": session.last_segment})

    async def live_poll(self, session):
        while not session.cancel.is_set():
            try:
                events = await self.provider.events(session.game.id)
                session.feed = {"status": "connected", "last_success": now(),
                                "latest_play_at": events[-1].occurred_at if events else None,
                                "source": self.provider.last_source, "warning": self.provider.last_error}
                if not session.seen:
                    session.pending.extend(events[-1:])
                    for event in events:
                        await self.emit(session, "source.event", {"event": event.model_dump(), "baseline": True})
                else:
                    for event in events:
                        previous = session.seen.get(event.id)
                        if previous is None or event.revision != previous.revision:
                            await self.emit(session, "source.event", {"event": event.model_dump(), "baseline": False})
                            if previous:
                                event.flags = list(set(event.flags + ["correction"]))
                            session.pending = [queued for queued in session.pending if queued.id != event.id]
                            session.pending.append(event)
                session.seen.update({e.id: e for e in events})
                session.pending.sort(key=lambda event: event.sequence)
                if len(session.pending) > 32:
                    important = [p for p in session.pending if set(p.flags) & {"scoring", "touchdown", "turnover", "interception", "fumble_lost", "penalty", "correction"}]
                    newest = session.pending[-8:]
                    retained = {p.id: p for p in important + newest}
                    skipped_ids = [p.id for p in session.pending if p.id not in retained]
                    session.pending = sorted(retained.values(), key=lambda e: e.sequence)
                    await self.emit(session, "trace", {"node": "backpressure", "phase": "coalesced",
                                    "skipped": len(skipped_ids), "skipped_event_ids": skipped_ids,
                                    "reason": "Routine stale commentary coalesced; important plays retained."})
            except Exception as exc:
                session.feed = {**session.feed, "status": "error", "error": str(exc)}
                await self.emit(session, "error", {"component": "live_feed", "message": str(exc), "recoverable": True})
            await self.emit(session, "feed", session.feed)
            await asyncio.sleep(5 if session.feed["status"] == "connected" else 10)

    async def run(self, session, once=False):
        poller = None
        epoch = session.epoch
        current = None
        prior_turns = len(session.turns)
        consumed = False
        try:
            if session.config.mode == "live":
                poller = asyncio.create_task(self.live_poll(session))
            while not session.cancel.is_set():
                if session.config.mode == "replay":
                    if session.index >= len(session.replay):
                        session.status = "completed"
                        break
                    event = session.replay[session.index]
                else:
                    if not session.pending:
                        await asyncio.sleep(0.2)
                        continue
                    event = session.pending.pop(0)
                current = event
                prior_turns = len(session.turns)
                consumed = False
                await self.one(session, event)
                session.index += 1
                consumed = True
                await self.state(session)
                if once:
                    await self.wait_playback(session)
                    session.status = "paused"
                    break
                await self.wait_playback(session)
                await asyncio.sleep(max(0.5, 4 / session.config.speed))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            session.status = "error"
            await self.emit(session, "error", {"component": "orchestrator", "message": str(exc), "recoverable": True}, epoch)
            log.exception("broadcast_failed", session_id=session.id)
        finally:
            if current and not consumed:
                published = session.turns[prior_turns:]
                if published:
                    # Preserve audible history and explicitly consume an interrupted
                    # partial exchange instead of silently repeating the lead call.
                    for turn in published:
                        turn["interrupted"] = True
                    session.index += 1
                    await self.emit(session, "trace", {"node": "recovery", "phase": "interrupted",
                                    "event_id": current.id, "published_turns": len(published),
                                    "message": "Partial exchange retained; the next play resumes without repeating speech."})
                elif session.config.mode == "live":
                    session.pending.insert(0, current)
            if poller:
                poller.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await poller
            if epoch == session.epoch:
                await self.state(session)

    async def close(self):
        for session in list(self.sessions.values()):
            await self.halt(session)
        await self.provider.aclose()
