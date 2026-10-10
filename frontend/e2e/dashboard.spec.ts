import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

type SpeechEvent = { type: string; text: string };
type SpeechState = { active: string | null; pending: string[]; events: SpeechEvent[] };
type SpeechHarness = {
  snapshot(): SpeechState;
  start(): void;
  end(): void;
  fail(error: string): void;
};
type SpeechWindow = Window & { __speechTest: SpeechHarness };

async function installBrowserSpeech(page: Page) {
  // Replace only the OS speech boundary. Sessions, A2A and playback ACKs still
  // travel through the real studio API and WebSocket.
  await page.addInitScript(() => {
    const queue: SpeechSynthesisUtterance[] = [];
    const events: SpeechEvent[] = [];
    let active: SpeechSynthesisUtterance | null = null;
    let paused = false;
    const errorEvent = (error: string) => Object.assign(new Event('error'), { error });
    Object.defineProperty(window, 'speechSynthesis', {
      configurable: true,
      value: {
        // Chromium keeps an idle pause internally while reporting paused=false.
        get paused() {
          return paused && (active !== null || queue.length > 0);
        },
        get speaking() {
          return active !== null;
        },
        get pending() {
          return queue.length > 0;
        },
        getVoices: () => [],
        speak(utterance: SpeechSynthesisUtterance) {
          events.push({ type: 'queued', text: utterance.text });
          queue.push(utterance);
        },
        cancel() {
          const cancelled = [...(active ? [active] : []), ...queue];
          active = null;
          queue.length = 0;
          for (const utterance of cancelled) {
            events.push({ type: 'cancel', text: utterance.text });
            utterance.dispatchEvent(errorEvent('interrupted'));
          }
        },
        resume() {
          if (active || queue.length) paused = false;
        },
        pause() {
          paused = true;
        },
      },
    });
    (window as SpeechWindow).__speechTest = {
      snapshot: () => ({
        active: active?.text ?? null,
        pending: queue.map((utterance) => utterance.text),
        events: [...events],
      }),
      start() {
        if (paused) throw new Error('Speech must resume after a line is queued');
        if (active || queue.length !== 1) throw new Error('Speech must start one line at a time');
        active = queue.shift()!;
        events.push({ type: 'start', text: active.text });
        active.dispatchEvent(new Event('start'));
      },
      end() {
        if (!active) throw new Error('No browser speech is playing');
        const utterance = active;
        active = null;
        events.push({ type: 'end', text: utterance.text });
        utterance.dispatchEvent(new Event('end'));
      },
      fail(error) {
        const utterance = active ?? queue.shift();
        if (!utterance) throw new Error('No browser speech can fail');
        active = null;
        events.push({ type: 'error', text: utterance.text });
        utterance.dispatchEvent(errorEvent(error));
      },
    };
  });
}

async function speechState(page: Page) {
  return page.evaluate(() => (window as SpeechWindow).__speechTest.snapshot());
}

function observePlayback(page: Page) {
  const acknowledgements: {
    segment_id: string;
    played_samples: number;
    cancelled: boolean;
  }[] = [];
  const traces: { phase: string; segment_id: string }[] = [];
  const turns: { id: string; agent_id: string; text: string; spoken_text?: string }[] = [];
  page.on('websocket', (socket) => {
    socket.on('framesent', ({ payload }) => {
      const message = JSON.parse(payload.toString());
      if (message.type === 'playback.ack') acknowledgements.push(message);
    });
    socket.on('framereceived', ({ payload }) => {
      const message = JSON.parse(payload.toString());
      if (message.type === 'trace' && message.data.node === 'playback') traces.push(message.data);
      if (message.type === 'turn') turns.push(message.data.turn);
    });
  });
  return { acknowledgements, traces, turns };
}

async function enterStudio(page: Page) {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text());
  });
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'The broadcast booth.' })).toBeVisible();
  await expect(page.getByText('Studio connected', { exact: true })).toBeAttached();
  await expect(page.getByRole('button', { name: 'Advance one play' })).toBeEnabled();
  return errors;
}
async function noOverflow(page: Page) {
  const dimensions = await page.evaluate(() => ({
    width: document.documentElement.clientWidth,
    scroll: document.documentElement.scrollWidth,
  }));
  expect(dimensions.scroll).toBeLessThanOrEqual(dimensions.width);
}

async function finishBrowserTurn(
  page: Page,
  playback: ReturnType<typeof observePlayback>,
  index: number,
) {
  const turn = playback.turns[index];
  const phrases: string[] = [];
  for (let phrase = 0; phrase < 3; phrase++) {
    await expect.poll(() => speechState(page).then((state) => state.pending.length)).toBe(1);
    phrases.push((await speechState(page)).pending[0]);
    await page.evaluate(() => (window as SpeechWindow).__speechTest.start());
    await expect(page.locator('.persona-card.speaking')).toContainText(
      turn.agent_id === 'a' ? 'Max Carter' : 'Riley Brooks',
    );
    await page.evaluate(() => (window as SpeechWindow).__speechTest.end());
    await expect
      .poll(
        async () =>
          playback.acknowledgements.some((ack) => ack.segment_id === turn.id) ||
          (await speechState(page)).pending.length > 0,
      )
      .toBe(true);
    if (playback.acknowledgements.some((ack) => ack.segment_id === turn.id)) break;
  }
  await expect
    .poll(() => playback.acknowledgements.filter((ack) => ack.segment_id === turn.id))
    .toEqual([expect.objectContaining({ cancelled: false })]);
  expect(phrases.join(' ').replace(/\s+/g, ' ').trim()).toBe(
    (turn.spoken_text ?? turn.text).replace(/\s+/g, ' ').trim(),
  );
  return phrases;
}

test('test voice checks the speaker without acknowledging or advancing a play', async ({
  page,
}) => {
  await installBrowserSpeech(page);
  const playback = observePlayback(page);
  const errors = await enterStudio(page);
  await expect(page.getByRole('button', { name: 'Enable voice', exact: true })).toHaveAttribute(
    'aria-pressed',
    'false',
  );
  await page.evaluate(() => window.speechSynthesis.pause());
  await page.getByRole('button', { name: 'Test voice', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Voice on', exact: true })).toHaveAttribute(
    'aria-pressed',
    'true',
  );
  await expect
    .poll(() => speechState(page))
    .toEqual({
      active: null,
      pending: ['Commentary voice is ready.'],
      events: [{ type: 'queued', text: 'Commentary voice is ready.' }],
    });
  await page.evaluate(() => (window as SpeechWindow).__speechTest.start());
  await expect(page.locator('.persona-card.speaking')).toContainText('Max Carter');
  await expect(page.getByRole('button', { name: 'Test voice', exact: true })).toBeDisabled();
  await page.evaluate(() => (window as SpeechWindow).__speechTest.end());
  await expect(page.locator('.persona-card.speaking')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Test voice', exact: true })).toBeEnabled();
  expect((await speechState(page)).events).toEqual([
    { type: 'queued', text: 'Commentary voice is ready.' },
    { type: 'start', text: 'Commentary voice is ready.' },
    { type: 'end', text: 'Commentary voice is ready.' },
  ]);
  await expect(page.locator('.commentary-turn')).toHaveCount(0);
  expect(playback.turns).toEqual([]);
  expect(playback.acknowledgements).toEqual([]);
  expect(playback.traces).toEqual([]);
  expect(errors).toEqual([]);
});

test('demo voice plays both commentators serially, confirms playback and stops when muted', async ({
  page,
}) => {
  await installBrowserSpeech(page);
  const playback = observePlayback(page);
  const errors = await enterStudio(page);
  await page.getByRole('button', { name: 'Enable voice', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Voice on', exact: true })).toHaveAttribute(
    'aria-pressed',
    'true',
  );
  await page.getByRole('button', { name: 'Advance one play' }).click();
  // The pre-snap setup is on screen while it is spoken; the call waits until it is heard.
  await expect(page.locator('.commentary-turn')).toHaveCount(1);
  await expect.poll(() => playback.turns.length).toBe(3);
  await expect.poll(() => speechState(page).then((state) => state.pending.length)).toBe(1);
  expect(playback.acknowledgements).toEqual([]);
  await expect(page.locator('.persona-card.speaking')).toHaveCount(0);
  const setup = await finishBrowserTurn(page, playback, 0);
  await expect(page.locator('.commentary-turn')).toHaveCount(2);
  const lead = await finishBrowserTurn(page, playback, 1);
  const peer = await finishBrowserTurn(page, playback, 2);
  await expect.poll(() => playback.traces.length).toBe(3);
  expect(playback.acknowledgements).toMatchObject(
    playback.turns.map((turn) => ({ segment_id: turn.id, cancelled: false })),
  );
  expect(playback.traces).toEqual(
    playback.turns.map((turn) =>
      expect.objectContaining({ segment_id: turn.id, phase: 'complete' }),
    ),
  );
  expect((await speechState(page)).events).toEqual(
    [...setup, ...lead, ...peer].flatMap((text) => [
      { type: 'queued', text },
      { type: 'start', text },
      { type: 'end', text },
    ]),
  );
  await expect(page.locator('.persona-card.speaking')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Advance one play' })).toBeEnabled();

  await page.getByRole('button', { name: 'Advance one play' }).click();
  await expect(page.locator('.commentary-turn')).toHaveCount(4);
  await expect.poll(() => speechState(page).then((state) => state.pending.length)).toBe(1);
  await page.evaluate(() => (window as SpeechWindow).__speechTest.start());
  await expect(page.locator('.persona-card.speaking')).toContainText('Riley Brooks');
  await page.getByRole('button', { name: 'Voice on', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Enable voice', exact: true })).toHaveAttribute(
    'aria-pressed',
    'false',
  );
  await expect.poll(() => playback.acknowledgements.length).toBe(6);
  expect(playback.acknowledgements.slice(3)).toMatchObject(
    playback.turns.slice(3).map((turn) => ({ segment_id: turn.id, cancelled: true })),
  );
  expect(await speechState(page)).toMatchObject({ active: null, pending: [] });
  expect((await speechState(page)).events.at(-1)?.type).toBe('cancel');
  await expect(page.locator('.persona-card.speaking')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Advance one play' })).toBeEnabled();
  expect(errors).toEqual([]);
});

test('stadium controls preserve crowd level and voice choice without autoplay', async ({
  page,
}) => {
  await installBrowserSpeech(page);
  const errors = await enterStudio(page);
  const atmosphere = page.getByRole('button', { name: 'Stadium atmosphere' });
  await expect(atmosphere).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByRole('slider', { name: 'Crowd level' })).toHaveValue('35');
  await page.getByRole('slider', { name: 'Crowd level' }).fill('70');
  await expect(page.locator('.crowd-level output')).toHaveText('70%');
  await atmosphere.click();
  await expect(page.getByRole('slider', { name: 'Crowd level' })).toBeDisabled();
  expect((await speechState(page)).events).toEqual([]);
  await page.reload();
  await expect(page.getByRole('button', { name: 'Advance one play' })).toBeEnabled();
  await expect(atmosphere).toHaveAttribute('aria-pressed', 'false');
  await expect(page.getByRole('slider', { name: 'Crowd level' })).toHaveValue('70');
  await expect(page.getByRole('button', { name: 'Enable voice', exact: true })).toHaveAttribute(
    'aria-pressed',
    'false',
  );
  await atmosphere.click();
  await expect(page.getByRole('slider', { name: 'Crowd level' })).toBeEnabled();
  await page.setViewportSize({ width: 390, height: 844 });
  await noOverflow(page);
  expect((await speechState(page)).events).toEqual([]);
  expect(errors).toEqual([]);
});

test('blocked browser speech turns voice off and explains how to enable audio', async ({
  page,
}) => {
  await installBrowserSpeech(page);
  const playback = observePlayback(page);
  const errors = await enterStudio(page);
  await page.getByRole('button', { name: 'Enable voice', exact: true }).click();
  await page.getByRole('button', { name: 'Advance one play' }).click();
  await expect(page.locator('.commentary-turn')).toHaveCount(1);
  await expect.poll(() => speechState(page).then((state) => state.pending.length)).toBe(1);
  await page.evaluate(() => (window as SpeechWindow).__speechTest.fail('not-allowed'));
  await expect(page.getByRole('button', { name: 'Enable voice', exact: true })).toHaveAttribute(
    'aria-pressed',
    'false',
  );
  await expect(page.getByRole('alert')).toContainText('Your browser blocked spoken commentary.');
  await expect(page.getByRole('alert')).toContainText('Allow sound for this site');
  await expect(page.getByRole('alert')).toContainText('click Voice to enable it again');
  await expect.poll(() => playback.acknowledgements.length).toBe(3);
  await expect(page.locator('.commentary-turn')).toHaveCount(3);
  expect(playback.acknowledgements).toMatchObject(
    playback.turns.map((turn) => ({ segment_id: turn.id, cancelled: true })),
  );
  expect(await speechState(page)).toMatchObject({ active: null, pending: [] });
  expect((await speechState(page)).events.filter((event) => event.type === 'queued')).toHaveLength(
    1,
  );
  await expect(page.locator('.persona-card.speaking')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Advance one play' })).toBeEnabled();
  expect(errors).toEqual([]);
});

test('desktop: real A2A replay, possession leads, debugger and reset', async ({
  page,
}, testInfo) => {
  const errors = await enterStudio(page);
  await expect(page.getByRole('button', { name: /^Filter / })).toHaveCount(32);
  await expect
    .poll(() =>
      page
        .locator('.team-option img')
        .evaluateAll((images) =>
          images.every(
            (img) =>
              (img as HTMLImageElement).complete && (img as HTMLImageElement).naturalWidth > 0,
          ),
        ),
    )
    .toBe(true);
  const config = await (await page.request.get('/api/config')).json();
  if (!config.azure_configured) {
    await expect(
      page.getByRole('option', { name: /Azure OpenAI · add credentials/ }),
    ).toBeDisabled();
  }
  expect(
    await page.locator('.team-list').evaluate((rail) => {
      const bounds = rail.getBoundingClientRect();
      return [...rail.querySelectorAll('button')].every((button) => {
        const item = button.getBoundingClientRect();
        return item.left >= bounds.left - 1 && item.right <= bounds.right + 1;
      });
    }),
  ).toBe(true);
  await page.getByRole('button', { name: 'Advance one play' }).click();
  await expect(page.locator('.commentary-turn')).toHaveCount(3);
  // Baltimore has the ball: Riley sets up the snap and calls it, Max answers.
  expect(await page.locator('.turn-meta strong').allTextContents()).toEqual([
    'Riley Brooks',
    'Riley Brooks',
    'Max Carter',
  ]);
  await expect(page.locator('.turn-kind')).toHaveText(['SETUP', 'CALL', /REPLY/]);
  const hashes = (await page.locator('.turn-proof').allTextContents()).map(
    (value) => value.match(/snapshot (\w+)/)?.[1],
  );
  // The call and reply share one frozen result; the setup's snapshot never held it.
  expect(hashes[2]).toBe(hashes[1]);
  expect(hashes[0]).not.toBe(hashes[1]);
  await expect(page.getByRole('button', { name: 'Advance one play' })).toBeEnabled();
  await page.getByRole('button', { name: 'Advance one play' }).click();
  await expect(page.locator('.commentary-turn')).toHaveCount(6);
  expect((await page.locator('.turn-meta strong').allTextContents()).slice(3)).toEqual([
    'Riley Brooks',
    'Riley Brooks',
    'Max Carter',
  ]);
  await page.getByRole('button', { name: /Agent debugger/ }).click();
  await expect(page.locator('.trace-list')).toContainText('a2a');
  await page.locator('.trace-list button').filter({ hasText: 'a2a' }).first().click();
  await expect(page.locator('.trace-grid pre')).toContainText('snapshot_hash');
  await noOverflow(page);
  await page.screenshot({ path: testInfo.outputPath('desktop-studio.png'), fullPage: true });
  expect(errors).toEqual([]);
  await page.getByRole('button', { name: 'Restart game' }).click();
  await expect(page.locator('.commentary-turn')).toHaveCount(0);
  await expect(page.getByText('Great rivals make great radio.')).toBeVisible();
});

test('agent network proves both services are up and every A2A message arrived', async ({
  page,
}) => {
  const errors = await enterStudio(page);
  const network = page.getByRole('region', { name: 'AGENT NETWORK' });
  await expect(network).toContainText('All systems working');
  await expect(network.locator('.network-node.up')).toHaveCount(3);
  await expect(network).toContainText('No messages yet');
  await network.getByRole('button', { name: 'Send a test play' }).click();
  await expect(network.locator('.flow-step.done')).toHaveCount(5);
  await expect(network).toContainText('Riley and Max exchanged messages');
  await expect(network).toContainText('Riley sends that call to Max and asks for a reply');
  await expect(network.locator('.flow-step small', { hasText: /A2A task \w{8}/ })).toHaveCount(2);
  await expect(network).toContainText('1 agent-to-agent reply so far');
  await expect(page.locator('.handoff-note')).toContainText('Riley sent this call to Max over A2A');
  await page.getByRole('button', { name: 'Advance one play' }).click();
  await expect(network).toContainText('Riley sends that call to Max and asks for a reply');
  await expect(network.locator('.flow-step.done')).toHaveCount(5);
  await expect(network).toContainText('2 agent-to-agent replies so far');
  expect(errors).toEqual([]);
});

test('mobile: broadcast controls, complete team rail and readable layout', async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const errors = await enterStudio(page);
  await expect(page.getByRole('button', { name: /^Filter / })).toHaveCount(32);
  await page.getByRole('button', { name: 'Advance one play' }).click();
  await expect(page.locator('.commentary-turn')).toHaveCount(3);
  await expect(page.getByRole('button', { name: 'Start broadcast' })).toBeVisible();
  await noOverflow(page);
  await page.screenshot({ path: testInfo.outputPath('mobile-studio.png'), fullPage: true });
  expect(errors).toEqual([]);
});

test('live feed failures remain explicit and do not fall back to replay', async ({ page }) => {
  const errors = await enterStudio(page);
  await page.route('**/api/games?mode=live*', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        games: [],
        feed: { status: 'error', last_success: null, error: 'ESPN fixture outage' },
      }),
    }),
  );
  await page.getByRole('button', { name: 'Live feed', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('ESPN fixture outage');
  await expect(page.getByRole('button', { name: 'Start broadcast' })).toBeDisabled();
  await expect(page.locator('.score-list')).toContainText('No live games');
  await noOverflow(page);
  expect(errors).toEqual([]);
});

test('replay moments jump to a real reversal without exposing future results', async ({ page }) => {
  const errors = await enterStudio(page);
  const moments = page.getByRole('region', { name: 'Replay moments' });
  await expect(moments).toBeVisible();
  await moments.getByRole('button', { name: /Review reversal/ }).click();
  await expect(page.locator('.score-kicker')).toContainText('Q4');
  await expect(page.locator('.commentary-turn')).toHaveCount(3);
  await expect(page.locator('.conversation')).toContainText(/incomplete|review|overturn/i);
  await page.getByRole('button', { name: /Agent debugger/ }).click();
  await expect(page.getByLabel('Agent workflow').locator('.done')).toHaveCount(5);
  await expect(page.locator('.turn-proof').first()).toBeVisible();
  await noOverflow(page);
  expect(errors).toEqual([]);
});

test('one-click replay locks navigation during handoff and groups frozen facts', async ({
  page,
}) => {
  const errors = await enterStudio(page);
  let release: () => void = () => {};
  const blocked = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route('**/api/sessions/*/control', async (route) => {
    if (route.request().postDataJSON().action === 'step') await blocked;
    await route.continue();
  });
  await page
    .getByRole('region', { name: 'Replay moments' })
    .getByRole('button', { name: /Review reversal/ })
    .click();
  await expect(page.locator('#game')).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Live feed', exact: true })).toBeDisabled();
  release();
  await expect(page.getByLabel('Exchange 1')).toBeVisible();
  await expect(page.locator('.commentary-turn')).toHaveCount(3);
  await expect(page.locator('.exchange-context')).toContainText('Q4');
  await page.locator('.exchange-facts summary').click();
  await expect(page.locator('.exchange-facts p')).toContainText(/reversed|incomplete/i);
  const download = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Download commentary transcript' }).click();
  expect((await download).suggestedFilename()).toMatch(/^banterbots-BAL-KC-.*\.txt$/);
  await noOverflow(page);
  expect(errors).toEqual([]);
});

test('offline commentator blocks starting and recovers when service health returns', async ({
  page,
}) => {
  await page.clock.install();
  let healthy = false;
  await page.route('**/api/health', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        status: healthy ? 'ok' : 'degraded',
        agents: { a: true, b: healthy },
      }),
    }),
  );
  await page.goto('/');
  await expect(page.getByText('Studio connected', { exact: true })).toBeAttached();
  await expect(page.locator('.recovery-banner')).toContainText('Riley is offline');
  await expect(page.getByRole('button', { name: 'Start broadcast' })).toBeDisabled();
  healthy = true;
  await page.clock.fastForward(10000);
  await expect(page.locator('.recovery-banner')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Start broadcast' })).toBeEnabled();
});

test('team filtering keeps active game honest and dialogs support keyboard close', async ({
  page,
}) => {
  await page.setViewportSize({ width: 1024, height: 900 });
  const errors = await enterStudio(page);
  await page.getByRole('button', { name: 'More NFL teams' }).click();
  await expect
    .poll(() => page.locator('.team-list').evaluate((el) => el.scrollLeft))
    .toBeGreaterThan(0);
  await page.getByRole('button', { name: 'Filter Arizona Cardinals' }).click();
  await expect(page.locator('.score-list')).toContainText('No games for this team');
  await expect(page.locator('#game')).toHaveValue('2024_01_BAL_KC');
  await page.getByRole('button', { name: 'Saved runs' }).click();
  await expect(page.getByRole('dialog', { name: 'Saved broadcasts' })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Saved runs' })).toBeFocused();
  await noOverflow(page);
  expect(errors).toEqual([]);
});

test('failed game creation leaves the previous session connected and recoverable', async ({
  page,
}) => {
  const errors = await enterStudio(page);
  await page.route('**/api/sessions', async (route) => {
    if (route.request().method() === 'POST')
      await route.fulfill({
        status: 503,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'Fixture creation failure' }),
      });
    else await route.continue();
  });
  await page.getByLabel('IN THE BOOTH').selectOption('2024_22_KC_PHI');
  await expect(page.getByRole('alert')).toContainText('Fixture creation failure');
  await expect(page.getByText('Studio connected', { exact: true })).toBeAttached();
  await expect(page.locator('#game')).toHaveValue('2024_01_BAL_KC');
  await page.unroute('**/api/sessions');
  await page.getByRole('button', { name: 'Advance one play' }).click();
  await expect(page.locator('.commentary-turn')).toHaveCount(3);
  // The mocked HTTP failure is the expected recovery condition.
  expect(errors.filter((error) => !error.includes('503'))).toEqual([]);
});

test('late live polls cannot replace the replay ticker', async ({ page }) => {
  await page.clock.install();
  await enterStudio(page);
  let calls = 0;
  let release: () => void = () => {};
  const blocked = new Promise<void>((resolve) => {
    release = resolve;
  });
  const stale = {
    id: 'late-live',
    home_team: 'DAL',
    away_team: 'NYG',
    start_time: '2026-10-06T00:00:00Z',
    mode: 'live',
    status: 'in progress',
    label: 'Stale live response',
    home_score: 7,
    away_score: 3,
    quarter: 1,
    clock: '10:00',
  };
  await page.route('**/api/games?mode=live*', async (route) => {
    calls++;
    if (calls > 1) await blocked;
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ games: calls > 1 ? [stale] : [], feed: { status: 'connected' } }),
    });
  });
  await page.getByRole('button', { name: 'Live feed', exact: true }).click();
  await expect(page.locator('.score-list')).toContainText('No live games');
  await page.clock.fastForward(15000);
  await expect.poll(() => calls).toBe(2);
  await page.getByRole('button', { name: 'Replay', exact: true }).click();
  await expect(page.locator('.mini-game')).toHaveCount(2);
  const staleResponse = page.waitForResponse((response) => response.url().includes('mode=live'));
  release();
  await (await staleResponse).finished();
  await page.clock.runFor(50);
  await expect(page.getByText('Studio connected', { exact: true })).toBeAttached();
  await expect(page.locator('.score-list')).not.toContainText('DAL');
  await expect(page.locator('.mini-game')).toHaveCount(2);
});

test('failed regeneration keeps saved results read only', async ({ page }) => {
  await enterStudio(page);
  await page.getByRole('button', { name: 'Advance one play' }).click();
  await expect(page.locator('.commentary-turn')).toHaveCount(3);
  await page.getByRole('button', { name: 'Saved runs' }).click();
  await page.locator('.saved-list button').first().click();
  await expect(page.locator('.saved-banner')).toBeVisible();
  await page.route('**/api/sessions', async (route) => {
    if (route.request().method() === 'POST')
      await route.fulfill({
        status: 503,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'Regeneration unavailable' }),
      });
    else await route.continue();
  });
  await page.getByRole('button', { name: 'Regenerate as a new run' }).click();
  await expect(page.getByRole('alert')).toContainText('Regeneration unavailable');
  await expect(page.locator('.saved-banner')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Start broadcast' })).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Advance one play' })).toBeDisabled();
  await expect(page.locator('.commentary-turn')).toHaveCount(3);
});

test('leaving a saved run for an empty live feed clears recorded status', async ({ page }) => {
  await enterStudio(page);
  await page.getByRole('button', { name: 'Advance one play' }).click();
  await expect(page.locator('.commentary-turn')).toHaveCount(3);
  await page.getByRole('button', { name: 'Saved runs' }).click();
  await page.locator('.saved-list button').first().click();
  await expect(page.locator('.saved-banner')).toBeVisible();
  await page.route('**/api/games?mode=live*', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ games: [], feed: { status: 'connected' } }),
    }),
  );
  await page.getByRole('button', { name: 'Live feed', exact: true }).click();
  await expect(page.locator('.score-list')).toContainText('No live games');
  await expect(page.locator('.saved-banner')).toHaveCount(0);
  await expect(page.getByText('Recorded session', { exact: true })).toHaveCount(0);
  await expect(page.locator('.commentary-turn')).toHaveCount(0);
});

test('failed mode transition keeps the current replay facts and labels', async ({ page }) => {
  await enterStudio(page);
  await page.route('**/api/sessions/*/control', async (route) => {
    if (route.request().postDataJSON().action === 'stop')
      await route.fulfill({
        status: 503,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'Studio stop unavailable' }),
      });
    else await route.continue();
  });
  await page.getByRole('button', { name: 'Live feed', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('Studio stop unavailable');
  await expect(page.locator('.mode-label')).toContainText('HISTORICAL REPLAY');
  await expect(page.locator('.mini-game')).toHaveCount(2);
  await expect(page.locator('#game')).toHaveValue('2024_01_BAL_KC');
  await expect(page.getByText('Studio connected', { exact: true })).toBeAttached();
});

test('studio retry recovers from an unavailable initial API without a page reload', async ({
  page,
}) => {
  let available = false;
  await page.route('**/api/config', async (route) => {
    if (!available)
      await route.fulfill({
        status: 503,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'Studio warming up' }),
      });
    else await route.continue();
  });
  await page.goto('/');
  await expect(page.getByRole('button', { name: 'Retry connection' })).toBeVisible();
  await expect(page.getByRole('alert')).toContainText('Studio warming up');
  available = true;
  await page.getByRole('button', { name: 'Retry connection' }).click();
  await expect(page.getByText('Studio connected', { exact: true })).toBeAttached();
  await page.getByRole('button', { name: 'Advance one play' }).click();
  await expect(page.locator('.commentary-turn')).toHaveCount(3);
});

test('your seat: preview label, pick a side, cheer, call a third down and ask the booth', async ({
  page,
}) => {
  await installBrowserSpeech(page);
  // Begin on the third-and-11 before a real conversion, so its setup opens a poll.
  await page.route('**/api/sessions', async (route) => {
    const request = route.request();
    if (request.method() !== 'POST') return route.continue();
    const body = JSON.parse(request.postData() ?? '{}');
    await route.continue({ postData: JSON.stringify({ ...body, start_index: 5 }) });
  });
  const listener: Record<string, unknown>[] = [];
  page.on('request', (request) => {
    if (request.method() === 'POST' && request.url().endsWith('/listener'))
      listener.push(JSON.parse(request.postData() ?? '{}'));
  });
  const errors = await enterStudio(page);
  await expect(page.getByText('PREVIEW VOICE', { exact: true })).toBeVisible();
  const seat = page.getByRole('region', { name: 'Your seat' });
  const home = seat.getByRole('group', { name: 'Pick a side' }).getByRole('button').first();
  await home.click();
  await expect(home).toHaveAttribute('aria-pressed', 'true');
  await seat.getByRole('button', { name: 'Cheer' }).click();
  await expect.poll(() => listener).toEqual([{ side: 'home' }, { reaction: 'cheer' }]);
  // Betting talk is declined before it reaches the booth.
  await seat.getByLabel('Ask the booth').fill('What are the odds they cover?');
  await seat.getByRole('button', { name: 'Send question' }).click();
  await expect(seat.getByRole('status')).toContainText('never talks betting');
  await seat.getByLabel('Ask the booth').fill('Who has more yards so far?');
  await seat.getByRole('button', { name: 'Send question' }).click();
  await expect(seat.getByRole('status')).toContainText('answers at the next break');
  // With voice on, the poll stays open while the setup line is heard.
  await page.getByRole('button', { name: 'Enable voice', exact: true }).click();
  await expect(page.getByText(/Preview voice: robotic browser speech/)).toBeVisible();
  await page.getByRole('button', { name: 'Advance one play' }).click();
  await expect(seat.getByText('Do they convert?')).toBeVisible();
  await seat.getByRole('button', { name: 'They convert' }).click();
  await expect(seat.getByRole('button', { name: 'They convert' })).toHaveAttribute(
    'aria-pressed',
    'true',
  );
  await expect.poll(() => listener.at(-1)).toMatchObject({ poll_vote: 'convert' });
  await page.setViewportSize({ width: 390, height: 844 });
  await noOverflow(page);
  expect(errors.filter((error) => !error.includes('422'))).toEqual([]);
});
