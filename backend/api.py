"""Dashboard API, run journal and ordered browser streams."""
import asyncio
import contextlib
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
import structlog
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from backend.config import AGENT_A_URL, AGENT_B_URL, ROOT, RUNTIME, TEXT_MODEL, VOICE_MODEL, azure_configured
from backend.commentary import load_persona
from backend.contracts import ListenerInput, SessionControl, SessionCreate, uid
from backend.engine import Engine, public_poll
from backend.journal import Journal
from backend.providers import get_replay_games, get_replay_highlights, get_teams

structlog.configure(processors=[structlog.processors.TimeStamper(fmt="iso"), structlog.processors.JSONRenderer()])


@asynccontextmanager
async def lifespan(app):
    journal = Journal()
    await journal.open()
    async with AsyncSqliteSaver.from_conn_string(str(RUNTIME / "coordinator-checkpoints.sqlite")) as saver:
        app.state.engine = Engine(journal, saver)
        app.state.live_cache = {}
        yield
        await app.state.engine.close()
    await journal.close()


app = FastAPI(title="BanterBots", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                   allow_methods=["GET", "POST"], allow_headers=["Content-Type"])


@app.get("/api/health")
async def health():
    async with httpx.AsyncClient(timeout=2) as client:
        async def status(url):
            try:
                response = await client.get(url + "/.well-known/agent-card.json")
                return response.status_code == 200
            except httpx.HTTPError:
                return False
        a, b = await asyncio.gather(status(AGENT_A_URL), status(AGENT_B_URL))
    return {"status": "ok" if a and b else "degraded", "agents": {"a": a, "b": b}}


@app.get("/api/config")
async def config():
    personas = [load_persona(agent_id) for agent_id in ("a", "b")]
    return {"azure_configured": azure_configured(), "text_model": TEXT_MODEL,
            "voice_model": VOICE_MODEL, "a2a_version": "1.0",
            "agent_urls": {"a": AGENT_A_URL, "b": AGENT_B_URL}, "personas": [
                {"agent_id": persona.id, "name": persona.name, "style": persona.style, "voice": persona.voice}
                for persona in personas]}


@app.get("/api/teams")
async def teams():
    return [team.model_dump() for team in get_teams()]


@app.get("/api/games")
async def games(mode: str = "replay", date: str | None = None):
    if mode == "replay":
        return {"games": [g.model_dump() for g in get_replay_games()], "feed": {"status": "replay"}}
    if mode != "live":
        raise HTTPException(400, "Choose replay or live")
    key = date or datetime.now(timezone.utc).strftime("%Y%m%d")
    cached = app.state.live_cache.get(key)
    if cached and asyncio.get_running_loop().time() - cached[0] < 15:
        return cached[1]
    try:
        result = {"games": [g.model_dump() for g in await app.state.engine.provider.games(date)],
                  "feed": {"status": "connected", "last_success": datetime.now(timezone.utc).isoformat()}}
        app.state.live_cache[key] = (asyncio.get_running_loop().time(), result)
        return result
    except Exception as exc:
        return {"games": cached[1]["games"] if cached else [], "feed": {"status": "error", "error": str(exc),
                 "last_success": cached[1]["feed"].get("last_success") if cached else None}}


@app.get("/api/games/{game_id}/highlights")
async def highlights(game_id: str):
    try:
        return {"game_id": game_id, "highlights": get_replay_highlights(game_id)}
    except ValueError as exc:
        raise HTTPException(404, "Replay game not found") from exc


async def session_or_404(sid):
    try:
        return await app.state.engine.get(sid)
    except KeyError:
        raise HTTPException(404, "Run not found") from None


@app.post("/api/sessions")
async def create_session(body: SessionCreate):
    if body.provider == "azure" and not azure_configured():
        raise HTTPException(400, "Add AZURE_OPENAI_API_KEY and AZURE_OPENAI_ENDPOINT to .env and restart "
                                 "before selecting Azure OpenAI commentary")
    try:
        return (await app.state.engine.create(body)).view()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, "The live feed is unavailable") from exc


@app.get("/api/sessions")
async def sessions():
    return [{k: v for k, v in state.items() if k not in {"turns", "events", "snapshot"}}
            for state in await app.state.engine.journal.list()]


@app.get("/api/sessions/{sid}")
async def get_session(sid: str):
    return (await session_or_404(sid)).view()


@app.post("/api/sessions/{sid}/control")
async def control(sid: str, body: SessionControl):
    session = await session_or_404(sid)
    try:
        await app.state.engine.control(session, body.action, body.speed)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return session.view()


# Questions the booth politely declines rather than sending to the model.
BLOCKED_QUESTION = re.compile(r"(?i)\b(bet|bets|betting|odds|spread|parlay|wager|gambl\w*|sportsbook|over/under|"
                              r"fuck\w*|shit\w*|bitch\w*|asshole|damn)\b")


@app.post("/api/sessions/{sid}/listener")
async def listener(sid: str, body: ListenerInput):
    """Listener input reaches the hosts as booth data at the next natural break."""
    session = await session_or_404(sid)
    state = session.listener
    clock = asyncio.get_running_loop().time()
    if body.side:
        state["side"] = body.side
    if body.reaction:
        # A cheer or boo every half second at most; a held button is one voice, not a crowd.
        if clock - state.get("last_reaction", 0) >= 0.5:
            state["last_reaction"] = clock
            state["cheers" if body.reaction == "cheer" else "boos"] = min(99, state.get(
                "cheers" if body.reaction == "cheer" else "boos", 0) + 1)
    if body.poll_vote:
        poll = state.get("poll")
        if not poll or poll.get("event_id") != body.poll_event_id:
            raise HTTPException(409, "That poll has closed")
        poll["pick"] = body.poll_vote
    if body.question:
        question = " ".join(body.question.split())
        if BLOCKED_QUESTION.search(question):
            raise HTTPException(422, "The booth keeps it family-friendly and never talks betting. Try another question.")
        if clock - state.get("last_question", 0) < 20 or len(state["questions"]) >= 3:
            raise HTTPException(429, "The booth already has a question waiting. Try again after the next play.")
        state["last_question"] = clock
        state["questions"].append(question)
    await app.state.engine.emit(session, "listener", {key: state.get(key) for key in ("side", "cheers", "boos")}
                                | {"questions_waiting": len(state["questions"]), "poll": public_poll(state.get("poll"))})
    return {"side": state.get("side"), "cheers": state.get("cheers", 0), "boos": state.get("boos", 0),
            "questions_waiting": len(state["questions"])}


@app.get("/api/sessions/{sid}/export")
async def export(sid: str):
    session = await session_or_404(sid)
    return {"session": session.view(), "events": await app.state.engine.journal.events(sid)}


@app.websocket("/api/sessions/{sid}/stream")
async def stream(websocket: WebSocket, sid: str, since: int = 0):
    try:
        session = await app.state.engine.get(sid)
    except KeyError:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    listener = uid()
    queue = asyncio.Queue(maxsize=1024)
    # Subscribe before replaying history to avoid a reconnect race.
    session.listeners[listener] = queue
    if session.player is None:
        session.player = listener
    cursor = since
    try:
        for event in await app.state.engine.journal.events(sid, since):
            if event["type"] not in {"audio", "segment.sealed", "turn.start"}:
                await websocket.send_json({**event, "historical": True})
                cursor = max(cursor, event["seq"])

        async def sender():
            nonlocal cursor
            audio_segments = set()
            live_turns = set()
            while True:
                event = await queue.get()
                if event.get("type") == "disconnect":
                    await websocket.close(code=1013, reason="Slow client; reconnect to recover history")
                    return
                if event["seq"] > cursor:
                    cursor = event["seq"]
                    data = event["data"]
                    segment_id = data.get("segment_id")
                    if event["type"] == "turn":
                        live_turns.add(data.get("turn", {}).get("id"))
                    elif event["type"] == "audio":
                        # A reconnect cannot reconstruct lost PCM. Only start
                        # segments whose first chunk this connection observed.
                        if segment_id not in audio_segments:
                            if data.get("chunk_index") != 0:
                                continue
                            audio_segments.add(segment_id)
                    elif event["type"] == "segment.sealed":
                        missing_audio = not data.get("browser_speech") and data.get("total_samples", 0) > 0 and segment_id not in audio_segments
                        missing_turn = data.get("browser_speech") and segment_id not in live_turns
                        if missing_audio or missing_turn:
                            event = {**event, "data": {**data, "discard_audio": True,
                                                      "browser_speech": False, "reconnect_gap": True}}
                            if session.player == listener:
                                session.playback.setdefault(segment_id, asyncio.Event()).set()
                        audio_segments.discard(segment_id)
                        live_turns.discard(segment_id)
                    await websocket.send_json(event)

        sender_task = asyncio.create_task(sender())
        try:
            while True:
                incoming = await websocket.receive_json()
                if incoming.get("type") == "playback.ack" and session.player == listener:
                    segment_id = incoming.get("segment_id", "")
                    if not incoming.get("cancelled"):
                        session.heard.add(segment_id)
                    session.playback.setdefault(segment_id, asyncio.Event()).set()
                    await app.state.engine.emit(session, "trace", {"node": "playback", "phase": "cancelled" if incoming.get("cancelled") else "complete",
                        "segment_id": segment_id, "played_samples": incoming.get("played_samples", 0)})
        finally:
            sender_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender_task
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        session.listeners.pop(listener, None)
        if session.player == listener:
            session.player = next(iter(session.listeners), None)
            for event in session.playback.values():
                event.set()


if (ROOT / "assets").exists():
    app.mount("/assets", StaticFiles(directory=ROOT / "assets"), name="assets")
if (ROOT / "frontend" / "dist").exists():
    app.mount("/", StaticFiles(directory=ROOT / "frontend" / "dist", html=True), name="dashboard")
