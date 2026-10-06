"""Review representative real broadcasts and causality after demo rewriting."""
import json

import pytest

from backend.commentary import Draft, Persona, build_prompt, demo_draft, derived_facts, load_persona, validate_draft
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
    response_request = reply_request(payload(point))
    response = demo_draft(response_request, load_persona("b"))
    assert "Ravens 7" in response_request.peer_utterance.text and "Chiefs 0" in response_request.peer_utterance.text
    assert "We're still in front" in response.text
    assert "Ravens 7" not in response.text and "Chiefs 0" not in response.text


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
    # Once the lead says the new score, the peer adds a response instead of
    # reciting the same scoreboard a second time.
    assert "Ravens 6" in home_lead.peer_utterance.text and "Chiefs 0" in home_lead.peer_utterance.text
    assert "Chiefs 0, Ravens 6" not in away.text
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
    events = get_replay_events(game_id)
    for index, event in enumerate(events):
        request = payload(event, recent[-8:])
        request.snapshot.drive_history = [prior.model_dump() for prior in events[max(0, index - 5):index]]
        request.snapshot.seal()
        lead_id = "a" if event.sequence % 2 == 0 else "b"
        peer_id = "b" if lead_id == "a" else "a"
        lead = load_persona(lead_id)
        call = demo_draft(request, lead)
        validate_draft(call, request, lead)
        response_request = reply_request(request, lead_id)
        peer = load_persona(peer_id)
        response = demo_draft(response_request, peer)
        validate_draft(response, response_request, peer)
        assert "!!" not in call.text + response.text
        recent.extend([{"agent_id": lead_id, "text": call.text}, {"agent_id": peer_id, "text": response.text}])


def test_next_down_math_is_source_aware_and_rejects_wrong_context():
    event = next(event for event in get_replay_events("2024_01_BAL_KC") if event.id == "118")
    replay = payload(event)
    assert derived_facts(replay)["next_down"] == 3
    assert derived_facts(replay)["next_distance"] == 11
    live = payload(event.model_copy(update={"source": "espn", "play_type": "pass_reception", "down": 3, "distance": 11}))
    assert derived_facts(live)["next_down"] == 3
    assert derived_facts(live)["next_distance"] == 11
    persona = load_persona("b")
    draft = demo_draft(live, persona)
    assert "Hill" in draft.text and "2 yards" in draft.text and "Third-and-11" in draft.text
    validate_draft(draft, live, persona)
    with pytest.raises(ValueError, match="down and distance"):
        validate_draft(Draft(text="Second-and-11 next.", emotion="engaged", evidence=["derived_facts"]), live, persona)


def test_real_fumble_and_accepted_penalty_both_remain_in_the_call():
    event = next(event for event in get_replay_events("2024_22_KC_PHI") if event.id == "3395")
    request = payload(event)
    call = demo_draft(request, load_persona("a"))
    assert "Chiefs lose possession" in call.text and "Williams" in call.text
    assert "Unsportsmanlike conduct" in call.text
    response_request = reply_request(request)
    response = demo_draft(response_request, load_persona("b"))
    assert "turnover counts" in response.text and "after the recovery" in response.text
    validate_draft(call, request, load_persona("a"))
    validate_draft(response, response_request, load_persona("b"))


def test_pick_six_reads_actual_returner_and_causal_prior_sacks():
    events = get_replay_events("2024_22_KC_PHI")
    index = next(index for index, event in enumerate(events) if event.id == "1468")
    request = payload(events[index])
    request.snapshot.drive_history = [prior.model_dump() for prior in events[index - 5:index]]
    request.snapshot.seal()
    call = demo_draft(request, load_persona("b"))
    assert "DeJean" in call.text and "touchdown Eagles" in call.text
    assert "Back-to-back sacks" in call.text
    # Sequence context cannot come from another game or a future clock.
    for changes in ({"game_id": "other-game"}, {"sequence": events[index].sequence + 1}, {"clock": "00:01"}):
        modified = request.model_copy(deep=True)
        modified.snapshot.drive_history = [dict(item, **changes) for item in modified.snapshot.drive_history]
        modified.snapshot.seal()
        assert "prior_back_to_back_sacks" not in derived_facts(modified)


def test_reversal_uses_corrected_receiver_and_actual_final_seconds():
    event = next(event for event in get_replay_events("2024_01_BAL_KC") if event.id == "4221")
    request = payload(event)
    call = demo_draft(request, load_persona("a"))
    assert "Incomplete for Likely" in call.text and event.clock in call.text
    assert "touchdown" not in call.text.lower()
    response_request = reply_request(request)
    response = demo_draft(response_request, load_persona("b"))
    assert "corrected ruling is incomplete" in response.text
    validate_draft(response, response_request, load_persona("b"))


def test_editable_delivery_and_small_word_budget_change_offline_behavior():
    event = next(event for event in get_replay_events("2024_01_BAL_KC") if event.id == "148")
    request = payload(event)
    base = load_persona("a")
    position = base.model_copy(deep=True)
    position.demo_delivery.analysis_focus = ["field_position"]
    situation = base.model_copy(deep=True)
    situation.demo_delivery.analysis_focus = ["situation"]
    assert "Ravens' 47" in demo_draft(request, position).text
    assert "needed 11" in demo_draft(request, situation).text
    brief = base.model_copy(update={"max_words": 12})
    assert len(demo_draft(request, brief).text.split()) <= 12
    assert len(demo_draft(reply_request(request), brief).text.split()) <= 12
    backwards_compatible = base.model_dump(exclude={"demo_delivery"})
    assert Persona.model_validate(backwards_compatible).demo_delivery.pace == "measured"
    with pytest.raises(ValueError):
        Persona.model_validate(dict(backwards_compatible, demo_delivery={"banter_frequency": 2}))


def test_openai_prompt_exposes_trusted_analysis_and_observed_peer():
    event = next(event for event in get_replay_events("2024_01_BAL_KC") if event.id == "320")
    request = reply_request(payload(event))
    request.peer_utterance.spoken_text = "Henry picks up three. A small gain."
    request.peer_utterance.text = "This discarded draft contains no real facts."
    system, material = build_prompt(request, load_persona("b"))
    assert "never subtract the gain from an ESPN post-play distance again" in system
    assert "A penalty after a retained fumble" in system
    assert '"next_distance": 1.0' in material
    assert "Henry picks up three" in material
    assert "discarded draft" not in material


def test_edited_manifest_cannot_silently_switch_a2a_service_identity(tmp_path, monkeypatch):
    import backend.commentary as commentary

    manifest = load_persona("a").model_dump()
    manifest["id"] = "b"
    (tmp_path / "personas").mkdir()
    (tmp_path / "personas" / "max-carter.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(commentary, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="match service a"):
        load_persona("a")


@pytest.mark.parametrize("home_score,away_score,scorer,expected", [
    (0, 7, "BAL", "We need an answer"),
    (7, 7, "BAL", "We're level now"),
    (14, 13, "KC", "puts us in front"),
    (14, 7, "BAL", "We're still in front"),
    (7, 14, "KC", "still chasing"),
])
def test_extra_point_peer_responds_to_actual_score_consequence(home_score, away_score, scorer, expected):
    event = next(event for event in get_replay_events("2024_01_BAL_KC") if event.id == "414")
    event = event.model_copy(update={"home_score": home_score, "away_score": away_score, "scoring_team": scorer})
    request = reply_request(payload(event), "b")
    response = demo_draft(request, load_persona("a"))
    assert expected in response.text
    assert "extra point" not in response.text.lower()
    assert f"Chiefs {home_score}" not in response.text
    validate_draft(response, request, load_persona("a"))


def test_kick_reply_says_score_when_lead_omits_it_and_recognizes_observed_spoken_score():
    event = next(event for event in get_replay_events("2024_01_BAL_KC") if event.id == "414")
    request = reply_request(payload(event), "b")
    request.peer_utterance.spoken_text = "Tucker adds the point."
    response = demo_draft(request, load_persona("a"))
    assert "Chiefs 0, Ravens 7" in response.text
    request.peer_utterance.spoken_text = "Tucker adds the point. Ravens seven, Chiefs zero."
    response = demo_draft(request, load_persona("a"))
    assert "Chiefs 0" not in response.text and "Ravens 7" not in response.text


def test_field_goal_reply_adds_score_perspective_without_repeating_the_kick():
    event = next(event for event in get_replay_events("2024_22_KC_PHI") if event.id == "1384")
    request = reply_request(payload(event))
    response = demo_draft(request, load_persona("b"))
    assert "chasing" in response.text and "answer" in response.text
    assert "Elliott" not in response.text and "good" not in response.text.lower()
    assert "Eagles 10" not in response.text
    validate_draft(response, request, load_persona("b"))
