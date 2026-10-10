import httpx
import pytest
from openai import APIStatusError

from backend.config import azure_base_url, azure_client, azure_configured
from backend.security import redact, safe_error


def api_failure(status, code=None, error_type=None):
    response = httpx.Response(status, request=httpx.Request(
        "POST", "https://example-resource.openai.azure.com/openai/v1/responses"))
    return APIStatusError("Private error body", response=response, body={"code": code, "type": error_type})


@pytest.mark.parametrize("status,code,error_type,expected", [
    (401, "invalid_api_key", None, "Replace the key in .env"),
    (401, "ip_not_authorized", None, "belong to the same resource"),
    (403, None, None, "network access rules"),
    (404, "DeploymentNotFound", None, "AZURE_OPENAI_TEXT_DEPLOYMENT"),
    (429, "credit_balance_exhausted", "insufficient_quota", "deployment quota"),
    (429, "project_spend_limit_exceeded", None, "spending limit"),
    (429, None, "insufficient_quota", "usage limits"),
    (429, "rate_limit_exceeded", "rate_limit_error", "Wait before trying again"),
])
def test_azure_error_guidance(status, code, error_type, expected):
    message = safe_error(api_failure(status, code, error_type))
    assert expected in message
    assert "Private error body" not in message


def test_wrapped_azure_error_and_unrelated_http_errors(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "private-token")
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


@pytest.mark.parametrize("endpoint", ["https://example-resource.openai.azure.com",
                                      "https://example-resource.openai.azure.com/",
                                      " https://example-resource.openai.azure.com/openai/v1/ "])
def test_azure_client_targets_the_resource_v1_api(monkeypatch, endpoint):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", endpoint)
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "azure-key")
    assert azure_base_url() == "https://example-resource.openai.azure.com/openai/v1/"
    client = azure_client(max_retries=0)
    assert str(client.base_url) == "https://example-resource.openai.azure.com/openai/v1/"
    assert client.api_key == "azure-key"
    assert azure_configured()
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT")
    assert not azure_configured()
