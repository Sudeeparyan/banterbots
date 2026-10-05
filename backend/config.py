import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
RUNTIME = Path(os.getenv("BANTERBOTS_RUNTIME", str(ROOT / ".runtime")))
RUNTIME.mkdir(parents=True, exist_ok=True)
AGENT_A_URL = os.getenv("AGENT_A_URL", "http://127.0.0.1:8001")
AGENT_B_URL = os.getenv("AGENT_B_URL", "http://127.0.0.1:8002")
TEXT_MODEL = os.getenv("OPENAI_TEXT_MODEL", "gpt-6-luna")
VOICE_MODEL = os.getenv("OPENAI_VOICE_MODEL", "gpt-live-1")
