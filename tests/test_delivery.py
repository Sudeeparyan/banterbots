"""The audible broadcast shape follows confirmed results, role, allegiance and the building."""
import pytest

from backend.contracts import AgentRequest, GameSnapshot, PlayEvent
from backend.delivery import delivery_direction, delivery_for, moment_for
from backend.providers import get_replay_events


def request(**updates):
    event = PlayEvent(id="delivery-play", game_id="delivery-game", source="nflverse", sequence=8,
                      quarter=1, clock="12:00", home_team="KC", away_team="BAL", home_score=0, away_score=0,
                      offense_team="KC", possession="KC", down=2, distance=8, yards_gained=2,
                      description="P.Mahomes pass short left to T.Kelce for 2 yards.", play_type="pass",
                      players=["P.Mahomes", "T.Kelce"])
    event = event.model_copy(update=updates)
    return AgentRequest(session_id="delivery-session", epoch=1, exchange_id="delivery-exchange",
                        snapshot=GameSnapshot(event=event).seal())


def test_touchdown_explodes_for_the_scorer_hurts_for_the_rival_and_the_home_crowd_roars():
    ordinary = delivery_for(request(), "KC", "a")
    touchdown = request(flags=["touchdown", "scoring"], scoring_team="KC", home_score=6, yards_gained=22)
    lead = delivery_for(touchdown, "KC", "a")
    opponent = delivery_for(touchdown, "BAL", "b")
    reply = delivery_for(touchdown.model_copy(update={"mode": "reply"}), "BAL", "b")
    happy_reply = delivery_for(touchdown.model_copy(update={"mode": "reply"}), "KC", "a")
    # Routine snaps are engaged, not hushed; the peak has somewhere to go.
    assert 0.5 <= ordinary.energy <= 0.6 and ordinary.moment == "routine"
    assert lead.energy == 1.0 and lead.energy > ordinary.energy + 0.4
    assert lead.contour == "explode" and opponent.contour == "groan" and opponent.energy >= 0.7
    assert lead.sound_cue == "crowd_roar" and lead.cue_intensity == 1.0 and lead.crowd_side == "home"
    assert reply.sound_cue == "none" and reply.cue_intensity == 0 and reply.overlap_ms > 0
    # A rival's joy for their own team's moment may exceed the lead's pain.
    assert happy_reply.energy > opponent.energy and happy_reply.overlap_ms > reply.overlap_ms
    assert "Explode" in delivery_direction(lead, "a") and "groan" in delivery_direction(opponent, "b")


def test_away_score_draws_a_home_groan_with_a_visiting_pocket():
    touchdown = request(flags=["touchdown", "scoring"], scoring_team="BAL", offense_team="BAL", possession="BAL",
                        away_score=6)
    plan = delivery_for(touchdown, "BAL", "b")
    assert plan.contour == "explode" and plan.sound_cue == "crowd_groan" and plan.away_pocket > 0


def test_defensive_touchdown_follows_scorer_instead_of_offense():
    touchdown = request(flags=["touchdown", "interception", "turnover", "scoring"], scoring_team="BAL",
                        possession="BAL", away_score=6)
    assert delivery_for(touchdown, "BAL", "b").contour == "explode"
    assert delivery_for(touchdown, "KC", "a").contour == "groan"
    inferred = request(flags=["touchdown", "interception", "turnover"], scoring_team=None, possession="BAL")
    assert delivery_for(inferred, "BAL", "b").contour == "explode"


@pytest.mark.parametrize("flag", ["reversed", "correction", "no_play", "play_deleted"])
def test_corrected_flags_never_celebrate_a_stale_touchdown(flag):
    corrected = request(flags=[flag, "touchdown", "scoring"], scoring_team="KC",
                        description="Touchdown! Call was reversed after review.")
    for team, agent in (("KC", "a"), ("BAL", "b")):
        plan = delivery_for(corrected, team, agent, emotion="excited")
        assert plan.reason == "ruling" and plan.pace == "deliberate" and plan.contour == "suspense"
        assert plan.sound_cue == "none" and plan.cue_intensity == 0
        assert plan.energy <= 0.6


def test_real_reversed_baltimore_touchdown_gets_tension_but_no_crowd_celebration():
    event = next(event for event in get_replay_events("2024_01_BAL_KC") if event.id == "4221")
    payload = AgentRequest(session_id="delivery", epoch=1, exchange_id=event.id, snapshot=GameSnapshot(event=event).seal())
    for team, agent in (("KC", "a"), ("BAL", "b")):
        for mode in ("exchange", "reply"):
            plan = delivery_for(payload.model_copy(update={"mode": mode}), team, agent)
            assert plan.sound_cue not in {"crowd_roar", "crowd_cheer", "crowd_swell"}
            assert plan.contour not in {"explode", "lift_and_settle"}


def test_penalty_after_retained_turnover_keeps_result_but_no_play_suppresses_it():
    turnover = request(flags=["fumble_lost", "turnover", "penalty"], possession="BAL")
    assert delivery_for(turnover, "BAL", "b").reason == "turnover"
    erased = request(flags=["fumble_lost", "turnover", "penalty", "no_play"], possession="KC")
    assert delivery_for(erased, "BAL", "b").sound_cue == "none"


def test_flag_on_the_home_team_draws_boos_but_not_on_the_visitors():
    home_flag = request(flags=["penalty"], description="PENALTY on KC-87-T.Kelce, Offensive Holding, 10 yards.")
    away_flag = request(flags=["penalty"], description="PENALTY on BAL-32-M.Williams, Defensive Holding, 5 yards.")
    assert delivery_for(home_flag, "KC", "a").sound_cue == "crowd_boo"
    assert delivery_for(away_flag, "KC", "a").sound_cue == "none"


def test_super_bowl_is_a_neutral_building():
    event = next(event for event in get_replay_events("2024_22_KC_PHI") if event.id == "3958")
    payload = AgentRequest(session_id="delivery", epoch=1, exchange_id=event.id,
                           snapshot=GameSnapshot(event=event).seal())
    plan = delivery_for(payload, "KC", "b")
    assert plan.reason == "score" and plan.contour == "lift_and_settle"
    assert plan.crowd_side == "neutral" and plan.sound_cue == "crowd_cheer" and plan.away_pocket > 0


def test_source_timing_controls_conversion_emphasis():
    replay = request(flags=["first_down"], down=3, distance=8, yards_gained=9)
    live = request(flags=["first_down"], source="espn", down=1, distance=10, yards_gained=9)
    replay_plan = delivery_for(replay, "KC", "a")
    live_plan = delivery_for(live, "KC", "a")
    assert replay_plan.energy > live_plan.energy and replay_plan.moment == "notable"
    assert replay_plan.sound_cue == "crowd_cheer" and live_plan.sound_cue == "none"


def test_close_late_game_builds_tension_without_inventing_a_touchdown():
    late = request(quarter=4, clock="0:45", home_score=20, away_score=23)
    plan = delivery_for(late, "KC", "a")
    assert plan.reason == "late_tension" and plan.contour == "suspense" and plan.moment == "big"
    assert plan.sound_cue == "crowd_cheer" and plan.cue_intensity < 1
    assert "play is already complete" in delivery_direction(plan, "a")
    assert delivery_for(request(quarter=4, clock="0:45", home_score=3, away_score=31), "KC", "a").reason == "ordinary"


@pytest.mark.parametrize("kind", ["qb_kneel", "timeout", "period_end", "game_end", "extra_point"])
def test_resets_are_warm_and_quiet_for_the_crowd(kind):
    plan = delivery_for(request(play_type=kind), "KC", "a")
    assert plan.reason == "reset" and plan.energy <= 0.5 and plan.contour == "warm_close"
    assert plan.sound_cue == "none"


def presnap(**updates):
    base = request(**updates)
    event = base.snapshot.event.model_copy(update={"description": "", "flags": [], "yards_gained": None,
                                                    "players": [], "play_type": "pre_snap"})
    return base.model_copy(update={"mode": "setup", "hop_budget": 0,
                                   "snapshot": GameSnapshot(event=event, phase="pre_snap").seal()})


def test_presnap_crowd_builds_for_the_home_defense_and_hushes_for_its_offense():
    defense = delivery_for(presnap(down=3, distance=6, offense_team="BAL", possession="BAL", yardline=40), "KC", "a")
    offense = delivery_for(presnap(down=3, distance=6, yardline=40), "KC", "a")
    first = delivery_for(presnap(down=1, distance=10, yardline=60), "KC", "a")
    assert defense.moment == "setup" and defense.contour == "build" and defense.sound_cue == "crowd_build"
    assert defense.cue_intensity > offense.cue_intensity > 0
    assert first.sound_cue == "none"
    red_zone = delivery_for(presnap(down=3, distance=6, offense_team="BAL", possession="BAL", yardline=12), "KC", "a")
    assert red_zone.cue_intensity > defense.cue_intensity
    assert "hasn't happened yet" in delivery_direction(defense, "a")


def test_segments_carry_stingers_and_final_verdicts():
    final = request(play_type="game_end", home_score=27, away_score=20)
    segment = final.model_copy(update={"mode": "segment", "topic": "final",
                                       "snapshot": GameSnapshot(event=final.snapshot.event, phase="segment").seal()})
    winner = delivery_for(segment, "KC", "a")
    loser = delivery_for(segment, "BAL", "b")
    assert moment_for(segment) == "segment" and winner.stinger == "final"
    assert winner.energy > loser.energy and winner.contour == "explode"
    reply = delivery_for(segment.model_copy(update={"mode": "reply"}), "BAL", "b")
    assert reply.stinger == "none" and reply.sound_cue == "none"


def test_real_replays_have_deterministic_bounded_profiles_and_no_reply_cues():
    for game_id in ("2024_01_BAL_KC", "2024_22_KC_PHI"):
        for event in get_replay_events(game_id):
            payload = AgentRequest(session_id="delivery", epoch=1, exchange_id=event.id,
                                   snapshot=GameSnapshot(event=event).seal())
            plan = delivery_for(payload, event.home_team, "a")
            assert plan == delivery_for(payload, event.home_team, "a")
            assert 0 <= plan.energy <= 1 and 0.85 <= plan.browser_rate <= 1.2
            assert 0.85 <= plan.browser_pitch <= 1.2 and 0.7 <= plan.browser_volume <= 1
            reply = delivery_for(payload.model_copy(update={"mode": "reply"}), event.away_team, "b")
            assert reply.sound_cue == "none" and reply.stinger == "none"
