"""Keep credentials out of exported traces and client-visible errors."""
import os
import re


def redact(value):
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        for key in ("OPENAI_API_KEY", "LANGSMITH_API_KEY"):
            secret = os.getenv(key)
            if secret:
                value = value.replace(secret, "[redacted]")
        return re.sub(r"\bsk-(?:proj-)?[A-Za-z0-9_*-]{12,}", "[redacted]", value)
    return value


def safe_error(exc: BaseException) -> str:
    """Explain OpenAI failures without persisting credential-bearing SDK bodies."""
    from openai import APIStatusError

    current = exc
    visited = set()
    while id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, APIStatusError):
            if current.status_code == 401:
                if current.code == "invalid_api_key":
                    return ("OpenAI rejected OPENAI_API_KEY. Replace the key in .env and restart all three "
                            "Python services (API and both commentators).")
                return ("OpenAI authentication failed. Check OPENAI_API_KEY and project/organization access, "
                        "then restart all three Python services (API and both commentators).")
            if current.status_code == 403:
                return ("OpenAI denied this request. Check your project/key permissions, model access, "
                        "and whether your region is supported.")
            if current.status_code == 429:
                quota_codes = {"insufficient_quota", "credit_balance_exhausted", "organization_spend_limit_exceeded",
                               "project_spend_limit_exceeded", "organization_usage_limit_exceeded"}
                if current.code in quota_codes or current.type == "insufficient_quota":
                    return ("OpenAI quota or spending limit was reached. Check API billing, credits, "
                            "and usage limits before trying again.")
                return "OpenAI rate limit was reached. Wait before trying again and check your API rate limits."
        cause = current.__cause__ or current.__context__
        if cause is None:
            break
        current = cause
    return redact(str(exc))
