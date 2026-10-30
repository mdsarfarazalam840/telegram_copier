import { createService } from './appwrite-service.js';
import { client, projectName } from './appwrite.js';
import { DEFAULT_CONFIG, validateConfig, metric, describeError, executionOutcome } from './data.js';

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const configKey = 'relayroom.appwrite.v1';
let service;
let user;
let generation = 0;
let busy = false;
let refreshing = false;
let days = 1;
let dashboard = null;
let executions = [];
let activeExecution = null;
let sourcePage = 0;
let historyPage = 0;
const SOURCE_PAGE_SIZE = 6;
const HISTORY_PAGE_SIZE = 5;

const labels = { completed: 'Completed', completed_with_errors: 'Completed with source errors', halted: 'Paused by Telegram', running: 'Running checkpoint', failed: 'Failed', processing: 'Processing', waiting: 'Queued', scheduled: 'Scheduled' };
const formatNumber = (value) => metric(value) === null ? '—' : value.toLocaleString('en-IN');
function time(value, short = false) {
  if (!value || !Number.isFinite(new Date(value).getTime())) return '—';
  return new Intl.DateTimeFormat('en-IN', { timeZone: 'Asia/Kolkata', ...(short ? {} : { month: 'short', day: 'numeric' }), hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(value)) + ' IST';
}
function text(selector, value) { $(selector).textContent = value; }
function element(tag, value, className = '') {
  const node = document.createElement(tag);
  node.textContent = value;
  node.className = className;
  return node;
}
function toast(message, success = true) {
  text('#toast-text', message);
  text('.toast-icon', success ? '✓' : '!');
  $('#toast').classList.add('show');
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => $('#toast').classList.remove('show'), 5000);
}
function connectionState(label) {
  text('#connection-state', label);
  text('#live-label', label);
}
function check(name, state, message) {
  const node = $(`#check-${name}`);
  node.className = state;
  node.innerHTML = `<i></i>${message}`;
}
function controls() {
  $('#run-button').disabled = !user || busy || !!activeExecution;
  $('#refresh-button').disabled = !user || refreshing;
  $('#signout-button').hidden = !user;
  $('#signout-button').disabled = busy;
  $$('#connection-form button, #connection-form input, #connection-form select').forEach((node) => { node.disabled = busy; });
  $$('.range').forEach((node) => { node.disabled = refreshing; });
}
function clearData() {
  dashboard = null;
  executions = [];
  activeExecution = null;
  sourcePage = 0;
  historyPage = 0;
  renderDashboard();
  renderExecutions();
  text('#checked-at', 'No data loaded');
  text('#data-message', 'No live data loaded.');
  text('#account-name', 'Signed out');
}
function renderChart(history) {
  const runs = history.filter((run) => run.observed !== null && run.timestamp >= Date.now() - days * 86400000).reverse();
  const max = Math.max(1, ...runs.map((run) => run.observed));
  const start = Date.now() - days * 86400000;
  const path = runs.map((run, i) => `${i ? 'L' : 'M'}${Math.max(0, (run.timestamp - start) / (days * 86400000) * 800).toFixed(1)},${(228 - run.observed / max * 208).toFixed(1)}`).join(' ');
  $('#cyan-path').setAttribute('d', runs.length === 1 ? `${path} l1,0` : path);
  text('#chart-total', `${runs.length} recorded runs`);
  text('#chart-start', runs.length ? time(runs[0].timestamp) : '—');
  text('#chart-end', runs.length ? time(runs.at(-1).timestamp) : '—');
}
function renderDashboard() {
  const latest = dashboard?.latest;
  const m = latest?.metrics || {};
  const stale = latest && Date.now() - latest.timestamp > 30 * 60000;
  text('#route-status', latest ? `${stale ? 'Old record · ' : ''}${labels[latest.status] || 'Unknown'}` : 'Unknown');
  text('#run-state', latest ? labels[latest.status] || 'Unknown status' : 'Waiting for data');
  text('#last-pulse', time(latest?.timestamp));
  text('#run-summary', latest ? `Recorded ${time(latest.timestamp)}` : 'Connect to load your copier state.');
  text('#run-note', stale ? 'Latest record is over 30 minutes old. Check the deployed schedule.' : '— means the deployed function did not persist this metric.');
  text('#window-value', latest?.window ? `${latest.window} min` : '—');
  text('#state-time', time(dashboard?.updatedAt, true));
  text('#snapshot-value', formatNumber(latest?.observed));
  for (const [id, key] of Object.entries({ 'fetched-value': 'fetched', 'copied-value': 'copied', 'held-value': 'skipped_invalid', 'scrubbed-value': 'filtered_links' })) text(`#${id}`, formatNumber(m[key]));
  const sources = Object.entries(dashboard?.state?.sources || {});
  text('#source-count', dashboard ? `${sources.length} checkpoints` : '—');
  $('#channel-list').replaceChildren();
  const sourcePages = Math.max(1, Math.ceil(sources.length / SOURCE_PAGE_SIZE));
  sourcePage = Math.min(sourcePage, sourcePages - 1);
  const visibleSources = sources.slice(sourcePage * SOURCE_PAGE_SIZE, (sourcePage + 1) * SOURCE_PAGE_SIZE);
  for (const [id, state] of visibleSources) {
    const row = element('div', '', 'channel-row');
    const info = element('div', '', 'channel-info');
    info.append(element('strong', id), element('small', `Cursor ${state.last_id ?? '—'} · ${time(state.updated_at_ist || state.updated_at_utc)}`));
    row.append(info);
    $('#channel-list').append(row);
  }
  if (!sources.length) text('#channel-list', 'No source checkpoints available.');
  text('#source-page-label', `${sources.length ? sourcePage + 1 : 1} / ${sourcePages}`);
  $('#source-pagination [data-page="source-prev"]').disabled = sourcePage === 0;
  $('#source-pagination [data-page="source-next"]').disabled = sourcePage >= sourcePages - 1;
  $('#policy-list').replaceChildren();
  for (const [title, key] of [['Ad markers blocked', 'skipped_blocked_ad'], ['GIFs blocked', 'skipped_blocked_gif'], ['Recent duplicates skipped', 'skipped_duplicate_recent'], ['Cursor gate skips', 'skipped_cursor_gate'], ['Invalid at runtime', 'skipped_invalid_runtime']]) {
    const row = element('div', '', 'policy-row');
    row.append(element('div', title), element('span', formatNumber(m[key]), 'policy-value'));
    $('#policy-list').append(row);
  }
  const history = dashboard?.history || [];
  $('#timeline').replaceChildren();
  const historyPages = Math.max(1, Math.ceil(history.length / HISTORY_PAGE_SIZE));
  historyPage = Math.min(historyPage, historyPages - 1);
  const visibleHistory = history.slice(historyPage * HISTORY_PAGE_SIZE, (historyPage + 1) * HISTORY_PAGE_SIZE);
  for (const run of visibleHistory) {
    const row = element('div', '', 'timeline-item');
    const info = element('div', '');
    info.append(element('strong', labels[run.status] || run.status || 'Unknown'), element('small', `${time(run.timestamp)} · ${formatNumber(run.observed)} observed`));
    row.append(info);
    $('#timeline').append(row);
  }
  if (!history.length) text('#timeline', 'No recorded runs in this period.');
  text('#history-page-label', `${history.length ? historyPage + 1 : 1} / ${historyPages}`);
  $('#history-pagination [data-page="history-prev"]').disabled = historyPage === 0;
  $('#history-pagination [data-page="history-next"]').disabled = historyPage >= historyPages - 1;
  renderChart(history);
  $('#details-button').disabled = !latest;
  const sourceErrors = latest?.metrics?.source_errors;
  const errorNote = $('#source-error-note');
  if (latest?.status === 'completed_with_errors') {
    errorNote.hidden = false;
    if (sourceErrors && typeof sourceErrors === 'object' && Object.keys(sourceErrors).length) {
      errorNote.textContent = `${Object.entries(sourceErrors).map(([id, error]) => `${id}: ${error}`).join(' · ')}`;
    } else {
      errorNote.textContent = 'One or more source channels failed. Details were not persisted by the deployed worker; redeploy the worker to retain them for future runs.';
    }
  } else {
    errorNote.hidden = true;
    errorNote.textContent = '';
  }
}
function renderExecutions() {
  $('#execution-list').replaceChildren();
  for (const execution of executions) {
    const row = element('div', '', 'timeline-item');
    row.append(element('div', `${time(execution.$createdAt)} · ${labels[execution.status] || execution.status}`), element('span', execution.$id, 'policy-value'));
    $('#execution-list').append(row);
  }
  if (!executions.length) text('#execution-list', 'No executions visible for this user. Scheduled runs are shown through database records above.');
}

async function refresh() {
  if (!user || refreshing) return;
  const current = generation;
  const api = service;
  refreshing = true;
  controls();
  const [dataResult, executionResult] = await Promise.allSettled([api.dashboard(days), api.executions()]);
  if (current !== generation) return;
  if (dataResult.status === 'fulfilled') {
    dashboard = dataResult.value;
    renderDashboard();
    connectionState('Connected');
    text('#checked-at', `Refreshed ${time(Date.now(), true)}`);
    text('#data-message', dashboard.truncated ? 'Partial history: displaying the newest 2,000 snapshot rows. Select a shorter period for fuller coverage.' : dashboard.latest ? 'Loaded persisted Appwrite records. Empty counters are unavailable, not zero.' : 'Connected, but no visible records. Check collection Read permissions and that the deployed function writes logs/state.');
    check('data', 'ok', 'Database reads: successful');
  } else {
    connectionState('Refresh failed');
    text('#data-message', `${describeError(dataResult.reason)}${dashboard ? ' Previously loaded data is retained and may be stale.' : ''}`);
    check('data', 'bad', `Database reads: ${dataResult.reason?.code || 'failed'}`);
  }
  if (executionResult.status === 'fulfilled') {
    executions = executionResult.value.executions;
    activeExecution = executions.find((item) => ['waiting', 'processing', 'scheduled'].includes(item.status))?.$id || null;
    renderExecutions();
    text('#execution-message', activeExecution ? 'An execution is pending. Run sync is disabled while it is active.' : 'Only executions created by this signed-in user are visible here.');
    check('function', 'ok', 'Function access: readable');
  } else {
    text('#execution-message', describeError(executionResult.reason));
    check('function', 'bad', `Function access: ${executionResult.reason?.code || 'failed'}`);
  }
  refreshing = false;
  controls();
}

async function connect(config) {
  generation++;
  service = createService(config);
  user = null;
  refreshing = false;
  clearData();
  controls();
  const current = generation;
  const api = service;
  connectionState('Connecting');
  check('api', 'working', 'API endpoint: checking');
  check('session', 'working', 'Project session: checking');
  check('data', '', 'Database reads: waiting for sign-in');
  check('function', '', 'Function access: waiting for sign-in');
  try {
    await api.ping();
    check('api', 'ok', 'API endpoint: reachable');
    const account = await api.account();
    if (current !== generation) return;
    user = account;
    check('session', 'ok', `Project session: ${account.email}`);
    text('#account-name', account.name || account.email);
    $('#login-form').hidden = true;
    text('#connection-message', `Signed in as ${account.email}.`);
    $('#connection').open = false;
    await refresh();
  } catch (error) {
    if (current !== generation) return;
    connectionState('Sign-in required');
    if (error?.code === 401) check('session', 'bad', 'Project session: sign-in required');
    else if (error?.code === 403) check('session', 'bad', 'Project session: denied');
    else if (!error?.code) check('api', 'bad', 'API endpoint: browser could not reach it');
    $('#login-form').hidden = false;
    $('#connection').open = true;
    text('#connection-message', describeError(error));
  }
  controls();
}

async function triggerSync() {
  if (!user || busy || activeExecution) return;
  const api = service;
  const current = generation;
  busy = true;
  controls();
  text('#execution-message', 'Submitting one execution…');
  try {
    let execution = await api.start();
    activeExecution = execution.$id;
    const deadline = Date.now() + 360000;
    while (executionOutcome(execution) === 'pending' && Date.now() < deadline) {
      text('#execution-message', `Execution ${execution.$id}: ${execution.status}. Waiting for completion…`);
      await new Promise((resolve) => setTimeout(resolve, 2500));
      if (current !== generation) return;
      execution = await api.execution(execution.$id);
    }
    const outcome = executionOutcome(execution);
    if (outcome === 'pending') toast('Still running. Use Refresh to check; no second execution was submitted.', false);
    else {
      activeExecution = null;
      toast(outcome === 'completed' ? 'Execution finished. Refreshing persisted run results.' : labels[outcome], outcome === 'completed');
    }
    await refresh();
  } catch (error) {
    // Never retry a createExecution call: the first request may have been accepted.
    text('#execution-message', `${describeError(error)} Check your execution list before submitting another sync.`);
    toast('Could not confirm execution completion. No automatic retry was made.', false);
  } finally {
    busy = false;
    controls();
  }
}

$('#connection-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    const config = validateConfig(Object.fromEntries(new FormData(event.currentTarget)));
    localStorage.setItem(configKey, JSON.stringify(config));
    await connect(config);
  } catch (error) { text('#connection-message', describeError(error)); }
});
$('#login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('button');
  const credentials = new FormData(form);
  const api = service;
  const current = generation;
  button.disabled = true;
  try {
    await api.signIn(credentials.get('email'), credentials.get('password'));
    if (current === generation) await connect(api.config);
  } catch (error) {
    if (current === generation) text('#connection-message', describeError(error));
  } finally {
    form.elements.password.value = '';
    credentials.delete('password');
    button.disabled = false;
  }
});
$('#signout-button').addEventListener('click', async () => {
  try {
    await service.signOut();
    generation++;
    user = null;
    refreshing = false;
    clearData();
    connectionState('Signed out');
    $('#login-form').hidden = false;
    $('#connection').open = true;
    text('#connection-message', 'Signed out.');
    text('#execution-message', 'Sign in to view executions created by your user.');
    controls();
  } catch (error) { toast(describeError(error), false); }
});
$('#refresh-button').addEventListener('click', refresh);
$('#run-button').addEventListener('click', triggerSync);
$('#details-button').addEventListener('click', () => {
  text('#details-content', JSON.stringify(dashboard?.latest, null, 2));
  $('#run-details').showModal();
});
$$('.range').forEach((button) => button.addEventListener('click', () => {
  days = Number(button.dataset.days);
  historyPage = 0;
  $$('.range').forEach((item) => item.classList.toggle('active', item === button));
  renderChart(dashboard?.history || []);
  refresh();
}));
$$('.page-button').forEach((button) => button.addEventListener('click', () => {
  if (button.dataset.page === 'source-prev') sourcePage = Math.max(0, sourcePage - 1);
  if (button.dataset.page === 'source-next') sourcePage += 1;
  if (button.dataset.page === 'history-prev') historyPage = Math.max(0, historyPage - 1);
  if (button.dataset.page === 'history-next') historyPage += 1;
  renderDashboard();
}));
$$('.nav-item').forEach((link) => link.addEventListener('click', () => {
  $$('.nav-item').forEach((item) => item.classList.toggle('active', item === link));
  if (link.hash === '#connection') $('#connection').open = true;
}));
setInterval(() => { if (!document.hidden && !busy) refresh(); }, 30000);
document.addEventListener('visibilitychange', () => { if (!document.hidden && !busy) refresh(); });
window.addEventListener('online', refresh);
window.addEventListener('offline', () => { connectionState('Offline'); text('#data-message', 'Offline. Previously loaded records may be stale.'); });

// One diagnostic ping per page startup, independent of login and refresh polling.
client.ping().then(() => {
  text('#sdk-status', `${projectName}: startup ping succeeded (Frankfurt).`);
  check('api', 'ok', 'API endpoint: reachable');
}).catch((error) => {
  text('#sdk-status', `${projectName}: startup ping failed. ${describeError(error)}`);
  check('api', 'bad', 'API endpoint: unreachable');
});

let saved;
try { saved = JSON.parse(localStorage.getItem(configKey) || 'null'); } catch { /* Invalid local settings: show setup. */ }
const initial = { ...DEFAULT_CONFIG, ...saved };
for (const key of Object.keys(DEFAULT_CONFIG)) $('#connection-form').elements[key].value = initial[key];
clearData();
if (saved) {
  try { await connect(validateConfig(saved)); } catch (error) { text('#connection-message', describeError(error)); }
}
// Decorative rendering must never prevent authentication or data loading.
import('./relay-core.js').then(({ createRelayCore }) => createRelayCore($('#relay-canvas'))).catch(() => {
  text('.label-bottom', 'RELAY CORE · 2D MODE');
});
