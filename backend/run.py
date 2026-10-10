"""Start the coordinator API and both A2A agents: `python run.py` from this folder."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    python = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    interpreter = str(python) if python.exists() else sys.executable
    jobs = [[interpreter, "-m", "backend.agents", "--agent", "a", "--port", "8001"],
            [interpreter, "-m", "backend.agents", "--agent", "b", "--port", "8002"],
            [interpreter, "-m", "uvicorn", "backend.api:app", "--host", "127.0.0.1", "--port", "8000"]]
    children = []
    interrupted = False
    try:
        # Children share this console, so their logs appear here and Ctrl+C reaches them too.
        for command in jobs:
            children.append(subprocess.Popen(command, cwd=ROOT))
        dashboard = "dashboard" if (ROOT / "frontend" / "dist").exists() else "API"
        print(f"\nBanterBots {dashboard}: http://localhost:8000")
        print("API docs: http://localhost:8000/docs | A2A agents: ports 8001, 8002")
        print("Press Ctrl+C to stop all services.\n", flush=True)
        while True:
            for child in children:
                if child.poll() is not None:
                    raise SystemExit(f"A service exited with code {child.returncode}. See its log above.")
            time.sleep(0.5)
    except KeyboardInterrupt:
        interrupted = True
    finally:
        if not interrupted:
            for child in children:
                if child.poll() is None:
                    child.terminate()
        for child in children:
            try:
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    main()
