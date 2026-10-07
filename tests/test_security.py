import httpx
import pytest
from openai import APIStatusError

from backend.security import redact, safe_error


def api_failure(status, code=None, error_type=None):
    response = httpx.Response(status, request=httpx.Request("POST", "https://api.openai.com/v1/responses"))
    return APIStatusError("Private error body", response=response, body={"code": code, "type": error_type})


@pytest.mark.parametrize("status,code,error_type,expected", [
    (401, "invalid_api_key", None, "Replace the key in .env"),
    (401, "ip_not_authorized", None, "project/organization access"),
    (403, None, None, "project/key permissions"),
    (429, "credit_balance_exhausted", "insufficient_quota", "billing, credits"),
    (429, "project_spend_limit_exceeded", None, "spending limit"),
    (429, None, "insufficient_quota", "usage limits"),
    (429, "rate_limit_exceeded", "rate_limit_error", "Wait before trying again"),
])
def test_openai_error_guidance(status, code, error_type, expected):
    message = safe_error(api_failure(status, code, error_type))
    assert expected in message
    assert "Private error body" not in message


def test_wrapped_openai_error_and_unrelated_http_errors(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "private-token")
    try:
        raise api_failure(401, "invalid_api_key")
    except APIStatusError as exc:
        try:
            raise RuntimeError("Speech failed") from exc
        except RuntimeError as wrapped:
            assert "Replace the key in .env" in safe_error(wrapped)

    response = httpx.Response(401, request=httpx.Request("GET", "https://example.com/feed"))
    unrelated = httpx.HTTPStatusError("Feed rejected private-token", request=response.request, response=response)
    assert safe_error(unrelated) == "Feed rejected [redacted]"
    assert safe_error(ValueError("Invalid private-token")) == "Invalid [redacted]"


def test_masked_api_key_fragments_are_redacted():
    assert redact("Incorrect key sk-proj-abcdef******uvwxyz") == "Incorrect key [redacted]"
