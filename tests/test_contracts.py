from google.protobuf.json_format import MessageToDict
from google.protobuf.struct_pb2 import Struct
import pytest
from pydantic import ValidationError

from backend.contracts import CommentaryTurn, GameSnapshot, VocalDelivery
from backend.providers import get_replay_events
from backend.security import redact


def test_snapshot_identity_survives_protobuf_struct_numbers():
    snapshot = GameSnapshot(event=get_replay_events("2024_01_BAL_KC")[5],
                            drive_history=[{"score": 7, "gain": 3.5}],
                            recent_turns=[{"ordinal": 2}]).seal()
    wire = Struct()
    wire.update(snapshot.model_dump())
    received = GameSnapshot.model_validate(MessageToDict(wire)).seal()
    assert received.hash == snapshot.hash


def test_trace_redacts_secrets_recursively(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "secret-project-token")
    monkeypatch.setenv("LANGSMITH_API_KEY", "langsmith-secret")
    result = redact({"errors": ["Bad key: secret-project-token", "langsmith-secret"],
                     "nested": {"token": "sk-proj-abcdefghijklmnopqrstuv"}, "score": 7})
    assert result == {"errors": ["Bad key: [redacted]", "[redacted]"],
                      "nested": {"token": "[redacted]"}, "score": 7}


def test_vocal_delivery_survives_agent_wire_and_old_turns_remain_readable():
    turn = CommentaryTurn(exchange_id="exchange", agent_id="a", persona="Max", team_id="KC",
                          event_id="play", snapshot_hash="snapshot", text="Touchdown, Kansas City!",
                          delivery=VocalDelivery(energy=0.95, browser_rate=1.12, pause_before_ms=180,
                                                 sound_cue="crowd_roar", cue_intensity=0.8, reason="touchdown"))
    wire = Struct()
    wire.update(turn.model_dump())
    restored = CommentaryTurn.model_validate(MessageToDict(wire))
    assert restored.delivery == turn.delivery
    older = CommentaryTurn.model_validate(turn.model_dump(exclude={"delivery"}))
    assert older.delivery is None
    snapshot = GameSnapshot(event=get_replay_events("2024_01_BAL_KC")[5],
                            recent_turns=[turn.model_dump()]).seal()
    wire = Struct()
    wire.update(snapshot.model_dump())
    assert GameSnapshot.model_validate(MessageToDict(wire)).seal().hash == snapshot.hash


@pytest.mark.parametrize("field,value", [("energy", 1.1), ("energy", float("nan")),
                                         ("browser_rate", 3), ("browser_pitch", 0.3),
                                         ("browser_volume", 1.5), ("pause_before_ms", 5000),
                                         ("cue_intensity", -0.1), ("sound_cue", "horn")])
def test_vocal_delivery_rejects_unbounded_mix_controls(field, value):
    with pytest.raises(ValidationError):
        VocalDelivery(**{field: value})
