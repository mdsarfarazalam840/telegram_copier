import { test, expect } from '@playwright/test';

const endpoint = 'https://relayroom-appwrite.test/v1';
test.beforeEach(async ({ page }) => {
  // External typography is unrelated to Appwrite and must not gate integration tests.
  await page.route('https://fonts.googleapis.com/**', (route) => route.fulfill({ contentType: 'text/css', body: '' }));
  await page.route('https://fra.cloud.appwrite.io/v1/ping', (route) => route.fulfill({ contentType: 'application/json', body: '{}' }));
  await page.route(`${endpoint}/ping`, (route) => route.fulfill({ contentType: 'application/json', body: '{}' }));
});
async function mockAppwrite(page, options = {}) {
  let signedIn = false;
  let submitted = 0;
  let status = 'processing';
  let failReads = false;
  let listCalls = 0;
  const requests = [];
  const now = Date.now();
  const execution = () => ({ $id: 'run-1', $createdAt: new Date(now).toISOString(), status, responseStatusCode: status === 'completed' ? 200 : 0, responseBody: '' });
  await page.route(`${endpoint}/**`, async (route) => {
    const request = route.request();
    requests.push(request);
    const url = new URL(request.url());
    const path = url.pathname;
    const respond = (body, statusCode = 200) => route.fulfill({ status: statusCode, contentType: 'application/json', body: JSON.stringify(body) });
    if (request.method() === 'OPTIONS') return respond({});
    if (path.endsWith('/ping')) return respond({});
    if (path.endsWith('/account/sessions/email')) {
      signedIn = true;
      return respond({ $id: 'session' }, 201);
    }
    if (path.endsWith('/account/sessions/current') && request.method() === 'DELETE') {
      signedIn = false;
      return respond({});
    }
    if (path.endsWith('/account')) return signedIn ? respond({ $id: 'user-1', name: 'Test operator', email: 'operator@example.test' }) : respond({ code: 401, message: 'Unauthorized', type: 'user_unauthorized' }, 401);
    if (path.includes('/functions/')) {
      if (request.method() === 'POST') {
        if (options.denyExecute) return respond({ code: 403, message: 'Forbidden' }, 403);
        submitted++;
        return respond(execution(), 201);
      }
      if (path.endsWith('/run-1')) { status = 'completed'; return respond(execution()); }
      return respond({ total: submitted, executions: submitted ? [execution()] : [] });
    }
    if (path.includes('/databases/') || path.includes('/tablesdb/')) {
      listCalls++;
      if (failReads || options.denyReads) return respond({ code: 403, message: 'Forbidden' }, 403);
      const isTables = path.includes('/tablesdb/');
      const key = isTables ? 'rows' : 'documents';
      if (path.includes('/telegram_copier_state/')) return respond({ total: 1, [key]: [{ $id: 'state', $updatedAt: new Date(now).toISOString(), payload_json: JSON.stringify({ sources: { '-100123': { last_id: 42 } }, meta: { last_run_ts_ms: now, status: 'completed', fetched: 9, copied: 7, skipped_invalid: 0, filtered_links: 2 } }) }] });
      const queries = [...url.searchParams].filter(([key]) => key.startsWith('queries[')).map(([, query]) => JSON.parse(query));
      const cursor = queries.some((query) => query.method === 'cursorAfter');
      const base = { function_name: 'telegram_copier_run_v1', run_ts_ms: now, source_id: -100123, status: 'completed', lookback_minutes: 60, message_ids: '1,2,3' };
      const rows = options.paginate && !cursor ? Array.from({ length: 100 }, (_, i) => ({ ...base, $id: `row-${i}` })) : [{ ...base, $id: 'last-row', message_ids: '3,4' }];
      return respond({ total: options.paginate ? 101 : 1, [key]: rows });
    }
    return respond({ code: 404, message: path }, 404);
  });
  return { requests, submitted: () => submitted, failReads: () => { failReads = true; }, listCalls: () => listCalls };
}
async function configure(page, storage = 'collections') {
  await page.goto('/');
  await page.getByLabel('API endpoint').fill(endpoint);
  await page.getByLabel('Logs database ID', { exact: true }).fill('db');
  await page.getByLabel('Storage API').selectOption(storage);
  await page.getByRole('button', { name: 'Save & connect' }).click();
  await page.getByLabel('Email', { exact: true }).fill('operator@example.test');
  await page.getByLabel('Password', { exact: true }).fill('test-password');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
}

test('SDK auth, paginated live records, manual execution polling, sign out', async ({ page }) => {
  const errors = [];
  const pings = [];
  page.on('request', (request) => {
    if (request.url() === 'https://fra.cloud.appwrite.io/v1/ping') pings.push(request);
  });
  page.on('pageerror', (error) => errors.push(error.message));
  const api = await mockAppwrite(page, { paginate: true });
  await configure(page);
  await expect(page.locator('#sdk-status')).toContainText('Tele-copier: startup ping succeeded');
  await expect(page.locator('#copied-value')).toHaveText('7');
  await expect(page.locator('#check-api')).toContainText('reachable');
  await expect(page.locator('#check-session')).toContainText('operator@example.test');
  await expect(page.locator('#check-data')).toContainText('successful');
  await expect(page.locator('#snapshot-value')).toHaveText('4');
  await expect(page.locator('#held-value')).toHaveText('0');
  await expect(page.locator('.policy-value').first()).toHaveText('—');
  expect(api.requests.some((r) => r.url().includes('cursorAfter'))).toBeTruthy();
  expect(api.requests.every((r) => !r.headers()['x-appwrite-key'])).toBeTruthy();
  expect(api.requests.some((r) => r.headers()['x-appwrite-project'] === '6996dedc00271499aa0b')).toBeTruthy();
  const local = await page.evaluate(() => JSON.stringify(localStorage));
  expect(local).not.toContain('test-password');
  await page.getByRole('button', { name: 'Run sync now', exact: false }).click();
  await expect(page.locator('#execution-message')).toContainText('Waiting for completion');
  await expect(page.locator('#run-button')).toBeDisabled();
  await expect(page.locator('#execution-list')).toContainText('Completed', { timeout: 15000 });
  expect(api.submitted()).toBe(1);
  const post = api.requests.find((r) => r.method() === 'POST' && r.url().includes('/functions/'));
  expect(post.postDataJSON()).toMatchObject({ async: true, path: '/', method: 'POST' });
  await page.getByRole('button', { name: 'View recorded details' }).click();
  await expect(page.locator('#run-details')).toBeVisible();
  await page.getByRole('button', { name: 'Close' }).click();
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  await expect(page.locator('#copied-value')).toHaveText('—');
  await expect(page.locator('#run-button')).toBeDisabled();
  expect(errors).toEqual([]);
  expect(pings).toHaveLength(1);
  expect(pings[0].headers()['x-appwrite-project']).toBe('6996dedc00271499aa0b');
});

test('TablesDB, mobile layout, and stale data after a permission failure', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const api = await mockAppwrite(page);
  await configure(page, 'tables');
  await expect(page.locator('#copied-value')).toHaveText('7');
  expect(api.requests.some((r) => r.url().includes('/tablesdb/db/tables/'))).toBeTruthy();
  api.failReads();
  await page.locator('#refresh-button').click();
  await expect(page.locator('#data-message')).toContainText('responded 403');
  await expect(page.locator('#data-message')).toContainText('may be stale');
  await expect(page.locator('#copied-value')).toHaveText('7');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
});

test('permission errors do not display fabricated numbers or retry sync', async ({ page }) => {
  const api = await mockAppwrite(page, { denyReads: true, denyExecute: true });
  await configure(page);
  await expect(page.locator('#data-message')).toContainText('responded 403');
  await expect(page.locator('#copied-value')).toHaveText('—');
  await page.locator('#run-button').click();
  await expect(page.locator('#execution-message')).toContainText('Check your execution list');
  expect(api.requests.filter((r) => r.method() === 'POST' && r.url().includes('/functions/')).length).toBe(1);
});

test('session restoration, time range queries and periodic refresh', async ({ page }) => {
  await page.clock.install();
  const api = await mockAppwrite(page);
  await configure(page);
  await expect(page.locator('#copied-value')).toHaveText('7');
  await page.reload();
  await expect(page.locator('#account-name')).toHaveText('Test operator');
  await page.getByRole('button', { name: '7d', exact: true }).click();
  await expect(page.locator('#refresh-button')).toBeEnabled();
  const logsRequest = api.requests.filter((r) => r.url().includes('/telegram_copier_logs/')).at(-1);
  expect(decodeURIComponent(logsRequest.url())).toContain('greaterThanEqual');
  const before = api.listCalls();
  await page.clock.fastForward(31000);
  await expect.poll(api.listCalls).toBeGreaterThan(before);
});
