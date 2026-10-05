# Dashboard development

From the repository root, start all three Python services using the development launcher described in the root README. Alternatively, run `npm ci` and `npm run dev` in this directory after the Python services are running.

`npm run check` runs TypeScript and the audio/session unit tests. `npm run build` produces the dashboard served by the coordinator at port 8000.

For isolated browser acceptance against a running local stack:

```powershell
npm.cmd exec playwright install chromium
npm.cmd run build
npm.cmd run test:browser
```

`BANTERBOTS_TEST_URL` can override the default `http://127.0.0.1:8000`. The tests open a fresh headless Chromium process; they do not access existing user browser profiles. They create their own demo runs and verify desktop/mobile layout, real A2A replay exchanges, lead alternation, shared snapshot hashes, debugger visibility, restart, and an explicit live-feed outage. Test screenshots and failure traces are written to `test-results/` and must be excluded from Git. Run them against a demo stack without OpenAI credentials; external live polling is replaced only in the outage test.

The dashboard bundles Barlow Condensed and DM Sans under their SIL Open Font Licenses in `public/fonts/`, so the replay UI does not require a font CDN.
