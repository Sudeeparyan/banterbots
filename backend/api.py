"""Dashboard API, run journal and ordered browser streams."""
import asyncio
import contextlib
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
import structlog
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from backend.config import AGENT_A_URL, AGENT_B_URL, ROOT, RUNTIME, TEXT_MODEL, VOICE_MODEL
from backend.commentary import load_persona
from backend.contracts import SessionControl, SessionCreate, uid
from backend.engine import Engine
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
    return {"openai_configured": bool(os.getenv("OPENAI_API_KEY")), "text_model": TEXT_MODEL,
            "voice_model": VOICE_MODEL, "a2a_version": "1.0", "personas": [
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
    if body.provider == "openai" and not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(400, "Add OPENAI_API_KEY to .env and restart before selecting OpenAI commentary")
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
