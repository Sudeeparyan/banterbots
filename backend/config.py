import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
RUNTIME = Path(os.getenv("BANTERBOTS_RUNTIME", str(ROOT / ".runtime")))
RUNTIME.mkdir(parents=True, exist_ok=True)
AGENT_A_URL = os.getenv("AGENT_A_URL", "http://127.0.0.1:8001")
AGENT_B_URL = os.getenv("AGENT_B_URL", "http://127.0.0.1:8002")
# Azure OpenAI addresses models by deployment name, not by model name.
TEXT_MODEL = os.getenv("AZURE_OPENAI_TEXT_DEPLOYMENT", "gpt-6-luna")
VOICE_MODEL = os.getenv("AZURE_OPENAI_VOICE_DEPLOYMENT", "gpt-live-1")


def azure_configured() -> bool:
    return bool(os.getenv("AZURE_OPENAI_API_KEY") and os.getenv("AZURE_OPENAI_ENDPOINT"))


def azure_base_url() -> str:
    """Azure OpenAI v1 API root; accepts a resource endpoint or one already ending in /openai/v1."""
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "").strip().rstrip("/")
    if not endpoint.endswith("/openai/v1"):
        endpoint += "/openai/v1"
    return endpoint + "/"


def azure_client(**options):
    """The v1 API mirrors OpenAI's surface (Responses and live sessions) on the Azure resource."""
    from openai import AsyncOpenAI
    return AsyncOpenAI(api_key=os.getenv("AZURE_OPENAI_API_KEY"), base_url=azure_base_url(), **options)
