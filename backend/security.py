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
        return re.sub(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{12,}", "[redacted]", value)
    return value
