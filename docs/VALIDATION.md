# POC validation

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

GitHub Actions now starts all services and repeats the HTTP smoke and browser
checks after unit tests and a clean production build on Linux. Browser failure
traces and service logs are uploaded when a check fails.

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
