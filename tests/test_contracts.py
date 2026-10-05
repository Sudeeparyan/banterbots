from google.protobuf.json_format import MessageToDict
from google.protobuf.struct_pb2 import Struct

from backend.contracts import GameSnapshot
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
    monkeypatch.setenv("OPENAI_API_KEY", "secret-project-token")
    monkeypatch.setenv("LANGSMITH_API_KEY", "langsmith-secret")
    result = redact({"errors": ["Bad key: secret-project-token", "langsmith-secret"],
                     "nested": {"token": "sk-proj-abcdefghijklmnopqrstuv"}, "score": 7})
    assert result == {"errors": ["Bad key: [redacted]", "[redacted]"],
                      "nested": {"token": "[redacted]"}, "score": 7}
