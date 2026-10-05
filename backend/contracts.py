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

    def seal(self) -> "GameSnapshot":
        causal = self.model_dump(exclude={"id", "hash"})
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
    mode: Literal["demo", "openai"] = "demo"
    reply_to_turn_id: str | None = None
    task_id: str | None = None
    speech_complete: bool | None = None
    speech_error: str | None = None


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
    provider: Literal["demo", "openai"] = "demo"
    voice_enabled: bool = True
    speed: float = Field(default=2, ge=0.25, le=8)
    start_index: int = Field(default=0, ge=0)
    date: str | None = Field(default=None, pattern=r"^\d{8}$")


class SessionControl(BaseModel):
    action: Literal["play", "pause", "step", "restart", "stop", "speed"]
    speed: float | None = Field(default=None, ge=0.25, le=8)


class AgentRequest(BaseModel):
    session_id: str
    epoch: int
    exchange_id: str
    snapshot: GameSnapshot
    mode: Literal["exchange", "reply"] = "exchange"
    provider: Literal["demo", "openai"] = "demo"
    voice_enabled: bool = True
    peer_utterance: CommentaryTurn | None = None
    hop_budget: int = Field(default=1, ge=0, le=1)
