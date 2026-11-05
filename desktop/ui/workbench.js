'use strict';
/**
 * Action Execution Workbench (Phase 6 Agent 2).
 *
 * The user-facing workflow: enter instruction -> see plan -> approve if needed
 * ->
 * execute -> see actual result/verification -> open history.
 *
 * This module is a thin client over the REAL local engine through the only
 * transport the page is allowed to use: `window.actionmesh.request(...)`
 * (preload bridge; CSP `connect-src 'none'` means no fetch/socket). It never
 * invents state, never auto-approves, and never reads content beyond what the
 * backend's result boundary already projected.
 *
 * Backend contracts used (verified in actionmesh/api.py, not invented):
 *   POST /plan        {intent} -> {intent, actions:[{type,parameters}], notes}
 *                     Proposal only. Nothing is authorized here. The plan
 *                     carries NO risk/decision/requires_approval: the client
 *                     looks up risk+permissions from GET /capabilities and
 *                     says plainly that the policy decision is "decided at
 *                     execute" (the engine evaluates it per node).
 *   GET /capabilities -> [{action, provider, permissions, risk_level}]
 *                     Registry facts. Risk shown here is the registry's risk;
 *                     a client-supplied risk can only be lifted outward, never
 *                     lowered, so the workbench sends NO risk with /execute.
 *   POST /execute     {graph, actor, policy} -> ExecutionResult.to_dict()
 *                     Graph is built from plan actions: linear depends_on
 *                     chain n0 -> n1 ... -> end (mirrors the backend's own
 *                     graph_from_actions). NO `grants` key is ever sent: the
 *                     server rejects it with 400 ("permission grants are
 *                     server-side; use /consents"). Consent comes from the
 *                     server-side store (POST /consents); policy rules name
 *                     the action + allow and echo the registry permissions
 *                     for that action (the engine matches both).
 *   GET /approvals    -> [{execution_id, actor, actions:[{action_id,
 *                         action_type, parameters, risk_level}]}]
 *                     CRITICAL (and HIGH without opt-in) parks the execution
 *                     in waiting_permission. action_id is server-minted.
 *   POST /approvals   {action_id, status: approved|rejected, action_type,
 *                         summary, execution_id} -> {status, expires_at (10-min
 *                         TTL when approved), execution (resumed result)}
 *                     Approval is per-action_id, never auto-made here.
 *   GET /executions/{id} -> ExecutionRecord (durable; works after reload)
 *   GET /executions?limit=N -> ExecutionRecord[] (history)
 *   POST /executions/{id}/cancel -> {execution_id, status}
 *
 * Invariants (AGENTS.md § Security — never weakened here):
 *  - Fail closed: no actor id -> no submit. Empty plan -> no execute.
 *  - CRITICAL is never auto-approved; waiting_permission renders as
 *    "Waiting for approval", never as success; verified stays visible.
 *  - Unverified / REQUIRES_USER is never reported as success.
 *  - safe_result_view allowlist: only node.result / verification fields the
 *    backend projected are rendered; raw content is never read.
 *  - Duplicate submissions are blocked while a plan/execute is in flight.
 *  - Every visible control works or is disabled with an explanation.
 *
 * DOM ids expected (Lead: add this markup to index.html, then load
 * workbench.js BEFORE app.js and call window.ActionMeshWorkbench.initWorkbench()
 * from app.js boot or from the markup's own init):
 *   form     #wbAsk           (wraps the input + submit; submit is intercepted)
 *   input    #wbAskText       (instruction text)
 *   button   #wbAskSend       (submit)
 *   p        #wbStatus        (aria-live polite status line)
 *   section  #wbPlan          (plan host; hidden until a plan exists)
 *   ol       #wbPlanList      (per-step rows)
 *   p        #wbPlanNotes     (planner notes, e.g. dropped clauses)
 *   button   #wbExecute       (Execute plan) — disabled until a plan exists
 *   button   #wbCancel        (Cancel run) — enabled only while executing/waiting
 *   div      #wbResult        (result host; hidden until an execute returns)
 *   ul       #wbHistoryList   (history rows)
 *   div      #wbHistoryDetail (selected execution detail)
 *   button   #wbHistoryRefresh(refresh history)
 *   p        #wbError         (error/detail line; empty means none)
 *   div      #wbApprovals     (pending approval card host)
 *
 * The module tolerates the hosts being absent: every render no-ops when its
 * host is missing, so page boot never throws before the Lead wires the markup.
 *
 * Pure helpers are exported for Node tests (desktop/test conventions: node:test,
 * no jsdom). In the browser they attach to window.ActionMeshWorkbench.
 *
 * Wrapped in an IIFE because index.html loads this file as a classic script,
 * and classic scripts share one global lexical scope: a top-level `const`
 * here that another script also declares is a SyntaxError that stops the
 * later script from loading at all. This module already collided with app.js
 * (`api`, `GET`, `POST`, `el`, `replace`, `isTerminal`, `TERMINAL`, …).
 * Everything the page needs is reached through window.ActionMeshWorkbench.
 */
(function registerWorkbench() {
  'use strict';

// ---------------------------------------------------------------------------
// Pure helpers (no DOM, no bridge) — unit-tested directly.
// ---------------------------------------------------------------------------

/** Node order is stable and total: n0, n1, ..., end. */
function planToGraph(plan) {
  const actions = (plan && Array.isArray(plan.actions)) ? plan.actions : [];
  const nodes = [];
  let prev = [];
  actions.forEach((action, index) => {
    const nodeId = `n${index}`;
    nodes.push({
      node_id: nodeId,
      node_type: 'action',
      action: {
        type: String(action.type || ''),
        parameters: (action && action.parameters && typeof action.parameters === 'object')
          ? { ...action.parameters } : {},
      },
      depends_on: prev,
    });
    prev = [nodeId];
  });
  nodes.push({ node_id: 'end', node_type: 'end', depends_on: prev });
  return { intent: String((plan && plan.intent) || ''), nodes };
}

/** True when the plan carries at least one executable action. */
function planExecutable(plan) {
  return Boolean(plan && Array.isArray(plan.actions) && plan.actions.length > 0);
}

/**
 * The /execute policy payload. One allow-rule per planned action, naming the
 * action and echoing its registry permissions so the engine can match them
 * against server-side grants. This authorizes nothing by itself (the engine
 * still checks consent + risk); it states what the operator is willing to
 * consider, the same precedent app.js follows for screen actions.
 */
function buildPolicy(plan, capabilities) {
  const byAction = new Map();
  for (const cap of (capabilities || [])) {
    if (cap && typeof cap.action === 'string') byAction.set(cap.action, cap);
  }
  const rules = (plan && Array.isArray(plan.actions) ? plan.actions : []).map((action) => {
    const cap = byAction.get(action.type);
    return {
      action: String(action.type || ''),
      decision: 'allow',
      permissions: cap ? [...(cap.permissions || [])] : [],
    };
  });
  return { rules, default_decision: 'deny' };
}

/**
 * Per-step view rows for the plan, joined with /capabilities facts.
 * `decision` is honestly 'not decided — decided at execute' for every step:
 * /plan does not carry it and the workbench must not invent it.
 */
function planSteps(plan, capabilities) {
  const byAction = new Map();
  for (const cap of (capabilities || [])) {
    if (cap && typeof cap.action === 'string') byAction.set(cap.action, cap);
  }
  return (plan && Array.isArray(plan.actions) ? plan.actions : []).map((action, index) => {
    const cap = byAction.get(action.type);
    const risk = cap ? String(cap.risk_level || 'medium') : 'unknown';
    const permissions = cap ? [...(cap.permissions || [])] : [];
    return {
      index,
      type: String(action.type || ''),
      parameters: (action && action.parameters && typeof action.parameters === 'object')
        ? { ...action.parameters } : {},
      risk,
      permissions,
      requiresApproval: risk === 'critical' || risk === 'high',
      decision: 'not decided — decided at execute',
      known: Boolean(cap),
    };
  });
}

/**
 * Reuse the backend's own label table semantics (mirrors history.js, kept
 * local so this module has no load-order dependency on it): unverified
 * success is never "verified".
 */
const TERMINAL = new Set(['success', 'failed', 'cancelled', 'denied', 'timeout', 'partially_completed', 'interrupted']);
function isTerminal(state) { return TERMINAL.has(String(state || '')); }

function isVerifiedSuccess(result) {
  // ExecutionResult.to_dict(): {state, nodes:{id: view}}.
  // ExecutionRecord: {state, results:{id: view}}.
  if (!result || String(result.state) !== 'success') return false;
  const views = result.nodes || result.results || {};
  const list = Object.values(views).filter((v) => v && typeof v === 'object');
  if (!list.length) return false;
  const withVerification = list.filter((v) => v.verification && typeof v.verification === 'object');
  if (!withVerification.length) return false;
  return withVerification.every((v) => v.verification.verified === true)
    && withVerification.some((v) => v.verification.verified === true);
}

function resultLabel(result) {
  const state = String((result && result.state) || 'unknown');
  if (state === 'success' && !isVerifiedSuccess(result)) return 'Succeeded (unverified)';
  if (state === 'waiting_permission' || state === 'pending' || state === 'awaiting_approval') return 'Waiting for approval';
  if (state === 'requires_user') return 'Unverified — needs you';
  if (state === 'success') return 'Succeeded';
  if (state === 'failed') return 'Failed';
  if (state === 'denied') return 'Denied';
  if (state === 'cancelled') return 'Cancelled';
  if (state === 'timeout') return 'Timed out';
  if (state === 'interrupted') return 'Interrupted';
  if (state === 'partially_completed') return 'Partially completed';
  return state.replace(/_/g, ' ');
}

function verificationLabel(result) {
  const views = (result && (result.nodes || result.results)) || {};
  const list = Object.values(views).filter((v) => v && v.verification && typeof v.verification === 'object');
  if (!list.length) return 'not recorded';
  if (list.every((v) => v.verification.verified === true)) return 'verified';
  if (list.some((v) => v.verification.verified === true)) return 'partially verified';
  return 'not verified';
}

/** Node rows for a result, in stable node order, projected fields only. */
function nodeViews(result) {
  const views = (result && (result.nodes || result.results)) || {};
  return Object.keys(views).sort().map((nodeId) => {
    const v = views[nodeId] || {};
    const verification = (v.verification && typeof v.verification === 'object') ? v.verification : null;
    return {
      nodeId,
      state: String(v.state || 'unknown'),
      actionType: String(v.action_type || ''),
      actionId: String(v.action_id || ''),
      verified: v.verified === true ? true : (v.verified === false ? false : null),
      verification: verification ? {
        verified: verification.verified === true,
        method: String(verification.method || ''),
        simulated: verification.simulated !== false,
        detail: typeof verification.detail === 'string' ? verification.detail.slice(0, 200) : '',
      } : null,
      error: typeof v.error === 'string' ? v.error : '',
      result: (v.result && typeof v.result === 'object') ? { ...v.result } : {},
    };
  });
}

// ---------------------------------------------------------------------------
// DOM helpers (createElement + textContent only; never innerHTML).
// ---------------------------------------------------------------------------

function el(tag, attrs, children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value === undefined || value === null || value === false) continue;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key === 'dataset') Object.assign(node.dataset, value);
      else node.setAttribute(key, value === true ? '' : String(value));
    }
  }
  for (const child of [].concat(children || [])) {
    if (child === null || child === undefined || child === false) continue;
    // `Node` may be undefined in Node tests; fall back to instanceof check via tagName when present.
    if (typeof Node !== 'undefined' && child instanceof Node) node.append(child);
    else if (child && typeof child === 'object' && child.nodeType) node.append(child);
    else if (child && typeof child === 'object' && typeof child.append === 'function') node.append(child);
    else node.append(document.createTextNode(String(child)));
  }
  return node;
}

function replace(node, children) {
  if (!node) return;
  while (node.firstChild) node.removeChild(node.firstChild);
  for (const child of [].concat(children || [])) {
    if (child === null || child === undefined || child === false) continue;
    if (typeof Node !== 'undefined' && child instanceof Node) node.append(child);
    else if (child && typeof child === 'object' && child.nodeType) node.append(child);
    else if (child && typeof child === 'object' && typeof child.append === 'function') node.append(child);
    else node.append(document.createTextNode(String(child)));
  }
}

function byId(id) {
  return (typeof document !== 'undefined') ? document.getElementById(id) : null;
}

function stateChip(state) {
  const label = String(state || 'unknown').replace(/_/g, ' ');
  return el('span', { class: 'state', dataset: { s: String(state || 'unknown') }, text: label });
}

function riskChip(risk) {
  return el('span', { class: 'risk', dataset: { r: String(risk || 'medium') }, text: String(risk || 'medium') });
}

function setStatus(message) {
  const host = byId('wbStatus');
  if (host) host.textContent = String(message || '');
}

function setError(message) {
  const host = byId('wbError');
  if (!host) return;
  host.textContent = String(message || '');
  host.hidden = !message;
}

// ---------------------------------------------------------------------------
// Transport (the only one). Mirrors app.js: bridge.request, ApiError on !ok.
// ---------------------------------------------------------------------------

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status || 0;
  }
}

function bridge() {
  return (typeof window !== 'undefined') ? window.actionmesh : null;
}

async function api(method, path, body, opts) {
  const b = bridge();
  if (!b || typeof b.request !== 'function') throw new ApiError('backend bridge unavailable', 0);
  const result = await b.request({
    method,
    path,
    body: body === undefined ? null : body,
    long: Boolean(opts && opts.long),
  });
  if (!result || !result.ok) {
    const detail = (result && result.body && result.body.detail)
      || (result && result.error)
      || (result && result.text)
      || `request failed (${result ? result.status : 'no answer'})`;
    throw new ApiError(String(detail), result ? result.status : 0);
  }
  return result.body;
}

const GET = (path) => api('GET', path);
const POST = (path, body, opts) => api('POST', path, body === undefined ? {} : body, opts);

// The actor is the local operator principal the page already uses. Sending NO
// actor would make the request actor-less and the engine fails closed (denied);
// sending a `grants` key would be rejected. The server is the authority.
const ACTOR_ID = 'voiceos-local';

// ---------------------------------------------------------------------------
// Workbench state (renderer-only; the backend is the record).
// ---------------------------------------------------------------------------

const workbench = {
  plan: null,
  capabilities: null,
  result: null,
  executionId: '',
  awaitingActionId: '',
  running: false,
  planning: false,
  history: [],
  selectedExecutionId: '',
};

// ---------------------------------------------------------------------------
// Plan rendering (BEFORE execution: risk + permissions from /capabilities,
// decision honestly "not decided — decided at execute").
// ---------------------------------------------------------------------------

function renderPlan() {
  const host = byId('wbPlan');
  const listHost = byId('wbPlanList');
  const notesHost = byId('wbPlanNotes');
  const executeBtn = byId('wbExecute');
  if (!host || !listHost) return;
  if (!workbench.plan) {
    host.hidden = true;
    if (executeBtn) executeBtn.disabled = true;
    if (executeBtn) executeBtn.setAttribute('aria-disabled', 'true');
    return;
  }
  const steps = planSteps(workbench.plan, workbench.capabilities);
  const rows = steps.map((step) => {
    const sub = step.known
      ? `risk ${step.risk} · permissions ${step.permissions.length ? step.permissions.join(', ') : 'none beyond the actor'} · decision ${step.decision}${step.requiresApproval ? ' · this step will ask for approval (CRITICAL/HIGH)' : ''}`
      : `unknown action — the registry has no entry for it; the engine will refuse it rather than run it unlabelled`;
    const paramsText = Object.keys(step.parameters || {}).length
      ? JSON.stringify(step.parameters, null, 2) : '(no parameters)';
    return el('li', { class: 'row', dataset: { step: String(step.index) } }, [
      el('div', { class: 'row-main' }, [
        el('div', { class: 'row-title mono', text: `${step.index + 1}. ${step.type}` }),
        el('div', { class: 'row-sub', text: sub }),
        el('pre', { class: 'mono', text: paramsText }),
      ]),
      riskChip(step.known ? step.risk : 'unknown'),
    ]);
  });
  replace(listHost, rows.length ? rows : [el('li', { class: 'empty', text: 'The planner proposed no executable actions.' })]);
  if (notesHost) {
    const notes = Array.isArray(workbench.plan.notes) ? workbench.plan.notes : [];
    replace(notesHost, notes.map((note) => el('p', { class: 'fine', text: String(note) })));
  }
  host.hidden = false;
  const canRun = planExecutable(workbench.plan) && !workbench.running && !workbench.planning;
  if (executeBtn) {
    executeBtn.disabled = !canRun;
    if (canRun) executeBtn.removeAttribute('aria-disabled');
    else executeBtn.setAttribute('aria-disabled', 'true');
    executeBtn.textContent = workbench.running ? 'Running…' : 'Execute plan';
    executeBtn.title = planExecutable(workbench.plan)
      ? 'Send this plan to the policy engine and executor.'
      : 'Disabled: the planner proposed nothing to run. Try a plainer phrasing.';
  }
}

function showPlan(plan) {
  workbench.plan = plan || null;
  workbench.result = null;
  workbench.executionId = '';
  workbench.awaitingActionId = '';
  renderPlan();
  renderResult();
  renderApprovals();
  setStatus(plan && planExecutable(plan) ? 'Plan ready. Review it, then press Execute plan.' : 'No plan to run.');
}

// ---------------------------------------------------------------------------
// Result rendering (truthful: verified stays visible; unverified is never
// success; only safe_result_view fields are shown).
// ---------------------------------------------------------------------------

// A host div lives inside a card that is hidden until there is something to
// show; an empty card would otherwise sit on the page claiming nothing.
function showCardFor(host, visible) {
  if (!host || typeof host.closest !== 'function') return;
  const card = host.closest('.card');
  if (card) card.hidden = !visible;
}

function renderResult() {
  const host = byId('wbResult');
  if (!host) return;
  const result = workbench.result;
  if (!result) {
    host.hidden = true;
    showCardFor(host, false);
    return;
  }
  const label = resultLabel(result);
  const parts = [
    el('div', { class: 'detail-head' }, [
      el('div', {}, [
        el('div', { class: 'detail-title', text: result.execution_id || 'no execution id' }),
        el('div', { class: 'detail-meta', text: `device ${result.device_id || 'this laptop'} · verification ${verificationLabel(result)}${result.simulated === true ? ' · simulated (mock provider)' : ''}` }),
      ]),
      stateChip(result.state || 'unknown'),
    ]),
    el('dl', { class: 'kv' }, [
      el('dt', { text: 'State' }), el('dd', { text: label }),
      el('dt', { text: 'Verification' }), el('dd', { text: verificationLabel(result) }),
      el('dt', { text: 'Error' }), el('dd', { text: String(result.error || '—') }),
    ]),
    el('ol', { class: 'rows' }, nodeViews(result).map((view) => {
      const verifyText = view.verification
        ? `${view.verification.verified ? 'verified' : 'not verified'} (${view.verification.method}${view.verification.simulated ? ', simulated' : ''})${view.verification.detail ? ` — ${view.verification.detail}` : ''}`
        : (view.verified === null ? 'not recorded' : (view.verified ? 'verified' : 'not verified'));
      const resultText = Object.keys(view.result || {}).length
        ? JSON.stringify(view.result) : '(no projected result)';
      return el('li', { class: 'row' }, [
        el('div', { class: 'row-main' }, [
          el('div', { class: 'row-title mono', text: `${view.nodeId}${view.actionType ? ` · ${view.actionType}` : ''}${view.actionId ? ` · ${view.actionId}` : ''}` }),
          el('div', { class: 'row-sub', text: `${String(view.state).replace(/_/g, ' ')} · ${verifyText}` }),
          view.error ? el('div', { class: 'row-sub', text: view.error }) : null,
          el('pre', { class: 'mono', text: resultText }),
        ]),
        stateChip(view.state),
      ]);
    })),
  ];
  if (String(result.state || '') === 'waiting_permission' || String(result.state || '') === 'pending') {
    parts.push(el('div', { class: 'notice', text: 'This run is parked for your decision. It has not executed the waiting step. Use the approval below to approve or reject it — nothing runs until you do.' }));
  }
  if (String(result.state || '') === 'requires_user') {
    parts.push(el('div', { class: 'notice', text: 'Unverified — needs you. This is not a success.' }));
  }
  if (String(result.state || '') === 'denied') {
    parts.push(el('div', { class: 'notice bad', text: 'The policy engine refused this run. The reason above is the one the engine gave.' }));
  }
  replace(host, parts);
  host.hidden = false;
  showCardFor(host, true);
  refreshCancel();
}

function refreshCancel() {
  const btn = byId('wbCancel');
  if (!btn) return;
  const state = workbench.result && workbench.result.state;
  const active = Boolean(workbench.executionId) && !isTerminal(String(state || ''));
  btn.disabled = !active;
  if (active) btn.removeAttribute('aria-disabled');
  else btn.setAttribute('aria-disabled', 'true');
  btn.title = active
    ? 'Ask the executor to stop this run.'
    : 'Disabled: there is no active run to cancel.';
}

// ---------------------------------------------------------------------------
// Pending approvals (CRITICAL/HIGH) — approve/reject wired to POST /approvals.
// ---------------------------------------------------------------------------

function renderApprovals() {
  const host = byId('wbApprovals');
  if (!host) return;
  const entries = Array.isArray(workbench.pendingApprovals) ? workbench.pendingApprovals : [];
  if (!entries.length) {
    replace(host, []);
    showCardFor(host, false);
    return;
  }
  replace(host, entries.map((entry) => {
    const actions = (entry.actions || []).map((action) => {
      const block = el('div', { class: 'action-block' }, [
        el('div', { class: 'approval-head' }, [
          el('span', { class: 'approval-title', text: action.action_type || 'action' }),
          riskChip(action.risk_level),
          el('span', { class: 'fine', text: `action ${action.action_id || ''}` }),
        ]),
        el('pre', { class: 'mono', text: JSON.stringify(action.parameters || {}, null, 2) }),
      ]);
      const row = el('div', { class: 'btn-row' });
      const approve = el('button', { class: 'btn primary', type: 'button', text: 'Approve this action' });
      approve.addEventListener('click', () => decideApproval(entry, action, 'approved', approve));
      const reject = el('button', { class: 'btn danger ghost', type: 'button', text: 'Reject' });
      reject.addEventListener('click', () => decideApproval(entry, action, 'rejected', reject));
      row.append(approve, reject);
      block.append(row);
      return block;
    });
    return el('div', { class: 'approval' }, [
      el('div', { class: 'approval-head' }, [
        el('span', { class: 'approval-title', text: 'Waiting for your approval' }),
        el('span', { class: 'chip', text: entry.execution_id || '' }),
      ]),
      el('p', { class: 'fine', text: 'Approving authorizes only this named action. Consent is not an approval, and an approval is not a standing permission.' }),
      ...actions,
    ]);
  }));
  showCardFor(host, true);
}

async function refreshPendingApprovals() {
  try {
    workbench.pendingApprovals = await GET('/approvals');
  } catch (error) {
    workbench.pendingApprovals = [];
    setError(`Could not read pending approvals: ${error.message}`);
  }
  renderApprovals();
}

async function decideApproval(entry, action, status, button) {
  if (button) button.disabled = true;
  setError('');
  try {
    const answer = await POST('/approvals', {
      action_id: action.action_id,
      status,
      action_type: action.action_type,
      summary: status === 'approved'
        ? 'approved from the workbench'
        : 'rejected from the workbench',
      execution_id: entry.execution_id,
    });
    if (answer && answer.execution) {
      workbench.result = answer.execution;
      workbench.executionId = answer.execution.execution_id || workbench.executionId;
      workbench.awaitingActionId = '';
      renderResult();
      renderPlan();
    }
    setStatus(status === 'approved'
      ? `Approved. Execution state: ${resultLabel(workbench.result || {})}.`
      : 'Rejected. The run will not proceed past that action.');
    await refreshPendingApprovals();
    await refreshHistory();
  } catch (error) {
    setError(`The decision was not recorded: ${error.message}`);
    if (button) button.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// History (durable /executions — survives reload; detail via GET /executions/{id}).
// ---------------------------------------------------------------------------

function renderHistory() {
  const listHost = byId('wbHistoryList');
  if (!listHost) return;
  const records = Array.isArray(workbench.history) ? workbench.history : [];
  if (!records.length) {
    replace(listHost, [el('li', { class: 'empty', text: 'No executions yet. What you run here will appear.' })]);
    return;
  }
  replace(listHost, records.map((record) => {
    const row = el('li', { class: 'row', dataset: { id: record.execution_id || '' } }, [
      el('div', { class: 'row-main' }, [
        el('div', { class: 'row-title mono', text: record.execution_id || '(no id)' }),
        el('div', { class: 'row-sub', text: `${resultLabel(record)} · actor ${record.actor_id || '—'}` }),
      ]),
      stateChip(record.state || 'unknown'),
    ]);
    row.setAttribute('tabindex', '0');
    row.setAttribute('role', 'button');
    row.setAttribute('aria-label', `Execution ${record.execution_id || ''}, ${resultLabel(record)}`);
    const pick = () => { openExecution(record.execution_id); };
    row.addEventListener('click', pick);
    row.addEventListener('keydown', (event) => {
      if (event && (event.key === 'Enter' || event.key === ' ')) {
        if (typeof event.preventDefault === 'function') event.preventDefault();
        pick();
      }
    });
    return row;
  }));
}

async function openExecution(executionId) {
  if (!executionId) return;
  workbench.selectedExecutionId = executionId;
  const detailHost = byId('wbHistoryDetail');
  if (!detailHost) return;
  try {
    const record = await GET(`/executions/${encodeURIComponent(executionId)}`);
    replace(detailHost, [
      el('dl', { class: 'kv' }, [
        el('dt', { text: 'Execution' }), el('dd', { text: record.execution_id || '' }),
        el('dt', { text: 'State' }), el('dd', { text: resultLabel(record) }),
        el('dt', { text: 'Verification' }), el('dd', { text: verificationLabel(record) }),
        el('dt', { text: 'Error' }), el('dd', { text: String(record.error || '—') }),
      ]),
      el('ol', { class: 'rows' }, nodeViews(record).map((view) => el('li', { class: 'row' }, [
        el('div', { class: 'row-main' }, [
          el('div', { class: 'row-title mono', text: `${view.nodeId}${view.actionType ? ` · ${view.actionType}` : ''}` }),
          el('div', { class: 'row-sub', text: `${String(view.state).replace(/_/g, ' ')} · ${view.verification ? (view.verification.verified ? 'verified' : 'not verified') : 'not recorded'}` }),
        ]),
      ]))),
    ]);
  } catch (error) {
    replace(detailHost, [el('div', { class: 'notice bad', text: `Could not open that execution: ${error.message}` })]);
  }
}

async function refreshHistory() {
  try {
    workbench.history = await GET('/executions?limit=25');
  } catch (error) {
    setError(`Could not read history: ${error.message}`);
    workbench.history = [];
  }
  renderHistory();
}

// ---------------------------------------------------------------------------
// Flows: submitInstruction (plan) -> runExecution (execute) -> approve -> result.
// ---------------------------------------------------------------------------

async function submitInstruction(instruction) {
  const text = String(instruction || '').trim();
  if (!text) {
    setError('Type what you would like the workbench to do first.');
    setStatus('Nothing was sent.');
    return null;
  }
  if (workbench.planning || workbench.running) {
    setStatus('Still working on the previous request — wait for it to finish.');
    return null;
  }
  workbench.planning = true;
  workbench.plan = null;
  workbench.result = null;
  workbench.executionId = '';
  workbench.awaitingActionId = '';
  setError('');
  setStatus('Planning…');
  renderPlan();
  renderResult();
  renderApprovals();
  const sendBtn = byId('wbAskSend');
  if (sendBtn) sendBtn.disabled = true;
  try {
    // Capabilities first (risk/permissions for the plan view); a failure here
    // does not block planning but makes the plan view honest about it.
    try {
      workbench.capabilities = await GET('/capabilities');
    } catch (error) {
      workbench.capabilities = null;
      setError(`Could not read capabilities: ${error.message}. Risk labels may be unknown.`);
    }
    const plan = await POST('/plan', { intent: text });
    showPlan(plan);
    if (!planExecutable(plan)) {
      const notes = Array.isArray(plan.notes) && plan.notes.length
        ? plan.notes.join(' · ') : 'the planner proposed no executable actions';
      setError(`Nothing to run: ${notes}.`);
    }
    return plan;
  } catch (error) {
    setError(`Planning failed: ${error.message}`);
    setStatus('Planning failed. Nothing was executed.');
    return null;
  } finally {
    workbench.planning = false;
    if (sendBtn) sendBtn.disabled = false;
    renderPlan();
  }
}

async function runExecution() {
  if (workbench.running) {
    setStatus('Already running — duplicate submissions are ignored.');
    return null;
  }
  if (!planExecutable(workbench.plan)) {
    setError('There is no executable plan to run. Submit an instruction first.');
    return null;
  }
  workbench.running = true;
  setError('');
  setStatus('Running…');
  renderPlan();
  const executeBtn = byId('wbExecute');
  if (executeBtn) executeBtn.disabled = true;
  try {
    const graph = planToGraph(workbench.plan);
    const result = await POST('/execute', {
      graph,
      actor: { id: ACTOR_ID },
      policy: buildPolicy(workbench.plan, workbench.capabilities),
    }, { long: true });
    workbench.result = result;
    workbench.executionId = result.execution_id || '';
    const state = String(result.state || '');
    if (state === 'waiting_permission' || state === 'pending') {
      const first = nodeViews(result).find((v) => v.state === 'waiting_permission' && v.actionId);
      workbench.awaitingActionId = first ? first.actionId : '';
      setStatus('Waiting for your approval. The step has not run yet.');
      await refreshPendingApprovals();
    } else if (state === 'denied') {
      setStatus('The policy engine denied this run.');
    } else if (state === 'success') {
      setStatus(isVerifiedSuccess(result)
        ? 'Succeeded and verified.'
        : 'Succeeded (unverified) — verification did not confirm it.');
    } else {
      setStatus(`Finished with state: ${resultLabel(result)}.`);
    }
    renderResult();
    renderPlan();
    await refreshHistory();
    return result;
  } catch (error) {
    setError(`Execute failed: ${error.message}`);
    setStatus('Execute failed. Nothing is running.');
    return null;
  } finally {
    workbench.running = false;
    renderPlan();
    refreshCancel();
  }
}

async function cancelExecution() {
  if (!workbench.executionId) return null;
  const btn = byId('wbCancel');
  if (btn) btn.disabled = true;
  try {
    const answer = await POST(`/executions/${encodeURIComponent(workbench.executionId)}/cancel`);
    setStatus(`Cancel: ${answer.status || 'requested'}. The executor has not confirmed it stopped.`);
    try {
      const record = await GET(`/executions/${encodeURIComponent(workbench.executionId)}`);
      workbench.result = record;
      renderResult();
    } catch (_ignored) { /* keep the cancel answer; the record will refresh below */ }
    await refreshHistory();
    return answer;
  } catch (error) {
    setError(`Cancel failed: ${error.message}`);
    return null;
  } finally {
    refreshCancel();
  }
}

// ---------------------------------------------------------------------------
// Wiring.
// ---------------------------------------------------------------------------

function initWorkbench() {
  const doc = (typeof document !== 'undefined') ? document : null;
  if (!doc) return false;
  const form = byId('wbAsk');
  const text = byId('wbAskText');
  const send = byId('wbAskSend');
  if (!form || !text || !send) return false; // lead has not wired the markup; stay silent

  // Live-region + status affordances the harness/browser read back.
  const status = byId('wbStatus');
  if (status) {
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    status.setAttribute('aria-atomic', 'true');
  }
  const err = byId('wbError');
  if (err) {
    err.setAttribute('role', 'alert');
    err.hidden = true;
  }
  const planHost = byId('wbPlan');
  if (planHost) planHost.hidden = true;
  const resultHost = byId('wbResult');
  if (resultHost) resultHost.hidden = true;
  const executeBtn = byId('wbExecute');
  if (executeBtn) {
    executeBtn.disabled = true;
    executeBtn.setAttribute('aria-disabled', 'true');
    executeBtn.title = 'Disabled: submit an instruction first.';
    executeBtn.addEventListener('click', () => { void runExecution(); });
  }
  const cancelBtn = byId('wbCancel');
  if (cancelBtn) {
    cancelBtn.disabled = true;
    cancelBtn.setAttribute('aria-disabled', 'true');
    cancelBtn.title = 'Disabled: there is no active run to cancel.';
    cancelBtn.addEventListener('click', () => { void cancelExecution(); });
  }
  const refreshBtn = byId('wbHistoryRefresh');
  if (refreshBtn) refreshBtn.addEventListener('click', () => { void refreshHistory(); });

  const onSubmit = (event) => {
    if (event && typeof event.preventDefault === 'function') event.preventDefault();
    void submitInstruction(text.value);
  };
  form.addEventListener('submit', onSubmit);
  send.addEventListener('click', onSubmit);

  // Initial data; failures land in the error line, never as a throw.
  void (async () => {
    try { workbench.capabilities = await GET('/capabilities'); } catch (_ignored) { workbench.capabilities = null; }
    await refreshHistory();
    await refreshPendingApprovals();
    renderPlan();
  })();
  return true;
}

const api2 = {
  planToGraph, planExecutable, buildPolicy, planSteps,
  isTerminal, isVerifiedSuccess, resultLabel, verificationLabel, nodeViews,
  showPlan, renderPlan, renderResult, renderApprovals,
  submitInstruction, runExecution, cancelExecution,
  refreshHistory, refreshPendingApprovals, decideApproval, openExecution,
  initWorkbench, workbench, EXPECTED_IDS: [
    'wbAsk', 'wbAskText', 'wbAskSend', 'wbStatus', 'wbPlan', 'wbPlanList',
    'wbPlanNotes', 'wbExecute', 'wbCancel', 'wbResult', 'wbHistoryList',
    'wbHistoryDetail', 'wbHistoryRefresh', 'wbError', 'wbApprovals',
  ],
};

if (typeof module !== 'undefined' && module.exports) {
  module.exports = api2;
}
if (typeof window !== 'undefined') {
  window.ActionMeshWorkbench = api2;
}
})();
