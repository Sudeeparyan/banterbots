import hashlib
import json

import httpx
import pytest

from backend.contracts import Game, GameSnapshot
from backend.providers import (
    DATA, ROOT, ESPNProvider, flatten_summary_plays, get_replay_events,
    get_replay_games, get_teams, normalize_espn, normalize_nflverse, parse_scoreboard,
)


def live_game():
    return Game(id="123", home_team="KC", away_team="BAL", start_time="2026-10-04T17:00:00Z", mode="live")


def espn_play(id="p1", sequence=100, text="A run for four yards"):
    return {"id": id, "sequenceNumber": str(sequence), "text": text,
            "type": {"text": "Rush"}, "homeScore": 0, "awayScore": 0,
            "period": {"number": 1}, "clock": {"displayValue": "14:20"},
            "wallclock": "2026-10-04T17:01:00Z", "statYardage": 4,
            "start": {"down": 1, "distance": 10, "team": {"id": "12"}},
            "end": {"down": 2, "distance": 6, "yardsToEndzone": 66, "team": {"id": "12"}}}


def summary(plays):
    return {"header": {"id": "123", "competitions": [{"id": "123", "date": "2026-10-04T17:00Z",
             "competitors": [{"homeAway": "home", "team": {"abbreviation": "KC"}, "score": "99"},
                             {"homeAway": "away", "team": {"abbreviation": "BAL"}, "score": "99"}]}]},
            "drives": {"previous": [{"id": "1", "plays": plays}]}}


def test_replays_are_real_complete_and_open_at_zero():
    expected = {"2024_01_BAL_KC": (178, 27, 20), "2024_22_KC_PHI": (176, 40, 22)}
    for game in get_replay_games():
        events = get_replay_events(game.id)
        count, home, away = expected[game.id]
        assert len(events) == game.play_count == count
        assert game.home_score == game.away_score == events[0].home_score == events[0].away_score == 0
        assert (events[-1].home_score, events[-1].away_score) == (home, away)
        assert events[0].description == "GAME" and events[-1].description == "END GAME"
        assert [event.sequence for event in events] == list(range(count))


def test_post_play_scores_and_no_future_fields_in_snapshot():
    events = get_replay_events("2024_01_BAL_KC")
    first_score = next(index for index, event in enumerate(events) if "touchdown" in event.flags)
    assert (events[first_score - 1].home_score, events[first_score - 1].away_score) == (0, 0)
    assert (events[first_score].home_score, events[first_score].away_score) == (0, 6)
    assert (events[first_score + 1].home_score, events[first_score + 1].away_score) == (0, 7)
    raw = {"play_id": "1", "game_id": "sample", "home_team": "KC", "away_team": "BAL",
           "qtr": "1", "desc": "A four yard run", "total_home_score": "0", "total_away_score": "0",
           "home_score": "27", "away_score": "20", "result": "7", "drive_ended_with_score": "1",
           "total_home_epa": "20", "wp": "0.99", "fixed_drive_result": "Touchdown"}
    normalized = normalize_nflverse(raw, 0)
    assert normalized["home_score"] == normalized["away_score"] == 0
    assert not {"result", "wp", "fixed_drive_result", "total_home_epa", "drive_ended_with_score"} & normalized.keys()
    snapshot = GameSnapshot(event=events[first_score]).seal().model_dump()
    assert snapshot["event"]["away_score"] == 6 and snapshot["drive_history"] == []


def test_overturned_final_touchdown_is_incomplete_and_does_not_score():
    events = get_replay_events("2024_01_BAL_KC")
    event = next(item for item in events if item.id == "4221")
    assert "TOUCHDOWN" in event.description and "REVERSED" in event.description
    assert {"reversed", "correction", "incomplete_pass"} <= set(event.flags)
    assert "touchdown" not in event.flags and "scoring" not in event.flags
    assert (event.home_score, event.away_score) == (27, 20)
    assert (events[event.sequence - 1].home_score, events[event.sequence - 1].away_score) == (27, 20)


def test_penalties_and_turnovers_preserve_structured_flags():
    events = get_replay_events("2024_22_KC_PHI")
    assert any("penalty" in item.flags for item in events)
    pick_six = next(item for item in events if {"touchdown", "interception"} <= set(item.flags))
    assert "turnover" in pick_six.flags and pick_six.home_score == 16
    assert pick_six.id == "1468" and pick_six.possession == "KC" and pick_six.scoring_team == "PHI"
    assert any("fumble_lost" in item.flags and "turnover" in item.flags for item in events)


def test_exactly_current_32_teams_and_cached_icons():
    teams = get_teams()
    assert len(teams) == len({team.id for team in teams}) == 32
    assert {"LAR", "WAS", "LV", "LAC"} <= {team.id for team in teams}
    assert not {"LA", "WSH", "OAK", "SD", "STL"} & {team.id for team in teams}
    for team in teams:
        assert (ROOT / team.logo.lstrip("/")).is_file()


def test_bundled_file_hashes_match_provenance():
    provenance = json.loads((DATA / "provenance.json").read_text())
    for name, details in provenance["replay_files"].items():
        assert hashlib.sha256((DATA / name).read_bytes()).hexdigest() == details["sha256"]


def test_unknown_replay_id_cannot_access_paths():
    with pytest.raises(ValueError, match="Unknown replay"):
        get_replay_events("../teams")


def test_summary_deduplicates_current_drive_and_orders_numeric_sequence():
    p1, p2 = espn_play("p1", 9), espn_play("p2", 10)
    p1_new = {**p1, "text": "Corrected description"}
    payload = {"drives": {"previous": [{"id": "1", "plays": [p2, p1]}],
                          "current": {"id": "1", "plays": [p1_new, p2]}}}
    rows = flatten_summary_plays(payload)
    assert [play["id"] for play, _ in rows] == ["p1", "p2"]
    assert rows[0][0]["text"] == "Corrected description"


def test_espn_normalizes_post_play_situation_and_corrected_type():
    raw = espn_play(text="Originally TOUCHDOWN. Play was REVERSED. Pass incomplete.")
    raw["type"] = {"text": "Pass Incompletion"}
    event = normalize_espn(raw, live_game())
    assert event.possession == "KC" and event.down == 2 and event.distance == 6 and event.yardline == 66
    assert "touchdown" not in event.flags and "scoring" not in event.flags
    assert "reversed" in event.flags
    assert event.home_score == 0  # Header's final/current score is never copied into a play.


def test_reviewed_confirmed_touchdown_keeps_scoring_without_reversed_flag():
    raw = espn_play(text="Passing touchdown. The Replay Official reviewed the play and confirmed the ruling.")
    raw.update({"isReviewed": True, "reviewResult": "confirmed", "scoringPlay": True,
                "scoringType": {"name": "touchdown"}, "type": {"text": "Passing Touchdown"}, "homeScore": 7})
    event = normalize_espn(raw, live_game())
    assert {"touchdown", "scoring", "reviewed"} <= set(event.flags)
    assert not {"reversed", "correction"} & set(event.flags)
    assert event.home_score == 7
    raw.update({"reviewResult": {"name": "reversed"}, "scoringPlay": False,
                "type": {"text": "Pass Incompletion"}, "homeScore": 0, "text": "Incomplete pass after review."})
    event = normalize_espn(raw, live_game())
    assert {"reviewed", "reversed", "correction", "incomplete_pass"} <= set(event.flags)
    assert "touchdown" not in event.flags and event.home_score == 0


def test_turnover_keeps_action_team_distinct_from_post_play_possession():
    raw = espn_play(text="Mahomes pass intercepted. Baltimore takes possession.")
    raw.update({"type": {"text": "Interception"}, "isTurnover": True})
    raw["end"]["team"] = {"id": "33"}
    event = normalize_espn(raw, live_game())
    assert event.offense_team == "KC" and event.possession == "BAL"
    assert {"interception", "turnover"} <= set(event.flags)
    nflverse = normalize_nflverse({"play_id": "1", "game_id": "sample", "posteam": "LA"}, 0)
    assert nflverse["offense_team"] == nflverse["possession"] == "LAR"


@pytest.mark.asyncio
async def test_live_defensive_score_uses_causal_delta_and_ignores_final_header():
    baseline = espn_play("p1", 1)
    pick_six = espn_play("p2", 2, "Interception returned for a Baltimore touchdown.")
    pick_six.update({"type": {"text": "Interception Return Touchdown"}, "isTurnover": True,
                     "scoringPlay": True, "awayScore": 6})
    payload = summary([baseline, pick_six])  # Header deliberately holds a bogus 99-99 score.
    provider = ESPNProvider(httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))))
    try:
        events = await provider.events("123")
        assert events[0].scoring_team is None
        assert events[1].offense_team == "KC" and events[1].scoring_team == "BAL"
        assert (events[1].home_score, events[1].away_score) == (0, 6)
        assert events[1].revision == (await provider.events("123"))[1].revision == 1
        payload["drives"]["previous"][0]["plays"][1]["awayScore"] = 0
        payload["drives"]["previous"][0]["plays"][1]["scoringPlay"] = False
        corrected = (await provider.events("123"))[1]
        assert corrected.scoring_team is None and corrected.revision == 2
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_repeated_polls_do_not_create_revisions_but_corrections_do():
    payload = summary([espn_play()])
    def respond(request):
        return httpx.Response(200, json=payload)
    provider = ESPNProvider(httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    try:
        first, again = await provider.events("123"), await provider.events("123")
        assert first[0].revision == again[0].revision == 1
        assert first[0].home_score == 0
        payload["drives"]["previous"][0]["plays"][0]["text"] = "Play description corrected"
        changed = await provider.events("123")
        assert changed[0].revision == 2
        assert provider.last_success_at and provider.last_play_at == first[0].occurred_at
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_core_fallback_and_feed_failure_are_visible():
    paths = []
    def respond(request):
        paths.append(request.url.path)
        if "summary" in request.url.path:
            return httpx.Response(200, json=summary([]))
        return httpx.Response(200, json={"pageCount": 1, "items": [espn_play()]})
    provider = ESPNProvider(httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    try:
        events = await provider.events("123")
        assert len(events) == 1 and provider.last_source == "core"
        assert any(path.endswith("/plays") for path in paths)
    finally:
        await provider.aclose()

    def broken(request):
        return httpx.Response(503, json={"error": "unavailable"})
    provider = ESPNProvider(httpx.AsyncClient(transport=httpx.MockTransport(broken)))
    provider._games["123"] = live_game()
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await provider.events("123")
        assert provider.last_error and provider.last_success_at is None
    finally:
        await provider.aclose()


def test_scoreboard_canonical_teams_and_live_score():
    item = summary([])["header"]
    item["week"] = 4  # summary uses an integer; scoreboard can use {number: 4}.
    item["competitions"][0]["competitors"][0]["team"]["abbreviation"] = "WSH"
    item["competitions"][0]["competitors"][1]["team"]["abbreviation"] = "LA"
    item["competitions"][0]["status"] = {"type": {"state": "in"}, "period": 3, "displayClock": "5:20"}
    game = parse_scoreboard({"events": [item]})[0]
    assert game.home_team == "WAS" and game.away_team == "LAR" and game.status == "live"
    assert game.home_score == 99 and game.quarter == 3 and game.clock == "5:20"
    assert game.week == 4
    item["week"] = {"number": 4}
    assert parse_scoreboard({"events": [item]})[0].week == 4
