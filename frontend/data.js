export const DEFAULT_CONFIG = {
  endpoint: 'https://fra.cloud.appwrite.io/v1', projectId: '6996dedc00271499aa0b',
  functionId: 'telegram-copier-webhook', databaseId: '', stateDatabaseId: '',
  logsId: 'telegram_copier_logs', stateId: 'telegram_copier_state', storage: 'collections',
};

export function validateConfig(input) {
  const config = Object.fromEntries(Object.keys(DEFAULT_CONFIG).map((key) => [key, String(input[key] ?? DEFAULT_CONFIG[key]).trim()]));
  let url;
  try { url = new URL(config.endpoint); } catch { throw new Error('Enter your Appwrite API endpoint from Project Settings.'); }
  if (url.username || url.password || url.search || url.hash || !['https:', 'http:'].includes(url.protocol)) throw new Error('Use an API endpoint without credentials, query parameters, or fragments.');
  if (url.protocol !== 'https:' && !['localhost', '127.0.0.1'].includes(url.hostname)) throw new Error('Use HTTPS for a remote Appwrite endpoint.');
  config.endpoint = config.endpoint.replace(/\/+$/, '');
  if (!config.endpoint.endsWith('/v1')) throw new Error('The Appwrite API endpoint must end in /v1.');
  for (const key of ['projectId', 'functionId', 'databaseId', 'logsId', 'stateId']) {
    if (!/^[\w.-]{1,36}$/.test(config[key])) throw new Error(`Enter a valid ${key} (1–36 letters, numbers, dots, underscores or hyphens).`);
  }
  if (config.stateDatabaseId && !/^[\w.-]{1,36}$/.test(config.stateDatabaseId)) throw new Error('Enter a valid state database ID.');
  if (!['collections', 'tables'].includes(config.storage)) throw new Error('Choose Collections or TablesDB.');
  return config;
}

export function parseObject(value) {
  try {
    const parsed = typeof value === 'string' ? JSON.parse(value.replace(/^telegram_copier_state_v1:/, '')) : value;
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : null;
  } catch { return null; }
}

export function metric(value) {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
}

export function snapshotIds(row) {
  let ids = row.message_ids_json;
  if (typeof ids === 'string') {
    try { ids = JSON.parse(ids); } catch { ids = []; }
  }
  if (!Array.isArray(ids) || !ids.length) ids = String(row.message_ids ?? '').split(',');
  return ids.map(String).filter((id) => /^\d+$/.test(id));
}

export function normalizeDashboard(stateRows, logRows) {
  const document = stateRows[0];
  const state = parseObject(document?.payload_json) || parseObject(document?.payload) || {};
  const meta = state.meta || {};
  const runs = new Map();
  for (const row of logRows) {
    if (row.function_name !== 'telegram_copier_run_v1' || !Number.isFinite(Number(row.run_ts_ms))) continue;
    const timestamp = Number(row.run_ts_ms);
    if (!timestamp) continue;
    if (!runs.has(timestamp)) runs.set(timestamp, { timestamp, status: row.status, window: metric(row.lookback_minutes), observed: new Set(), sources: new Map() });
    const run = runs.get(timestamp);
    if (['halted', 'completed_with_errors'].includes(row.status)) run.status = row.status;
    const source = String(row.source_id);
    if (!run.sources.has(source)) run.sources.set(source, new Set());
    for (const id of snapshotIds(row)) {
      run.observed.add(`${source}:${id}`);
      run.sources.get(source).add(id);
    }
  }
  const stateTimestamp = Number(meta.last_run_ts_ms);
  if (stateTimestamp > 0) {
    const run = runs.get(stateTimestamp) || { timestamp: stateTimestamp, status: meta.status, observed: null, sources: new Map() };
    // Running checkpoints can still contain counters from the preceding run.
    if (['completed', 'completed_with_errors', 'halted'].includes(meta.status)) {
      run.status = meta.status;
      run.metrics = meta;
    }
    runs.set(stateTimestamp, run);
  }
  const history = [...runs.values()].sort((a, b) => b.timestamp - a.timestamp).map((run) => ({
    ...run, observed: run.observed?.size ?? null,
    sources: [...run.sources].map(([id, ids]) => ({ id, observed: ids.size })),
  }));
  // The backend writes counters to the global state row and snapshots to log rows.
  // A state update can succeed while its snapshot log is delayed/failed, so do not
  // require an exact timestamp match before showing persisted counters.
  const finalStatuses = ['completed', 'completed_with_errors', 'halted'];
  if (finalStatuses.includes(meta.status) && stateTimestamp > 0) {
    const target = history.find((run) => run.timestamp === stateTimestamp) || history[0];
    if (target && !target.metrics) target.metrics = meta;
  }
  return { state, history, latest: history[0] || null, updatedAt: document?.$updatedAt || null };
}

export function executionOutcome(execution) {
  const result = parseObject(execution.responseBody);
  if (execution.status === 'failed' || execution.responseStatusCode >= 400 || result?.ok === false) return 'failed';
  if (execution.status !== 'completed') return 'pending';
  if (result?.halted) return 'halted';
  if (result?.source_errors_count) return 'completed_with_errors';
  return 'completed';
}

export function describeError(error) {
  const code = Number(error?.code || error?.status || 0);
  const detail = error?.type ? ` [${error.type}]` : '';
  if (code === 401) return `Appwrite API responded 401${detail}: this project user is not signed in, or the session has expired. Sign in with an Auth user created inside this Appwrite project.`;
  if (code === 403) return `Appwrite API responded 403${detail}: this user lacks the required Read/Execute permission. Check the collection/table and function Execute permissions.`;
  if (code === 404) return `Appwrite API responded 404${detail}: check the endpoint, project, database, collection/table, and function IDs.`;
  if (code === 429) return 'Appwrite is rate limiting requests. Wait a minute, then refresh.';
  if (!code && /fetch|network|load failed|cors|origin/i.test(error?.message || '')) return `The browser could not reach Appwrite${detail}. Add this exact frontend hostname under Appwrite Project → Platforms → Web, then check the API endpoint and browser console.`;
  return `${error?.message || 'Appwrite request failed.'}${detail}${code ? ` (HTTP ${code})` : ''}`;
}
