"""Keep credentials out of exported traces and client-visible errors."""
import os
import re

RESTART = "then restart all three Python services (API and both commentators)."


def redact(value):
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        for key in ("AZURE_OPENAI_API_KEY", "LANGSMITH_API_KEY"):
            secret = os.getenv(key)
            if secret:
                value = value.replace(secret, "[redacted]")
        return re.sub(r"\bsk-(?:proj-)?[A-Za-z0-9_*-]{12,}", "[redacted]", value)
    return value


def safe_error(exc: BaseException) -> str:
    """Explain Azure OpenAI failures without persisting credential-bearing SDK bodies."""
    from openai import APIStatusError

    current = exc
    visited = set()
    while id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, APIStatusError):
            if current.status_code == 401:
                if current.code == "invalid_api_key":
                    return ("Azure OpenAI rejected AZURE_OPENAI_API_KEY. Replace the key in .env, confirm it belongs "
                            f"to the resource in AZURE_OPENAI_ENDPOINT, {RESTART}")
                return ("Azure OpenAI authentication failed. Check AZURE_OPENAI_API_KEY and AZURE_OPENAI_ENDPOINT "
                        f"belong to the same resource, {RESTART}")
            if current.status_code == 403:
                return ("Azure OpenAI denied this request. Check the resource's network access rules, key permissions "
                        "and deployment access.")
            if current.status_code == 404 and current.code in {"DeploymentNotFound", "model_not_found"}:
                return ("Azure OpenAI could not find the deployment. Set AZURE_OPENAI_TEXT_DEPLOYMENT and "
                        f"AZURE_OPENAI_VOICE_DEPLOYMENT to deployment names on this resource, {RESTART}")
            if current.status_code == 429:
                quota_codes = {"insufficient_quota", "credit_balance_exhausted", "organization_spend_limit_exceeded",
                               "project_spend_limit_exceeded", "organization_usage_limit_exceeded"}
                if current.code in quota_codes or current.type == "insufficient_quota":
                    return ("Azure OpenAI quota or spending limit was reached. Check the deployment quota, "
                            "subscription credits and usage limits before trying again.")
                return ("Azure OpenAI rate limit was reached. Wait before trying again or raise the deployment's "
                        "tokens-per-minute limit.")
        cause = current.__cause__ or current.__context__
        if cause is None:
            break
        current = cause
    return redact(str(exc))
