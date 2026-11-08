'use strict';
/**
 * Execution history / timeline (Agent 5).
 *
 * Read-only view over two existing, actor-scoped contracts — no new endpoints,
 * no new transport, no second journal:
 *   GET /executions?limit=N          -> ExecutionRecord[] (durable, survives reload)
 *   GET /executions/{id}             -> one ExecutionRecord
 *   GET /audit?limit=N               -> { chain, head, records } (hash-chained ledger)
 *   GET /events  (SSE, Last-Event-ID cursor, stream-ticket auth)
 *
 * Invariants enforced here (mirroring the backend, never weakening it):
 *  - No fake success: `waiting_permission`, `requires_user`, `interrupted`,
 *    `partially_completed` and unverified results are NEVER displayed as
 *    "succeeded" / "verified". Only state === 'success' with a verified
 *    result counts as verified success.
 *  - Unverified is not verified: `requires_user` / unverified nodes render
 *    as "Unverified", not "Succeeded".
 *  - Transient output (`execution.output`) is never rendered from history:
 *    it is live-only, never audited/persisted. This module ignores it.
 *  - Sensitive content never crosses: only allowlisted summary fields are
 *    read (ids, states, timestamps, counts, safe error text). No stdout,
 *    stderr, file bodies, tokens or page content are read or rendered.
 *  - Dedup: SSE replay is keyed by stable `event_id`; a cursor
 *    (`since_event_id` / Last-Event-ID) plus an in-memory seen-set means a
 *    reconnect or reload never appends a duplicate timeline entry.
 *  - Browser/page content is untrusted text: everything is built with
 *    createElement + textContent. No innerHTML anywhere in this file.
 *  - Light theme only: reuses `.state[data-s]`, `.row`, `.kv`, `.empty`
 *    tokens from styles.css. No new global restyle.
 *
 * Lead wiring (do NOT edit forbidden files to wire this in — see report):
 *   1. index.html: add `<script src="history.js"></script>` before app.js
 *      and a second card in the history view, e.g.
 *      <section class="view" data-view="history" hidden> ... existing
 *      automation card ... <article class="card">
 *      <div class="card-head"><h2 class="card-title">Executions</h2>
 *      <span class="chip" id="execHistoryCount">0</span>
 *      <button class="btn ghost small" id="execHistoryRefresh">Refresh</button></div>
 *      <ul class="rows" id="execHistoryList"><li class="empty">Loading…</li></ul>
 *      <div id="execHistoryDetail"></div></article></section>
 *   2. app.js: in showView()/refreshCurrent() add case 'history' ->
 *      ActionMeshHistory.refreshExecHistory(); call
 *      ActionMeshHistory.handleStreamEvent(event) from handleStreamEvent();
 *      wire #execHistoryRefresh click -> refreshExecHistory().
 *      (Exact diff in the Agent 5 report to Lead.)
 *
 * The pure helpers below are exported for Node tests (desktop/test conventions:
 * node:test, no jsdom). When loaded in the browser they attach to
 * window.ActionMeshHistory. The DOM entry points tolerate a missing bridge
 * or missing hosts and render an honest empty/error state instead of throwing.
 */

(function () {
  // ---------------------------------------------------------------------------
  // Lifecycle mapping — the no-fake-success table.
  // Backend ActionState values (protocol/models.py) mapped to presentation.
  // Anything not verified-success stays visibly not-success.
  // ---------------------------------------------------------------------------
  const TERMINAL_STATES = new Set([
    'success', 'failed', 'cancelled', 'denied', 'timeout',
    'partially_completed', 'interrupted',
  ]);

  const DISPLAY = {
    created: { label: 'Planned', chip: 'created' },
    planned: { label: 'Planned', chip: 'planned' },
    validating: { label: 'Validating', chip: 'validating' },
    authorized: { label: 'Approved', chip: 'authorized' },
    waiting_permission: { label: 'Waiting for approval', chip: 'waiting_permission' },
    requires_user: { label: 'Unverified — needs you', chip: 'requires_user' },
    executing: { label: 'Running', chip: 'executing' },
    verifying: { label: 'Verifying', chip: 'verifying' },
    retrying: { label: 'Retrying', chip: 'retrying' },
    success: { label: 'Succeeded', chip: 'success' },
    failed: { label: 'Failed', chip: 'failed' },
    cancelled: { label: 'Cancelled', chip: 'cancelled' },
    denied: { label: 'Denied', chip: 'denied' },
    timeout: { label: 'Timed out', chip: 'timeout' },
    partially_completed: { label: 'Partially completed', chip: 'partial' },
    interrupted: { label: 'Interrupted', chip: 'interrupted' },
    // Node-level / VoiceOS spellings seen in node_states and steps.
    succeeded: { label: 'Succeeded', chip: 'succeeded' },
    succeeded_unverified: { label: 'Succeeded (unverified)', chip: 'succeeded_unverified' },
    awaiting_approval: { label: 'Waiting for approval', chip: 'awaiting_approval' },
    running: { label: 'Running', chip: 'running' },
    pending: { label: 'Waiting', chip: 'pending' },
    skipped: { label: 'Skipped', chip: 'skipped' },
    unknown: { label: 'Unknown', chip: 'unknown' },
  };

  function displayState(state) {
    const key = String(state || 'unknown');
    return DISPLAY[key] || { label: key.replace(/_/g, ' '), chip: key };
  }

  function isTerminal(state) {
    return TERMINAL_STATES.has(String(state || ''));
  }

  /**
   * Totally strict: the only verified-success shape is
   * state === 'success' AND every node result that carries a verification
   * object reports verified === true. A missing node list is NOT proof of
   * verification, so it returns false rather than guessing.
   */
  function isVerifiedSuccess(record) {
    if (!record || String(record.state) !== 'success') return false;
    const results = record.results && typeof record.results === 'object' ? record.results : {};
    const views = Object.values(results);
    if (!views.length) return false;
    return views.every((v) => !v || !v.verification || v.verification.verified === true)
      && views.some((v) => v && v.verification && v.verification.verified === true);
  }

  /** Display label for a record, never upgrading unverified to success. */
  function recordLabel(record) {
    const d = displayState(record && record.state);
    if (String(record && record.state) === 'success' && !isVerifiedSuccess(record)) {
      return 'Succeeded (unverified)';
    }
    return d.label;
  }

  /**
   * Chip for a record: an unverified `success` must not wear the green
   * success chip. It is downgraded to `succeeded_unverified` (amber) so the
   * row and the detail agree with recordLabel() — the chip colour can never
   * claim a verification the record does not have.
   */
  function recordChip(record) {
    if (String(record && record.state) === 'success' && !isVerifiedSuccess(record)) {
      return { label: 'Succeeded (unverified)', chip: 'succeeded_unverified' };
    }
    return displayState(record && record.state);
  }

  function verificationLabel(record) {
    const results = record && record.results && typeof record.results === 'object'
      ? record.results : {};
    const views = Object.values(results).filter((v) => v && v.verification);
    if (!views.length) return 'not recorded';
    if (views.every((v) => v.verification.verified === true)) return 'verified';
    if (views.some((v) => v.verification.verified === true)) return 'partially verified';
    return 'not verified';
  }

  /** Safe failure reason: backend `error` text only, length-capped. No bodies. */
  function safeFailureReason(record) {
    const raw = record && typeof record.error === 'string' ? record.error.trim() : '';
    if (!raw) return '';
    return raw.length > 240 ? `${raw.slice(0, 240)}…` : raw;
  }

  /** Result summary from the allowlisted projection only (safe_result_view). */
  function resultSummary(record) {
    const results = record && record.results && typeof record.results === 'object'
      ? record.results : {};
    const states = record && record.node_states && typeof record.node_states === 'object'
      ? record.node_states : {};
    const ids = new Set([...Object.keys(results), ...Object.keys(states)]);
    if (!ids.size) return 'No node results recorded yet.';
    const parts = [];
    for (const id of [...ids].slice(0, 8)) {
      const view = results[id] || {};
      const st = (view && view.state) || states[id] || 'unknown';
      let suffix = '';
      if (view && view.verification) suffix = view.verification.verified ? ' · verified' : ' · unverified';
      else if (view && view.simulated) suffix = ' · simulated';
      parts.push(`${id}: ${displayState(st).label}${suffix}`);
    }
    return parts.join(' · ');
  }

  // ---------------------------------------------------------------------------
  // SSE dedup: stable ids + cursor. Mirrors EventHub.history()/_after() and the
  // dashboard's seenEvents set; the desktop stream merges via event_id too.
  // ---------------------------------------------------------------------------
  function createDedup(maxSeen = 2000) {
    const seen = new Set();
    return {
      /** True if this event is new (and records it). False = duplicate, drop it. */
      accept(event) {
        const id = event && (event.event_id || event.eventId);
        if (!id) return true; // no stable id: cannot dedupe, do not drop a fact
        if (seen.has(id)) return false;
        seen.add(id);
        while (seen.size > maxSeen) seen.delete(seen.values().next().value);
        return true;
      },
      has(id) { return seen.has(id); },
      get size() { return seen.size; },
      clear() { seen.clear(); },
    };
  }

  /** Merge a replayed batch into a timeline without duplicates (by event_id). */
  function mergeTimeline(existing, incoming) {
    const seen = new Set((existing || []).map((e) => e && (e.event_id || e.eventId)).filter(Boolean));
    const out = [...(existing || [])];
    for (const event of incoming || []) {
      const id = event && (event.event_id || event.eventId);
      if (id && seen.has(id)) continue;
      if (id) seen.add(id);
      out.push(event);
    }
    return out;
  }

  /** Drop transient output from any history payload — defence in depth. */
  function isHistoryEvent(event) {
    if (!event) return false;
    if (event.event_type === 'execution.output') return false;
    return true;
  }

  // ---------------------------------------------------------------------------
  // Filters — over the durable /executions payload only.
  // ---------------------------------------------------------------------------
  function filterExecutions(records, { query = '', bucket = 'all' } = {}) {
    const list = Array.isArray(records) ? records : [];
    const q = String(query || '').trim().toLowerCase();
    return list.filter((r) => {
      if (bucket === 'waiting') {
        if (!['waiting_permission', 'requires_user', 'awaiting_approval'].includes(String(r.state))) return false;
      } else if (bucket === 'running') {
        if (isTerminal(r.state)) return false;
      } else if (bucket === 'attention') {
        if (!['failed', 'denied', 'timeout', 'interrupted', 'partially_completed'].includes(String(r.state))) return false;
      } else if (bucket === 'succeeded') {
        if (String(r.state) !== 'success') return false;
      }
      if (q) {
        const hay = [r.execution_id, r.actor_id, r.device_id, r.device_name, r.state]
          .filter(Boolean).join(' ').toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }

  // ---------------------------------------------------------------------------
  // DOM rendering. Everything via createElement + textContent (CSP: no inline
  // handlers, no innerHTML). Reuses existing styles.css tokens only.
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
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function replace(node, children) {
    while (node && node.firstChild) node.removeChild(node.firstChild);
    if (!node) return;
    for (const child of [].concat(children || [])) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
  }

  function timeAgo(iso) {
    if (!iso) return '';
    const then = Date.parse(iso);
    if (!Number.isFinite(then)) return String(iso);
    const seconds = Math.round((Date.now() - then) / 1000);
    if (seconds < 5) return 'just now';
    if (seconds < 60) return `${seconds}s ago`;
    if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
    return `${Math.round(seconds / 86400)}d ago`;
  }

  function stateChip(state) {
    const d = displayState(state);
    return el('span', { class: 'state', dataset: { s: d.chip }, text: d.label });
  }

  /** Chip for a whole record (see recordChip) — never green on unverified. */
  function recordStateChip(record) {
    const d = recordChip(record);
    return el('span', { class: 'state', dataset: { s: d.chip }, text: d.label });
  }

  function executionRow(record, onPick) {
    const device = record.device_name || record.device_id || 'this laptop';
    const chip = recordChip(record);
    const row = el('li', { class: 'row', dataset: { id: record.execution_id } }, [
      el('div', { class: 'row-main' }, [
        el('div', { class: 'row-title', text: record.execution_id || '(no id)' }),
        el('div', {
          class: 'row-sub',
          text: `device ${device} · actor ${record.actor_id || '—'} · ${timeAgo(record.started_at || record.updated_at)}`,
        }),
        el('div', { class: 'row-sub', text: resultSummary(record) }),
      ]),
      recordStateChip(record),
    ]);
    if (onPick) {
      row.setAttribute('tabindex', '0');
      row.setAttribute('role', 'button');
      row.setAttribute('aria-label', `Execution ${record.execution_id || '(no id)'}, ${chip.label}`);
      row.addEventListener('click', () => onPick(record.execution_id));
      row.addEventListener('keydown', (event) => {
        if (event && (event.key === 'Enter' || event.key === ' ')) {
          if (typeof event.preventDefault === 'function') event.preventDefault();
          onPick(record.execution_id);
        }
      });
    }
    return row;
  }

  function renderExecutionDetail(host, record) {
    if (!host) return;
    if (!record) {
      replace(host, el('div', { class: 'empty-state' }, [el('p', { text: 'Select an execution to see its history.' })]));
      return;
    }
    const device = record.device_name || record.device_id || 'this laptop';
    const label = recordLabel(record);
    const parts = [
      el('div', { class: 'detail-head' }, [
        el('div', {}, [
          el('div', { class: 'detail-title', text: record.execution_id }),
          el('div', {
            class: 'detail-meta',
            text: `device ${device} · actor ${record.actor_id || '—'} · started ${timeAgo(record.started_at)} · updated ${timeAgo(record.updated_at)}`,
          }),
        ]),
        el('div', { class: 'detail-actions' }, [recordStateChip(record)]),
      ]),
      el('dl', { class: 'kv' }, [
        el('dt', { text: 'State' }), el('dd', { text: label }),
        el('dt', { text: 'Verification' }), el('dd', { text: verificationLabel(record) }),
        el('dt', { text: 'Device' }), el('dd', { text: device }),
        el('dt', { text: 'Actor' }), el('dd', { text: record.actor_id || '—' }),
        el('dt', { text: 'Cancel requested' }), el('dd', { text: record.cancel_requested ? 'yes' : 'no' }),
        el('dt', { text: 'Result summary' }), el('dd', { text: resultSummary(record) }),
      ]),
    ];
    const reason = safeFailureReason(record);
    if (reason) parts.push(el('div', { class: 'notice bad', text: reason }));
    if (record.cancel_requested && !isTerminal(record.state)) {
      parts.push(el('div', { class: 'notice', text: 'Stop was requested. The executor has not confirmed it stopped yet.' }));
    }
    if (!isTerminal(record.state) && ['waiting_permission', 'requires_user', 'awaiting_approval'].includes(String(record.state))) {
      parts.push(el('div', { class: 'notice', text: 'This run is waiting on you. It has not run yet and is not a success.' }));
    }
    replace(host, parts);
  }

  function renderList(host, records, onPick) {
    if (!host) return;
    if (!records || !records.length) {
      replace(host, el('li', { class: 'empty', text: 'Nothing matches. That is a fact, not a loading state.' }));
      return;
    }
    replace(host, records.map((r) => executionRow(r, onPick)));
  }

  function renderError(host, message) {
    if (!host) return;
    replace(host, el('li', { class: 'empty', text: `Could not read execution history: ${message}` }));
  }

  // ---------------------------------------------------------------------------
  // Bridge wiring (tolerates absence; never throws at boot). `bridge` is
  // window.actionmesh from preload.js — the only transport, per app.js rules.
  // ---------------------------------------------------------------------------
  const dedup = createDedup();
  let selectedId = '';
  let allRecords = [];
  let filterState = { query: '', bucket: 'all' };

  async function fetchExecutions() {
    const bridge = typeof window !== 'undefined' ? window.actionmesh : null;
    if (!bridge || typeof bridge.request !== 'function') {
      throw new Error('backend bridge unavailable');
    }
    const result = await bridge.request({ method: 'GET', path: '/executions?limit=100', body: null, long: false });
    if (!result || !result.ok) {
      const detail = (result && result.body && result.body.detail) || (result && result.error)
        || (result && result.text) || 'request failed';
      throw new Error(String(detail));
    }
    return Array.isArray(result.body) ? result.body : [];
  }

  async function refreshExecHistory() {
    const listHost = typeof document !== 'undefined' ? document.getElementById('execHistoryList') : null;
    const countHost = typeof document !== 'undefined' ? document.getElementById('execHistoryCount') : null;
    const detailHost = typeof document !== 'undefined' ? document.getElementById('execHistoryDetail') : null;
    if (!listHost) return; // lead has not wired the hosts yet; stay silent, not broken
    listHost.setAttribute('role', 'status');
    listHost.setAttribute('aria-live', 'polite');
    listHost.setAttribute('aria-atomic', 'false');
    if (detailHost) { detailHost.setAttribute('role', 'status'); detailHost.setAttribute('aria-live', 'polite'); detailHost.setAttribute('aria-atomic', 'false'); }
    if (countHost) { countHost.setAttribute('aria-live', 'polite'); }
    replace(listHost, el('li', { class: 'empty', text: 'Loading…' }));
    try {
      allRecords = await fetchExecutions();
      if (countHost) countHost.textContent = String(allRecords.length);
      renderList(listHost, filterExecutions(allRecords, filterState), (id) => {
        selectedId = id;
        const record = allRecords.find((r) => r.execution_id === id) || null;
        renderExecutionDetail(detailHost, record);
      });
      if (selectedId) {
        const record = allRecords.find((r) => r.execution_id === selectedId) || null;
        renderExecutionDetail(detailHost, record);
      }
    } catch (error) {
      renderError(listHost, error && error.message ? error.message : String(error));
    }
  }

  /**
   * Feed a stream event into the history timeline. Transient output is ignored
   * (live-only by contract); everything else is deduped by event_id and, if it
   * concerns the selected execution, re-renders the detail from the durable
   * record via refreshExecHistory() — never by patching state from the payload.
   * Returns true if the event was accepted as new.
   */
  function handleStreamEvent(event) {
    if (!event || !isHistoryEvent(event)) return false;
    const data = event.data || event;
    if (!dedup.accept(data)) return false;
    if (data.execution_id && data.execution_id === selectedId) {
      refreshExecHistory();
    }
    return true;
  }

  function setFilter(next) {
    filterState = { ...filterState, ...(next || {}) };
    const listHost = typeof document !== 'undefined' ? document.getElementById('execHistoryList') : null;
    if (listHost) {
      renderList(listHost, filterExecutions(allRecords, filterState), (id) => {
        selectedId = id;
        const detailHost = document.getElementById('execHistoryDetail');
        renderExecutionDetail(detailHost, allRecords.find((r) => r.execution_id === id) || null);
      });
    }
  }

  const api = {
    TERMINAL_STATES, DISPLAY, displayState, isTerminal, isVerifiedSuccess,
    recordLabel, recordChip, recordStateChip, verificationLabel, safeFailureReason, resultSummary,
    createDedup, mergeTimeline, isHistoryEvent, filterExecutions,
    executionRow, renderExecutionDetail, renderList, renderError,
    refreshExecHistory, handleStreamEvent, setFilter,
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  }
  if (typeof window !== 'undefined') {
    window.ActionMeshHistory = api;
  }
})();
