# POC validation

## Broadcast rebuild — October 10, 2026

This pass covers the work in [BROADCAST_RESEARCH.md](BROADCAST_RESEARCH.md): play-by-play and colour roles, a line before
each snap, fast handoffs, CC0 stadium sound, the listener seat, and the master stage. All checks ran on Windows against the
configured Azure resource, using expressive voices (GPT-Live).

### Checks

- Backend: **224 tests passed**; Ruff is clean. New coverage includes:
  - early replies, both kept and withdrawn;
  - the moderation retry, and the duration cap measured from first speech;
  - laughter kept in transcripts;
  - unknown callbacks dropped;
  - the demo fallback after failed drafts;
  - screen-follows-audio (`present_after`);
  - the setup line overlapping the call;
  - prefetch adoption, and cancelling a prefetch on halt;
  - poll scoring at the next booth, with no outcome leaked to the browser;
  - every shipped audio file listed with its license and exact hash.
- Frontend: **107 tests passed**; TypeScript, formatting and the production build passed. New coverage includes:
  - recorded stadium layers, with the procedural fallback;
  - the master stage;
  - stingers that reactions cannot fade;
  - early handoffs to another host, and same-host lines kept serial;
  - natural Demo voices preferred;
  - the presenter holding the screen until the audio arrives;
  - polls and the listener seat.
- Browser: **19 Chromium checks passed** against an isolated Demo stack. These include possession leads, setup cards
  and the new "your seat" panel.
- Launcher: `tools/dev.py` started healthy and served the audio assets.

### Listening comparison

`tools/listen_compare.mjs` drove the real dashboard in Chromium at 2× speed and recorded the browser's own output. It
ran the same nine BAL@KC scenarios before any change and after the last one:

| Scenario | Replay index |
|---|---|
| Opening drive | 1–5, continuous, including the 3rd-down conversion |
| 3rd down | 5 |
| BAL touchdown | 14 |
| KC touchdown | 21 |
| Turnover | 36 |
| Halftime | 88 |
| Long gain | 91 |
| Reversed BAL touchdown | 176 |
| End of game | 177 |

Gaps are measured from audible samples in the recording, not from the schedule.

| Metric | Before | After |
|---|---|---|
| Handoff between hosts, median / p90 | 7,601 / 10,300 ms | **252 / 334 ms** |
| Silence between plays, median / p90 | 10,625 / 13,025 ms | **468 / 4,816 ms** |
| Same host, setup line to call, median / p90 | no setup line | 280 / 1,918 ms |
| Trailing silence inside a line | 1,147 ms | **125 ms** |
| Words per minute | 148 | 144 |
| Lines / words per play | 2.0 / 45 | 2.9 / 69 |
| Lines with a reaction (laugh, "Oh!", a repeated word) | 15% | **45%** |
| Lines with a validated callback | 0% | **18%** |
| Spoken-fact flags | 0 | 0 |
| Integrated loudness | −18.8 to −21.6 LUFS | **−14.5 to −16.9 LUFS** |
| True peak | −2.3 to −3.9 dBTP | −2.6 to −3.4 dBTP |

The p90 between plays (4.8 s) comes from the first play of a session, where the voice session is still cold.
Once the drive is under way, the gap between plays is about half a second.

### Cost per game

The opening drive is the only continuous scenario, so it gives the fairest per-game figure.

| | Before | After |
|---|---|---|
| Recorded time for 5 plays | 189.9 s | 139.2 s |
| Words spoken | 228 | 263 |
| Cost for a 178-play game | $3.17 | **$3.95** (voice $3.71, text $0.24) |

Prices use the list rate for GPT-Live ($0.05 per minute of open session) and the text model's token prices. Azure does not
publish a separate GPT-Live rate.

Across all nine scenarios the figure is $5.03 per game, up from $3.28. That number is inflated: each single-play scenario
opens a cold session and pays the first line's 2–10 s start-up.

### Known limits

- **Cold first line.** The first line on a fresh voice session takes 2–10 s to start. Persistent sessions then take 1.3–2.3 s
  from cue to first audio. This first line is the 4.8 s p90 between plays.
- **Idle billing.** A persistent session is billed for up to 25 s of idle time before it closes.
- **Recordings use the Opus codec.** The WAVs were captured through MediaRecorder (Opus), then decoded. Codec overshoot is
  included in the true-peak figures.
- **What GPT-Live can't do.** It laughs, stretches words and lifts its energy for big moments. Its "explode" lines are only
  about 1–2 dB louder than routine ones, and it cannot speak two voices in one call, so overlaps are scheduled by the browser.
  - A second engine would need a new key and was not added.
  - The research notes compare Azure Speech HD (Omni) and ElevenLabs dialogue as the next experiment.

### Two-minute listening checklist

Play `.runtime/listening/before/<name>.wav` and then the matching `after/<name>.wav`.

1. **`opening_drive.wav`, first 60 s.** Can you hear the down and distance before each snap? Is any gap between voices
   longer than a breath?
2. **`third_down.wav`.** Does the crowd build before the snap and answer the conversion?
3. **`td_bal.wav`.** Does the call make you sit up? Does the KC host groan rather than cheer, and the away pocket lift?
4. **`reversed_td.wav`.** The touchdown is overturned. You should hear groans or boos and no roar, and nobody celebrates the
   score.
5. **Any file.** Does anyone laugh or needle the other host? Does a line call back to something said earlier?
6. **At low volume.** Turn it right down. Is every word still clear over the crowd?
7. **In the app** (about 30 s). Pick a side and press Cheer, then Boo. On a 3rd down, vote in the poll and ask the booth a
   question. Then pause, mute, restart and switch games: each one should stop every sound at once.

### Where the evidence is

All of this is ignored by Git.

- `.runtime/listening/before/` and `.runtime/listening/after/` hold the WAV for each scenario, its timeline JSON and
  `metrics.json`.
- `runtime/` holds the service logs and journals.
- Earlier "after" passes are kept as `after-v1/` and `after-v2/`.

## Broadcast audio follow-up — October 9, 2026

The current code was checked on Windows with the configured Azure resource. These results supersede the earlier credential and audio-access limitations below.

- Backend: **185 tests passed**. Ruff passed for `backend`, `tools`, and `tests`. Delivery checks cover corrected rulings, retained turnover/scoring penalties, allegiance, lead/reply contrast, bounded metadata, old-run compatibility, spoken-stage-direction rejection, and known-model versus custom-deployment request parameters.
- Frontend: **92 tests passed**; TypeScript, formatting, and the production build passed. Playback checks cover phrase completion/cancellation, one acknowledgment per turn, cue ownership and onset, scene cleanup, and PCM timing after initial atmosphere generation.
- Browser: **18 full Chromium checks passed**. The final crowd slider build also passed its targeted control/persistence/mobile check. Desktop and mobile screenshots were inspected, with no horizontal overflow.
- Actual browser speech: a native preview and four touchdown phrases completed with no speech errors, alerts, or page errors. The two hosts acknowledged once each after their complete turns; the preview acknowledged nothing. One atmosphere bed and one reaction played and cleaned up. Headless Chromium used its default English voice without enumerating installed voices, so this does not establish the installed-voice experience on every browser.
- Actual Web Audio rendering: crowd ducking reduced RMS by approximately **7.7 dB**. All rendered samples were finite; the loudest crowd-only peak was **0.223**. Zero crowd level produced silent atmosphere while preserving PCM voice output bit-for-bit.
- Actual Azure: routine and touchdown voice auditions finalized with the intended words. The final browser touchdown exchange streamed **207 PCM chunks**, completed both hosts with positive played-sample acknowledgments, generated one crowd reaction, and showed no errors or alerts. Draft generation took **1.729 and 2.006 seconds** with low reasoning effort. Two additional text-only probes passed factual validation in 3.271 and 1.798 seconds. These observations are not a controlled latency benchmark.
- One earlier paired attempt returned a deployment-not-found error for the first voice request while the second request to the same deployment succeeded. The dashboard surfaced the failure; a full rerun then passed both hosts. No credential or deployment changes were made to conceal that upstream failure.

The current API and both agents were restarted with these changes and report healthy. The isolated validation services were stopped. Local evidence is retained under ignored `.runtime/broadcast-validation/` and `.runtime/broadcast-audition/`, including `browser-results.json`, `native-speech.json`, `soundscape-offline.json`, and the mixed `broadcast-touchdown.webm` audition. These checks establish generation, mixing, playback and recovery behavior; perceived entertainment and naturalness still require the listener's judgment. Fresh cloud voice sessions and finalized peer speech still add handoff delay.

The [research notes](BROADCAST_RESEARCH.md) map the codebase, primary sources, production choices, and listening acceptance criteria.

## Earlier baseline

Checked locally on Windows on October 7, 2026, without OpenAI credentials.

- Backend: **132 tests passed**, including real A2A HTTP calls in both directions,
  matching snapshot hashes, cancellation during peer generation, causal replay
  scores, corrections, feed recovery, queue pressure, persistence, WebSocket
  ownership/reconnect, and speech ordering/failure boundaries.
  Replay export also verifies portable LF bytes so provenance survives Git checkouts.
  Additional coverage checks concurrent broadcast ownership, causal context after
  pause/reopen/highlight jumps, live resume, ordered revisions, saved scoreboard
  dates, and every bundled play's demo call/reply with rolling conversation memory.
  New regressions verify ordered emission through slow journal writes, shared run
  restoration, live baseline timing, stale poll cancellation, revision deduplication
  after crashes, final-game completion, pregame waiting, and terminal state persistence.
  Commentary checks use actual previous plays, source-aware down/distance, retained
  turnover penalties, named defenders and validated editable offline delivery.
  Credential regressions verify actionable OpenAI failures survive A2A propagation,
  distinguish quota from rate limits, and keep keys out of task records, logs, and
  LangGraph checkpoints, including failed generation and speech nodes.
- Frontend: **54 tests passed**; TypeScript checks, formatting and Vite production build passed.
  Audio regressions cover mid-segment unmute, delayed callbacks, partial streams,
  playback ownership, actual played samples and stale state responses.
  Additional tests cover missing speech callbacks, blocked audio resume, failed
  cancellation/scheduling, silent playback ownership, bounded HTTP/JSON requests,
  caller abort cleanup, snapshot-specific exchange grouping and transcript export.
- Browser: **13 isolated Chromium checks passed**, covering desktop, mobile,
  all 32 loaded team logos, actual agent lead alternation, debugger, restart,
  explicit live-feed failure, real highlight selection, keyboard focus, failed
  game changes, stale polling and read-only saved runs after failed regeneration.
  They also check one-click handoff navigation locks, frozen source facts,
  transcript download, agent health recovery, leaving saved runs for empty live
  feeds, failed mode transitions and retry after an initial API failure.
  No unexpected browser errors or page overflow observed; recovery tests deliberately
  simulate failed HTTP responses.
- Ruff and Python dependency checks passed. npm audit reported zero vulnerabilities.
- `tools/smoke.py` passed against all three running services: four turns,
  alternating leads, identical snapshot hashes and separate A2A task IDs.
- Additional HTTP replay checks passed for Super Bowl play `1468` (Philadelphia
  interception-return touchdown) and opening-night play `4221` (reversed Baltimore
  touchdown ruled incomplete). The two agents used the corrected frozen facts.
- On October 7, the actual ESPN adapter fetched scoreboard metadata and **188 distinct
  summary plays** for opening-night event `401671789`, with verified final status
  and no fallback warning.

See the root README for repeatable commands. Runtime journals, credentials, logs,
and caches are excluded from Git. The three supplied Word documents are preserved.

Credential follow-up on October 7, 2026: the supplied `.env` was present, but the
previously running services had not loaded it. Restarting the API and both agents
made `/api/config` report the configured key. The demo HTTP smoke and all 13
browser checks passed again against the restarted services. Actual OpenAI model
access returned **401 `invalid_api_key`**. `tools/smoke.py --provider openai` also
failed through the real commentary/A2A path with the actionable key-replacement
message, rather than hiding it behind a generic failed-task message. The dashboard
now labels a loaded key **key configured**, without claiming authentication passed.

Actual model-driven commentary and GPT-Live speech remain unverified until a valid
API key is supplied. Unit tests exercise the voice transport with controlled events;
they do not establish live voice quality, model access, or naturalness.

Provider switch on October 7, 2026: model calls now go to Azure OpenAI through the
resource's v1 API (`AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY` and deployment
names). The entries above predate the switch. Backend (**136 tests**), ruff,
frontend typecheck/unit tests and formatting passed after it. Actual Azure text
commentary and GPT-Live speech are unverified until valid Azure credentials are
used with `tools/smoke.py --provider azure`. The GitHub Actions workflow was
removed; run the checks locally with the README commands.
