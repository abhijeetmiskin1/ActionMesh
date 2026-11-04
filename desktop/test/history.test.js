'use strict';
/** history.js pure-helper tests (Agent 5). node:test, no jsdom. */
const assert = require('node:assert/strict');
const { test } = require('node:test');
const H = require('../ui/history.js');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const verified = { state:'success', results:{ n1:{ verification:{verified:true} } } };
const unverified = { state:'success', results:{ n1:{ verification:{verified:false} } } };

test('no fake success labels', () => {
  assert.equal(H.displayState('waiting_permission').label, 'Waiting for approval');
  assert.notEqual(H.displayState('waiting_permission').chip, 'success');
  assert.equal(H.displayState('requires_user').label, 'Unverified — needs you');
  assert.equal(H.displayState('interrupted').label, 'Interrupted');
  assert.equal(H.recordLabel(unverified), 'Succeeded (unverified)');
  assert.equal(H.recordLabel(verified), 'Succeeded');
});

test('isVerifiedSuccess is strict', () => {
  assert.equal(H.isVerifiedSuccess(verified), true);
  assert.equal(H.isVerifiedSuccess(unverified), false);
  assert.equal(H.isVerifiedSuccess({ state:'success', results:{} }), false);
  assert.equal(H.isVerifiedSuccess({ state:'failed', results:{ n1:{ verification:{verified:true} } } }), false);
  assert.equal(H.isVerifiedSuccess(null), false);
});

test('verificationLabel distinguishes states', () => {
  assert.equal(H.verificationLabel(verified), 'verified');
  assert.equal(H.verificationLabel(unverified), 'not verified');
  assert.equal(H.verificationLabel({ state:'success', results:{} }), 'not recorded');
  assert.equal(H.verificationLabel({ state:'success', results:{ a:{verification:{verified:true}}, b:{verification:{verified:false}} } }), 'partially verified');
});

test('safeFailureReason caps at 240 and reads only error', () => {
  const long = 'x'.repeat(300);
  assert.equal(H.safeFailureReason({ error: long }).length, 241);
  assert.equal(H.safeFailureReason({ error: '  denied  ' }), 'denied');
  assert.equal(H.safeFailureReason({ results:{ n1:{ stdout:'leak' } } }), '');
});

test('dedup drops replay duplicates by event_id', () => {
  const d = H.createDedup(10);
  assert.equal(d.accept({ event_id: 'evt_1' }), true);
  assert.equal(d.accept({ event_id: 'evt_1' }), false);
  assert.equal(d.accept({ event_id: 'evt_2' }), true);
  assert.equal(d.size, 2);
  const merged = H.mergeTimeline([{ event_id: 'evt_1' }], [{ event_id: 'evt_1' }, { event_id: 'evt_2' }]);
  assert.deepEqual(merged.map((e) => e.event_id), ['evt_1', 'evt_2']);
});

test('transient output is not a history event', () => {
  assert.equal(H.isHistoryEvent({ event_type: 'execution.output' }), false);
  assert.equal(H.isHistoryEvent({ event_type: 'execution.completed' }), true);
  assert.equal(H.isHistoryEvent(null), false);
});

test('filters bucket waiting vs running vs succeeded', () => {
  const recs = [
    { execution_id: 'a', state: 'waiting_permission' },
    { execution_id: 'b', state: 'executing' },
    { execution_id: 'c', state: 'success' },
    { execution_id: 'd', state: 'failed' },
  ];
  assert.deepEqual(H.filterExecutions(recs, { bucket: 'waiting' }).map((r) => r.execution_id), ['a']);
  assert.deepEqual(H.filterExecutions(recs, { bucket: 'running' }).map((r) => r.execution_id), ['a', 'b']);
  assert.deepEqual(H.filterExecutions(recs, { bucket: 'succeeded' }).map((r) => r.execution_id), ['c']);
  assert.deepEqual(H.filterExecutions(recs, { bucket: 'attention' }).map((r) => r.execution_id), ['d']);
  assert.deepEqual(H.filterExecutions(recs, { query: 'B' }).map((r) => r.execution_id), ['b']);
});

test('no innerHTML in history.js (CSP safety)', () => {
  const src = fs.readFileSync(path.join(__dirname, '..', 'ui', 'history.js'), 'utf8');
  // Header comments mention innerHTML by name; a DOM assignment is the real risk.
  assert.equal(src.includes('.innerHTML'), false);
});

test('history.js parses under node', () => {
  execFileSync('node', ['--check', path.join(__dirname, '..', 'ui', 'history.js')], { stdio: 'pipe' });
});

// ---------------------------------------------------------------------------
// Phase 2 Agent 5 — terminal-state honesty + P2-3 accessibility
// ---------------------------------------------------------------------------

test('terminal states render distinctly: success vs failed/denied/cancelled/timeout', () => {
  const ok = { state: 'success', results: { n1: { verification: { verified: true } } } };
  const cases = [
    [{ state: 'success', results: { n1: { verification: { verified: true } } } }, 'Succeeded', 'success'],
    [{ state: 'failed' }, 'Failed', 'failed'],
    [{ state: 'denied' }, 'Denied', 'denied'],
    [{ state: 'cancelled' }, 'Cancelled', 'cancelled'],
    [{ state: 'timeout' }, 'Timed out', 'timeout'],
  ];
  for (const [rec, label, chip] of cases) {
    assert.equal(H.recordLabel(rec), label, `${rec.state} label`);
    assert.equal(H.displayState(rec.state).chip, chip, `${rec.state} chip`);
    assert.equal(H.isTerminal(rec.state), true, `${rec.state} is terminal`);
  }
  assert.equal(H.isVerifiedSuccess(ok), true);
  // UNVERIFIED / REQUIRES_USER are never success.
  assert.equal(H.recordLabel(unverified), 'Succeeded (unverified)');
  assert.notEqual(H.recordLabel(unverified), 'Succeeded');
  assert.equal(H.isVerifiedSuccess(unverified), false);
  assert.equal(H.displayState('requires_user').chip, 'requires_user');
  assert.notEqual(H.displayState('requires_user').chip, 'success');
  assert.equal(H.displayState('requires_user').label, 'Unverified — needs you');
  // succeeded bucket only contains true success records.
  const recs = [
    { execution_id: 'a', state: 'success', results: { n1: { verification: { verified: true } } } },
    { execution_id: 'b', state: 'failed' },
    { execution_id: 'c', state: 'denied' },
    { execution_id: 'd', state: 'cancelled' },
    { execution_id: 'e', state: 'timeout' },
  ];
  assert.deepEqual(H.filterExecutions(recs, { bucket: 'succeeded' }).map((r) => r.execution_id), ['a']);
  // 'attention' is failed/denied/timeout/interrupted/partially_completed;
  // cancelled is terminal but not in the attention bucket (see history.js).
  assert.deepEqual(H.filterExecutions(recs, { bucket: 'attention' }).map((r) => r.execution_id), ['b', 'c', 'e']);
});

test('history.js sets aria-live on its hosts at refresh time (P2-3)', () => {
  const src = fs.readFileSync(path.join(__dirname, '..', 'ui', 'history.js'), 'utf8');
  assert.ok(src.includes("aria-live"), 'history.js sets aria-live');
  assert.ok(src.includes("'polite'"), 'aria-live is polite');
  assert.ok(src.includes("role', 'status'"), 'hosts get role=status');
  assert.equal(/\.style\.(color|background|backgroundColor|borderColor)\s*=/.test(src), false,
    'history.js sets no inline colour');
  assert.equal(/style\s*=\s*["'][^"']*(#[0-9a-fA-F]{3,8}|rgb)/.test(src), false,
    'history.js has no inline hard-coded colour');
});

test('unverified success never wears the green success chip', () => {
  const unverifiedRec = { execution_id: 'u1', state: 'success', results: { n1: { verification: { verified: false } } } };
  const verifiedRec = { execution_id: 'v1', state: 'success', results: { n1: { verification: { verified: true } } } };
  assert.deepEqual(H.recordChip(unverifiedRec), { label: 'Succeeded (unverified)', chip: 'succeeded_unverified' });
  assert.deepEqual(H.recordChip(verifiedRec), { label: 'Succeeded', chip: 'success' });
  assert.equal(H.recordChip({ state: 'requires_user' }).chip, 'requires_user');
  assert.notEqual(H.recordChip({ state: 'requires_user' }).chip, 'success');
  assert.equal(H.recordChip({ state: 'failed' }).chip, 'failed');
});
