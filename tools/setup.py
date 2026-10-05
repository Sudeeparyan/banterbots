"""Install locked dependencies and build the dashboard on Windows, macOS or Linux."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
venv = ROOT / ".venv"
python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
if not python.exists():
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
subprocess.run([str(python), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")], check=True)
npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
if not npm:
    raise SystemExit("Install Node.js 24+ and run this setup again.")
subprocess.run([npm, "ci"], cwd=ROOT / "frontend", check=True)
subprocess.run([npm, "run", "build"], cwd=ROOT / "frontend", check=True)
print("Ready. Run: python tools/dev.py")
