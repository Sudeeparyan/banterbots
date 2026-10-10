"""Review representative real broadcasts and causality after demo rewriting."""
import json

import pytest

from backend.commentary import (Draft, Persona, build_prompt, demo_draft, derived_facts, draft_mismatch, fact_issue,
                                game_stats, generate_turn, load_persona, presnap_event, ready_for_reply, validate_draft,
                                word_limit, words_remaining)
from backend.contracts import AgentRequest, CommentaryTurn, GameSnapshot, ReplyDraft
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


def test_azure_prompt_exposes_trusted_analysis_and_observed_peer():
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


@pytest.mark.asyncio
async def test_generated_demo_turn_has_event_sensitive_delivery_without_spoken_directions():
    event = next(event for event in get_replay_events("2024_01_BAL_KC") if event.id == "391")
    events = []

    async def emit(value):
        events.append(value)

    turn = await generate_turn(payload(event), load_persona("b"), "delivery-task", emit)
    # Riley's team scores: the call explodes. The home crowd at Kansas City groans
    # while the visiting Baltimore pocket cheers.
    assert turn.delivery.reason == "touchdown" and turn.delivery.contour == "explode"
    assert turn.delivery.energy >= 0.95 and turn.delivery.moment == "big"
    assert turn.delivery.sound_cue == "crowd_groan" and turn.delivery.away_pocket > 0
    assert turn.text.startswith(("Yes!", "Oh, yes!", "Here we go!"))
    assert "[" not in turn.text and "<break" not in turn.text
    assert any(value["type"] == "trace" and value["data"].get("node") == "delivery" for value in events)


def test_text_prompt_gives_listener_first_response_and_separate_delivery_plan():
    event = next(event for event in get_replay_events("2024_01_BAL_KC") if event.id == "391")
    system, material = build_prompt(payload(event), load_persona("b"))
    assert "listener who cannot see the screen" in system
    assert "answer it first" in system and "No SSML" in system
    assert json.loads(material)["delivery_plan"]["reason"] == "touchdown"


def test_two_point_score_and_between_downs_penalty_both_reach_listener():
    event = next(event for event in get_replay_events("2024_22_KC_PHI") if event.id == "3958")
    request = payload(event)
    call = demo_draft(request, load_persona("b"))
    assert "two-point try is good" in call.text and "Illegal use of hands follows" in call.text
    validate_draft(call, request, load_persona("b"))
    reply = reply_request(request, "b")
    response = demo_draft(reply, load_persona("a"))
    assert "two-point try" not in response.text
    validate_draft(response, reply, load_persona("a"))


@pytest.mark.parametrize("direction", ["[excited]", "<break time='200ms'/>", "(laughs)", "*cheers*"])
def test_model_performance_directions_are_rejected_before_voice(direction):
    event = next(event for event in get_replay_events("2024_01_BAL_KC") if event.id == "183")
    draft = Draft(text=f"Henry gains 1 yard. {direction}", emotion="engaged", evidence=["event.description"])
    with pytest.raises(ValueError, match="performance directions"):
        validate_draft(draft, payload(event), load_persona("a"))


@pytest.mark.asyncio
@pytest.mark.parametrize("deployment", ["gpt-6-luna", "studio-custom-text-alias"])
async def test_azure_parse_uses_documented_low_effort_only_for_known_model(monkeypatch, deployment):
    from types import SimpleNamespace

    from backend import commentary

    calls = []
    event = next(event for event in get_replay_events("2024_01_BAL_KC") if event.id == "183")
    request = payload(event).model_copy(update={"provider": "azure"})
    persona = load_persona("a")
    draft = demo_draft(request, persona)

    class Client:
        responses = None

        def __init__(self):
            self.responses = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def parse(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(output_parsed=draft, id="bounded-response", usage=None)

    monkeypatch.setattr(commentary, "TEXT_MODEL", deployment)
    monkeypatch.setattr(commentary, "azure_configured", lambda: True)
    monkeypatch.setattr(commentary, "azure_client", lambda **kwargs: Client())

    async def emit(value):
        pass

    turn = await commentary.generate_turn(request, persona, "latency-test", emit)
    assert turn.text == draft.text and len(calls) == 1
    assert calls[0]["model"] == deployment and calls[0]["text_format"] is Draft
    assert calls[0]["max_output_tokens"] == 700
    if deployment == "gpt-6-luna":
        assert calls[0]["reasoning"] == {"effort": "low"}
    else:
        assert "reasoning" not in calls[0]


def presnap_request(events, index, agent_mode="setup"):
    event = presnap_event(events[index], events[index - 1] if index else None)
    snapshot = GameSnapshot(event=event, phase="pre_snap",
                            drive_history=[prior.model_dump() for prior in events[max(0, index - 5):index]]).seal()
    return AgentRequest(session_id="presnap", epoch=1, exchange_id=f"setup-{event.id}", snapshot=snapshot,
                        mode=agent_mode, hop_budget=0, voice_enabled=False)


def test_presnap_snapshot_cannot_carry_the_result():
    events = get_replay_events("2024_01_BAL_KC")
    index = next(i for i, event in enumerate(events) if event.id == "148")
    request = presnap_request(events, index)
    event = request.snapshot.event
    assert event.description == "" and event.flags == [] and event.yards_gained is None and event.players == []
    assert (event.down, event.distance, event.yardline) == (3, 11, 71)
    assert (event.home_score, event.away_score) == (events[index - 1].home_score, events[index - 1].away_score)
    persona = load_persona("b")
    draft = demo_draft(request, persona)
    assert draft.text.startswith("Third and 11, Ravens at their own 29")
    validate_draft(draft, request, persona)
    for leak in ("Third and 11. Hill caught it.", "Third and 11. Converted!", "Jackson gains 18 yards.",
                 "Touchdown! Third and 11."):
        with pytest.raises(ValueError):
            validate_draft(Draft(text=leak, emotion="excited", evidence=["event.down"]), request, persona)
    validate_draft(Draft(text="Third and 11. They need a touchdown drive.", emotion="engaged",
                         evidence=["event.down"]), request, persona)


@pytest.mark.parametrize("game_id", ["2024_01_BAL_KC", "2024_22_KC_PHI"])
def test_every_presnap_setup_passes_its_gates(game_id):
    events = get_replay_events(game_id)
    for index, event in enumerate(events):
        if event.down is None or event.play_type not in {"run", "pass", "punt", "field_goal"}:
            continue
        request = presnap_request(events, index)
        for agent_id in ("a", "b"):
            persona = load_persona(agent_id)
            draft = demo_draft(request, persona)
            validate_draft(draft, request, persona)
            assert len(draft.text.split()) <= word_limit(persona, request)


def test_moment_budgets_keep_routine_short_and_let_big_plays_run():
    events = get_replay_events("2024_01_BAL_KC")
    routine = payload(next(event for event in events if event.id == "183"))
    big = payload(next(event for event in events if event.id == "391"))
    persona = load_persona("a")
    assert word_limit(persona, routine) <= 16 < word_limit(persona, big)
    assert word_limit(persona, reply_request(routine, "b")) <= 14
    long_routine = Draft(text=" ".join(["word"] * 20), emotion="engaged", evidence=["event.description"])
    with pytest.raises(ValueError, match="word limit"):
        validate_draft(long_routine, routine, persona)


def segment_request(events, index, topic, booth=None, mode="segment"):
    event = events[index]
    stats = game_stats(events, event)
    snapshot = GameSnapshot(event=event, phase="segment", booth={"stats": stats, **(booth or {})}).seal()
    return AgentRequest(session_id="segment", epoch=1, exchange_id=f"{topic}-{event.id}", snapshot=snapshot,
                        mode=mode, topic=topic, voice_enabled=False)


def test_segments_quote_only_causal_totals_and_the_frozen_score():
    events = get_replay_events("2024_01_BAL_KC")
    half = next(i for i, event in enumerate(events) if event.id == "2145")
    stats = game_stats(events, events[half])
    expected = sum(int(event.yards_gained or 0) for event in events[:half + 1]
                   if (event.offense_team or event.possession) == "KC" and event.play_type in {"run", "pass"}
                   and not set(event.flags) & {"no_play", "reversed", "play_deleted"})
    assert stats["teams"]["KC"]["yards"] == expected
    assert game_stats(events, events[-1])["teams"]["KC"]["yards"] != expected  # later plays are excluded at halftime
    for topic, index in (("halftime", half), ("final", len(events) - 1), ("quarter", 34)):
        request = segment_request(events, index, topic)
        persona = load_persona("a")
        draft = demo_draft(request, persona)
        validate_draft(draft, request, persona)
        assert f"{events[index].home_score}" in draft.text
    final = demo_draft(segment_request(events, len(events) - 1, "final"), load_persona("a"))
    assert "We win it" in final.text
    with pytest.raises(ValueError, match="numeric"):
        validate_draft(Draft(text="The Chiefs have 999 yards.", emotion="engaged", evidence=["booth"]),
                       segment_request(events, half, "halftime"), load_persona("a"))


def test_callbacks_must_point_at_a_real_remembered_remark():
    event = next(event for event in get_replay_events("2024_01_BAL_KC") if event.id == "183")
    memory = [{"id": "m1", "agent_id": "b", "text": "Henry won't find a yard all day."}]
    request = AgentRequest(session_id="memory", epoch=1, exchange_id="x",
                           snapshot=GameSnapshot(event=event, booth={"memory": memory}).seal(), voice_enabled=False)
    persona = load_persona("a")
    validate_draft(Draft(text="Henry, 1 yard. Found one, Riley.", emotion="dry", evidence=["booth"],
                         callback_to="m1"), request, persona)
    # A label naming no remembered remark is dropped; the line itself still airs.
    invented = Draft(text="Henry, 1 yard. Found one, Riley.", emotion="dry", evidence=["booth"], callback_to="m9")
    validate_draft(invented, request, persona)
    assert invented.callback_to is None


def test_reply_drafted_during_speech_is_used_only_when_spoken_words_carry_its_facts():
    event = next(event for event in get_replay_events("2024_01_BAL_KC") if event.id == "391")
    request = reply_request(payload(event), "a")
    script = request.peer_utterance.text
    draft = ReplyDraft(text="Yes! Our finish, Max. Henry earned it.", basis=script, emotion="excited",
                       evidence=["partner"])
    request.peer_utterance.spoken_text = script.replace("5", "five")
    assert draft_mismatch(request.model_copy(update={"draft": draft})) is None
    request.peer_utterance.spoken_text = "Touchdown, wow."
    assert "omitted" in draft_mismatch(request.model_copy(update={"draft": draft}))
    request.peer_utterance.spoken_text = script
    other = draft.model_copy(update={"basis": "x"})
    assert "different script" in draft_mismatch(request.model_copy(update={"draft": other}))
    flagged = request.peer_utterance.model_copy(update={"fact_flag": "wrong score"})
    assert "correct the record" in draft_mismatch(request.model_copy(update={"draft": draft, "peer_utterance": flagged}))


@pytest.mark.asyncio
async def test_generate_turn_reuses_a_valid_draft_without_calling_the_model():
    event = next(event for event in get_replay_events("2024_01_BAL_KC") if event.id == "391")
    request = reply_request(payload(event), "a")
    request.peer_utterance.spoken_text = request.peer_utterance.text
    draft = ReplyDraft(text="Yes! Our finish, Max.", basis=request.peer_utterance.text, emotion="excited",
                       evidence=["partner"])
    events = []

    async def emit(value):
        events.append(value)

    turn = await generate_turn(request.model_copy(update={"draft": draft, "provider": "azure"}), load_persona("b"),
                               "draft-task", emit)
    assert turn.text == "Yes! Our finish, Max." and turn.kind == "reply"
    assert any(value["data"].get("phase") == "draft_reused" for value in events)


def test_spoken_words_are_audited_with_the_same_fact_gates():
    events = get_replay_events("2024_01_BAL_KC")
    request = payload(next(event for event in events if event.id == "391"))
    assert fact_issue("Oh yes! Henry, five yards, touchdown Ravens!", request) is None
    assert "numeric" in fact_issue("Henry, 15 yards, touchdown Ravens!", request)
    reversed_play = payload(next(event for event in events if event.id == "4221"))
    assert "touchdown" in fact_issue("Touchdown Ravens!", reversed_play)


def test_partner_starts_only_after_every_fact_is_heard_and_a_few_words_remain():
    request = payload(next(event for event in get_replay_events("2024_01_BAL_KC") if event.id == "391"))
    script = "Touchdown Ravens! Henry from 5 yards out, and Baltimore strikes first in Kansas City."
    assert words_remaining(script, "Touchdown Ravens! Oh, Henry from five yards out") == 7
    # Facts heard but too much of the line left: the partner waits.
    assert not ready_for_reply(request, script, "Touchdown Ravens! Henry from five yards out,")
    assert ready_for_reply(request, script, "Touchdown Ravens! Yes! Henry from five yards out, and Baltimore strikes first")
    # A missing name or number never hands off early, nor does a misspoken fact.
    assert not ready_for_reply(request, "Henry from 5, touchdown Ravens, and that is that.", "Henry from, touchdown Ravens, and that")
    assert not ready_for_reply(request, script, "Touchdown Ravens! Henry from fifteen yards out, and Baltimore strikes first")
    assert not ready_for_reply(request, script, "")
