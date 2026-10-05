# POC validation

Checked locally on Windows on October 5, 2026, without OpenAI credentials.

- Backend: **73 tests passed**, including real A2A HTTP calls in both directions,
  matching snapshot hashes, cancellation during peer generation, causal replay
  scores, corrections, feed recovery, queue pressure, persistence, WebSocket
  ownership/reconnect, and speech ordering/failure boundaries.
- Frontend: **15 tests passed**; TypeScript checks and Vite production build passed.
- Browser: **3 isolated Chromium checks passed**, covering desktop, mobile,
  all 32 loaded team logos, actual agent lead alternation, debugger, restart,
  and explicit live-feed failure. No browser errors or page overflow observed.
- Ruff and Python dependency checks passed. npm audit reported zero vulnerabilities.
- `tools/smoke.py` passed against all three running services: four turns,
  alternating leads, identical snapshot hashes and separate A2A task IDs.
- Additional HTTP replay checks passed for Super Bowl play `1468` (Philadelphia
  interception-return touchdown) and opening-night play `4221` (reversed Baltimore
  touchdown ruled incomplete). The two agents used the corrected frozen facts.
- The actual ESPN adapter fetched scoreboard metadata and **188 summary plays**
  for opening-night event `401671789`, with successful polling and no fallback warning.

See the root README for repeatable commands. Runtime journals, credentials, logs,
and caches are excluded from Git. The three supplied Word documents are preserved.

Actual GPT-Live speech and model-driven commentary remain to be checked after an
OpenAI key is supplied. Unit tests exercise the voice transport with controlled
events; they do not establish live voice quality, model access, or naturalness.
