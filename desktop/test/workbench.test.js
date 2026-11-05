'use strict';
/**
 * Workbench unit tests (desktop/test conventions: node:test, no jsdom).
 * Exercises the pure helpers the workbench UI is built on; DOM behavior is
 * covered by the shared dom_harness in the Lead's wiring pass. The security
 * invariants tested here mirror AGENTS.md:
 *  - unverified / requires_user is never success,
 *  - CRITICAL/HIGH always renders as requiring approval,
 *  - empty plans never produce an executable graph,
 *  - the /execute payload never carries grants or a risk level,
 *  - rendering uses textContent, never innerHTML (source check, like
 *    history.test.js).
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const wb = require('../ui/workbench.js');

const CAPS = [
  { action: 'search.web', provider: 'mock-search', permissions: ['search.web'], risk_level: 'low' },
  { action: 'payment.prepare', provider: 'mock-payments', permissions: ['payment.prepare'], risk_level: 'critical' },
  { action: 'file.share', provider: 'mock-files', permissions: ['file.send'], risk_level: 'high' },
];

test('planToGraph: linear chain n0 -> n1 -> end, typed action nodes', () => {
  const graph = wb.planToGraph({
    intent: 'do two things',
    actions: [
      { type: 'search.web', parameters: { query: 'cats' } },
      { type: 'reminder.create', parameters: { text: 'drink water' } },
    ],
  });
  assert.equal(graph.intent, 'do two things');
  assert.deepEqual(graph.nodes.map((n) => n.node_id), ['n0', 'n1', 'end']);
  assert.deepEqual(graph.nodes[0].depends_on, []);
  assert.deepEqual(graph.nodes[1].depends_on, ['n0']);
  assert.deepEqual(graph.nodes[2].depends_on, ['n1']);
  assert.equal(graph.nodes[0].node_type, 'action');
  assert.deepEqual(graph.nodes[0].action, { type: 'search.web', parameters: { query: 'cats' } });
  assert.equal(graph.nodes[2].node_type, 'end');
});

test('planToGraph: empty plan has only the END node and is not executable', () => {
  const graph = wb.planToGraph({ intent: '', actions: [] });
  assert.deepEqual(graph.nodes.map((n) => n.node_id), ['end']);
  assert.equal(wb.planExecutable({ actions: [] }), false);
  assert.equal(wb.planExecutable(null), false);
  assert.equal(wb.planExecutable({ actions: [{ type: 'search.web', parameters: {} }] }), true);
});

test('buildPolicy: allow-rules echo registry permissions and default to deny', () => {
  const plan = { actions: [
    { type: 'search.web', parameters: { query: 'x' } },
    { type: 'totally.unknown', parameters: {} },
  ] };
  const policy = wb.buildPolicy(plan, CAPS);
  assert.equal(policy.default_decision, 'deny');
  assert.deepEqual(policy.rules[0], { action: 'search.web', decision: 'allow', permissions: ['search.web'] });
  // Unknown actions get an empty permission list — the engine refuses them.
  assert.deepEqual(policy.rules[1].permissions, []);
});

test('planSteps: risk and permissions come from /capabilities, never the plan', () => {
  const steps = wb.planSteps(
    { actions: [{ type: 'payment.prepare', parameters: {} }, { type: 'unknown.action', parameters: {} }] },
    CAPS,
  );
  assert.equal(steps[0].risk, 'critical');
  assert.equal(steps[0].requiresApproval, true);
  assert.equal(steps[0].decision, 'not decided — decided at execute');
  assert.deepEqual(steps[0].permissions, ['payment.prepare']);
  assert.equal(steps[1].known, false);
  assert.equal(steps[1].risk, 'unknown');
});

test('planSteps: LOW risk does not require approval; HIGH does', () => {
  const steps = wb.planSteps(
    { actions: [{ type: 'search.web', parameters: {} }, { type: 'file.share', parameters: {} }] },
    CAPS,
  );
  assert.equal(steps[0].requiresApproval, false);
  assert.equal(steps[1].requiresApproval, true);
});

test('isVerifiedSuccess: strict — unverified success is not verified success', () => {
  const ok = { state: 'success', nodes: { n0: { verification: { verified: true } } } };
  assert.equal(wb.isVerifiedSuccess(ok), true);
  const unverified = { state: 'success', nodes: { n0: { verification: { verified: false } } } };
  assert.equal(wb.isVerifiedSuccess(unverified), false);
  assert.equal(wb.resultLabel(unverified), 'Succeeded (unverified)');
  const noVerification = { state: 'success', nodes: {} };
  assert.equal(wb.isVerifiedSuccess(noVerification), false);
  const requiresUser = { state: 'requires_user', nodes: {} };
  assert.equal(wb.isVerifiedSuccess(requiresUser), false);
  assert.equal(wb.resultLabel(requiresUser), 'Unverified — needs you');
});

test('resultLabel: waiting_permission is never success', () => {
  assert.equal(wb.resultLabel({ state: 'waiting_permission' }), 'Waiting for approval');
  assert.equal(wb.resultLabel({ state: 'denied' }), 'Denied');
  assert.equal(wb.resultLabel({ state: 'failed' }), 'Failed');
});

test('nodeViews: projects only safe fields, truncates detail', () => {
  const result = {
    state: 'success',
    nodes: {
      n0: {
        state: 'success', action_type: 'search.web', action_id: 'act_1',
        verified: true, error: '',
        verification: { verified: true, method: 'observed_state', simulated: true, detail: 'x'.repeat(500) },
        result: { state: 'ok' },
        evidence: { secret: 'must not cross' },
      },
    },
  };
  const views = wb.nodeViews(result);
  assert.equal(views.length, 1);
  assert.equal(views[0].verification.detail.length, 200);
  assert.equal(views[0].evidence, undefined);
  assert.deepEqual(views[0].result, { state: 'ok' });
});

test('source hygiene: no fetch/XMLHttpRequest/innerHTML and no grants key', () => {
  const src = fs.readFileSync(path.join(__dirname, '..', 'ui', 'workbench.js'), 'utf8');
  // BUILDER comments may mention the word; assignments are what would execute.
  assert.equal(src.includes('.innerHTML'), false, 'must never assign innerHTML');
  assert.equal(src.includes('XMLHttpRequest'), false);
  // fetch( as a call (not prose mentioning fetch).
  assert.equal(/(^|[^A-Za-z])fetch\s*\(/.test(src), false, 'must use the bridge, not fetch');
  // The /execute payload is built in runExecution — it must never carry grants.
  const executeBody = src.slice(src.indexOf('async function runExecution'));
  const runSlice = executeBody.slice(0, executeBody.indexOf('async function cancelExecution'));
  assert.equal(runSlice.includes('grants'), false, 'never send grants (server rejects; invariant 1)');
  assert.equal(runSlice.includes('risk_level'), false, 'never send risk (engine lifts, never lowers)');
});

test('EXPECTED_IDS documents the markup the Lead must add', () => {
  const ids = wb.EXPECTED_IDS;
  for (const required of ['wbAsk', 'wbAskText', 'wbAskSend', 'wbPlanList', 'wbExecute', 'wbResult', 'wbApprovals', 'wbHistoryList']) {
    assert.ok(ids.includes(required), `missing ${required}`);
  }
});
