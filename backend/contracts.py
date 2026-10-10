"""Shared, causal contracts for providers, agents and the dashboard."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def uid() -> str:
    return uuid4().hex


class Team(BaseModel):
    id: str
    name: str
    abbreviation: str
    color: str
    alternate_color: str = "#FFFFFF"
    logo: str


class Game(BaseModel):
    id: str
    home_team: str
    away_team: str
    start_time: str
    mode: Literal["replay", "live"]
    status: str = "scheduled"
    label: str = ""
    week: int | None = None
    season: int | None = None
    home_score: int = 0
    away_score: int = 0
    quarter: int = 0
    clock: str = "15:00"
    play_count: int | None = None


class PlayEvent(BaseModel):
    id: str
    game_id: str
    source: str
    sequence: int
    revision: int = 1
    occurred_at: str | None = None
    observed_at: str = Field(default_factory=now)
    quarter: int
    clock: str
    home_team: str
    away_team: str
    home_score: int
    away_score: int
    possession: str | None = None
    offense_team: str | None = None
    scoring_team: str | None = None
    down: int | None = None
    distance: int | None = None
    yardline: int | None = None
    yards_gained: float | None = None
    description: str
    play_type: str = "unknown"
    players: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    drive: int | None = None


class GameSnapshot(BaseModel):
    id: str = Field(default_factory=uid)
    hash: str = ""
    event: PlayEvent
    drive_history: list[dict[str, Any]] = Field(default_factory=list)
    recent_turns: list[dict[str, Any]] = Field(default_factory=list)
    # "pre_snap" snapshots carry only the situation before the snap; the
    # result fields are blanked by construction, never merely hidden.
    phase: Literal["result", "pre_snap", "segment"] = "result"
    # Causal booth context: remembered remarks, predictions, listener input
    # and stats computed from earlier plays. Opinions and data, never facts
    # about the current play beyond what the event itself states.
    booth: dict[str, Any] = Field(default_factory=dict)

    def seal(self) -> "GameSnapshot":
        causal = self.model_dump(exclude={"id", "hash"})
        # Default phase/booth are omitted so snapshots sealed before these
        # fields existed keep their identity.
        if causal.get("phase") == "result":
            causal.pop("phase")
        if not causal.get("booth"):
            causal.pop("booth", None)
        # Protobuf Struct represents JSON numbers as doubles. Canonicalize
        # integral values so a wire roundtrip cannot change snapshot identity.
        def canonical(value):
            if isinstance(value, dict):
                return {key: canonical(item) for key, item in value.items()}
            if isinstance(value, list):
                return [canonical(item) for item in value]
            if isinstance(value, float) and value.is_integer():
                return int(value)
            return value

        self.hash = hashlib.sha256(json.dumps(canonical(causal), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return self


Moment = Literal["setup", "routine", "notable", "big", "ruling", "reset", "segment"]


class VocalDelivery(BaseModel):
    """A factual play's vocal direction and bounded client-side delivery controls."""
    energy: float = Field(default=0.4, ge=0, le=1)
    pace: Literal["steady", "brisk", "deliberate"] = "steady"
    contour: Literal["steady", "lift_and_settle", "stunned_drop", "dry_lilt", "suspense", "warm_close",
                     "explode", "groan", "build"] = "steady"
    browser_rate: float = Field(default=1, ge=0.85, le=1.2)
    browser_pitch: float = Field(default=1, ge=0.85, le=1.2)
    browser_volume: float = Field(default=1, ge=0.7, le=1)
    pause_before_ms: int = Field(default=160, ge=0, le=600)
    # The stadium crowd hears the play from the home side (neutral at a
    # Super Bowl). Older journals only contain the first four values.
    sound_cue: Literal["none", "crowd_swell", "crowd_roar", "crowd_gasp", "crowd_build", "crowd_cheer",
                       "crowd_groan", "crowd_boo"] = "none"
    cue_intensity: float = Field(default=0, ge=0, le=1)
    crowd_side: Literal["home", "neutral"] = "home"
    # The away team's fans in the building, heard under a home groan.
    away_pocket: float = Field(default=0, ge=0, le=1)
    stinger: Literal["none", "intro", "quarter", "halftime", "final", "score"] = "none"
    # A partner may jump in just before the lead finishes on a big moment.
    overlap_ms: int = Field(default=0, ge=0, le=400)
    moment: Moment = "routine"
    reason: Literal["ordinary", "explosive", "conversion", "touchdown", "turnover", "ruling", "reset", "score",
                    "late_tension", "stop", "setup", "segment"] = "ordinary"


class CommentaryTurn(BaseModel):
    id: str = Field(default_factory=uid)
    exchange_id: str
    agent_id: Literal["a", "b"]
    persona: str
    team_id: str
    event_id: str
    snapshot_hash: str
    text: str
    spoken_text: str | None = None
    kind: str = "call"
    emotion: str = "engaged"
    created_at: str = Field(default_factory=now)
    voice: str = ""
    mode: Literal["demo", "azure"] = "demo"
    reply_to_turn_id: str | None = None
    task_id: str | None = None
    speech_complete: bool | None = None
    speech_error: str | None = None
    delivery: VocalDelivery | None = None
    # setup | call | reply | segment; "call" and "reply" keep their old meaning.
    topic: str | None = None
    callback_to: str | None = None
    bit: str | None = None
    prediction: str | None = None
    # Set when the observed (spoken) words fail a fact check the script passed.
    fact_flag: str | None = None


class RunEvent(BaseModel):
    seq: int
    session_id: str
    epoch: int
    type: str
    timestamp: str = Field(default_factory=now)
    data: dict[str, Any] = Field(default_factory=dict)


class SessionCreate(BaseModel):
    game_id: str = "2024_01_BAL_KC"
    mode: Literal["replay", "live"] = "replay"
    provider: Literal["demo", "azure"] = "demo"
    voice_enabled: bool = True
    speed: float = Field(default=2, ge=0.25, le=8)
    start_index: int = Field(default=0, ge=0)
    date: str | None = Field(default=None, pattern=r"^\d{8}$")


class ListenerInput(BaseModel):
    """One listener action. Text is data for the booth, never an instruction."""
    side: Literal["home", "away", "neutral"] | None = None
    reaction: Literal["cheer", "boo"] | None = None
    poll_vote: Literal["convert", "stopped"] | None = None
    poll_event_id: str | None = Field(default=None, max_length=40)
    question: str | None = Field(default=None, min_length=3, max_length=200)


class SessionControl(BaseModel):
    action: Literal["play", "pause", "step", "restart", "stop", "speed"]
    speed: float | None = Field(default=None, ge=0.25, le=8)


class ReplyDraft(BaseModel):
    """A reply written while the partner was still speaking, and the script it answered."""
    text: str = Field(min_length=1, max_length=700)
    emotion: str = "engaged"
    evidence: list[str] = Field(default_factory=list)
    basis: str = Field(min_length=1, max_length=700)
    callback_to: str | None = None
    bit: str | None = None


class AgentRequest(BaseModel):
    session_id: str
    epoch: int
    exchange_id: str
    snapshot: GameSnapshot
    # exchange: lead call + peer reply. reply: answer an observed utterance.
    # draft: write (not speak) a reply to a partner's validated script.
    # setup: a pre-snap line from a pre_snap snapshot, no peer.
    # segment: a between-play exchange (topic says which) + peer reply.
    mode: Literal["exchange", "reply", "draft", "setup", "segment"] = "exchange"
    provider: Literal["demo", "azure"] = "demo"
    voice_enabled: bool = True
    peer_utterance: CommentaryTurn | None = None
    hop_budget: int = Field(default=1, ge=0, le=1)
    topic: str | None = Field(default=None, max_length=40)
    draft: ReplyDraft | None = None
