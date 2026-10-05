"""Causal NFL data adapters. No provider result includes a game's final result.

ESPN's public endpoints are unofficial and may change; failures are surfaced to
the coordinator, never silently replaced by historical data.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx

from backend.contracts import Game, PlayEvent, Team, now

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
TEAM_ALIASES = {"LA": "LAR", "WSH": "WAS", "SD": "LAC", "OAK": "LV", "STL": "LAR"}
ESPN_TEAM_IDS = {
    "1": "ATL", "2": "BUF", "3": "CHI", "4": "CIN", "5": "CLE", "6": "DAL",
    "7": "DEN", "8": "DET", "9": "GB", "10": "TEN", "11": "IND", "12": "KC",
    "13": "LV", "14": "LAR", "15": "MIA", "16": "MIN", "17": "NE", "18": "NO",
    "19": "NYG", "20": "NYJ", "21": "PHI", "22": "ARI", "23": "PIT", "24": "LAC",
    "25": "SF", "26": "SEA", "27": "TB", "28": "WAS", "29": "CAR", "30": "JAX",
    "33": "BAL", "34": "HOU",
}


def canonical_team(value: str | None) -> str | None:
    return TEAM_ALIASES.get(value, value) if value else None


def _number(value: Any, default: int | None = None) -> int | None:
    try:
        return int(float(value))
    except (ValueError, TypeError, OverflowError):
        return default


def _truth(value: Any) -> bool:
    return str(value).lower() in {"1", "1.0", "true"}


def normalize_nflverse(row: dict[str, Any], sequence: int) -> dict[str, Any]:
    """Explicit allowlist: omit precomputed final scores, EPA/WP and drive outcomes.

    nflverse total_*_score is POST-play. Description can retain a reversed call;
    scoring/turnover flags come from corrected structured fields, not that text.
    """
    flags = [name for name in (
        "touchdown", "interception", "fumble_lost", "penalty", "incomplete_pass",
        "sack", "safety", "fumble", "first_down", "timeout", "touchback",
        "qb_scramble", "qb_kneel", "qb_spike", "play_deleted",
    ) if _truth(row.get(name))]
    if any(_truth(row.get(key)) for key in ("first_down_rush", "first_down_pass", "first_down_penalty")):
        if "first_down" not in flags:
            flags.append("first_down")
    result = str(row.get("replay_or_challenge_result", "")).lower()
    if result == "reversed":
        flags.extend(["reversed", "correction"])
    if row.get("play_type") == "no_play":
        flags.append("no_play")
    if row.get("field_goal_result") == "made":
        flags.append("field_goal")
    if _truth(row.get("sp")):
        flags.append("scoring")
    if any(flag in flags for flag in ("interception", "fumble_lost")):
        flags.append("turnover")
    players = list(dict.fromkeys(str(row[key]) for key in (
        "passer_player_name", "receiver_player_name", "rusher_player_name",
        "kicker_player_name", "interception_player_name", "fumble_recovery_1_player_name",
    ) if row.get(key)))
    if not row.get("play_type"):
        play_type = "game_start" if row.get("desc") == "GAME" else "game_end" if row.get("desc") == "END GAME" else "period_end"
    else:
        play_type = str(row["play_type"])
    yardage = row.get("yards_gained")
    return {
        "id": str(row["play_id"]), "game_id": str(row["game_id"]), "source": "nflverse",
        "sequence": sequence, "revision": 1, "occurred_at": row.get("time_of_day") or None,
        "quarter": _number(row.get("qtr"), 1), "clock": row.get("time") or "15:00",
        "home_team": canonical_team(row.get("home_team")), "away_team": canonical_team(row.get("away_team")),
        "home_score": _number(row.get("total_home_score"), 0),
        "away_score": _number(row.get("total_away_score"), 0),
        "possession": canonical_team(row.get("posteam")), "offense_team": canonical_team(row.get("posteam")),
        "scoring_team": canonical_team(row.get("td_team")) if "touchdown" in flags else None,
        "down": _number(row.get("down")),
        "distance": _number(row.get("ydstogo")), "yardline": _number(row.get("yardline_100")),
        "yards_gained": float(yardage) if yardage not in (None, "") else None,
        "description": row.get("desc") or "Game update", "play_type": play_type,
        "players": players, "flags": list(dict.fromkeys(flags)), "drive": _number(row.get("drive")),
    }


def get_teams() -> list[Team]:
    return [Team.model_validate(team) for team in json.loads((DATA / "teams.json").read_text(encoding="utf-8"))]


def get_replay_games() -> list[Game]:
    return [Game.model_validate(game) for game in json.loads((DATA / "games.json").read_text(encoding="utf-8"))]


def get_replay_events(game_id: str) -> list[PlayEvent]:
    # Resolve only an allowlisted ID; do not interpolate client paths unchecked.
    if game_id not in {game.id for game in get_replay_games()}:
        raise ValueError(f"Unknown replay game: {game_id}")
    rows = json.loads((DATA / f"{game_id}.json").read_text(encoding="utf-8"))
    return attribute_scores([PlayEvent.model_validate(row) for row in rows])


def attribute_scores(events: list[PlayEvent]) -> list[PlayEvent]:
    """Attribute a score only from this play's change against its predecessor.

    Never use the game header: it can contain a later or final scoreboard.
    A partial live feed's first score stays unattributed without source evidence.
    """
    previous = None
    for event in events:
        if previous is not None and "scoring" in event.flags:
            home_delta = event.home_score - previous.home_score
            away_delta = event.away_score - previous.away_score
            if home_delta > 0 and away_delta == 0:
                event.scoring_team = event.home_team
            elif away_delta > 0 and home_delta == 0:
                event.scoring_team = event.away_team
        previous = event
    return events


def _competition_game(event: dict[str, Any], competition: dict[str, Any]) -> Game:
    competitors = competition.get("competitors", [])
    home = next((item for item in competitors if item.get("homeAway") == "home"), {})
    away = next((item for item in competitors if item.get("homeAway") == "away"), {})
    if not home or not away:
        raise ValueError("ESPN competition is missing teams")
    status = competition.get("status", event.get("status", {}))
    state = status.get("type", {}).get("state", "pre")
    home_team = canonical_team(home.get("team", {}).get("abbreviation"))
    away_team = canonical_team(away.get("team", {}).get("abbreviation"))
    week = event.get("week")
    week = week.get("number") if isinstance(week, dict) else week
    return Game(
        id=str(event.get("id", competition.get("id"))), home_team=home_team,
        away_team=away_team, start_time=competition.get("date", event.get("date", "")),
        mode="live", status={"pre": "scheduled", "in": "live", "post": "final"}.get(state, state),
        label=event.get("shortName", f"{away_team} at {home_team}"),
        week=_number(week),
        season=_number(event.get("season", {}).get("year")),
        home_score=_number(home.get("score", 0), 0), away_score=_number(away.get("score", 0), 0),
        quarter=_number(status.get("period"), 0), clock=status.get("displayClock", "15:00"),
    )


def parse_scoreboard(payload: dict[str, Any]) -> list[Game]:
    return [_competition_game(event, competition) for event in payload.get("events", [])
            for competition in event.get("competitions", [])]


def flatten_summary_plays(payload: dict[str, Any]) -> list[tuple[dict[str, Any], int | None]]:
    drives = payload.get("drives") or {}
    previous = drives.get("previous") or []
    current = drives.get("current")
    all_drives = previous + ([current] if isinstance(current, dict) else [])
    # ESPN can repeat the current drive in previous. Keep the most recent copy.
    dedup: dict[str, tuple[dict[str, Any], int | None]] = {}
    for index, drive in enumerate(all_drives, 1):
        drive_number = _number(drive.get("id"), index)
        for play in drive.get("plays") or []:
            if play.get("id"):
                dedup[str(play["id"])] = (play, drive_number)
    return sorted(dedup.values(), key=lambda pair: (_number(pair[0].get("sequenceNumber"), 0), str(pair[0]["id"])))


def normalize_espn(play: dict[str, Any], game: Game, drive: int | None = None) -> PlayEvent:
    start, end = play.get("start") or {}, play.get("end") or {}
    # end is the post-play situation; fallback to start for partially populated feeds.
    situation = end if end.get("team") else start
    team = situation.get("team") or {}
    possession = canonical_team(team.get("abbreviation")) or ESPN_TEAM_IDS.get(str(team.get("id", "")))
    offense = start.get("team") or {}
    offense_team = canonical_team(offense.get("abbreviation")) or ESPN_TEAM_IDS.get(str(offense.get("id", "")))
    text = str(play.get("text") or play.get("shortText") or "Game update")
    kind = str((play.get("type") or {}).get("text", "unknown")).lower().replace(" ", "_")
    flags: list[str] = []
    scoring_type = str((play.get("scoringType") or {}).get("name", "")).replace("-", "_")
    if play.get("scoringPlay"):
        flags.append("scoring")
        # ESPN corrected type/scoringPlay, rather than stale text, determines TD.
        if "touchdown" in kind or scoring_type == "touchdown":
            flags.append("touchdown")
        elif "field_goal" in kind or scoring_type == "field_goal":
            flags.append("field_goal")
        elif "safety" in kind or scoring_type == "safety":
            flags.append("safety")
    if play.get("isPenalty"):
        flags.append("penalty")
    if play.get("isTurnover"):
        flags.append("turnover")
    if "interception" in kind:
        flags.extend(["interception", "turnover"])
    if "fumble" in kind and play.get("isTurnover"):
        flags.append("fumble_lost")
    if "sack" in kind:
        flags.append("sack")
    if "incomplete" in kind or "incompletion" in kind:
        flags.append("incomplete_pass")
    if "penalty" in kind:
        flags.append("penalty")
    if "no_play" in kind or "no play" in text.lower():
        flags.append("no_play")
    review = play.get("review") or {}
    review = review if isinstance(review, dict) else {}
    ruling = play.get("reviewResult") or play.get("replayResult") or review.get("result") or play.get("ruling") or ""
    if isinstance(ruling, dict):
        ruling = ruling.get("name") or ruling.get("text") or ruling.get("displayName") or ""
    ruling = str(ruling).lower().strip()
    if play.get("isReviewed") or ruling or "reviewed" in text.lower():
        flags.append("reviewed")
    reversal_text = "reversed" in text.lower() and "not reversed" not in text.lower()
    if ruling in {"reversed", "overturned"} or (ruling not in {"confirmed", "stands", "upheld"} and reversal_text):
        flags.extend(["reversed", "correction"])
    if _number(end.get("down")) == 1 and _number(start.get("down"), 0) > 0 and not play.get("isTurnover"):
        flags.append("first_down")
    players = []
    for participant in play.get("participants") or []:
        athlete = participant.get("athlete") or {}
        name = athlete.get("displayName") or athlete.get("fullName")
        if name:
            players.append(str(name))
    # yardsToEndzone is consistently offense-relative (yardLine alone is ambiguous).
    down = _number(situation.get("down"))
    return PlayEvent(
        id=str(play["id"]), game_id=game.id, source="espn", sequence=_number(play.get("sequenceNumber"), 0),
        occurred_at=play.get("wallclock"), quarter=_number((play.get("period") or {}).get("number"), 0),
        clock=(play.get("clock") or {}).get("displayValue", "00:00"),
        home_team=game.home_team, away_team=game.away_team,
        home_score=_number(play.get("homeScore"), 0), away_score=_number(play.get("awayScore"), 0),
        possession=possession, offense_team=offense_team, down=down if down in (1, 2, 3, 4) else None,
        distance=_number(situation.get("distance")), yardline=_number(situation.get("yardsToEndzone")),
        yards_gained=play.get("statYardage"), description=text, play_type=kind,
        players=list(dict.fromkeys(players)), flags=list(dict.fromkeys(flags)), drive=drive,
    )


class ESPNProvider:
    SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
    SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary"
    CORE = "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/events/{game_id}/competitions/{game_id}/plays"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.client = client or httpx.AsyncClient(timeout=20.0, follow_redirects=True, headers={
            "User-Agent": "Mozilla/5.0", "Accept": "application/json",
            "Referer": "https://www.espn.com/nfl/scoreboard",
        })
        self._games: dict[str, Game] = {}
        self._revisions: dict[tuple[str, str], tuple[str, int]] = {}
        self.last_poll_at: str | None = None
        self.last_success_at: str | None = None
        self.last_error: str | None = None
        self.last_play_at: str | None = None
        self.last_source = "summary"

    async def _get(self, url: str, **params: Any) -> dict[str, Any]:
        response = await self.client.get(url, params=params)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("ESPN returned a non-object response")
        return payload

    async def games(self, date: str | None = None) -> list[Game]:
        self.last_poll_at = now()
        try:
            params = {"dates": date.replace("-", "")} if date else {}
            games = parse_scoreboard(await self._get(self.SCOREBOARD, **params))
            self._games.update({game.id: game for game in games})
            self.last_success_at, self.last_error = now(), None
            return games
        except Exception as exc:
            self.last_error = f"ESPN scoreboard: {exc}"
            raise

    async def _core_plays(self, game_id: str) -> list[tuple[dict[str, Any], None]]:
        payload = await self._get(self.CORE.format(game_id=game_id), limit=1000)
        items = list(payload.get("items") or [])
        for page in range(2, int(payload.get("pageCount", 1)) + 1):
            if page > 20:
                raise ValueError("ESPN core play pagination exceeded safe limit")
            next_page = await self._get(self.CORE.format(game_id=game_id), limit=1000, page=page)
            items.extend(next_page.get("items") or [])
        dedup = {str(play["id"]): play for play in items if play.get("id")}
        return [(play, None) for play in sorted(dedup.values(), key=lambda item: _number(item.get("sequenceNumber"), 0))]

    def _revision(self, event: PlayEvent) -> PlayEvent:
        content = event.model_dump(exclude={"observed_at", "revision"})
        fingerprint = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
        key = (event.game_id, event.id)
        previous = self._revisions.get(key)
        revision = previous[1] + 1 if previous and previous[0] != fingerprint else previous[1] if previous else 1
        self._revisions[key] = (fingerprint, revision)
        return event.model_copy(update={"revision": revision})

    async def events(self, game_id: str) -> list[PlayEvent]:
        self.last_poll_at = now()
        try:
            summary_error = None
            try:
                payload = await self._get(self.SUMMARY, event=game_id)
            except (httpx.HTTPError, ValueError) as exc:
                payload, summary_error = {}, str(exc)
            header = payload.get("header") or {}
            competitions = header.get("competitions") or []
            if competitions:
                self._games[game_id] = _competition_game(header, competitions[0])
            if game_id not in self._games:
                await self.games()
            game = self._games.get(game_id)
            if not game:
                raise ValueError(f"ESPN game {game_id} has no verified team metadata")
            plays = flatten_summary_plays(payload)
            if not plays:
                plays = await self._core_plays(game_id)
                self.last_source = "core"
            else:
                self.last_source = "summary"
            normalized = attribute_scores([normalize_espn(play, game, drive) for play, drive in plays])
            events = [self._revision(event) for event in normalized]
            self.last_success_at, self.last_error = now(), None
            if summary_error:
                # Successful core recovery remains visible without failing the feed.
                self.last_error = f"Summary unavailable; using ESPN core: {summary_error}"
            if events:
                self.last_play_at = events[-1].occurred_at
            return events
        except Exception as exc:
            self.last_error = f"ESPN play-by-play: {exc}"
            raise

    async def aclose(self) -> None:
        await self.client.aclose()
