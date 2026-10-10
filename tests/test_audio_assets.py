"""Every sound the dashboard ships is licensed, listed and unchanged."""
import hashlib
import json
from pathlib import Path

AUDIO = Path(__file__).resolve().parents[1] / "frontend" / "public" / "audio"


def test_every_audio_file_is_listed_with_its_license_and_exact_hash():
    licenses = (AUDIO / "LICENSES.md").read_text(encoding="utf-8")
    files = sorted(path for path in AUDIO.rglob("*") if path.suffix.lower() in {".mp3", ".wav", ".ogg", ".flac"})
    assert files, "no audio shipped"
    for path in files:
        name = path.relative_to(AUDIO).as_posix()
        row = next((line for line in licenses.splitlines() if f"`{name}`" in line), None)
        assert row, f"{name} is not listed in LICENSES.md"
        assert hashlib.sha256(path.read_bytes()).hexdigest() in row, f"{name} changed since it was licensed"
        # Recordings are CC0 with a source page; designed sound names the script that makes it.
        assert ("CC0" in row and "freesound.org" in row) if name.startswith("crowd/") else "make_stingers.py" in row


def test_manifest_only_names_shipped_files_and_measured_gains():
    manifest = json.loads((AUDIO / "manifest.json").read_text(encoding="utf-8"))
    for group in ("layers", "stingers"):
        for name, entry in manifest[group].items():
            assert (AUDIO / entry["file"]).is_file(), f"{group}.{name} points at a missing file"
            assert -40 <= entry["gain_db"] <= 20 and entry["target_lufs"] == -20
            if "start" in entry:
                assert 0 <= entry["start"] < entry["end"]
