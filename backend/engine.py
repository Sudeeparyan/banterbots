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

from backend.commentary import game_stats, presnap_event
from backend.config import AGENT_A_URL, AGENT_B_URL
from backend.contracts import AgentRequest, Game, GameSnapshot, PlayEvent, RunEvent, SessionCreate, now, uid
from backend.journal import Journal
from backend.providers import ESPNProvider, get_replay_events, get_replay_games
from backend.security import redact

log = structlog.get_logger()
# Snaps that get a pre-snap setup line in replay (nflverse situations are pre-snap).
SETUP_KINDS = {"run", "pass", "punt", "field_goal", "qb_kneel", "qb_spike", "no_play"}
FRAME_KINDS = ("game_start", "period_end", "game_end")
MEMORY_SIZE = 12


def public_poll(poll: dict | None) -> dict | None:
    """A poll as listeners may see it: never the result the studio already knows."""
    return {key: value for key, value in poll.items() if key != "converted"} if poll else None


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
    # The newest segment sent to the browser; screen events wait until it has been heard.
    screen_after: str | None = None
    # Pre-snap setups still being spoken, by the exchange that calls their play.
    setup_runs: dict[str, dict] = field(default_factory=dict)
    # The next play's setup, started while this play's reply is on air.
    prefetch: asyncio.Task | None = None
    history_start_seq: int = 0
    seen: dict[str, PlayEvent] = field(default_factory=dict)
    pending: list[PlayEvent] = field(default_factory=list)
    commented_revisions: dict[str, int] = field(default_factory=dict)
    emit_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Remarks worth calling back to and predictions with their outcomes; causal by construction.
    memory: list[dict] = field(default_factory=list)
    # The listener's side, reactions since the last play, poll vote and queued questions.
    listener: dict = field(default_factory=lambda: {"side": None, "cheers": 0, "boos": 0, "poll": None,
                                                    "questions": [], "resolved_poll": None})
    # Segments whose playback the listener actually completed.
    heard: set[str] = field(default_factory=set)

    def view(self):
        return {"id": self.id, "epoch": self.epoch, "game": self.game.model_dump(),
                "listener": {"side": self.listener.get("side"), "poll": public_poll(self.listener.get("poll"))},
                "mode": self.config.mode, "provider": self.config.provider, "status": self.status,
                "speed": self.config.speed, "index": self.index, "total": len(self.replay),
                "date": self.config.date,
                "voice_enabled": self.config.voice_enabled, "created_at": self.created_at,
                "snapshot": self.snapshot, "turns": self.turns, "events": self.events[-300:],
                "feed": self.feed, "metrics": self.metrics, "exchanges": self.exchanges,
                "commented_revisions": self.commented_revisions,
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
        self.restore_lock = asyncio.Lock()
        graph = StateGraph(GraphState)
        for name, handler in [("normalize", self.normalize), ("choose_lead", self.choose_lead),
                              ("setup", self.setup), ("snapshot", self.make_snapshot),
                              ("a2a_exchange", self.agent_exchange), ("segments", self.segments),
                              ("finalize", self.finalize)]:
            graph.add_node(name, handler)
        # The pre-snap setup is generated and published before the play's result snapshot.
        names = [START, "normalize", "choose_lead", "setup", "snapshot", "a2a_exchange", "segments", "finalize", END]
        for left, right in zip(names, names[1:]):
            graph.add_edge(left, right)
        self.graph = graph.compile(checkpointer=checkpointer)

    async def emit(self, session: Broadcast, kind: str, data: dict, epoch: int | None = None):
        # Polling, graph output and playback acknowledgements share one stream.
        # A slower SQLite write must not let a later sequence overtake its facts.
        async with session.emit_lock:
            await self._emit(session, kind, data, epoch)

    async def _emit(self, session: Broadcast, kind: str, data: dict, epoch: int | None = None):
        if epoch is not None and epoch != session.epoch:
            return
        data = redact(data)
        if kind != "audio" and session.screen_after and "present_after" not in data:
            # The next play is prepared while this one is heard: the browser shows
            # results and captions only after the audio they follow has played.
            data = {**data, "present_after": session.screen_after}
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
        # HTTP and WebSocket reconnects can request the same saved run together.
        # Restore one shared object so listeners and controls cannot diverge.
        async with self.restore_lock:
            return await self._get(sid)

    async def _get(self, sid):
        if sid in self.sessions:
            return self.sessions[sid]
        saved = await self.journal.get(sid)
        if not saved:
            raise KeyError(sid)
        # Runs saved before the Azure OpenAI switch recorded the provider as "openai".
        provider = "azure" if saved["provider"] == "openai" else saved["provider"]
        config = SessionCreate(game_id=saved["game"]["id"], mode=saved["mode"], provider=provider,
                               speed=saved["speed"], start_index=saved.get("start_index", 0),
                               voice_enabled=saved.get("voice_enabled", True), date=saved.get("date"))
        session = Broadcast(id=sid, config=config, game=Game.model_validate(saved["game"]),
                            epoch=saved["epoch"] + 1, index=saved["index"],
                            status=saved["status"] if saved["status"] in {"completed", "stopped", "error"} else "paused",
                            created_at=saved["created_at"], snapshot=saved["snapshot"],
                            turns=saved["turns"], metrics=saved["metrics"],
                            exchanges=saved.get("exchanges", 0), feed=saved["feed"],
                            commented_revisions=saved.get("commented_revisions", {}),
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
        if config.mode == "live":
            snapshots = {event["data"]["snapshot"]["hash"]: event["data"]["snapshot"]["event"]
                         for event in events if event["type"] == "snapshot" and event["seq"] >= session.history_start_seq}
            for turn in session.turns:
                captured = snapshots.get(turn["snapshot_hash"])
                if captured:
                    session.commented_revisions[captured["id"]] = max(
                        session.commented_revisions.get(captured["id"], 0), captured.get("revision", 1))
        self.sessions[sid] = session
        return session

    async def halt(self, session, status="paused"):
        session.cancel.set()
        session.epoch += 1
        session.status = status
        self.drop_setups(session)
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
                session.memory.clear()
                session.heard.clear()
                session.screen_after = None
                session.history_start_seq = session.seq + 1
                session.seen.clear()
                session.commented_revisions.clear()
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

    def context(self, session, event: PlayEvent) -> tuple[list[dict], list[dict]]:
        """Earlier plays and earlier dialogue only; an interrupted, unheard line is not context."""
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
        known = session.seen.values() if session.config.mode == "live" else session.replay
        sequence_by_id = {play.id: play.sequence for play in known}
        recent_turns = [turn for turn in session.turns
                        if sequence_by_id.get(turn["event_id"], event.sequence + 1) <= event.sequence
                        and not (turn.get("interrupted") and turn["event_id"] == event.id)][-8:]
        return history, recent_turns

    def booth(self, session, event: PlayEvent, phase: str, topic: str | None = None) -> dict:
        """Booth memory, listener input and (for segments) totals of plays already completed."""
        booth: dict = {}
        if session.memory:
            booth["memory"] = session.memory[-MEMORY_SIZE:]
        listener = session.listener
        poll = listener.get("poll")
        if poll and "converted" in poll and poll.get("pick"):
            listener["resolved_poll"] = {"question": poll.get("question"), "pick": poll["pick"],
                                         "outcome": "right" if (poll["pick"] == "convert") == poll["converted"] else "wrong"}
            listener["poll"] = None
        heard = {key: listener[key] for key in ("side", "cheers", "boos") if listener.get(key)}
        if listener.get("resolved_poll"):
            heard["poll"] = listener["resolved_poll"]
        if topic == "listener_question" and listener.get("asking"):
            heard["question"] = listener["asking"]
        if heard:
            booth["listener"] = heard
        if phase == "segment":
            known = sorted(session.seen.values(), key=lambda play: play.sequence) if session.config.mode == "live" else session.replay
            booth["stats"] = game_stats(list(known), event)
        return booth

    async def make_snapshot(self, state):
        session = self.sessions[state["session_id"]]
        if state["epoch"] != session.epoch:
            raise asyncio.CancelledError
        event = PlayEvent.model_validate(state["play"])
        history, recent_turns = self.context(session, event)
        snapshot = GameSnapshot(event=event, drive_history=history, recent_turns=recent_turns,
                                booth=self.booth(session, event, "result")).seal()
        session.snapshot = snapshot.model_dump()
        await self.emit(session, "snapshot", {"snapshot": session.snapshot}, state["epoch"])
        await self.emit(session, "trace", {"node": "snapshot", "phase": "complete", "snapshot_hash": snapshot.hash}, state["epoch"])
        return {"snapshot": session.snapshot}

    @staticmethod
    def framing(event: PlayEvent) -> bool:
        return any(kind in event.play_type.lower() for kind in FRAME_KINDS) or event.description.strip().upper() == "GAME"

    def lead_for(self, session, event: PlayEvent) -> str:
        """Whoever's team has the ball calls the play-by-play; framing moments alternate."""
        team = event.offense_team or event.possession
        if team in {event.home_team, event.away_team} and not self.framing(event):
            return "a" if team == event.home_team else "b"
        return "a" if session.exchanges % 2 == 0 else "b"

    async def choose_lead(self, state):
        session = self.sessions[state["session_id"]]
        lead = self.lead_for(session, PlayEvent.model_validate(state["play"]))
        await self.emit(session, "trace", {"node": "choose_lead", "phase": "complete", "lead": lead,
                                          "exchange_id": state["exchange_id"]}, state["epoch"])
        return {"lead": lead}

    def forwarder(self, session, state, reply_on_air: asyncio.Event | None = None):
        epoch = state["epoch"]

        async def forward(item):
            if epoch != session.epoch:
                return
            kind, data = item["type"], redact(item.get("data", {}))
            if kind == "audio" and reply_on_air is not None and data.get("agent_id") not in (None, state.get("lead")):
                reply_on_air.set()
            if kind == "caption.delta" and (data.get("delta") or data.get("text")):
                session.metrics.setdefault("first_text_ms", round((time.perf_counter() - state["started"]) * 1000))
            if kind == "turn":
                turn = data["turn"]
                if not any(t["id"] == turn["id"] for t in session.turns):
                    session.turns.append(turn)
                    session.last_segment = turn["id"]
                    session.screen_after = turn["id"]
                    session.playback.setdefault(turn["id"], asyncio.Event())
                    session.metrics.setdefault("first_text_ms", round((time.perf_counter() - state["started"]) * 1000))
            if kind == "segment.sealed":
                session.last_segment = data["segment_id"]
                session.screen_after = data["segment_id"]
                session.playback.setdefault(data["segment_id"], asyncio.Event())
            if kind == "audio":
                session.metrics.setdefault("first_audio_ms", round((time.perf_counter() - state["started"]) * 1000))
            await self.emit(session, kind, data, epoch)

        return forward

    def remember(self, session, turns) -> None:
        """Keep remarks a host marked as worth a callback, and every prediction."""
        for turn in turns:
            if not (turn.bit or turn.prediction):
                continue
            session.memory.append({"id": f"m{len(session.memory) + 1}", "agent_id": turn.agent_id, "persona": turn.persona,
                                   "text": turn.spoken_text or turn.text, "bit": turn.bit, "prediction": turn.prediction,
                                   "event_id": turn.event_id})
        del session.memory[:-40]

    def resolve(self, session, event: PlayEvent) -> None:
        """Score predictions and the listener's poll once the play's result is known."""
        flags = set(event.flags)
        converted = bool(flags & {"first_down", "touchdown"}) and not flags & {"reversed", "no_play", "play_deleted"}
        for item in session.memory:
            if item.get("event_id") == event.id and item.get("prediction") and "outcome" not in item:
                expected = {"convert": converted, "stopped": not converted, "score": "scoring" in flags}.get(item["prediction"])
                if expected is not None:
                    item["outcome"] = "right" if expected else "wrong"
        poll = session.listener.get("poll")
        if poll and poll.get("event_id") == event.id:
            # The studio knows the result now, but the listener may still be hearing the
            # setup line: votes stay open, and the next break scores them.
            poll["converted"] = converted

    def wants_setup(self, session, event: PlayEvent) -> bool:
        if session.config.mode != "replay" or event.source != "nflverse" or event.down not in (1, 2, 3, 4):
            return False
        if event.play_type not in SETUP_KINDS:
            return False
        speed = session.config.speed
        critical = event.down in (3, 4) or (event.yardline is not None and event.yardline <= 20)
        return speed <= 2 or (speed <= 4 and critical)

    async def setup(self, state):
        """A pre-snap line from a snapshot that cannot contain the result.

        The line is written and spoken while the call is written: the graph moves on as
        soon as the setup's voice segment exists, and the result waits behind it.
        """
        session = self.sessions[state["session_id"]]
        if state["epoch"] != session.epoch:
            raise asyncio.CancelledError
        session.last_segment = None
        event = PlayEvent.model_validate(state["play"])
        run = await self.adopt_prefetch(session, event, state["epoch"])
        if run is None:
            if not self.wants_setup(session, event):
                return {}
            run = await self.start_setup(session, state["epoch"], event, state["lead"])
        session.setup_runs[state["exchange_id"]] = run
        await run["written"].wait()
        if run.get("segment"):
            # The result, its captions and its poll result appear once the setup has been heard.
            session.screen_after = run["segment"]
        return {}

    async def start_setup(self, session, epoch: int, event: PlayEvent, lead: str) -> dict:
        """Open the poll and send the pre-snap line; returns at once with the running exchange."""
        from backend.a2a_client import exchange

        previous = max((play for play in session.replay if play.sequence < event.sequence),
                       key=lambda play: play.sequence, default=None)
        history, recent_turns = self.context(session, event)
        snapshot = GameSnapshot(event=presnap_event(event, previous), drive_history=history, recent_turns=recent_turns,
                                phase="pre_snap", booth=self.booth(session, event, "pre_snap")).seal()
        payload = AgentRequest(session_id=session.id, epoch=epoch, exchange_id=uid(), snapshot=snapshot,
                               provider=session.config.provider, voice_enabled=session.config.voice_enabled,
                               mode="setup", hop_budget=0)
        if event.down in (3, 4):
            # A prediction poll for the listener, open while the setup is heard.
            question = "Do they convert?"
            session.listener["poll"] = {"event_id": event.id, "question": question, "pick": None}
            await self.emit(session, "poll.open", {"event_id": event.id, "question": question,
                                                  "options": [{"id": "convert", "label": "They convert"},
                                                              {"id": "stopped", "label": "They're stopped"}],
                                                  "down": event.down, "distance": event.distance}, epoch)
        run: dict = {"event_id": event.id, "epoch": epoch, "lead": lead, "exchange_id": payload.exchange_id,
                     "written": asyncio.Event(), "segment": None}
        base = self.forwarder(session, {"epoch": epoch, "lead": lead, "started": time.perf_counter()})

        async def forward(item):
            if item.get("type") == "turn.start" and not run["written"].is_set():
                run["segment"] = item.get("data", {}).get("segment_id")
                run["written"].set()
            await base(item)

        async def speak() -> list:
            try:
                turns = await asyncio.wait_for(exchange(AGENT_A_URL if lead == "a" else AGENT_B_URL, payload,
                                                        forward, session.cancel), timeout=45)
                if epoch == session.epoch:
                    self.remember(session, turns)
                return turns
            except (asyncio.CancelledError, KeyboardInterrupt):
                raise
            except Exception as exc:
                # The setup is atmosphere; the play itself must still be called.
                await self.emit(session, "trace", {"node": "setup", "phase": "skipped", "event_id": event.id,
                                                  "reason": redact(str(exc))}, epoch)
                return []
            finally:
                run["written"].set()

        run["task"] = asyncio.create_task(speak())
        return run

    async def adopt_prefetch(self, session, event: PlayEvent, epoch: int) -> dict | None:
        """Take over the setup started during the previous play's reply, if it is for this play."""
        task, session.prefetch = session.prefetch, None
        if task is None:
            return None
        try:
            run = await task
        except (asyncio.CancelledError, Exception):
            return None
        if run is None or run["event_id"] != event.id or run["epoch"] != epoch:
            if run is not None:
                run["task"].cancel()
            return None
        return run

    def drop_setups(self, session) -> None:
        """Cancel setups still in flight; anything they published is marked as cut."""
        runs = list(session.setup_runs.values())
        session.setup_runs.clear()
        if session.prefetch is not None:
            session.prefetch.cancel()
            session.prefetch = None
        for run in runs:
            run["task"].cancel()

    def next_play(self, session, event: PlayEvent) -> PlayEvent | None:
        if session.config.mode != "replay":
            return None
        index = next((i for i, play in enumerate(session.replay) if play.id == event.id), None)
        return session.replay[index + 1] if index is not None and index + 1 < len(session.replay) else None

    async def prefetch_setup(self, session, epoch: int, event: PlayEvent, upcoming: PlayEvent,
                             on_air: asyncio.Event, exchanged: asyncio.Task) -> dict | None:
        """Start the next play's setup once this play's reply is on air, so no silence follows it."""
        waiter = asyncio.create_task(on_air.wait())
        try:
            await asyncio.wait({waiter, exchanged}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
        if epoch != session.epoch or (exchanged.done() and (exchanged.cancelled() or exchanged.exception())):
            return None
        # This play's result is settled: its poll is scored before the next one opens.
        self.resolve(session, event)
        await self.emit(session, "trace", {"node": "setup", "phase": "prefetch", "event_id": upcoming.id}, epoch)
        return await self.start_setup(session, epoch, upcoming, self.lead_for(session, upcoming))

    def wants_prefetch(self, session, event: PlayEvent) -> PlayEvent | None:
        """Only a voiced, playing replay prepares ahead, and never past a segment that must come first."""
        if (session.status != "playing" or session.config.provider != "azure" or not session.config.voice_enabled
                or self.segment_topics(session, event)):
            return None
        upcoming = self.next_play(session, event)
        return upcoming if upcoming and not self.framing(upcoming) and self.wants_setup(session, upcoming) else None

    def segment_topics(self, session, event: PlayEvent) -> list[str]:
        """Between-play talk the completed play makes natural; fewer at high replay speed."""
        kind = event.play_type.lower()
        flags = set(event.flags)
        speed = session.config.speed
        topics: list[str] = []
        if event.description.strip().upper() == "GAME" or "game_start" in kind:
            topics.append("intro")
        elif "game_end" in kind:
            topics.append("final")
        elif "period_end" in kind and event.quarter in (1, 2, 3):
            topics.append("halftime" if event.quarter == 2 else "quarter")
        elif speed <= 2 and event.drive is not None:
            ended = (kind in {"extra_point", "field_goal", "punt"} or "two_point" in kind
                     or bool(flags & {"interception", "fumble_lost", "safety"})
                     or (event.down == 4 and kind in {"run", "pass"} and not flags & {"first_down", "touchdown", "penalty"}))
            if ended:
                known = session.replay if session.config.mode == "replay" else list(session.seen.values())
                drive = game_stats(sorted(known, key=lambda play: play.sequence), event).get("drive", {})
                if drive.get("plays", 0) >= 4 or flags & {"interception", "fumble_lost"} or kind in {"extra_point", "field_goal"} or "two_point" in kind:
                    topics.append("drive_summary")
        if (session.listener.get("questions") and speed <= 4 and "intro" not in topics
                and session.prefetch is None):
            topics.append("listener_question")
        return topics

    def segment_lead(self, session, event: PlayEvent, topic: str) -> str:
        if topic == "intro":
            return "a"
        if topic == "listener_question":
            side = session.listener.get("side")
            return "a" if side == "home" else "b" if side == "away" else ("a" if session.exchanges % 2 == 0 else "b")
        if topic == "drive_summary":
            drive_team = event.offense_team or event.possession
            return "b" if drive_team == event.home_team else "a"
        if event.home_score != event.away_score:
            return "a" if event.home_score < event.away_score else "b"
        return "a" if session.exchanges % 2 == 0 else "b"

    async def segments(self, state):
        from backend.a2a_client import exchange

        session = self.sessions[state["session_id"]]
        if state["epoch"] != session.epoch:
            raise asyncio.CancelledError
        event = PlayEvent.model_validate(state["play"])
        turns = list(state.get("turns", []))
        for topic in self.segment_topics(session, event):
            if topic == "listener_question":
                session.listener["asking"] = session.listener["questions"].pop(0)
            history, recent_turns = self.context(session, event)
            snapshot = GameSnapshot(event=event, drive_history=history, recent_turns=recent_turns, phase="segment",
                                    booth=self.booth(session, event, "segment", topic)).seal()
            payload = AgentRequest(session_id=session.id, epoch=state["epoch"], exchange_id=uid(), snapshot=snapshot,
                                   provider=session.config.provider, voice_enabled=session.config.voice_enabled,
                                   mode="segment", topic=topic)
            lead = self.segment_lead(session, event, topic)
            try:
                produced = await asyncio.wait_for(exchange(AGENT_A_URL if lead == "a" else AGENT_B_URL, payload,
                                                           self.forwarder(session, state), session.cancel), timeout=90)
                self.remember(session, produced)
                turns.extend(turn.model_dump() for turn in produced)
            except (asyncio.CancelledError, KeyboardInterrupt):
                raise
            except Exception as exc:
                await self.emit(session, "trace", {"node": "segments", "phase": "skipped", "topic": topic,
                                                  "reason": redact(str(exc))}, state["epoch"])
            finally:
                session.listener.pop("asking", None)
        return {"turns": turns}

    async def agent_exchange(self, state):
        from backend.a2a_client import exchange

        session = self.sessions[state["session_id"]]
        epoch = state["epoch"]
        event = PlayEvent.model_validate(state["play"])
        if self.framing(event):
            # Game start, quarter ends and the final whistle are segments, not play calls.
            self.resolve(session, event)
            return {"turns": []}
        on_air = asyncio.Event()
        forward = self.forwarder(session, state, on_air)
        payload = AgentRequest(session_id=session.id, epoch=epoch, exchange_id=state["exchange_id"],
                               snapshot=GameSnapshot.model_validate(state["snapshot"]), provider=session.config.provider,
                               voice_enabled=session.config.voice_enabled)
        await self.emit(session, "trace", {"node": "a2a_exchange", "phase": "start", "from": "coordinator",
                        "to": state["lead"], "snapshot_hash": payload.snapshot.hash}, epoch)
        exchanged = asyncio.create_task(asyncio.wait_for(exchange(AGENT_A_URL if state["lead"] == "a" else AGENT_B_URL,
                                                                  payload, forward, session.cancel), timeout=90))
        upcoming = self.wants_prefetch(session, event)
        if upcoming is not None and session.prefetch is None:
            session.prefetch = asyncio.create_task(self.prefetch_setup(session, epoch, event, upcoming, on_air, exchanged))
        setup = session.setup_runs.pop(state["exchange_id"], None)
        try:
            turns = await exchanged
            if setup is not None:
                # Spoken before the call (one voice per host), so this is already settled.
                await setup["task"]
        except BaseException:
            exchanged.cancel()
            if setup is not None:
                setup["task"].cancel()
            raise
        self.remember(session, turns)
        self.resolve(session, event)
        # Reactions answered by this play are spent; new ones gather for the next.
        session.listener.update(cheers=0, boos=0)
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

    async def wait_playback(self, session, segment: str | None = None):
        segment = segment or session.last_segment
        if session.player and session.config.voice_enabled and segment:
            event = session.playback.setdefault(segment, asyncio.Event())
            try:
                # The acknowledgement is for the peer, after both utterances.
                # Browser voices may legitimately take 25 seconds each.
                await asyncio.wait_for(event.wait(), timeout=60)
            except TimeoutError:
                await self.emit(session, "trace", {"node": "playback", "phase": "timeout", "segment_id": segment})

    async def live_poll(self, session):
        epoch = session.epoch
        while not session.cancel.is_set():
            try:
                events = await self.provider.events(session.game.id)
                if epoch != session.epoch:
                    return
                current_game = self.provider.known_game(session.game.id)
                if current_game is not None:
                    session.game = current_game
                session.feed = {"status": "connected", "last_success": now(),
                                "latest_play_at": events[-1].occurred_at if events else None,
                                "source": self.provider.last_source, "warning": self.provider.last_error,
                                "game_status": session.game.status}
                baseline = not session.seen
                source_updates = []
                if baseline:
                    source_updates = [(event, True) for event in events]
                else:
                    for event in events:
                        previous = session.seen.get(event.id)
                        if previous is None or event.revision != previous.revision:
                            if previous:
                                event.flags = list(set(event.flags + ["correction"]))
                            source_updates.append((event, False))
                            session.pending = [queued for queued in session.pending if queued.id != event.id]
                            session.pending.append(event)
                # Seed all observed facts before exposing the latest play to the
                # consumer. Persisting baseline rows can otherwise yield and
                # let commentary start with an empty drive-history snapshot.
                session.seen.update({e.id: e for e in events})
                if baseline:
                    session.pending.extend(event for event in events[-1:]
                                           if session.commented_revisions.get(event.id) != event.revision)
                session.pending.sort(key=lambda event: event.sequence)
                if len(session.pending) > 32:
                    important = [p for p in session.pending if set(p.flags) & {"scoring", "touchdown", "turnover", "interception", "fumble_lost", "penalty", "correction"}]
                    newest = session.pending[-8:]
                    retained = {p.id: p for p in important + newest}
                    skipped_ids = [p.id for p in session.pending if p.id not in retained]
                    session.pending = sorted(retained.values(), key=lambda e: e.sequence)
                    await self.emit(session, "trace", {"node": "backpressure", "phase": "coalesced",
                                    "skipped": len(skipped_ids), "skipped_event_ids": skipped_ids,
                                    "reason": "Routine stale commentary coalesced; important plays retained."}, epoch)
                for event, is_baseline in source_updates:
                    await self.emit(session, "source.event", {"event": event.model_dump(), "baseline": is_baseline}, epoch)
                    if epoch != session.epoch:
                        return
            except Exception as exc:
                if epoch != session.epoch:
                    return
                session.feed = {**session.feed, "status": "error", "error": str(exc)}
                await self.emit(session, "error", {"component": "live_feed", "message": str(exc), "recoverable": True}, epoch)
            await self.emit(session, "feed", session.feed, epoch)
            await asyncio.sleep(5 if session.feed["status"] == "connected" else 10)

    async def run(self, session, once=False):
        poller = None
        epoch = session.epoch
        current = None
        prior_turns = len(session.turns)
        consumed = False
        # The last segment of the previous play: the next play is prepared while
        # this one is heard, but never more than one play ahead of the listener.
        ahead: str | None = None
        try:
            if session.config.mode == "live":
                poller = asyncio.create_task(self.live_poll(session))
            while not session.cancel.is_set():
                if session.config.mode == "replay":
                    if session.index >= len(session.replay):
                        await self.wait_playback(session, ahead)
                        session.status = "completed"
                        break
                    event = session.replay[session.index]
                else:
                    if not session.pending:
                        if session.game.status == "final" and session.feed["status"] == "connected":
                            session.status = "completed"
                            break
                        await asyncio.sleep(0.2)
                        continue
                    event = session.pending.pop(0)
                current = event
                prior_turns = len(session.turns)
                consumed = False
                await self.one(session, event)
                if session.config.mode == "live":
                    session.commented_revisions[event.id] = event.revision
                session.index += 1
                consumed = True
                await self.state(session)
                if once:
                    await self.wait_playback(session)
                    finished = session.config.mode == "replay" and session.index >= len(session.replay)
                    finished = finished or (session.config.mode == "live" and session.game.status == "final" and not session.pending)
                    session.status = "completed" if finished else "paused"
                    break
                if ahead:
                    await self.wait_playback(session, ahead)
                ahead = session.last_segment
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            session.status = "error"
            await self.emit(session, "error", {"component": "orchestrator", "message": str(exc), "recoverable": True}, epoch)
            log.exception("broadcast_failed", session_id=session.id)
        finally:
            if current and not consumed:
                published = session.turns[prior_turns:]
                unheard = session.config.mode == "replay" and not any(turn["id"] in session.heard for turn in published)
                if published and unheard:
                    # Prepared ahead but never played: keep the transcript, mark it cut,
                    # and call the play again on resume rather than skipping it.
                    for turn in published:
                        turn["interrupted"] = True
                    await self.emit(session, "trace", {"node": "recovery", "phase": "unheard",
                                    "event_id": current.id, "published_turns": len(published),
                                    "message": "Prepared commentary was never played; the play resumes from its start."})
                elif published:
                    if session.config.mode == "live":
                        session.commented_revisions[current.id] = current.revision
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
            await self.halt(session, session.status if session.status in {"completed", "stopped", "error"} else "paused")
        await self.provider.aclose()
