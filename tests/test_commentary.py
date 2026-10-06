"""Review representative real broadcasts and causality after demo rewriting."""
import pytest

from backend.commentary import demo_draft, load_persona, validate_draft
from backend.contracts import AgentRequest, CommentaryTurn, GameSnapshot
from backend.providers import get_replay_events


def payload(event, recent=None):
    return AgentRequest(session_id="commentary-review", epoch=1, exchange_id=event.id,
                        snapshot=GameSnapshot(event=event, recent_turns=recent or []).seal(),
                        voice_enabled=False)


def reply_request(request, agent_id="a"):
    persona = load_persona(agent_id)
    draft = demo_draft(request, persona)
    event = request.snapshot.event
    turn = CommentaryTurn(exchange_id=request.exchange_id, agent_id=agent_id, persona=persona.name,
                          team_id=event.home_team if agent_id == "a" else event.away_team,
                          event_id=event.id, snapshot_hash=request.snapshot.hash, text=draft.text)
    return request.model_copy(update={"mode": "reply", "hop_budget": 0, "peer_utterance": turn})


def test_real_one_yard_run_and_third_down_conversion_sound_like_the_play():
    events = get_replay_events("2024_01_BAL_KC")
    one_yard = next(event for event in events if event.id == "183")
    draft = demo_draft(payload(one_yard), load_persona("a"))
    assert "Henry" in draft.text and "1 yard" in draft.text
    assert "1 yards" not in draft.text
    converted = next(event for event in events if event.id == "148")
    draft = demo_draft(payload(converted), load_persona("b"))
    assert "Hill" in draft.text and "18 yards" in draft.text
    assert "Third-down conversion" in draft.text


def test_returned_kickoff_and_extra_point_avoid_reading_raw_feed_codes():
    events = get_replay_events("2024_01_BAL_KC")
    kickoff = next(event for event in events if event.id == "429")
    draft = demo_draft(payload(kickoff), load_persona("a"))
    assert "Steele" in draft.text and "33" in draft.text
    assert "9-J." not in draft.text and "42-C." not in draft.text
    point = next(event for event in events if event.id == "414")
    response = demo_draft(reply_request(payload(point)), load_persona("b"))
    assert "Ravens 7" in response.text and "Chiefs 0" in response.text


def test_ordinary_drive_varies_without_inventing_tactics():
    events = get_replay_events("2024_01_BAL_KC")
    recent = []
    ordinary_calls = []
    for event in events[:15]:
        request = payload(event, recent[-8:])
        call = demo_draft(request, load_persona("a"))
        response = demo_draft(reply_request(request), load_persona("b"))
        if event.play_type in {"run", "pass"} and not event.flags and 0 < event.yards_gained < 10:
            ordinary_calls.append(call.text)
        recent.extend([{"agent_id": "a", "text": call.text}, {"agent_id": "b", "text": response.text}])
    # The old broadcast repeated this exact filler on almost every small gain.
    assert len(ordinary_calls) >= 5
    assert sum("Make them keep earning it." in text for text in ordinary_calls) <= 2
    assert not any(word in " ".join(ordinary_calls).lower() for word in ("coverage scheme", "reads the defense", "blown assignment"))


def test_touchdown_reply_agrees_with_own_side_and_concedes_opponent():
    event = next(event for event in get_replay_events("2024_01_BAL_KC") if event.id == "391")
    home_lead = reply_request(payload(event), "a")
    away = demo_draft(home_lead, load_persona("b"))
    assert "our finish" in away.text.lower() or "touchdown ravens" in away.text.lower()
    assert "Ravens 6" in away.text and "Chiefs 0" in away.text
    away_lead = reply_request(payload(event), "b")
    home = demo_draft(away_lead, load_persona("a"))
    assert "our finish" not in home.text.lower()
    assert "touchdown Chiefs" not in home.text


def test_callback_quotes_only_a_real_previous_partner_remark():
    event = next(event for event in get_replay_events("2024_01_BAL_KC") if event.id == "273")
    persona = load_persona("b").model_copy(update={"callback_frequency": 0.5})
    history = [{"agent_id": "a", "text": "One stop now, please."}]
    stopped = event.model_copy(update={"id": "callback-stop", "flags": ["incomplete_pass"], "yards_gained": 0})
    # Vary only snapshot identity to exercise a target frequency without depending
    # on a specific hash. No prior remark means no claim of remembering one.
    found = None
    for sequence in range(30):
        candidate = stopped.model_copy(update={"sequence": sequence})
        request = reply_request(payload(candidate, history))
        draft = demo_draft(request, persona)
        if "stop you asked for" in draft.text:
            found = request
            break
    assert found is not None
    without_history = reply_request(payload(found.snapshot.event))
    assert "stop you asked for" not in demo_draft(without_history, persona).text
    disabled = persona.model_copy(update={"callback_frequency": 0})
    assert "stop you asked for" not in demo_draft(found, disabled).text


@pytest.mark.parametrize("game_id", ["2024_01_BAL_KC", "2024_22_KC_PHI"])
def test_every_replay_call_and_reply_passes_factual_gates_with_rolling_memory(game_id):
    recent = []
    for event in get_replay_events(game_id):
        request = payload(event, recent[-8:])
        lead_id = "a" if event.sequence % 2 == 0 else "b"
        peer_id = "b" if lead_id == "a" else "a"
        lead = load_persona(lead_id)
        call = demo_draft(request, lead)
        validate_draft(call, request, lead)
        response_request = reply_request(request, lead_id)
        peer = load_persona(peer_id)
        response = demo_draft(response_request, peer)
        validate_draft(response, response_request, peer)
        recent.extend([{"agent_id": lead_id, "text": call.text}, {"agent_id": peer_id, "text": response.text}])
