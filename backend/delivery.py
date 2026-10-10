"""A broadcast director grounded in the corrected, frozen play.

These controls describe performance and atmosphere, never game facts. The same
plan steers GPT-Live, browser speech and the stadium crowd. Energy spans the
whole range: routine snaps are engaged rather than hushed, a host's own big
moment explodes, the other side's big moment hurts out loud. Corrected rulings
never celebrate, and only the lead's turn owns a crowd reaction.
"""
from __future__ import annotations

import hashlib
import re

from backend.contracts import AgentRequest, PlayEvent, VocalDelivery

RULING_FLAGS = {"reversed", "correction", "no_play", "play_deleted"}
TURNOVER_FLAGS = {"interception", "fumble_lost", "turnover"}
SCRIMMAGE = ("pass", "reception", "run", "rush", "scramble", "spike")
RESETS = ("qb_kneel", "timeout", "period_end", "game_end", "extra_point", "game_start")


def clock_seconds(clock: str) -> int | None:
    match = re.fullmatch(r"(\d{0,2}):(\d{2})", clock.strip())
    return int(match.group(1) or 0) * 60 + int(match.group(2)) if match else None


def neutral_site(game_id: str) -> bool:
    """nflverse ids are season_week_away_home; week 22 is the Super Bowl."""
    return bool(re.match(r"^\d{4}_22_", game_id))


def late_and_close(event: PlayEvent) -> bool:
    seconds = clock_seconds(event.clock)
    return (event.quarter >= 4 and seconds is not None and seconds <= 120
            and abs(event.home_score - event.away_score) <= 8)


def scorer(event: PlayEvent) -> str | None:
    """A score belongs to the recorded scoring team; infer only a flagged return."""
    flags = set(event.flags)
    offense = event.offense_team or event.possession
    if event.scoring_team or "touchdown" not in flags:
        return event.scoring_team
    if flags & TURNOVER_FLAGS:
        if offense in {event.home_team, event.away_team}:
            return event.away_team if offense == event.home_team else event.home_team
        return None
    return offense


def penalized_team(event: PlayEvent) -> str | None:
    match = re.search(r"PENALTY on ([A-Z]{2,3})-", event.description)
    return match.group(1) if match else None


def is_ruling(event: PlayEvent) -> bool:
    flags = set(event.flags)
    return bool(flags & RULING_FLAGS or ("penalty" in flags and not flags & ({"touchdown", "scoring"} | TURNOVER_FLAGS)))


def big_play(event: PlayEvent) -> bool:
    """A result big on its own, not only because of the clock."""
    flags = set(event.flags)
    return bool("touchdown" in flags or flags & TURNOVER_FLAGS or "safety" in flags
                or ((event.yards_gained or 0) >= 20 and not flags & {"incomplete_pass", "sack"})
                or (event.source == "nflverse" and event.down == 4 and "first_down" in flags))


def moment_for(request: AgentRequest) -> str:
    """Classify the moment once; word budgets, energy and the crowd all follow it."""
    snapshot = request.snapshot
    if request.mode == "segment" or snapshot.phase == "segment":
        return "segment"
    if request.mode == "setup" or snapshot.phase == "pre_snap":
        return "setup"
    event = snapshot.event
    flags = set(event.flags)
    kind = event.play_type.lower()
    if is_ruling(event):
        return "ruling"
    if any(value in kind for value in RESETS):
        return "reset"
    yards = event.yards_gained or 0
    critical_down = event.source == "nflverse" and event.down in (3, 4)
    if big_play(event) or (late_and_close(event) and any(value in kind for value in SCRIMMAGE)):
        return "big"
    if ("scoring" in flags or "field_goal" in kind or yards >= 10 or "sack" in flags or "penalty" in flags
            or (critical_down and flags & {"first_down", "incomplete_pass"})):
        return "notable"
    return "routine"


def favors(event: PlayEvent, team_id: str) -> bool | None:
    """Whether the confirmed result is good news for this host's team (None: neither)."""
    flags = set(event.flags)
    offense = event.offense_team or event.possession
    if not offense and not event.scoring_team:
        return None
    own_attack = offense == team_id
    if "touchdown" in flags or "scoring" in flags:
        team = scorer(event) or (offense if "scoring" in flags else None)
        return None if team is None else team == team_id
    if "safety" in flags:
        return event.scoring_team == team_id if event.scoring_team else not own_attack
    if flags & TURNOVER_FLAGS or "sack" in flags:
        return not own_attack
    if "incomplete_pass" in flags and event.source == "nflverse" and event.down in (3, 4):
        return not own_attack
    if "first_down" in flags or (event.yards_gained or 0) > 0:
        return own_attack
    if (event.yards_gained or 0) < 0 or "incomplete_pass" in flags:
        return not own_attack
    return None


def _crowd(request: AgentRequest, moment: str) -> dict:
    """What a real crowd in this building does, heard from the home side."""
    event = request.snapshot.event
    flags = set(event.flags)
    neutral = neutral_site(event.game_id)
    side = "neutral" if neutral else "home"
    cue: dict = {"crowd_side": side}
    if moment == "setup":
        if event.down not in (3, 4) and not (event.yardline is not None and event.yardline <= 20) and not late_and_close(event):
            return cue
        level = {3: 0.55, 4: 0.8}.get(event.down or 0, 0.35)
        if event.yardline is not None and event.yardline <= 20:
            level += 0.15
        if late_and_close(event):
            level += 0.2
        # Home fans get loud for their defense and hush for their offense.
        offense = event.offense_team or event.possession
        if not neutral and offense == event.home_team:
            level *= 0.55
        return {**cue, "sound_cue": "crowd_build", "cue_intensity": round(min(1.0, level), 2)}
    if moment == "segment" or (moment == "reset" and "scoring" not in flags):
        return cue
    if moment == "ruling":
        # A flag on the home team draws boos; a reversal never draws a celebration.
        if (not flags & RULING_FLAGS and not neutral
                and penalized_team(event) == event.home_team):
            return {**cue, "sound_cue": "crowd_boo", "cue_intensity": 0.55}
        return cue
    home_good = favors(event, event.home_team)
    if home_good is None:
        return cue
    strength = {"big": 1.0, "notable": 0.55, "reset": 0.3, "routine": 0.0}.get(moment, 0)
    if moment == "big" and not big_play(event):
        strength = 0.55
    if not strength:
        return cue
    if neutral:
        return {**cue, "sound_cue": "crowd_roar" if strength >= 0.9 else "crowd_cheer",
                "cue_intensity": round(0.75 * strength, 2), "away_pocket": round(0.6 * strength, 2)}
    if home_good:
        return {**cue, "sound_cue": "crowd_roar" if strength >= 0.9 else "crowd_cheer",
                "cue_intensity": round(strength, 2)}
    return {**cue, "sound_cue": "crowd_groan", "cue_intensity": round(0.8 * strength, 2),
            "away_pocket": round(0.7 * strength, 2)}


STINGERS = {"intro": "intro", "quarter": "quarter", "halftime": "halftime", "final": "final"}


def delivery_for(request: AgentRequest, team_id: str, agent_id: str,
                 text: str = "", emotion: str = "engaged") -> VocalDelivery:
    event = request.snapshot.event
    flags = set(event.flags)
    reply = request.mode in {"reply", "draft"}
    moment = moment_for(request)
    good = favors(event, team_id)
    seed = int(hashlib.sha256((request.snapshot.hash + agent_id + "delivery").encode()).hexdigest()[:8], 16)
    variation = (seed % 5 - 2) / 100
    base_rate, base_pitch = (1.04, 1.02) if agent_id == "a" else (0.99, 0.95)

    reason, contour, pace = "ordinary", "steady", "steady"
    if moment == "setup":
        crowd = _crowd(request, moment)
        tension = crowd.get("cue_intensity", 0) if crowd.get("sound_cue") == "crowd_build" else 0
        energy = 0.55 + (0.3 if tension >= 0.5 else 0.18 if tension else 0)
        contour, pace, reason = "build", "brisk", "setup"
    elif moment == "segment":
        topic = request.topic or ""
        if topic == "final":
            own = event.home_score if team_id == event.home_team else event.away_score
            other = event.away_score if team_id == event.home_team else event.home_score
            energy, contour = (0.9, "explode") if own > other else (0.6, "stunned_drop") if own < other else (0.7, "lift_and_settle")
        elif topic == "intro":
            energy, contour = 0.8, "lift_and_settle"
        else:
            energy, contour = 0.62, "dry_lilt" if agent_id == "b" else "warm_close"
        reason = "segment"
    elif moment == "ruling":
        energy, contour, pace, reason = 0.55, "suspense", "deliberate", "ruling"
    elif moment == "reset":
        energy, contour, pace, reason = 0.45, "warm_close", "steady", "reset"
        if "scoring" in flags and good:
            energy = 0.55
    elif moment == "big":
        reason = ("touchdown" if "touchdown" in flags else "turnover" if flags & (TURNOVER_FLAGS | {"safety"})
                  else "explosive" if big_play(event) else "late_tension")
        if reason == "late_tension":
            # A short gain late in a close game is tense, not a touchdown.
            energy, contour, pace = (0.8 if good else 0.7), "suspense", "brisk"
        elif good:
            energy, contour, pace = (1.0 if reason == "touchdown" else 0.92), "explode", "brisk"
        else:
            energy, contour, pace = 0.75, "groan", "brisk"
    elif moment == "notable":
        reason = ("score" if "scoring" in flags or "field_goal" in flags else "conversion" if "first_down" in flags
                  else "stop" if "incomplete_pass" in flags else "explosive" if (event.yards_gained or 0) >= 10
                  else "ruling" if "penalty" in flags else "ordinary")
        energy, contour = (0.76, "lift_and_settle") if good else (0.62, "stunned_drop") if good is False else (0.66, "steady")
        pace = "brisk"
    else:
        energy = (0.56 if agent_id == "a" else 0.52) + variation
        contour = "dry_lilt" if reply and agent_id == "b" else "steady"

    crowd = {} if reply else _crowd(request, moment)
    if request.mode == "segment" and not reply:
        stinger = STINGERS.get(request.topic or "", "none")
        if stinger != "none":
            crowd = {**crowd, "stinger": stinger}
    plan = VocalDelivery(energy=round(min(1.0, max(0.3, energy)), 2), pace=pace, contour=contour,
                         moment=moment, reason=reason,
                         pause_before_ms=260 if moment == "setup" else 120 if moment == "segment" else 70,
                         browser_rate=base_rate, browser_pitch=base_pitch, browser_volume=0.95, **crowd)

    if reply:
        # The partner answers from their own allegiance: joy for their team's
        # moment can exceed the lead's, pain stays dramatic, routine needling
        # stays quick. No second crowd reaction.
        overlap = 0
        if moment == "big":
            overlap = 260 if good else 140
        elif moment == "notable":
            overlap = 80
        plan = plan.model_copy(update={
            "energy": round(max(0.35, plan.energy - 0.05), 2),
            "pace": "brisk" if moment in {"big", "notable"} else "steady",
            "contour": plan.contour if plan.contour in {"explode", "groan", "stunned_drop", "suspense"} else
            ("dry_lilt" if agent_id == "b" or good is False else "lift_and_settle" if good else "steady"),
            "sound_cue": "none", "cue_intensity": 0, "away_pocket": 0, "stinger": "none",
            "overlap_ms": overlap, "pause_before_ms": 0 if overlap else 40})
    if plan.reason == "ordinary" and emotion == "reflective":
        plan = plan.model_copy(update={"contour": "warm_close"})
    lift = (plan.energy - 0.55) * 0.16
    return plan.model_copy(update={
        "browser_rate": round(min(1.2, max(0.85, base_rate * (1 + lift * 0.8))), 3),
        "browser_pitch": round(min(1.2, max(0.85, base_pitch + lift)), 3),
        "browser_volume": round(min(1.0, max(0.7, 0.86 + plan.energy * 0.14)), 3),
    })


def _intensity(energy: float) -> str:
    if energy >= 0.9:
        return "Full-throated: this is as loud and excited as you get."
    if energy >= 0.74:
        return "Big and loud, real adrenaline in the voice."
    if energy >= 0.6:
        return "Lively and animated."
    if energy >= 0.45:
        return "Engaged and quick, like you're enjoying the game with a friend."
    return "Relaxed and warm."


CONTOURS = {
    "steady": "Natural and quick: punch the player and the result, keep it moving.",
    "lift_and_settle": "Lift with genuine enthusiasm on the result, hit the key word and number, then ease into what it means.",
    "stunned_drop": "A quick, surprised reaction, a beat, then a lower, rueful finish that still gives credit.",
    "dry_lilt": "Amused and needling: a grin in the voice, a beat before the punchline, a short laugh if it's genuinely funny.",
    "suspense": "Hold the tension with deliberate diction and a small beat before the ruling, then react to what it means. The play is already complete; don't act out unseen action.",
    "warm_close": "Warm and conversational, like a recap with a friend.",
    "explode": "This is your team's big moment. Explode: shout the result, stretch the key word, let your voice crack, laugh or whoop with joy, then catch your breath on the consequence.",
    "groan": "This one hurts your team. React out loud: a real groan or 'oh no', dramatic disbelief, then grudging credit. Big emotion, not flat.",
    "build": "Build anticipation: crisp down, distance and field position, tension rising toward the snap. The play hasn't happened yet; never guess its result as fact.",
}
PACES = {"steady": "Easy conversational pace.",
         "brisk": "Quick and urgent through the setup, then let the payoff ring out.",
         "deliberate": "Deliberate, without drawing out every word."}


def personality(agent_id: str) -> str:
    return ("Warm, quick-witted home-side voice with an easy laugh and crisp consonants."
            if agent_id == "a" else
            "Dry, sharp away-side voice with a lower settled register who can still go off when it matters.")


def delivery_direction(plan: VocalDelivery, agent_id: str, *, reply: bool = False) -> str:
    """Trusted, concise performance direction; never part of the script."""
    role = {
        "setup": "You're setting up the next snap as play-by-play.",
        "segment": "You're talking between plays; sound like two friends in a booth, not a report.",
    }.get(plan.moment, "Answer your partner like someone sitting right beside you: react first, then your point."
          if reply else "You're calling the play as it lands.")
    return (f"{personality(agent_id)} {role} {_intensity(plan.energy)} {CONTOURS[plan.contour]} {PACES[plan.pace]} "
            "Vary pitch and rhythm; uneven pauses are fine. Names and numbers stay clear. "
            "Never say stage directions or sound effects, add facts, or imitate a real person.")
