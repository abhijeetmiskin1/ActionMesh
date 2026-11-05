'use strict';
/** activity.js pure-helper + no-fake-success tests. node:test, no jsdom. */
const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const A = require('../ui/activity.js');

test('active counting covers both state spellings and excludes terminal', () => {
  const recs = [
    { execution_id: 'a', state: 'executing' },
    { execution_id: 'b', state: 'waiting_permission' },
    { execution_id: 'c', state: 'running' },
    { execution_id: 'd', state: 'pending' },
    { execution_id: 'e', state: 'success', results: { n1: { verification: { verified: true } } } },
    { execution_id: 'f', state: 'failed' },
    { execution_id: 'g', state: 'cancelled' },
    { execution_id: 'h', state: 'interrupted' },
  ];
  assert.equal(A.countActive(recs), 4);
  assert.equal(A.isTerminalState('success'), true);
  assert.equal(A.isActiveState('executing'), true);
  assert.equal(A.isActiveState('success'), false);
});

test('unverified success is never labelled Succeeded', () => {
  const unverified = { state: 'success', results: { n1: { verification: { verified: false } } } };
  const verified = { state: 'success', results: { n1: { verification: { verified: true } } } };
  assert.equal(A.recordLabel(unverified), 'Succeeded (unverified)');
  assert.equal(A.displayChip(unverified), 'succeeded_unverified');
  assert.equal(A.recordLabel(verified), 'Succeeded');
  assert.equal(A.displayChip(verified), 'success');
  assert.equal(A.recordLabel({ state: 'requires_user' }), 'Unverified — needs you');
  assert.equal(A.recordLabel({ state: 'waiting_permission' }), 'Waiting for approval');
  assert.equal(A.isVerifiedSuccess(unverified), false);
  assert.equal(A.isVerifiedSuccess(verified), true);
});

test('approval and device counts read real shapes', () => {
  assert.equal(A.approvalCount([{ actions: [1, 2] }, { actions: [1] }, {}]), 3);
  assert.equal(A.onlineCount([{ online: true }, { online: false }, { online: true }]), 2);
});

test('session progress reads steps array (TaskSession.to_dict shape)', () => {
  const s = { steps: [{ state: 'succeeded' }, { state: 'executing' }, { state: 'failed' }] };
  assert.deepEqual(A.sessionProgress(s), { settled: 2, total: 3 });
  assert.deepEqual(A.sessionProgress({ steps: [] }), { settled: 0, total: 0 });
  assert.deepEqual(A.sessionProgress(null), { settled: 0, total: 0 });
});

test('greeting and timeAgo are honest about unknown input', () => {
  assert.equal(A.greetingForHour(9), 'Good morning');
  assert.equal(A.greetingForHour(15), 'Good afternoon');
  assert.equal(A.greetingForHour(21), 'Good evening');
  assert.equal(A.timeAgo(null, new Date()), '');
  assert.equal(A.timeAgo('not-a-date', new Date()), '');
  assert.ok(A.timeAgo(new Date(Date.now() - 30_000).toISOString(), new Date()) === 'just now');
});

test('no innerHTML / no fetch / no hardcoded success in activity.js', () => {
  const src = fs.readFileSync(path.join(__dirname, '..', 'ui', 'activity.js'), 'utf8');
  assert.equal(src.includes('.innerHTML'), false);
  assert.equal(/\bfetch\s*\(/.test(src), false);
  assert.equal(/\bXMLHttpRequest\b/.test(src), false);
  // /dashboard is an HTML page, must not be used for stats
  assert.equal(src.includes("'/dashboard'") || src.includes('"/dashboard"'), false);
  // must go through the bridge
  assert.ok(src.includes('window.actionmesh') || src.includes('globalThis.actionmesh'));
  assert.ok(src.includes("'/ready'"));
  assert.ok(src.includes("'/devices'"));
  assert.ok(src.includes("'/approvals'"));
  assert.ok(src.includes("'/executions?limit=8'"));
  assert.ok(src.includes("'/automations'"));
  assert.ok(src.includes("'/audit?limit=8'"));
  // quick actions call real APIs
  assert.ok(src.includes("'/voiceos/messages'"));
  assert.ok(src.includes('/cancel'));
});

test('exports the documented surface', () => {
  for (const name of ['loadDashboard', 'renderDashboard', 'initActivity', 'propose', 'pauseExecution', 'copyId']) {
    assert.equal(typeof A[name], 'function', name);
  }
});

test('audit evidence renders allowlisted fields only — never payload (invariant 4)', () => {
  const { install } = require('./dom_harness.js');
  const page = install({});
  // Give the renderer a document and the three audit hosts.
  const g = globalThis;
  const saved = { document: g.document };
  g.document = page.document;
  try {
    const SECRET = 'CONTENT-MUST-NOT-LEAK-0123456789';
    const data = {
      audit: {
        chain: { ok: true, records: 2 },
        head: 'abc123def4567890',
        records: [
          { seq: 1, result: 'node.completed', actor: 'voiceos-local', subject: 'graph_aaa',
            timestamp: new Date().toISOString(), payload: { data: { result: { content: SECRET } } } },
          { seq: 2, result: 'execution.completed', actor: 'voiceos-local', subject: 'graph_aaa',
            timestamp: new Date().toISOString(), payload: { data: { result: { content: SECRET } } } },
        ],
      },
      auditVerify: { ok: true, records: 2, head: 'abc123def4567890' },
    };
    A.renderAuditEvidence(data);

    const text = (id) => page.document.getElementById(id).textContent;
    assert.match(text('homeAuditChain'), /chain verified/);
    assert.match(text('homeAuditHead'), /2 records · head abc123def4567890/);
    assert.match(text('homeAuditLog'), /execution\.completed/);

    // The decisive check: nothing from `payload` may reach the DOM.
    const rendered = ['homeAuditChain', 'homeAuditHead', 'homeAuditLog'].map(text).join(' ');
    assert.equal(rendered.includes(SECRET), false, 'action result content crossed the boundary');
    assert.equal(rendered.includes('payload'), false);
  } finally {
    if (saved.document === undefined) delete g.document; else g.document = saved.document;
  }
});

test('audit evidence never paints a broken or unreadable chain as healthy', () => {
  const { install } = require('./dom_harness.js');
  const page = install({});
  const g = globalThis;
  const saved = { document: g.document };
  g.document = page.document;
  try {
    A.renderAuditEvidence({ audit: { chain: { ok: false, records: 5 } }, auditVerify: { ok: false } });
    assert.match(page.document.getElementById('homeAuditChain').textContent, /chain broken/);
    assert.match(page.document.getElementById('homeAuditChain').className, /warn/);

    // Unreadable (both reads failed) is its own word — not "broken", not "verified".
    A.renderAuditEvidence({});
    assert.match(page.document.getElementById('homeAuditChain').textContent, /unreadable/);
  } finally {
    if (saved.document === undefined) delete g.document; else g.document = saved.document;
  }
});
