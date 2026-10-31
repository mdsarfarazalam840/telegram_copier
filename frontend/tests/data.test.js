import test from 'node:test';
import assert from 'node:assert/strict';
import { DEFAULT_CONFIG, validateConfig, normalizeDashboard, executionOutcome, describeError } from '../data.js';

const row = (timestamp, extra = {}) => ({ function_name: 'telegram_copier_run_v1', run_ts_ms: timestamp, source_id: -1001, status: 'completed', lookback_minutes: 60, message_ids: '1,2', ...extra });
test('legacy marker and JSON-string state support real backend formats', () => {
  const state = { sources: { '-1001': { last_id: 3 } }, meta: { status: 'completed', last_run_ts_ms: 1000, copied: 0, fetched: 3 } };
  for (const doc of [{ payload_json: JSON.stringify(state) }, { payload: 'telegram_copier_state_v1:' + JSON.stringify(state) }, { payload_json: 'invalid', payload: 'telegram_copier_state_v1:' + JSON.stringify(state) }]) {
    const data = normalizeDashboard([doc], [row(1000)]);
    assert.equal(data.latest.metrics.copied, 0);
    assert.equal(data.latest.metrics.skipped_blocked_ad, undefined);
  }
});
test('snapshot parts/retries deduplicate per source, not across sources', () => {
  const data = normalizeDashboard([], [row(1000), row(1000), row(1000, { message_ids_json: '[2,3]' }), row(1000, { source_id: -1002, message_ids_json: ['1'] })]);
  assert.equal(data.latest.observed, 4);
  assert.equal(data.history.length, 1);
  assert.deepEqual(data.latest.sources, [{ id: '-1001', observed: 3 }, { id: '-1002', observed: 1 }]);
});
test('new snapshots never inherit copied counts from an older checkpoint', () => {
  const data = normalizeDashboard([{ payload_json: { meta: { last_run_ts_ms: 1000, status: 'completed', copied: 12 } } }], [row(2000)]);
  assert.equal(data.latest.timestamp, 2000);
  assert.equal(data.latest.metrics, undefined);
});
test('running checkpoint counters are not mistaken for the current result', () => {
  const data = normalizeDashboard([{ payload_json: { meta: { last_run_ts_ms: 2000, status: 'running', copied: 12 } } }], [row(2000)]);
  assert.equal(data.latest.status, 'completed');
  assert.equal(data.latest.metrics, undefined);
});
test('empty data stays empty and partial statuses remain visible', () => {
  assert.equal(normalizeDashboard([], []).latest, null);
  assert.equal(normalizeDashboard([], [row(2000, { status: 'halted' })]).latest.status, 'halted');
  assert.equal(normalizeDashboard([], [row(2000, { function_name: 'other' })]).latest, null);
});
test('state counters fill the latest snapshot when timestamps differ', () => {
  const data = normalizeDashboard([
    { payload_json: { sources: {}, meta: { last_run_ts_ms: 2000, status: 'completed_with_errors', fetched: 19, copied: 11, skipped_invalid: 3, filtered_links: 2, source_errors: { '-1002': 'Channel unavailable' } } } },
  ], [row(1900, { status: 'completed_with_errors' })]);
  assert.deepEqual(data.latest.metrics, {
    last_run_ts_ms: 2000, status: 'completed_with_errors', fetched: 19, copied: 11, skipped_invalid: 3, filtered_links: 2, source_errors: { '-1002': 'Channel unavailable' },
  });
});
test('execution result distinguishes queue, HTTP failure, application failure, and flood wait', () => {
  assert.equal(executionOutcome({ status: 'processing' }), 'pending');
  assert.equal(executionOutcome({ status: 'completed', responseStatusCode: 500 }), 'failed');
  assert.equal(executionOutcome({ status: 'completed', responseBody: '{"ok":false}' }), 'failed');
  assert.equal(executionOutcome({ status: 'completed', responseBody: '{"ok":true,"halted":1}' }), 'halted');
  assert.equal(executionOutcome({ status: 'completed', responseBody: '{"source_errors_count":2}' }), 'completed_with_errors');
  assert.equal(executionOutcome({ status: 'completed', responseBody: '' }), 'completed');
});
test('connection config only retains public allowed fields and validates endpoint', () => {
  const input = { ...DEFAULT_CONFIG, endpoint: 'https://example.test/v1/', databaseId: 'db', apiKey: 'not-a-real-key' };
  const config = validateConfig(input);
  assert.equal(config.endpoint, 'https://example.test/v1');
  assert.equal(config.apiKey, undefined);
  assert.throws(() => validateConfig({ ...input, endpoint: 'https://user:pass@example.test/v1' }));
  assert.throws(() => validateConfig({ ...input, endpoint: 'http://example.test/v1' }));
});
test('errors have actionable permission and network guidance', () => {
  assert.match(describeError({ code: 403 }), /Read.*Execute/);
  assert.match(describeError({ message: 'Failed to fetch' }), /Platform|browser could not reach/);
});
