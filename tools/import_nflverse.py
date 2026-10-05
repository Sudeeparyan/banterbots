"""Rebuild the two committed replay fixtures from the official nflverse release.

Run: .venv/Scripts/python.exe tools/import_nflverse.py
Only an explicit causal allowlist is written, never full source rows.
"""
from __future__ import annotations

import concurrent.futures
import csv
import gzip
import hashlib
import io
import json
from pathlib import Path
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.providers import canonical_team, normalize_nflverse  # noqa: E402

PBP_URL = "https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_2024.csv.gz"
TEAMS_URL = "https://raw.githubusercontent.com/nflverse/nflverse-pbp/master/teams_colors_logos.csv"
GAME_IDS = {"2024_01_BAL_KC": (178, "Opening night · Baltimore at Kansas City"),
            "2024_22_KC_PHI": (176, "Super Bowl LIX · Kansas City at Philadelphia")}


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "BanterBots NFL POC", "Accept": "*/*"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


def main() -> None:
    data_dir, icon_dir = ROOT / "data", ROOT / "assets" / "teams"
    data_dir.mkdir(exist_ok=True)
    icon_dir.mkdir(parents=True, exist_ok=True)
    compressed = fetch(PBP_URL)
    rows_by_game: dict[str, list[dict[str, str]]] = {key: [] for key in GAME_IDS}
    for row in csv.DictReader(io.StringIO(gzip.decompress(compressed).decode("utf-8"))):
        if row["game_id"] in rows_by_game:
            rows_by_game[row["game_id"]].append(row)
    games, files = [], {}
    for game_id, (expected_count, label) in GAME_IDS.items():
        rows = rows_by_game[game_id]
        if len(rows) != expected_count:
            raise ValueError(f"Source changed: {game_id} has {len(rows)}, expected {expected_count}")
        # Provider order_sequence is canonical; play_id can go backwards after reviews.
        rows.sort(key=lambda row: float(row.get("order_sequence") or row["play_id"]))
        events = [normalize_nflverse(row, index) for index, row in enumerate(rows)]
        path = data_dir / f"{game_id}.json"
        write_json(path, events)
        files[path.name] = {"plays": len(events), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        first = rows[0]
        # The source start_time is local to the venue; use verified UTC scheduled times.
        start_time = {"2024_01_BAL_KC": "2024-09-06T00:20:00Z", "2024_22_KC_PHI": "2025-02-09T23:30:00Z"}[game_id]
        games.append({"id": game_id, "home_team": canonical_team(first["home_team"]),
                      "away_team": canonical_team(first["away_team"]), "start_time": start_time,
                      "mode": "replay", "status": "replay", "label": label,
                      "week": int(first["week"]), "season": int(first["season"]),
                      "home_score": 0, "away_score": 0, "quarter": 1, "clock": "15:00", "play_count": len(events)})
    write_json(data_dir / "games.json", games)
    team_csv = fetch(TEAMS_URL)
    team_rows = [row for row in csv.DictReader(io.StringIO(team_csv.decode("utf-8")))
                 if row["team_abbr"] not in {"OAK", "SD", "STL", "LA"}]
    if len(team_rows) != 32:
        raise ValueError(f"Expected 32 teams, received {len(team_rows)}")

    def icon(row: dict[str, str]) -> dict[str, str]:
        team_id = canonical_team(row["team_abbr"])
        url = row["team_logo_espn"]
        try:
            contents = fetch(url)
            if not contents.startswith(b"\x89PNG"):
                raise ValueError("Logo response was not PNG")
            name = f"{team_id}.png"
            (icon_dir / name).write_bytes(contents)
        except Exception as exc:
            print(f"Logo {team_id} fallback: {exc}")
            name = f"{team_id}.svg"
            (icon_dir / name).write_text(f'<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 96 96"><rect width="96" height="96" rx="24" fill="{row["team_color"]}"/><text x="48" y="56" text-anchor="middle" font-family="Arial,sans-serif" font-weight="700" font-size="28" fill="white">{team_id}</text></svg>', encoding="utf-8")
        return {"id": team_id, "name": row["team_name"], "abbreviation": team_id,
                "color": row["team_color"], "alternate_color": row["team_color2"], "logo": f"/assets/teams/{name}"}

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        teams = list(executor.map(icon, team_rows))
    write_json(data_dir / "teams.json", teams)
    write_json(data_dir / "provenance.json", {"source": PBP_URL, "source_compressed_sha256": hashlib.sha256(compressed).hexdigest(),
               "teams_source": TEAMS_URL, "replay_files": files,
               "license": "CC BY 4.0 (nflverse play-by-play); logos remain team/league trademarks"})
    print(f"Imported {sum(len(v) for v in rows_by_game.values())} real plays and {len(teams)} team icons.")


if __name__ == "__main__":
    main()
