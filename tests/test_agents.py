"""Protocol tests use two real HTTP services, never in-process peer mocks."""
import asyncio
import json
import socket
import sqlite3

import httpx
import pytest
import pytest_asyncio
import uvicorn

from backend.a2a_client import exchange
from backend.agents import create_app
from backend.commentary import Draft, demo_draft, load_persona, validate_draft, validate_request
from backend.contracts import AgentRequest, CommentaryTurn, GameSnapshot, PlayEvent, uid
from backend.security import safe_error


def request(**changes):
    event = PlayEvent(id="play_42", game_id="2024_01_BAL_KC", source="nflverse", sequence=42,
                      quarter=1, clock="09:25", home_team="KC", away_team="BAL", home_score=0, away_score=0,
                      possession="BAL", down=2, distance=7, yardline=63, yards_gained=12,
                      description="L.Jackson pass short right to Z.Flowers for 12 yards.", play_type="pass")
    return AgentRequest(session_id=uid(), epoch=1, exchange_id=uid(), snapshot=GameSnapshot(event=event).seal(),
                        voice_enabled=False, **changes)


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest_asyncio.fixture
async def agents(tmp_path):
    ports = [unused_port(), unused_port()]
    urls = [f"http://127.0.0.1:{port}" for port in ports]
    apps = [create_app("a", ports[0], urls[1], tmp_path / "a"),
            create_app("b", ports[1], urls[0], tmp_path / "b")]
    servers = [uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
               for app, port in zip(apps, ports)]
    tasks = [asyncio.create_task(server.serve()) for server in servers]
    for _ in range(100):
        if all(server.started for server in servers):
            break
        await asyncio.sleep(0.05)
    assert all(server.started for server in servers)
    yield urls, apps, tmp_path
    for server in servers:
        server.should_exit = True
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)


@pytest.mark.asyncio
@pytest.mark.parametrize("lead", [0, 1])
async def test_real_direct_a2a_exchange_and_persistence(agents, lead):
    urls, apps, path = agents
    events = []

    async def emit(event):
        events.append(event)

    payload = request()
    turns = await exchange(urls[lead], payload, emit)
    assert [turn.agent_id for turn in turns] == (["a", "b"] if lead == 0 else ["b", "a"])
    assert len(turns) == 2
    assert all(turn.snapshot_hash == payload.snapshot.hash for turn in turns)
    assert turns[1].reply_to_turn_id == turns[0].id
    assert turns[0].task_id != turns[1].task_id
    sent = [value["data"] for value in events if value["type"] == "trace" and value["data"].get("node") == "a2a.send"]
    assert len(sent) == 2
    assert sent[1]["target"] == urls[1 - lead]
    assert sent[1]["mode"] == "reply" and sent[1]["hop_budget"] == 0
    assert sent[1]["request"]["peer_utterance"]["id"] == turns[0].id
    assert sent[1]["snapshot_hash"] == payload.snapshot.hash
    assert {value["data"]["state"] for value in events if value["data"].get("node") == "a2a.status"} >= {"submitted", "working", "completed"}
    for agent_id in ["a", "b"]:
        with sqlite3.connect(path / agent_id / f"agent_{agent_id}_tasks.sqlite") as db:
            assert db.execute("select count(*) from tasks").fetchone()[0] >= 1
        with sqlite3.connect(path / agent_id / f"agent_{agent_id}_checkpoints.sqlite") as db:
            assert db.execute("select count(*) from checkpoints").fetchone()[0] >= 6
    async with httpx.AsyncClient(trust_env=False) as client:
        card = (await client.get(urls[lead] + "/.well-known/agent-card.json")).json()
    assert card["supportedInterfaces"][0]["protocolVersion"] == "1.0"
    assert card["capabilities"]["streaming"] is True


@pytest.mark.asyncio
async def test_changed_snapshot_is_rejected_over_protocol(agents):
    urls, _, _ = agents
    payload = request()
    payload.snapshot.event.home_score = 99
    events = []

    async def emit(event):
        events.append(event)

    with pytest.raises(RuntimeError, match="FAILED"):
        await exchange(urls[0], payload, emit)
    assert any("integrity" in event["data"].get("message", "").lower() for event in events)
    assert not any(event["type"] == "turn" for event in events)


@pytest.mark.asyncio
async def test_invalid_key_failure_stays_actionable_and_private_over_protocol(agents, monkeypatch, caplog):
    from openai import AuthenticationError
    from backend import agents as agent_module

    secret = "sk-proj-testsecretthatmustnotbepersisted"
    response = httpx.Response(401, request=httpx.Request("POST", "https://api.openai.com/v1/responses"))
    failure = AuthenticationError(f"Incorrect API key provided: {secret}", response=response,
                                  body={"code": "invalid_api_key", "type": "invalid_request_error"})

    async def rejected(*args, **kwargs):
        raise failure

    monkeypatch.setattr(agent_module, "generate_turn", rejected)
    events = []

    async def emit(event):
        events.append(event)

    with pytest.raises(RuntimeError, match="FAILED.*Replace the key in .env.*all three") as raised:
        await exchange(agents[0][0], request(provider="openai"), emit)
    assert safe_error(failure) in str(raised.value)
    assert any(event["type"] == "error" and event["data"]["message"] == safe_error(failure) for event in events)
    assert not any(event["type"] == "turn" for event in events)
    assert secret not in str(raised.value) + json.dumps(events) + caplog.text

    # Both A2A records and LangGraph's failed-node checkpoint writes are durable.
    for suffix in ("tasks", "checkpoints"):
        with sqlite3.connect(agents[2] / "a" / f"agent_a_{suffix}.sqlite") as db:
            records = "\n".join(db.iterdump())
        assert secret not in records
        assert secret.encode().hex().upper() not in records.upper()


@pytest.mark.asyncio
async def test_speech_authentication_failure_checkpoint_uses_safe_message(agents, monkeypatch):
    from openai import AuthenticationError
    from backend import agents as agent_module
    from backend.speech import SpeechFailure

    secret = "sk-proj-testsecretinspeechcheckpoint"
    response = httpx.Response(401, request=httpx.Request("POST", "https://api.openai.com/v1/realtime"))
    failure = AuthenticationError(f"Incorrect API key provided: {secret}", response=response,
                                  body={"code": "invalid_api_key"})

    async def drafted(payload, persona, task_id, emit):
        return CommentaryTurn(exchange_id=payload.exchange_id, agent_id=persona.id, persona=persona.name,
                              team_id=payload.snapshot.event.home_team if persona.id == "a" else payload.snapshot.event.away_team,
                              event_id=payload.snapshot.event.id, snapshot_hash=payload.snapshot.hash,
                              text="A grounded call.", mode="openai", task_id=task_id,
                              reply_to_turn_id=payload.peer_utterance.id if payload.peer_utterance else None)

    async def rejected(*args, **kwargs):
        raise SpeechFailure(str(failure)) from failure

    monkeypatch.setattr(agent_module, "generate_turn", drafted)
    monkeypatch.setattr(agent_module, "speak", rejected)
    events = []

    async def emit(event):
        events.append(event)

    payload = request(provider="openai").model_copy(update={"voice_enabled": True})
    turns = await exchange(agents[0][0], payload, emit)
    assert all(turn.speech_complete is False and turn.speech_error == safe_error(failure) for turn in turns)
    assert secret not in json.dumps(events)
    for agent_id in ("a", "b"):
        with sqlite3.connect(agents[2] / agent_id / f"agent_{agent_id}_checkpoints.sqlite") as db:
            records = "\n".join(db.iterdump())
        assert secret not in records
        assert secret.encode().hex().upper() not in records.upper()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_style", ["event", "task"])
async def test_cancel_during_generation_cancels_remote_task(agents, monkeypatch, cancel_style):
    from backend import agents as agent_module
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def delayed(*args, **kwargs):
        started.set()
        try:
            await asyncio.sleep(30)
        finally:
            stopped.set()

    monkeypatch.setattr(agent_module, "generate_turn", delayed)
    cancel = asyncio.Event()
    events = []

    async def emit(event):
        events.append(event)

    task = asyncio.create_task(exchange(agents[0][0], request(), emit, cancel))
    await asyncio.wait_for(started.wait(), 5)
    if cancel_style == "event":
        cancel.set()
    else:
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(stopped.wait(), 5)
    assert not any(event["type"] == "turn" for event in events)


def test_peer_identity_and_recursion_guard():
    payload = request()
    validate_request(payload, "a")
    with pytest.raises(ValueError, match="one hop"):
        validate_request(payload.model_copy(update={"hop_budget": 0}), "a")
    peer = CommentaryTurn(exchange_id=payload.exchange_id, agent_id="a", persona="Max Carter", team_id="KC",
                          event_id=payload.snapshot.event.id, snapshot_hash=payload.snapshot.hash, text="A good gain.")
    reply = payload.model_copy(update={"mode": "reply", "hop_budget": 0, "peer_utterance": peer})
    validate_request(reply, "b")
    with pytest.raises(ValueError, match="match"):
        validate_request(reply, "a")
    with pytest.raises(ValueError, match="hop_budget=0"):
        validate_request(reply.model_copy(update={"hop_budget": 1}), "b")


@pytest.mark.asyncio
@pytest.mark.parametrize("attempt", range(4))
async def test_cancellation_during_peer_generation_stops_both_services(agents, monkeypatch, attempt):
    from backend import agents as agent_module
    original = agent_module.generate_turn
    peer_started, peer_stopped = asyncio.Event(), asyncio.Event()

    async def delayed_peer(payload, persona, task_id, emit):
        if persona.id == "a":
            return await original(payload, persona, task_id, emit)
        peer_started.set()
        try:
            await asyncio.sleep(30)
        finally:
            peer_stopped.set()

    monkeypatch.setattr(agent_module, "generate_turn", delayed_peer)
    events = []

    async def emit(event):
        events.append(event)

    task = asyncio.create_task(exchange(agents[0][0], request(), emit))
    await asyncio.wait_for(peer_started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(peer_stopped.wait(), 5)
    published = [event["data"]["turn"]["agent_id"] for event in events if event["type"] == "turn"]
    assert len(published) <= 1 and all(agent_id == "a" for agent_id in published)


@pytest.mark.parametrize("description,kind,flags,expected", [
    ("P.Mahomes pass incomplete short middle.", "pass", ["incomplete_pass"], "incomplete"),
    ("L.Jackson pass intercepted by J.Reid.", "pass", ["interception", "turnover"], "intercept"),
    ("P.Mahomes pass TOUCHDOWN. The ruling is REVERSED. No play.", "no_play", ["reversed", "incomplete_pass"], "incomplete"),
    ("PENALTY on KC, false start, 5 yards. No play.", "no_play", ["penalty", "no_play"], "false start"),
    ("H.Butker 45 yard field goal is GOOD.", "field_goal", ["field_goal", "scoring"], "good"),
])
def test_demo_uses_actual_event_and_handles_rulings(description, kind, flags, expected):
    payload = request()
    payload.snapshot.event.description = description
    payload.snapshot.event.play_type = kind
    payload.snapshot.event.flags = flags
    payload.snapshot.event.yards_gained = 0
    payload.snapshot.seal()
    draft = demo_draft(payload, load_persona("a"))
    assert expected in draft.text.lower()
    if "reversed" in description.lower():
        assert "touchdown" not in draft.text.lower()
    assert "Super Bowl" not in draft.text


def test_reply_engages_completed_observed_words():
    payload = request()
    peer = CommentaryTurn(exchange_id=payload.exchange_id, agent_id="a", persona="Max Carter", team_id="KC",
                          event_id=payload.snapshot.event.id, snapshot_hash=payload.snapshot.hash,
                          text="Draft that wasn't spoken.", spoken_text="Flowers picks up twelve yards. Useful gain.")
    payload = payload.model_copy(update={"mode": "reply", "hop_budget": 0, "peer_utterance": peer})
    draft = demo_draft(payload, load_persona("b"))
    assert "12 yards" in draft.text and "Max" in draft.text
    assert "Draft" not in draft.text
    changed = payload.model_copy(update={"peer_utterance": peer.model_copy(update={"spoken_text": "Keep working, Ravens."})})
    assert demo_draft(changed, load_persona("b")).text != draft.text


@pytest.mark.parametrize("flags", [["interception", "turnover"], ["fumble_lost", "turnover"]])
def test_turnover_commentary_uses_pre_play_offense_not_new_possession(flags):
    payload = request()
    payload.snapshot.event.offense_team = "BAL"
    payload.snapshot.event.possession = "KC"
    payload.snapshot.event.flags = flags
    payload.snapshot.seal()
    home = demo_draft(payload, load_persona("a"))
    away = demo_draft(payload, load_persona("b"))
    assert "Ravens lose" in home.text
    assert "Ravens lose" in away.text
    assert "Chiefs lose" not in home.text + away.text
    assert "gift for the Chiefs" in home.text or "chance we needed" in home.text
    assert "couldn't afford" in away.text or "Hang on" in away.text


def test_optional_pre_play_offense_preserves_ordinary_call():
    payload = request()
    original = demo_draft(payload, load_persona("b"))
    payload.snapshot.event.offense_team = payload.snapshot.event.possession
    # Choice seeds must match: compare identical sealed hashes for fallback and explicit forms.
    amended = demo_draft(payload, load_persona("b"))
    assert amended.text == original.text


def test_actual_super_bowl_pick_six_credits_eagles_not_offensive_players():
    from backend.providers import get_replay_events
    event = next(item for item in get_replay_events("2024_22_KC_PHI") if item.id == "1468")
    payload = AgentRequest(session_id="pick-six", epoch=1, exchange_id=event.id,
                           snapshot=GameSnapshot(event=event).seal(), voice_enabled=False)
    assert event.scoring_team == "PHI" and event.possession == "KC"
    home = demo_draft(payload, load_persona("a"))
    away = demo_draft(payload, load_persona("b"))
    for draft in [home, away]:
        assert "touchdown Eagles" in draft.text and "Intercepted" in draft.text
        assert "touchdown Chiefs" not in draft.text and "Mahomes" not in draft.text
        validate_draft(draft, payload, load_persona("a"))
    assert home.emotion == "excited" and away.emotion == "concerned"


@pytest.mark.parametrize("flags", [["touchdown", "fumble_lost", "turnover", "scoring"], ["touchdown", "scoring"]])
def test_return_touchdown_does_not_mistake_offensive_actor_for_scorer(flags):
    payload = request()
    payload.snapshot.event.offense_team = "KC"
    payload.snapshot.event.possession = "BAL"
    payload.snapshot.event.scoring_team = "BAL"
    payload.snapshot.event.flags = flags
    payload.snapshot.event.players = ["P.Mahomes"]
    payload.snapshot.seal()
    call = demo_draft(payload, load_persona("b"))
    assert "Ravens" in call.text and "Mahomes" not in call.text
    assert call.emotion == "excited"


@pytest.mark.parametrize("text", [
    "Jackson gains 99 yards.",
    "The score is 7 to 0.",
    "Chiefs 7, Ravens 0.",
    "Chiefs lead 7-0.",
    "A lead of 7 points.",
    "Touchdown Ravens!",
    "Flowers picks up 7 yards.",
])
def test_openai_draft_rejects_unfounded_numeric_scores_and_results(text):
    draft = Draft(text=text, emotion="engaged", evidence=["event.description"])
    with pytest.raises(ValueError):
        validate_draft(draft, request(), load_persona("a"))


def test_openai_draft_accepts_current_facts_and_corrected_touchdown_denial():
    payload = request()
    validate_draft(Draft(text="Jackson finds Flowers for 12 yards.", emotion="engaged", evidence=["event.description"]),
                   payload, load_persona("a"))
    payload.snapshot.event.flags = ["reversed", "incomplete_pass"]
    validate_draft(Draft(text="No touchdown. The call was reversed.", emotion="reflective", evidence=["event.flags"]),
                   payload, load_persona("a"))


def test_all_bundled_replays_keep_short_original_leads_and_replies():
    from backend.providers import get_replay_events
    for game_id in ["2024_01_BAL_KC", "2024_22_KC_PHI"]:
        for event in get_replay_events(game_id):
            payload = AgentRequest(session_id="audit", epoch=1, exchange_id=event.id,
                                   snapshot=GameSnapshot(event=event).seal(), voice_enabled=False)
            for agent_id, peer_id in [("a", "b"), ("b", "a")]:
                persona = load_persona(agent_id)
                call = demo_draft(payload, persona)
                assert len(call.text.split()) <= persona.max_words
                if "reversed" in event.flags and "touchdown" not in event.flags:
                    assert "touchdown" not in call.text.lower()
                if event.description == "GAME":
                    assert "GAME." not in call.text
                turn = CommentaryTurn(exchange_id=payload.exchange_id, agent_id=agent_id, persona=persona.name,
                                      team_id=event.home_team if agent_id == "a" else event.away_team,
                                      event_id=event.id, snapshot_hash=payload.snapshot.hash, text=call.text)
                reply = demo_draft(payload.model_copy(update={"mode": "reply", "hop_budget": 0, "peer_utterance": turn}),
                                   load_persona(peer_id))
                assert len(reply.text.split()) <= load_persona(peer_id).max_words
                assert "recorded gain" not in reply.text.lower()
