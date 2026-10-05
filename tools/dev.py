"""Start all four local services without PowerShell activation or execution-policy changes."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-frontend", action="store_true", help="Serve the built dashboard on port 8000")
    args = parser.parse_args()
    python = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    interpreter = str(python) if python.exists() else sys.executable
    jobs = [([interpreter, "-m", "backend.agents", "--agent", "a", "--port", "8001"], ROOT),
            ([interpreter, "-m", "backend.agents", "--agent", "b", "--port", "8002"], ROOT),
            ([interpreter, "-m", "uvicorn", "backend.api:app", "--host", "127.0.0.1", "--port", "8000"], ROOT)]
    if not args.no_frontend:
        npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
        if not npm:
            raise SystemExit("Install Node.js and run npm install in frontend first.")
        jobs.append(([npm, "run", "dev", "--", "--host", "127.0.0.1"], ROOT / "frontend"))
    children = []
    try:
        for command, folder in jobs:
            children.append(subprocess.Popen(command, cwd=folder,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
        print("\nBanterBots: http://localhost:8000" if args.no_frontend else "\nBanterBots: http://localhost:5173")
        print("API docs: http://localhost:8000/docs | A2A agents: ports 8001, 8002")
        print("Press Ctrl+C to stop all services.\n", flush=True)
        while True:
            for child in children:
                if child.poll() is not None:
                    raise SystemExit(f"A service exited with code {child.returncode}. See its log above.")
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for child in reversed(children):
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
