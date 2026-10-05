# Real replay data and live feed

The committed replay fixtures contain **354 actual plays**, extracted from the
[nflverse 2024 play-by-play release](https://github.com/nflverse/nflverse-data/releases/tag/pbp).
The games are Baltimore at Kansas City on September 5, 2024 (178 rows) and
Kansas City at Philadelphia on February 9, 2025 (176 rows). The kickoff times in
`games.json` are UTC; opening night is September 6 in UTC.

Credit: nflverse contributors, the nflfastR/nflverse play-by-play project and its
upstream data sources. The data project's [license](https://github.com/nflverse/nflverse-pbp/blob/master/LICENSE.md)
is [Creative Commons Attribution 4.0](https://creativecommons.org/licenses/by/4.0/).
The license is reproduced in `LICENSE.nflverse.txt`. BanterBots adapts the source
by selecting these two games, ordering plays by provider `order_sequence`,
mapping team aliases to current abbreviations, and retaining only causal game
facts. `provenance.json` records the downloaded compressed source SHA-256 and
each normalized fixture SHA-256.

Scores use **post-play `total_home_score` and `total_away_score`**, never the
source columns `home_score` and `away_score`, which hold final results on every
row. Final results, whole-game aggregates, win probabilities, EPA, and future
drive outcomes are removed. Replay game headers start at 0–0. Sequence follows
`order_sequence` because raw play IDs can move backwards around reviews.
The Baltimore final pass includes an initial touchdown call in its description;
its corrected structured flags identify an incomplete pass and reversed review,
not a touchdown. Snapshots must use those flags when interpreting the old call.

The replay down, distance and yardline describe the situation **at the start of
the recorded play**. Yardline means yards remaining to the opponent's endzone.
The ESPN adapter uses post-play end situation when supplied by the provider.
The separate `offense_team` identifies the team that began the play, so a
turnover call names the losing offense even after possession has changed.
The separate `scoring_team` is derived from each play's post-play scoreboard
change against the preceding play; this correctly credits defensive return
touchdowns without consulting the game's final score.

Rebuild with `.venv\Scripts\python.exe tools\import_nflverse.py`. The command
also caches the 32 team logos locally so the dashboard does not depend on a
third-party image request. Team metadata comes from
[nflverse teams/colors/logos](https://github.com/nflverse/nflverse-pbp/blob/master/teams_colors_logos.csv).
The logos are team/league trademarks, used here to identify clubs in a local POC;
they are not covered by an application code license.

## Research references

The requested Kaggle research example,
[NFL Play-by-Play 2015–2025 (Cleaned), by Preston Grinstead](https://www.kaggle.com/datasets/prestongrinstead/nfl-play-by-play-2015-2025-cleaned),
was verified through Kaggle's public dataset metadata on October 4, 2026. It
reports CC BY 4.0 attribution licensing. Its files are **not bundled**; the POC
uses the upstream nflverse source directly to retain provenance and exact play
fields.

## ESPN adapter

The public JSON endpoints were successfully checked on October 4, 2026:

- Scoreboard: `https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard`
- Game summary: `https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary?event=GAME_ID`
- Core fallback: `https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/events/GAME_ID/competitions/GAME_ID/plays?limit=1000`

These are public, unofficial ESPN interfaces without a guaranteed schema or
availability. Summary plays are flattened from previous and current drives,
deduplicated by ID, and sorted by numeric `sequenceNumber`. Repeated polls keep
the same revision; changed causal content increments it. Observed timestamps
do not trigger revisions. Missing summary plays use core pagination, while
failures remain visible to the coordinator. The coordinator owns the 15-second
scoreboard and 5-second play polling schedules and distinguishes poll health
from the age of the latest play. A successfully empty live feed stays live;
it never changes to replay automatically.
