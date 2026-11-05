'use strict';
/**
 * activity.js — Phase 6 Agent 1: Dashboard + Activity (HOME view)
 *
 * Real system overview backed by existing backend routes only. No hardcoded
 * connected/healthy/success, no second audit/journal store.
 *
 * Expects these DOM ids (present in desktop/ui/index.html):
 *   homeDevices / homeDevicesNote, homeBackend / homeBackendNote,
 *   homeApprovals, homeAudit / homeAuditNote,
 *   homeExecutions, homeSessions, homeGreeting
 *
 * Backend routes used (verified in actionmesh/api.py — /dashboard is HTML
 * and is deliberately NOT used for stats):
 *   GET /ready, GET /devices, GET /approvals,
 *   GET /executions?limit=8, GET /voiceos/sessions?limit=8,
 *   GET /automations, GET /audit?limit=8, GET /audit/verify
 * Transport: window.actionmesh.request (same bridge as app.js/history.js).
 * The renderer has CSP connect-src 'none', so no fetch/XHR here either.
 *
 * Honesty contract:
 *   - Backend down -> "—" + "backend offline", never fake zeros as healthy.
 *   - Unverified / REQUIRES_USER is never reported as success.
 *   - Only allowlisted fields render (ids, states, timestamps, counts).
 *     execution.output and stdout/stderr/file bodies are never rendered.
 *   - Every quick-action button calls a real API or is disabled with a
 *     title explaining why it cannot run.
 *
 * This module tolerates being loaded before or after history.js/app.js:
 * it reuses window.ActionMeshHistory helpers when present and falls back
 * to local copies. It never overwrites window.ActionMeshHistory.
 *
 * Wrapped in an IIFE because index.html loads this file as a classic script,
 * and classic scripts share one global lexical scope: a top-level `const`
 * here that another script also declares is a SyntaxError that stops the
 * *later* script from loading at all. `TERMINAL_STATES` was already declared
 * by history.js. Everything the page needs is on window.ActionMeshActivity.
 */
(function registerActivity() {
  'use strict';

const TERMINAL_STATES = new Set([
  'success', 'failed', 'denied', 'cancelled', 'timeout',
  'partially_completed', 'interrupted',
]);

// Both spellings seen in the tree: app.js refreshHome uses the former,
// the protocol ActionState uses the latter ('executing', 'waiting_permission'…).
const ACTIVE_STATES = new Set([
  'running', 'pending', 'awaiting_approval',
  'executing', 'waiting_permission', 'requires_user', 'verifying', 'retrying',
]);

const SETTLED_STEP_STATES = new Set([
  'succeeded', 'succeeded_unverified', 'failed', 'denied',
  'cancelled', 'skipped', 'interrupted',
]);

function isTerminalState(state) {
  return TERMINAL_STATES.has(String(state || ''));
}

function isActiveState(state) {
  return ACTIVE_STATES.has(String(state || ''));
}

function countActive(records) {
  const list = Array.isArray(records) ? records : [];
  return list.filter((r) => r && !isTerminalState(r.state) && isActiveState(r.state)).length;
}

function approvalCount(approvals) {
  const list = Array.isArray(approvals) ? approvals : [];
  return list.reduce((sum, entry) => sum + ((entry && entry.actions) || []).length, 0);
}

function onlineCount(devices) {
  const list = Array.isArray(devices) ? devices : [];
  return list.filter((d) => d && d.online).length;
}

function sessionProgress(session) {
  const steps = session && Array.isArray(session.steps) ? session.steps : [];
  const settled = steps.filter((s) => s && SETTLED_STEP_STATES.has(String(s.state || ''))).length;
  return { settled, total: steps.length };
}

function greetingForHour(hour) {
  const h = Number(hour);
  if (h < 12) return 'Good morning';
  if (h < 18) return 'Good afternoon';
  return 'Good evening';
}

function timeAgo(iso, now) {
  if (!iso) return '';
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return '';
  const at = now instanceof Date ? now.getTime() : (now || Date.now());
  let diff = Math.max(0, Math.floor((at - t) / 1000));
  if (diff < 60) return 'just now';
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

function recordLabel(record) {
  const H = globalThis.ActionMeshHistory;
  if (H && typeof H.recordLabel === 'function') return H.recordLabel(record);
  if (!record) return 'Unknown';
  if (record.state === 'success') {
    return isVerifiedSuccess(record) ? 'Succeeded' : 'Succeeded (unverified)';
  }
  if (record.state === 'waiting_permission') return 'Waiting for approval';
  if (record.state === 'requires_user') return 'Unverified — needs you';
  return String(record.state || 'Unknown');
}

function isVerifiedSuccess(record) {
  const H = globalThis.ActionMeshHistory;
  if (H && typeof H.isVerifiedSuccess === 'function') return H.isVerifiedSuccess(record);
  if (!record || record.state !== 'success') return false;
  const results = record.results && typeof record.results === 'object' ? record.results : {};
  const nodes = Object.values(results);
  if (!nodes.length) return false;
  let verified = 0;
  for (const n of nodes) {
    if (n && n.verification && n.verification.verified === true) verified += 1;
  }
  return verified >= 1 && verified === nodes.length;
}

function displayChip(record) {
  const H = globalThis.ActionMeshHistory;
  if (H && typeof H.recordChip === 'function') return H.recordChip(record).chip;
  if (!record) return 'unknown';
  if (record.state === 'success') return isVerifiedSuccess(record) ? 'success' : 'succeeded_unverified';
  return String(record.state || 'unknown');
}

/* ------------------------------- DOM helpers ------------------------------ */

function byId(id) {
  return (typeof document !== 'undefined' && document.getElementById) ? document.getElementById(id) : null;
}

function el(tag, attrs, children) {
  const node = document.createElement(tag);
  const a = attrs || {};
  if (a.class) node.className = a.class;
  if (a.text !== undefined && a.text !== null) node.textContent = String(a.text);
  if (a.title) node.title = a.title;
  if (a.disabled) node.disabled = true;
  if (a.role) node.setAttribute('role', a.role);
  if (a.tabindex !== undefined) node.setAttribute('tabindex', String(a.tabindex));
  if (a['aria-label']) node.setAttribute('aria-label', a['aria-label']);
  if (Array.isArray(children)) children.forEach((c) => { if (c) node.appendChild(c); });
  return node;
}

function replace(host, content) {
  if (!host) return;
  while (host.firstChild) host.removeChild(host.firstChild);
  if (Array.isArray(content)) content.forEach((c) => { if (c) host.appendChild(c); });
  else if (content) host.appendChild(content);
}

function emptyRow(text) {
  return el('li', { class: 'empty', text });
}

/* ------------------------------ Data loading ------------------------------ */

function bridge() {
  return (typeof window !== 'undefined' && window.actionmesh)
    ? window.actionmesh
    : (typeof globalThis !== 'undefined' && globalThis.actionmesh) || null;
}

async function request(path, method, body) {
  const b = bridge();
  if (!b || typeof b.request !== 'function') {
    throw new Error('backend bridge unavailable');
  }
  const result = await b.request({
    method: method || 'GET',
    path,
    body: body === undefined ? null : body,
    long: false,
  });
  if (!result || result.ok !== true) {
    const detail = (result && result.body && result.body.detail)
      || (result && (result.error || result.text)) || `request failed: ${path}`;
    const err = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
    err.status = result && result.status;
    throw err;
  }
  return result.body;
}

async function loadDashboard() {
  const [ready, devices, approvals, executions, sessions, automations, audit, auditVerify] =
    await Promise.allSettled([
      request('/ready'),
      request('/devices'),
      request('/approvals'),
      request('/executions?limit=8'),
      request('/voiceos/sessions?limit=8'),
      request('/automations'),
      request('/audit?limit=8'),
      request('/audit/verify'),
    ]);
  const ok = (r) => r.status === 'fulfilled' ? r.value : undefined;
  const failed = (r) => r.status !== 'fulfilled';
  const offline = failed(ready) && failed(devices) && failed(executions) && failed(sessions);
  return {
    offline,
    ready: ok(ready),
    devices: ok(devices),
    approvals: ok(approvals),
    executions: ok(executions),
    sessions: ok(sessions),
    automations: ok(automations),
    audit: ok(audit),
    auditVerify: ok(auditVerify),
    errors: { ready, devices, approvals, executions, sessions, automations, audit, auditVerify },
  };
}

/* ------------------------------ Rendering --------------------------------- */

function renderExecutionRow(record, onPick) {
  const chip = displayChip(record);
  const when = timeAgo(record.created_at || record.updated_at || record.timestamp);
  const row = el('li', { class: 'row act-row', role: 'button', tabindex: 0, 'aria-label': `Execution ${record.execution_id || ''}` }, [
    el('div', { class: 'row-main' }, [
      el('div', { class: 'row-title', text: record.execution_id || '(no id)' }),
      el('div', { class: 'row-sub', text: `${recordLabel(record)}${when ? ` · ${when}` : ''}` }),
    ]),
    el('span', { class: `chip chip-${chip}`, text: recordLabel(record) }),
  ]);
  if (typeof onPick === 'function') {
    row.addEventListener('click', () => onPick(record.execution_id));
    row.addEventListener('keydown', (e) => {
      if (e && (e.key === 'Enter' || e.key === ' ')) onPick(record.execution_id);
    });
  }
  return row;
}

function renderSessionRow(session, onPick) {
  const { settled, total } = sessionProgress(session);
  const progress = total ? `${settled}/${total} settled` : 'no steps yet';
  const when = timeAgo(session.updated_at || session.created_at);
  const row = el('li', { class: 'row act-row', role: 'button', tabindex: 0, 'aria-label': `Task ${session.session_id || ''}` }, [
    el('div', { class: 'row-main' }, [
      el('div', { class: 'row-title', text: session.request || session.session_id || '(task)' }),
      el('div', { class: 'row-sub', text: `${session.status || 'unknown'} · ${progress}${when ? ` · ${when}` : ''}` }),
    ]),
  ]);
  if (typeof onPick === 'function') {
    row.addEventListener('click', () => onPick(session.session_id));
    row.addEventListener('keydown', (e) => {
      if (e && (e.key === 'Enter' || e.key === ' ')) onPick(session.session_id);
    });
  }
  return row;
}

/**
 * Audit evidence: one row per ledger record, ALLOWLISTED FIELDS ONLY.
 *
 * `record.payload` is deliberately never read. It carries `data.result`, the
 * action's own output, and invariant 4 says content must not cross the result
 * boundary. What this card is for is narrower and honest: showing that the
 * hash-chained ledger recorded the run, and that the chain still verifies.
 */
function renderAuditRow(record) {
  const when = timeAgo(record && record.timestamp);
  const event = String((record && record.result) || 'event');
  const actor = String((record && record.actor) || '—');
  const subject = String((record && record.subject) || '');
  const seq = record && record.seq !== undefined ? `#${record.seq}` : '';
  return el('li', { class: 'row' }, [
    el('div', { class: 'row-main' }, [
      el('div', { class: 'row-title', text: event }),
      el('div', { class: 'row-sub', text: `${actor}${subject ? ` · ${subject}` : ''}${when ? ` · ${when}` : ''}` }),
    ]),
    el('span', { class: 'state', text: seq || 'event' }),
  ]);
}

function renderAuditEvidence(data) {
  const chain = data && data.audit && data.audit.chain;
  const verify = data && data.auditVerify;
  const chip = byId('homeAuditChain');
  const head = byId('homeAuditHead');
  const host = byId('homeAuditLog');
  if (!chip && !head && !host) return; // markup not present in this build

  // An unreadable ledger is never painted as healthy.
  const ok = Boolean(verify && verify.ok === true)
    || Boolean(chain && chain.ok === true && !data.auditVerify);

  if (chip) {
    chip.textContent = ok ? 'chain verified' : (chain || verify ? 'chain broken' : 'unreadable');
    chip.className = ok ? 'chip live' : 'chip warn';
  }

  const count = (chain && typeof chain.records === 'number') ? chain.records
    : (verify && typeof verify.records === 'number' ? verify.records : null);
  const headHash = (data && data.audit && data.audit.head) || (verify && verify.head) || '';
  if (head) {
    head.textContent = count === null
      ? 'The ledger could not be read; nothing is being called healthy here.'
      : `${count} record${count === 1 ? '' : 's'} · head ${String(headHash).slice(0, 16) || '(none)'}`;
  }

  if (!host) return;
  const records = (data && data.audit && Array.isArray(data.audit.records)) ? data.audit.records : [];
  if (!records.length) {
    replace(host, emptyRow(count === 0 ? 'The ledger is empty — nothing has run yet.' : 'No recent audit records.'));
    return;
  }
  // Newest first: the ledger is append-only, so the tail is the most recent.
  replace(host, records.slice().reverse().slice(0, 6).map(renderAuditRow));
}

function renderOffline(hosts) {
  for (const [valueId, noteId] of [['homeDevices', 'homeDevicesNote'], ['homeBackend', 'homeBackendNote'], ['homeApprovals', null], ['homeAudit', 'homeAuditNote']]) {
    const v = byId(valueId);
    if (v) v.textContent = '—';
    const n = noteId ? byId(noteId) : null;
    if (n) n.textContent = 'backend offline';
  }
  const execHost = byId('homeExecutions');
  if (execHost) replace(execHost, emptyRow('Backend offline.'));
  const sessHost = byId('homeSessions');
  if (sessHost) replace(sessHost, emptyRow('Backend offline.'));
  // The audit card must not keep showing a stale "chain verified" while the
  // backend is unreachable — an unread ledger is not a healthy one.
  const auditChip = byId('homeAuditChain');
  if (auditChip) { auditChip.textContent = 'backend offline'; auditChip.className = 'chip warn'; }
  const auditHead = byId('homeAuditHead');
  if (auditHead) auditHead.textContent = 'The ledger cannot be read while the backend is down.';
  const auditLog = byId('homeAuditLog');
  if (auditLog) replace(auditLog, emptyRow('Backend offline.'));
  for (const host of hosts || []) { if (host && host.needsRefresh) host.needsRefresh = true; }
}

function renderDashboard(data, hooks) {
  const h = hooks || {};
  const set = (id, value) => { const n = byId(id); if (n) n.textContent = value; };

  if (!data || data.offline) {
    renderOffline();
    return { ok: false, reason: 'offline' };
  }

  // Devices: prefer /ready online_devices, fall back to /devices online count.
  if (data.ready && typeof data.ready.online_devices === 'number') {
    set('homeDevices', String(data.ready.online_devices));
  } else if (Array.isArray(data.devices)) {
    set('homeDevices', String(onlineCount(data.devices)));
  } else {
    set('homeDevices', '—');
  }
  if (Array.isArray(data.devices)) {
    const online = onlineCount(data.devices);
    set('homeDevicesNote', data.devices.length ? `${online} of ${data.devices.length} online` : 'none enrolled');
  } else {
    set('homeDevicesNote', 'unreadable');
  }

  // Active executions: non-terminal across both state spellings.
  if (Array.isArray(data.executions)) {
    const active = countActive(data.executions);
    set('homeBackend', String(active));
    set('homeBackendNote', active ? 'in progress now' : 'nothing running');
    const execHost = byId('homeExecutions');
    if (execHost) {
      const records = data.executions.slice(0, 6);
      replace(execHost, records.length
        ? records.map((r) => renderExecutionRow(r, h.onPickExecution))
        : emptyRow('No executions yet.'));
    }
  } else {
    set('homeBackend', '—');
    set('homeBackendNote', 'unreadable');
    const execHost = byId('homeExecutions');
    if (execHost) replace(execHost, emptyRow('Unreadable.'));
  }

  // Approvals: pending action count, never zero-as-healthy when unreadable.
  if (Array.isArray(data.approvals)) {
    const count = approvalCount(data.approvals);
    set('homeApprovals', String(count));
    const badge = byId('badge-approvals');
    if (badge) badge.textContent = count ? String(count) : '';
  } else {
    set('homeApprovals', '—');
  }

  // homeAudit stat in index.html is "Saved automations" — keep that mapping.
  if (data.automations && Array.isArray(data.automations.workflows)) {
    const workflows = data.automations.workflows;
    const enabled = workflows.filter((f) => f && f.enabled).length;
    set('homeAudit', String(workflows.length));
    set('homeAuditNote', workflows.length ? `${enabled} enabled` : 'none yet');
  } else if (data.audit && data.audit.chain) {
    // Audit is read-only evidence; never reuse it as a second store.
    set('homeAudit', '—');
    set('homeAuditNote', data.audit.chain.ok ? 'chain ok' : 'chain broken');
  } else {
    set('homeAudit', '—');
    set('homeAuditNote', 'unreadable');
  }

  // Recent tasks: settled/total from steps[] (TaskSession.to_dict shape).
  if (Array.isArray(data.sessions)) {
    const sessHost = byId('homeSessions');
    if (sessHost) {
      const list = data.sessions.slice(0, 6);
      replace(sessHost, list.length
        ? list.map((s) => renderSessionRow(s, h.onPickTask))
        : emptyRow('No recent tasks.'));
    }
  } else {
    const sessHost = byId('homeSessions');
    if (sessHost) replace(sessHost, emptyRow('Unreadable.'));
  }

  const greet = byId('homeGreeting');
  if (greet) greet.textContent = `${greetingForHour(new Date().getHours())}. What should we work on?`;

  renderAuditEvidence(data);

  return { ok: true };
}

/* ------------------------------ Quick actions ------------------------------ */

async function quickRefresh() {
  const data = await loadDashboard();
  return renderDashboard(data, quickHooks);
}

function navigateTo(view) {
  const app = (typeof window !== 'undefined' && window.ActionMeshApp) || null;
  if (app && typeof app.showView === 'function') {
    app.showView(view);
    return true;
  }
  const host = (typeof document !== 'undefined' && document.querySelector)
    ? document.querySelector(`[data-goto="${view}"]`)
    : null;
  if (host && typeof host.click === 'function') {
    host.click();
    return true;
  }
  return false;
}

async function propose(text) {
  const trimmed = String(text || '').trim();
  if (!trimmed) throw new Error('nothing to propose');
  const send = byId('homeAskSend');
  const input = byId('homeAskText');
  if (send && typeof send.click === 'function' && input) {
    input.value = trimmed;
    send.click();
    return { via: 'homeAskSend' };
  }
  const data = await request('/voiceos/messages', 'POST', { text: trimmed, actor: { id: 'voiceos-local' } });
  return { via: 'bridge', data };
}

async function pauseExecution(executionId) {
  if (!executionId) throw new Error('no execution id');
  return request(`/executions/${encodeURIComponent(executionId)}/cancel`, 'POST');
}

async function copyId(text) {
  const nav = (typeof navigator !== 'undefined' && navigator.clipboard) || null;
  if (!nav || typeof nav.writeText !== 'function') throw new Error('clipboard unavailable');
  await nav.writeText(String(text || ''));
  return true;
}

const quickHooks = {
  onPickExecution(id) {
    if (id && navigateTo('live') && typeof window !== 'undefined' && window.ActionMeshApp && typeof window.ActionMeshApp.loadExecution === 'function') {
      window.ActionMeshApp.loadExecution(id);
    }
  },
  onPickTask(id) {
    if (id && navigateTo('tasks') && typeof window !== 'undefined' && window.ActionMeshApp && typeof window.ActionMeshApp.loadTask === 'function') {
      window.ActionMeshApp.loadTask(id);
    }
  },
};

function setDisabled(btn, disabled, reason) {
  if (!btn) return;
  btn.disabled = disabled;
  if (reason) btn.title = reason;
  else btn.removeAttribute('title');
}

function initActivity() {
  const missing = [];
  for (const id of ['homeDevices', 'homeBackend', 'homeApprovals', 'homeAudit', 'homeExecutions', 'homeSessions']) {
    if (!byId(id)) missing.push(id);
  }
  const online = !!bridge();
  // Wire quick actions present in the DOM; disable honestly when unavailable.
  const refreshBtn = byId('homeRefresh');
  if (refreshBtn) {
    refreshBtn.addEventListener('click', () => { quickRefresh().catch(() => {}); });
    setDisabled(refreshBtn, !online, online ? '' : 'Backend bridge unavailable');
  }
  const proposeBtn = byId('homePropose');
  if (proposeBtn) {
    proposeBtn.addEventListener('click', () => {
      const input = byId('homeAskText');
      propose(input ? input.value : '').catch(() => {});
    });
    setDisabled(proposeBtn, !online, online ? '' : 'Backend bridge unavailable');
  }
  const pauseBtn = byId('homePause');
  if (pauseBtn) {
    pauseBtn.addEventListener('click', () => {
      const id = pauseBtn.getAttribute('data-execution-id');
      if (id) pauseExecution(id).then(() => quickRefresh()).catch(() => {});
    });
    const id = pauseBtn.getAttribute('data-execution-id');
    setDisabled(pauseBtn, !online || !id, (!online ? 'Backend bridge unavailable' : '' ) || (!id ? 'No active execution selected' : ''));
  }
  return { ok: missing.length === 0, missing, online };
}

const api = {
  loadDashboard,
  renderDashboard,
  initActivity,
  quickRefresh,
  navigateTo,
  propose,
  pauseExecution,
  copyId,
  // pure helpers (tested)
  isTerminalState,
  isActiveState,
  countActive,
  approvalCount,
  onlineCount,
  sessionProgress,
  greetingForHour,
  timeAgo,
  recordLabel,
  isVerifiedSuccess,
  displayChip,
  renderAuditEvidence,
  renderAuditRow,
};

if (typeof window !== 'undefined') window.ActionMeshActivity = api;
if (typeof module !== 'undefined' && module.exports) module.exports = api;
})();
