import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

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

test('desktop: real A2A replay, alternating leads, debugger and reset', async ({
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
  if (!config.openai_configured) {
    await expect(page.getByRole('option', { name: /OpenAI · add credentials/ })).toBeDisabled();
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
  await expect(page.locator('.commentary-turn')).toHaveCount(2);
  const firstPair = await page.locator('.turn-meta strong').allTextContents();
  expect(firstPair).toEqual(['Max Carter', 'Riley Brooks']);
  const hashes = await page.locator('.turn-proof').allTextContents();
  expect(hashes.map((value) => value.match(/snapshot (\w+)/)?.[1])).toEqual([
    expect.any(String),
    hashes[0].match(/snapshot (\w+)/)?.[1],
  ]);
  await expect(page.getByRole('button', { name: 'Advance one play' })).toBeEnabled();
  await page.getByRole('button', { name: 'Advance one play' }).click();
  await expect(page.locator('.commentary-turn')).toHaveCount(4);
  expect((await page.locator('.turn-meta strong').allTextContents()).slice(2)).toEqual([
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

test('mobile: broadcast controls, complete team rail and readable layout', async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const errors = await enterStudio(page);
  await expect(page.getByRole('button', { name: /^Filter / })).toHaveCount(32);
  await page.getByRole('button', { name: 'Advance one play' }).click();
  await expect(page.locator('.commentary-turn')).toHaveCount(2);
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
  await expect(page.locator('.commentary-turn')).toHaveCount(2);
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
  await expect(page.locator('.commentary-turn')).toHaveCount(2);
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
  await expect(page.getByRole('status')).toContainText('Riley is offline');
  await expect(page.getByRole('button', { name: 'Start broadcast' })).toBeDisabled();
  healthy = true;
  await page.clock.fastForward(10000);
  await expect(page.getByRole('status')).toHaveCount(0);
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
  await expect(page.locator('.commentary-turn')).toHaveCount(2);
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
  await expect(page.locator('.commentary-turn')).toHaveCount(2);
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
  await expect(page.locator('.commentary-turn')).toHaveCount(2);
});

test('leaving a saved run for an empty live feed clears recorded status', async ({ page }) => {
  await enterStudio(page);
  await page.getByRole('button', { name: 'Advance one play' }).click();
  await expect(page.locator('.commentary-turn')).toHaveCount(2);
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
  await expect(page.locator('.commentary-turn')).toHaveCount(2);
});
