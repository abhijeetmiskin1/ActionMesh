'use strict';
/**
 * The desktop shell's renderer.
 *
 * Two rules shape everything in this file:
 *
 *  1. **Only `window.actionmesh`.** The page's CSP says `connect-src 'none'`, so it
 *     cannot fetch, open a socket, or load anything remote even if it tried. Every
 *     fact on screen arrived through the preload bridge, whose surface is a fixed
 *     list of named calls.
 *  2. **Text goes in as text.** Nothing untrusted is ever assigned to `innerHTML`:
 *     a session's request, an approval's parameters, a chunk of a program's output —
 *     all of it is built with `createElement` + `textContent`. The server already
 *     strips content at its result boundary; this is the second lock on the same door.
 *
 * The shell also never invents state. Every "running", "verified" or "waiting" badge
 * is a field a route returned. If a route cannot be reached the screen says so
 * rather than leaving the last known value up.
 */

const bridge = window.actionmesh;

// ---------------------------------------------------------------------------
// Small DOM helpers
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

const byId = (id) => document.getElementById(id);

/**
 * Wire one control by id, tolerating its absence.
 *
 * Every screen in this shell is built from the same document, so a control that is
 * not there is not a programming error to throw on at boot — it is a screen that was
 * not linked in. Returning `null` keeps the rest of `wire()` running: one missing
 * button must not take the whole window down.
 */
function on(id, type, handler, options) {
  const node = byId(id);
  if (!node) return null;
  node.addEventListener(type, handler, options);
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

function replace(node, children) {
  clear(node);
  for (const child of [].concat(children || [])) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

/** A state chip. The word is always shown; colour is decoration, not the message. */
function stateChip(state) {
  const label = String(state || 'unknown').replace(/_/g, ' ');
  return el('span', { class: 'state', dataset: { s: String(state || 'unknown') }, text: label });
}

function riskChip(risk) {
  return el('span', { class: 'risk', dataset: { r: String(risk || 'medium') }, text: String(risk || 'medium') });
}

function kvTable(rows) {
  const list = el('dl', { class: 'kv' });
  for (const [key, value] of rows) {
    list.append(el('dt', { text: key }), el('dd', { text: value === null || value === undefined ? '—' : String(value) }));
  }
  return list;
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

function shortId(id) {
  const text = String(id || '');
  return text.length > 18 ? `${text.slice(0, 8)}…${text.slice(-6)}` : text;
}

// ---------------------------------------------------------------------------
// Toasts — brief, non-modal, and never the only place a fact appears.
// ---------------------------------------------------------------------------
let toastTimer = null;
function toast(message, kind) {
  const node = byId('toast');
  // The toast is a convenience, never the record: a screen without one still shows
  // the same fact in place, so a missing node drops the message rather than the boot.
  if (!node) return;
  node.textContent = message;
  node.dataset.kind = kind || '';
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, kind === 'bad' ? 9000 : 5000);
}

// ---------------------------------------------------------------------------
// API access through the preload bridge
// ---------------------------------------------------------------------------
class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status || 0;
  }
}

async function api(method, path, body, { long = false } = {}) {
  const result = await bridge.request({ method, path, body: body === undefined ? null : body, long });
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
const PUT = (path, body) => api('PUT', path, body === undefined ? {} : body);
const DELETE = (path) => api('DELETE', path);

// ---------------------------------------------------------------------------
// Application state
// ---------------------------------------------------------------------------
const state = {
  view: 'home',
  appState: null,
  ready: null,
  selectedTask: null,
  voiceTask: null,
  automaticApprovals: new Map(),
  voiceSubmitting: false,
  runningTasks: new Set(),
  selectedExecution: null,
  tasks: [],
  executions: [],
  approvals: [],
  output: new Map(), // execution_id -> { node_id: { stdout: text, stderr: text } }
  // session_id -> the diagnosis body the person asked for. Held in the renderer only:
  // the excerpt it came from is transient, and re-deriving it on every refresh would
  // spend another model call (and another decision to share context) without being
  // asked to.
  diagnoses: new Map(),
  backendReady: false,
  // The id of the machine this backend actually serves, as `/ready` reports it. Empty
  // until that has been read, and empty forever on a control plane with no agent — in
  // which case a screen action has no target and says so, rather than naming a device
  // nobody enrolled.
  localDeviceId: '',
  // The automation whose detail is showing, and the last draft this page asked for.
  // Both are renderer-only: the server never keeps a draft, so a reload shows nothing
  // and there is no half-understood plan waiting to be saved by surprise.
  selectedAutomation: '',
  automationDraft: null,
  // The agent's declared capabilities, read from `/capabilities`. The command guide
  // uses it to say whether screen *analysis* is offered in this build; null means it
  // has not been read, and the guide then says "not supported" rather than guessing.
  capabilities: null,
  // AI provider: the closed table the main process publishes, and the last status it
  // reported. Both are non-secret — no key material ever appears in either.
  providerSpecs: [],
  providerStatus: null,
  // Which planner `/voiceos/status` says will answer the next turn. A saved provider
  // is not a connected one, so this is read from the backend rather than inferred
  // from the provider bar.
  voiceModel: null,
  // What `POST /voiceos/guide` measured for each example phrase: { phrase: {actions,
  // notes} }. `null` means "not measured yet", and the guide then says so rather than
  // showing a verdict it has not obtained.
  guideVerdicts: null,
  // Per-purpose specs and statuses (C42). Specs: purpose -> array; Statuses: purpose -> object.
  purposeSpecs: {},
  purposeStatuses: {},
};

const IDENTITY = { id: 'voiceos-local' };

/**
 * Step states that mean "this step is over, whatever the outcome".
 *
 * Mirrors `TaskStep.settled` in actionmesh/voiceos/models.py. It is duplicated here
 * rather than derived from the status, because "did this step finish?" and "did the
 * task finish?" are different questions: a failed read is settled but leaves the task
 * live, and a diagnosis is only meaningful once the *read* is settled.
 */
const SETTLED = new Set([
  'succeeded', 'succeeded_unverified', 'failed', 'denied', 'cancelled', 'skipped', 'interrupted',
]);

// ---------------------------------------------------------------------------
// Views: which one is showing, and what each is called
// ---------------------------------------------------------------------------
const VIEWS = {
  home: { eyebrow: 'Control plane', title: 'Home' },
  voice: { eyebrow: 'Voice', title: 'Voice Commands' },
  tasks: { eyebrow: 'VoiceOS', title: 'Task Center' },
  automations: { eyebrow: 'Personal automation', title: 'Automations' },
  live: { eyebrow: 'Execution', title: 'Live Control' },
  approvals: { eyebrow: 'Human in the loop', title: 'Approvals' },
  history: { eyebrow: 'What actually happened', title: 'History' },
  devices: { eyebrow: 'This laptop', title: 'Devices' },
  visual: { eyebrow: 'Seeing the screen', title: 'Visual Intelligence' },
  browser: { eyebrow: 'Web pages', title: 'Browser' },
  providers: { eyebrow: 'AI connections', title: 'Providers' },
  settings: { eyebrow: 'Application', title: 'Settings' },
};

function showView(name) {
  if (name !== 'voice') endVoiceConversation();
  // Voice is a real view now: the microphone, the transcript box and the quick
  // commands all live in this window. The original push-to-talk page is still
  // reachable from Settings, but nothing navigates there by default.
  if (!VIEWS[name]) name = 'home';
  if (state.view !== name) byId('mainScroll').scrollTop = 0;
  state.view = name;
  for (const section of document.querySelectorAll('section.view')) {
    section.hidden = section.dataset.view !== name;
  }
  for (const button of document.querySelectorAll('.nav-item')) {
    const active = button.dataset.view === name;
    if (active) button.setAttribute('aria-current', 'true');
    else button.removeAttribute('aria-current');
  }
  byId('viewEyebrow').textContent = VIEWS[name].eyebrow;
  byId('viewTitle').textContent = VIEWS[name].title;
  refreshCurrent();
}

// ---------------------------------------------------------------------------
// Backend / app state
// ---------------------------------------------------------------------------
function backendStatusLine(snapshot) {
  const status = snapshot ? snapshot.status : 'stopped';
  const labels = {
    ready: 'Backend ready', starting: 'Starting backend…', failed: 'Backend failed',
    stopping: 'Stopping…', stopped: 'Backend stopped',
  };
  return labels[status] || `Backend ${status}`;
}

function applyAppState(next) {
  if (!next) return;
  state.appState = next;
  const backend = next.backend || { status: 'stopped' };
  state.backendReady = backend.status === 'ready';
  const dot = byId('railDot');
  dot.dataset.state = backend.status;
  byId('railStatus').textContent = backendStatusLine(backend);
  byId('railVersion').textContent = next.version ? `v${next.version}` : '';
  const shortcut = next.settings && next.settings.shortcut;
  byId('railShortcut').textContent = shortcut ? prettyAccelerator(shortcut) : '';
  // The pill is owned by the stream status push; this is only the not-yet-connected
  // placeholder, so it must not overwrite a live state on a later backend push.
  const streamPill = byId('streamPill');
  if (streamPill.dataset.state !== 'open') {
    streamPill.textContent = state.backendReady ? 'stream …' : 'backend offline';
  }
  // The provider status rides on the same push, so the header stays current without a
  // second round-trip. It is a state and a sentence; never a credential.
  if (next.provider) {
    state.providerStatus = next.provider;
  }
  if (next.purposes) {
    // The per-purpose statuses ride on the same push, so the header and the lanes
    // stay current without a second round-trip. They are states and sentences;
    // never credentials.
    state.purposeStatuses = { ...state.purposeStatuses, ...next.purposes };
    if (next.purposes.chat) state.providerStatus = next.purposes.chat;
    renderProviderBar(state.purposeStatuses.chat || state.providerStatus);
    renderHomeProviderKv();
    renderSettingsProviderCard();
  } else if (next.provider) {
    renderProviderBar(next.provider);
  }
}

function prettyAccelerator(value) {
  // The page has no Node globals (the preload does not expose `process`), so the
  // modifier label is guessed from the user agent rather than read from a process.
  const mac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || '');
  return String(value || '')
    .replace(/CommandOrControl|CmdOrCtrl/gi, mac ? '⌘' : 'Ctrl')
    .replace(/Shift/gi, 'Shift')
    .replace(/Space/gi, 'Space')
    .replace(/\+/g, '+');
}

async function refreshAppState() {
  try {
    applyAppState(await bridge.state());
  } catch (error) {
    toast(`Could not read the app state: ${error.message}`, 'bad');
  }
}

/** The single place that decides whether an API call is even possible. */
function requireBackend() {
  if (state.backendReady) return true;
  toast('The backend is not running. Settings → Restart backend.', 'bad');
  return false;
}

// ---------------------------------------------------------------------------
// HOME
// ---------------------------------------------------------------------------
async function refreshHome() {
  await refreshAppState();
  if (!state.backendReady) {
    byId('homeBackend').textContent = '—';
    byId('homeBackendNote').textContent = 'backend offline';
    byId('homeDevices').textContent = '—';
    byId('homeDevicesNote').textContent = 'backend offline';
    byId('homeAudit').textContent = '—';
    byId('homeAuditNote').textContent = 'backend offline';
    byId('homeApprovals').textContent = '—';
    replace(byId('homeExecutions'), el('li', { class: 'empty', text: 'Backend offline.' }));
    replace(byId('homeSessions'), el('li', { class: 'empty', text: 'Backend offline.' }));
    return;
  }

  const ready = Promise.allSettled([GET('/ready'), GET('/devices')]);
  const approvals = GET('/approvals').then((entries) => {
    const count = entries.reduce((sum, entry) => sum + (entry.actions || []).length, 0);
    byId('homeApprovals').textContent = String(count);
    setBadge('approvals', count);
  }, () => { byId('homeApprovals').textContent = '—'; });
  const tasks = GET('/voiceos/sessions?limit=8').then((sessions) => {
    renderTaskRows(byId('homeSessions'), sessions.slice(0, 6), (id) => {
      showView('tasks');
      loadTask(id);
    });
  }, () => {
    replace(byId('homeSessions'), el('li', { class: 'empty', text: 'Could not load recent tasks.' }));
  });
  const activity = Promise.allSettled([
    GET('/executions?limit=8'), GET('/automations'),
  ]);
  const [readyResult, devicesResult] = await ready;

  // Connected computers: the number the backend asserts, and — just as important — the
  // one it does not. This build has enrolled a single laptop; the card says so.
  if (readyResult.status === 'fulfilled') {
    const ready = readyResult.value;
    state.ready = ready;
    state.localDeviceId = ready.local_device_id || state.localDeviceId;
    byId('homeDevices').textContent = String(ready.online_devices ?? 0);
  } else if (devicesResult.status === 'fulfilled') {
    byId('homeDevices').textContent = String(devicesResult.value.filter((d) => d.online).length);
  } else {
    byId('homeDevices').textContent = '—';
  }
  if (devicesResult.status === 'fulfilled') {
    const list = devicesResult.value;
    const online = list.filter((device) => device.online).length;
    byId('homeDevicesNote').textContent = list.length
      ? `${online} of ${list.length} online`
      : 'none enrolled';
    renderDevicePill(list);
  } else {
    byId('homeDevicesNote').textContent = 'unreadable';
  }

  const [executionsResult, automationsResult] = await activity;
  // Active executions: the ones that have not settled. The executions list carries
  // state per record, so this counts what the backend says rather than what this page
  // remembers having started.
  if (executionsResult.status === 'fulfilled') {
    const records = executionsResult.value;
    const active = records.filter((record) => ['running', 'pending', 'awaiting_approval'].includes(record.state));
    byId('homeBackend').textContent = String(active.length);
    byId('homeBackendNote').textContent = active.length ? 'in progress now' : 'nothing running';
    renderExecutionRows(byId('homeExecutions'), records.slice(0, 6), (id) => {
      state.selectedExecution = id;
      showView('live');
      loadExecution(id);
    });
  } else {
    byId('homeBackend').textContent = '—';
    byId('homeBackendNote').textContent = 'unreadable';
  }

  // Saved automations: the library, straight from the store.
  if (automationsResult.status === 'fulfilled') {
    const body = automationsResult.value || {};
    const workflows = Array.isArray(body.workflows) ? body.workflows : [];
    byId('homeAudit').textContent = String(workflows.length);
    const enabled = workflows.filter((flow) => flow.enabled).length;
    byId('homeAuditNote').textContent = workflows.length ? `${enabled} enabled` : 'none yet';
  } else {
    byId('homeAudit').textContent = '—';
    byId('homeAuditNote').textContent = 'unreadable';
  }

  renderHomeProviderKv();
  const hour = new Date().getHours();
  byId('homeGreeting').textContent = `${hour < 12 ? 'Good morning' : hour < 18 ? 'Good afternoon' : 'Good evening'}. What should we work on?`;
  await Promise.all([approvals, tasks]);
}

/** The header's device pill: the device this control plane actually serves. */
function renderDevicePill(devices) {
  const list = Array.isArray(devices) ? devices : [];
  const local = list.find((device) => device.device_id === state.localDeviceId)
    || list.find((device) => device.online)
    || list[0];
  const dot = byId('devicePillDot');
  if (!local) {
    byId('devicePillName').textContent = 'No device';
    dot.className = 'dot offline';
    return;
  }
  byId('devicePillName').textContent = local.name || local.device_id;
  dot.className = `dot ${local.online ? 'online' : 'offline'}`;
}

// The single-provider table that used to sit here was replaced by the four lanes;
// its body survived as a second `renderHomeProviderKv` declaration, and the later
// declaration won the hoist, so it silently did nothing. Removed rather than
// renamed: two functions with one name is a bug waiting to be re-introduced.

function renderExecutionRows(host, records, onPick) {
  if (!records.length) {
    replace(host, el('li', { class: 'empty', text: 'No executions yet.' }));
    return;
  }
  replace(host, records.map((record) => {
    const row = el('li', { class: 'row' }, [
      el('div', { class: 'row-main' }, [
        el('div', { class: 'row-title', text: record.execution_id }),
        el('div', {
          class: 'row-sub',
          text: `${(record.node_states ? Object.keys(record.node_states).length : 0)} node(s) · ${timeAgo(record.started_at || record.updated_at)}`,
        }),
      ]),
      stateChip(record.state),
    ]);
    if (onPick) {
      row.style.cursor = 'pointer';
      row.addEventListener('click', () => onPick(record.execution_id));
    }
    return row;
  }));
}

function renderTaskRows(host, sessions, onPick, selectedId) {
  if (!sessions.length) {
    replace(host, el('li', { class: 'empty', text: 'No tasks yet.' }));
    return;
  }
  replace(host, sessions.map((session) => {
    const row = el('li', { class: 'row', dataset: { id: session.session_id } }, [
      el('div', { class: 'row-main' }, [
        el('div', { class: 'row-title', text: session.request || session.session_id }),
        el('div', {
          class: 'row-sub',
          text: `${session.settled ?? ''}${session.steps !== undefined ? `/${session.steps}` : ''} steps · ${timeAgo(session.updated_at)}`,
        }),
      ]),
      stateChip(session.status),
    ]);
    if (selectedId && session.session_id === selectedId) row.setAttribute('aria-selected', 'true');
    if (onPick) {
      const openTask = () => onPick(session.session_id);
      row.tabIndex = 0;
      row.setAttribute('role', 'button');
      row.addEventListener('click', openTask);
      row.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          openTask();
        }
      });
    }
    return row;
  }));
}

function setBadge(name, count) {
  if (name === 'approvals') {
    byId('notifyDot').hidden = count === 0;
    byId('notifyBtn').setAttribute('aria-label', count ? `Review approvals (${count} waiting)` : 'Review approvals');
  }
  const badge = document.querySelector(`[data-badge="${name}"]`);
  if (!badge) return;
  if (count > 0) {
    badge.textContent = String(count);
    badge.hidden = false;
  } else {
    badge.hidden = true;
  }
}

// ---------------------------------------------------------------------------
// Ask / propose — one conversational turn. It never runs anything.
// ---------------------------------------------------------------------------
async function propose(text, replyHost, { onSession } = {}) {
  const clean = String(text || '').trim();
  if (!clean) return null;
  if (!requireBackend()) return null;
  try {
    const answer = await POST('/voiceos/messages', { text: clean, actor: IDENTITY });
    renderReply(replyHost, answer, onSession);
    return answer;
  } catch (error) {
    toast(`It could not be understood: ${error.message}`, 'bad');
    return null;
  }
}

function renderReply(host, answer, onSession) {
  host.hidden = false;
  const parts = [el('div', { class: 'reply-title', text: `VoiceOS · ${answer.kind}` })];
  parts.push(el('p', { text: answer.reply || '' }));
  if (answer.session_id) {
    const open = el('button', {
      class: 'btn small ghost', type: 'button',
      text: 'Open in Task Center →',
    });
    open.addEventListener('click', () => {
      if (onSession) onSession(answer.session_id);
      state.selectedTask = answer.session_id;
      showView('tasks');
      loadTask(answer.session_id);
    });
    parts.push(el('div', { class: 'btn-row' }, [open]));
    if (host.id !== 'voiceReply') {
      const results = el('button', { class: 'btn small ghost', type: 'button', text: 'Show task results' });
      results.addEventListener('click', () => {
        state.voiceTask = answer.session_id;
        showView('voice');
      });
      parts.at(-1).append(results);
    }
  }
  if (answer.notes && answer.notes.length) {
    parts.push(el('ul', { class: 'notes' }, answer.notes.map((note) => el('li', { text: note }))));
  }
  // `unresolved` is where the planner explains *why* it could not act — including
  // "the model did not answer this request" and the bridge's own state. Dropping it
  // would leave the reply blaming the sentence for a missing credential.
  if (host.id === 'voiceReply' && answer.unresolved?.includes('browser capability not registered')) {
    const settings = el('button', { class: 'btn small ghost', type: 'button', text: 'Open browser settings' });
    settings.addEventListener('click', () => {
      showView('settings');
      byId('setAllowBrowser').focus();
    });
    parts.push(settings);
  }
  if (answer.unresolved && answer.unresolved.length) {
    parts.push(el('ul', { class: 'notes' }, answer.unresolved.map((note) => el('li', { text: note }))));
  }
  // The server says `ran: false` on every turn of this route. Saying it here too is
  // the difference between "it answered" and "it acted".
  if (host.id !== 'voiceReply' || !answer.session_id) {
    parts.push(el('p', { class: 'fine', text: 'This reply did not execute an action.' }));
  }
  replace(host, parts);
}

/** Both ask boxes use the Voice Commands microphone and transcript. */
function wirePushToTalk(button) {
  if (!button) return;
  button.title = 'Open Voice Commands and start recording';
  button.addEventListener('click', () => {
    showView('voice');
    byId('voiceMic')?.click();
  });
}

// ---------------------------------------------------------------------------
// TASK CENTER
// ---------------------------------------------------------------------------
const FINISHED_TASKS = new Set(['completed', 'failed', 'stopped', 'cancelled', 'interrupted']);

function renderFilteredTasks() {
  const filter = document.querySelector('[data-task-filter][aria-pressed="true"]')?.dataset.taskFilter;
  const query = byId('taskSearch').value.trim().toLowerCase();
  const visible = state.tasks.filter((task) => {
    if (filter === 'approval' && task.status !== 'awaiting_approval') return false;
    if (filter === 'active' && FINISHED_TASKS.has(task.status)) return false;
    return !query || `${task.request} ${task.session_id}`.toLowerCase().includes(query);
  });
  byId('taskCount').textContent = String(visible.length);
  const list = byId('taskList');
  if (!visible.length && state.tasks.length) {
    replace(list, el('li', { class: 'empty', text: 'No recent tasks match these filters.' }));
    return;
  }
  renderTaskRows(list, visible, loadTask, state.selectedTask);
}

async function refreshTasks() {
  await refreshAppState();
  if (!state.backendReady) {
    replace(byId('taskList'), el('li', { class: 'empty', text: 'Backend offline.' }));
    return;
  }
  try {
    const sessions = await GET('/voiceos/sessions?limit=50');
    state.tasks = sessions;
    refreshTasksBadgeOnly();
    renderFilteredTasks();
  } catch (error) {
    toast(`Could not read tasks: ${error.message}`, 'bad');
  }
}

async function loadTask(sessionId) {
  state.selectedTask = sessionId;
  const host = byId('taskDetail');
  replace(host, el('div', { class: 'empty-state' }, [el('p', { text: 'Loading…' })]));
  try {
    const session = await GET(`/voiceos/sessions/${encodeURIComponent(sessionId)}`);
    renderTaskDetail(host, session);
    // The badge for "unsettled" is a count of tasks, not of steps; keep it current.
    refreshTasksBadgeOnly();
  } catch (error) {
    replace(host, el('div', { class: 'empty-state' }, [
      el('p', { text: 'This task could not be read.' }),
      el('p', { class: 'fine', text: error.message }),
    ]));
  }
}

function refreshTasksBadgeOnly() {
  const unsettled = state.tasks.filter((s) => !FINISHED_TASKS.has(s.status)).length;
  setBadge('tasks', unsettled);
}

const VOICE_SETUP_PERMISSIONS = {
  'device.app.launch_native': { scope: 'system.app.launch', risk: 'medium', label: 'app launch' },
  'device.app.status_native': { scope: 'system.app.status', risk: 'low', label: 'app status' },
  'browser.open': { scope: 'browser.control', risk: 'low', label: 'browser' },
  'browser.search': { scope: 'search.web', risk: 'low', label: 'web search' },
};

function renderTaskDetail(host, session) {
  const inVoice = host.id === 'voiceTaskResult';
  const head = el('div', { class: 'detail-head' }, [
    el('div', {}, [
      el('div', { class: 'detail-title', text: session.request || 'Task' }),
      el('div', { class: 'detail-meta', text: `${session.session_id} · ${session.actor_id} · updated ${timeAgo(session.updated_at)}` }),
    ]),
    el('div', { class: 'detail-actions' }, [
      stateChip(session.status),
    ]),
  ]);

  const actions = el('div', { class: 'btn-row' });
  const next = (session.steps || []).find((step) => !['succeeded', 'succeeded_unverified', 'failed', 'denied', 'cancelled', 'skipped'].includes(step.state));
  const terminal = FINISHED_TASKS.has(session.status)
    || (session.status === 'needs_review' && !next);

  const automaticWaiting = next?.state === 'awaiting_approval'
    && state.automaticApprovals.get(next.execution_id) === session.session_id;
  const simulated = inVoice && session.status === 'needs_review'
    && next?.error?.startsWith('This action is a simulation.');
  if (!terminal && !automaticWaiting && !simulated) {
    const run = el('button', { class: 'btn primary', type: 'button' });
    run.textContent = next && next.state === 'awaiting_approval'
      ? 'Check again after approving'
      : (next ? `Run: ${next.label}` : 'Advance one step');
    run.disabled = state.runningTasks.has(session.session_id);
    run.addEventListener('click', () => runSession(session.session_id, run, inVoice));
    actions.append(run);
  }
  if (session.status === 'awaiting_approval' && inVoice) {
    const review = el('button', { class: 'btn ghost', type: 'button', text: 'Review approvals' });
    review.addEventListener('click', () => showView('approvals'));
    actions.append(review);
  }
  const missing = session.steps?.find((step) => Object.hasOwn(VOICE_SETUP_PERMISSIONS, step.action_type)
    && step.state === 'denied' && step.error?.includes('missing permission(s)'));
  if (inVoice && missing) {
    const { scope, risk, label } = VOICE_SETUP_PERMISSIONS[missing.action_type];
    const permission = el('button', {
      class: 'btn ghost', type: 'button', text: `Set up ${label} permission`,
    });
    permission.addEventListener('click', () => {
      showView('approvals');
      byId('consentScope').value = scope;
      byId('consentRisk').value = risk;
      byId('consentGrant').focus();
    });
    actions.append(permission);
  }
  if (session.status === 'needs_review') {
    actions.append(el('p', { class: 'fine', text: simulated
      ? 'This command matched a simulated action. Edit the command to try a supported action.'
      : 'Automatic execution paused. Review the steps before continuing.' }));
  }
  if (session.request && (terminal || session.status === 'needs_review')) {
    const edit = el('button', { class: 'btn ghost', type: 'button', text: inVoice ? 'Edit command' : 'Reuse command' });
    edit.addEventListener('click', () => {
      if (!inVoice) showView('voice');
      const transcript = byId('voiceTranscript');
      transcript.value = session.request;
      transcript.focus();
    });
    actions.append(edit);
  }

  if (!terminal) {
    const cancel = el('button', { class: 'btn danger ghost', type: 'button', text: 'Stop this task' });
    cancel.addEventListener('click', () => cancelSession(session.session_id, inVoice));
    actions.append(cancel);
  }

  const steps = el('ol', { class: 'steps' });
  for (const step of session.steps || []) {
    steps.append(renderStep(step));
  }

  const parts = [head];
  if (session.description) {
    parts.push(el('pre', { class: 'mono', text: session.description }));
  }
  parts.push(actions);

  // A diagnosis plan carries a third step that is described but never dispatched (no
  // action type). Once the read it depends on has settled, the explanation is one
  // deliberate click away -- the request itself is what consents to sending the
  // bounded excerpt, so it is never sent as a side effect of opening a task.
  const logStep = (session.steps || []).find(
    (step) => step.action_type === 'device.process.logs',
  );
  const analysisStep = (session.steps || []).find((step) => !step.action_type);
  // "Settled", not "succeeded": a read that ran but whose result was not independently
  // verified is still a read, and the server draws its excerpt from the execution's
  // output either way. Refusing to explain it would hide evidence the user can see.
  if (analysisStep && logStep && SETTLED.has(logStep.state)) {
    const explain = el('button', { class: 'btn ghost', type: 'button' });
    explain.textContent = analysisStep.label || 'Explain the latest error';
    explain.addEventListener('click', () => diagnoseSession(session, explain, inVoice));
    actions.append(explain);
  }

  parts.push(steps);

  const diagnosis = state.diagnoses.get(session.session_id);
  if (diagnosis) {
    parts.push(renderDiagnosis(diagnosis));
  }

  if (session.summary) {
    parts.push(el('p', { class: 'fine', text: session.summary }));
  }
  replace(host, parts);
}

function renderStep(step) {
  const body = el('div', { class: 'step-body' });
  const rows = [];
  if (step.when) {
    // A condition is an object ({"step": 0, "exit_code": 0}), not a sentence. Handing
    // it to `text` renders "[object Object]", which looks like a bug in the step
    // rather than a rule attached to it.
    const when = typeof step.when === 'string' ? step.when : JSON.stringify(step.when);
    if (when && when !== '{}') rows.push(['when', when]);
  }
  if (step.execution_id) {
    rows.push(['execution', step.execution_id]);
    const details = el('button', { class: 'btn ghost small', type: 'button', text: 'Execution details and output' });
    details.addEventListener('click', () => {
      showView('live');
      loadExecution(step.execution_id);
    });
    body.append(details);
  }
  if (step.verified !== null && step.verified !== undefined) rows.push(['verified', step.verified ? 'yes' : 'no']);
  if (step.error) rows.push(['error', step.error]);
  if (rows.length) body.append(kvTable(rows));
  if (step.observed) {
    body.append(el('div', { class: 'fine', text: 'Observed (from what the provider returned):' }));
    body.append(el('pre', { class: 'mono', text: JSON.stringify(step.observed, null, 2) }));
  }
  if (step.parameters && Object.keys(step.parameters).length) {
    body.append(el('div', { class: 'fine', text: 'This step would ask for:' }));
    body.append(el('pre', { class: 'mono', text: JSON.stringify(step.parameters, null, 2) }));
  }

  const node = el('li', { class: 'step', dataset: { s: step.state } }, [
    el('div', { class: 'step-head' }, [
      el('span', { class: 'step-index', text: String((step.index ?? 0) + 1) }),
      el('span', { class: 'step-label', text: step.label || '(unlabelled)' }),
      el('span', { class: 'step-action', text: step.action_type || '' }),
      stateChip(step.state),
    ]),
    body,
  ]);
  return node;
}

function voiceRunReport(session) {
  if (session.status === 'awaiting_approval') return 'Waiting for your approval.';
  if (session.status === 'needs_review') return 'Task paused for review.';
  if (session.status !== 'completed') return `Task status: ${session.status.replaceAll('_', ' ')}.`;
  const steps = session.steps || [];
  const step = steps[0];
  if (steps.length === 1 && step.action_type === 'device.app.status_native' && step.verified === true
    && typeof step.observed?.running === 'boolean' && typeof step.observed.app === 'string') {
    return `${step.label?.replace(/^Check /, '') || step.observed.app} is ${step.observed.running ? 'running' : 'closed'}.`;
  }
  const search = steps.find((item) => item.action_type === 'browser.search' && item.verified === true);
  if (Number.isInteger(search?.observed?.count)) {
    const count = search.observed.count;
    return count === 0
      ? 'The search page opened. I could not confirm any result links.'
      : `I found ${count} result ${count === 1 ? 'link' : 'links'} in the browser. Prices and sellers still need checking.`;
  }
  const verified = steps.filter((item) => item.state === 'succeeded' && item.verified === true).length;
  return verified > 1 ? `Task completed. ${verified} steps verified.` : 'Task completed.';
}

async function runSession(sessionId, button, inVoice = false, automatic = false) {
  if (!requireBackend() || state.runningTasks.has(sessionId)) return;
  state.runningTasks.add(sessionId);
  const original = button?.textContent;
  if (button) {
    button.disabled = true;
    button.textContent = 'Working…';
  }
  // Subscribe before asking, exactly as the VoiceOS page does: live output is
  // transient and never replayed, so a stream opened after the answer misses it.
  try {
    const body = { actor: IDENTITY };
    if (automatic) body.stop_on_unverified = true;
    const updated = await POST(`/voiceos/sessions/${encodeURIComponent(sessionId)}/run`, body, { long: true });
    if (automatic && updated.status === 'awaiting_approval') {
      const waiting = updated.steps?.find((step) => step.state === 'awaiting_approval');
      if (waiting?.execution_id) {
        state.automaticApprovals.set(waiting.execution_id, sessionId);
      }
    }
    if (inVoice && state.voiceTask === sessionId) {
      const report = voiceRunReport(updated);
      renderReply(byId('voiceReply'), { kind: 'task', session_id: sessionId, reply: report });
      renderTaskDetail(byId('voiceTaskResult'), updated);
      if (automatic) speakVoiceReply(voiceController(), report, updated.status === 'completed');
    }
    if (updated.status === 'awaiting_approval') {
      toast('This step is parked until you approve it. Nothing else will run before that.', '');
      if (!inVoice) showView('approvals');
    } else {
      toast(`Task status: ${updated.status}.`, updated.status === 'completed' ? 'good' : '');
    }
  } catch (error) {
    if (inVoice && state.voiceTask === sessionId) {
      renderReply(byId('voiceReply'), {
        kind: 'task', session_id: sessionId, reply: `Could not run the task. ${error.message}`,
      });
    }
    toast(`The step was refused: ${error.message}`, 'bad');
    if (button) {
      button.disabled = false;
      button.textContent = original;
    }
  } finally {
    state.runningTasks.delete(sessionId);
    if (inVoice && state.voiceTask === sessionId) await refreshVoiceResult();
    else if (!inVoice && state.selectedTask === sessionId) await loadTask(sessionId);
  }
}

async function cancelSession(sessionId, inVoice = false) {
  if (!requireBackend()) return;
  try {
    const answer = await POST(`/voiceos/sessions/${encodeURIComponent(sessionId)}/cancel`);
    if (answer.status === 'nothing_running') {
      toast('No step of this task is live, so there is nothing to stop.', '');
    } else {
      // "requested" is what the server said, and it is what this says.
      toast('Stop requested. A task stops when the step it is running stops.', '');
    }
    if (inVoice) await refreshVoiceResult();
    else await loadTask(sessionId);
  } catch (error) {
    toast(`The stop request failed: ${error.message}`, 'bad');
  }
}

// ---------------------------------------------------------------------------
// Diagnosis: explain a log this task already read
// ---------------------------------------------------------------------------
async function diagnoseSession(session, button, inVoice = false) {
  if (!requireBackend()) return;
  const original = button.textContent;
  button.disabled = true;
  button.textContent = 'Reading…';
  try {
    // An explicit click is the consent to send the bounded excerpt, which is why the
    // route is never called on load. What crosses the wire is the redacted tail of a
    // log this session was already authorised to read -- never a file path.
    const body = await POST(
      `/voiceos/sessions/${encodeURIComponent(session.session_id)}/diagnose`,
      { actor: IDENTITY },
      { long: true },
    );
    state.diagnoses.set(session.session_id, body);
    if (inVoice) await refreshVoiceResult();
    else renderTaskDetail(byId('taskDetail'), session);
    if (body.status === 'diagnosed') {
      toast('The evidence was read and explained. Nothing was changed.', 'good');
    } else if (body.status === 'unavailable') {
      // Not a failure of the app: the deterministic scan still ran, and saying "no
      // model is configured" is the honest answer rather than an invented cause.
      toast('No model is configured, so the evidence is shown without an explanation.', '');
    } else {
      toast('There is nothing to explain yet: no log has been read by this task.', '');
    }
  } catch (error) {
    toast(`The explanation could not be produced: ${error.message}`, 'bad');
    button.disabled = false;
    button.textContent = original;
  }
}

function renderDiagnosis(body) {
  const diagnosed = body.status === 'diagnosed';
  const evidence = body.evidence || {};
  const source = body.source || {};

  const rows = [
    ['status', diagnosed ? `explained by ${body.model || 'the configured model'}` : body.status],
    ['evidence', body.evidence_summary || 'nothing was read'],
    ['from', [
      source.label || 'the latest read',
      source.execution_id ? `execution ${source.execution_id}` : '',
      body.computer ? `on ${body.computer}` : '',
    ].filter(Boolean).join(' · ')],
  ];
  if (evidence.latest_error_line !== null && evidence.latest_error_line !== undefined) {
    rows.push(['latest error', `line ${evidence.latest_error_line}: ${evidence.latest_error}`]);
  }
  if (evidence.exception) rows.push(['exception', evidence.exception]);
  if (body.confidence) rows.push(['confidence', body.confidence]);

  const parts = [
    el('h3', { class: 'card-title spaced', text: 'Analysis' }),
    // The sentence first: it always leads with what was actually read, on which
    // computer, so a reader who stops here still cannot mistake a guess for a fact.
    el('p', { text: body.description || '' }),
    kvTable(rows),
  ];

  if (diagnosed) {
    if (body.finding) {
      parts.push(el('div', { class: 'fine', text: 'What the evidence shows' }));
      parts.push(el('p', { text: body.finding }));
    }
    if (body.likely_cause) {
      parts.push(el('div', { class: 'fine', text: 'Likely cause' }));
      parts.push(el('p', { text: body.likely_cause }));
    }
    if (body.fixes && body.fixes.length) {
      parts.push(el('div', { class: 'fine', text: 'Suggested fixes (not applied)' }));
      const list = el('ol', { class: 'steps' });
      for (const fix of body.fixes) {
        list.append(el('li', { class: 'step' }, [el('div', { class: 'step-body', text: fix })]));
      }
      parts.push(list);
    }
    if (body.unanswered) {
      parts.push(el('p', { class: 'fine', text: `Left unanswered: ${body.unanswered}` }));
    }
  } else if (body.analyst) {
    parts.push(el('p', { class: 'fine', text: body.analyst }));
  }
  if (body.notes && body.notes.length) {
    parts.push(el('p', { class: 'fine', text: body.notes.join(' ') }));
  }
  parts.push(el('p', {
    class: 'fine',
    text: 'This is an explanation of a log that was already read. Nothing was run, approved or changed.',
  }));
  return el('div', { class: 'notice' }, parts);
}

// ---------------------------------------------------------------------------
// LIVE CONTROL
// ---------------------------------------------------------------------------
async function refreshLive() {
  await refreshAppState();
  if (!state.backendReady) {
    replace(byId('liveList'), el('li', { class: 'empty', text: 'Backend offline.' }));
    return;
  }
  try {
    const records = await GET('/executions?limit=50');
    state.executions = records;
    byId('liveCount').textContent = String(records.length);
    const running = records.filter((record) => !isTerminal(record.state)).length;
    setBadge('live', running);
    replace(byId('liveList'), records.length ? records.map((record) => {
      const row = el('li', {
        class: 'row',
        dataset: { id: record.execution_id },
        'aria-selected': record.execution_id === state.selectedExecution ? 'true' : 'false',
      }, [
        el('div', { class: 'row-main' }, [
          el('div', { class: 'row-title', text: record.execution_id }),
          el('div', { class: 'row-sub', text: `${timeAgo(record.started_at || record.updated_at)}${record.error ? ` · ${record.error}` : ''}` }),
        ]),
        stateChip(record.state),
      ]);
      row.addEventListener('click', () => {
        state.selectedExecution = record.execution_id;
        for (const other of byId('liveList').children) other.setAttribute('aria-selected', 'false');
        row.setAttribute('aria-selected', 'true');
        loadExecution(record.execution_id);
      });
      return row;
    }) : el('li', { class: 'empty', text: 'No executions yet.' }));
  } catch (error) {
    toast(`Could not read executions: ${error.message}`, 'bad');
  }
}

const TERMINAL = new Set([
  'success', 'failed', 'cancelled', 'denied', 'interrupted', 'timeout', 'partially_completed',
]);
const isTerminal = (stateValue) => TERMINAL.has(String(stateValue));

async function loadExecution(executionId) {
  state.selectedExecution = executionId;
  const host = byId('liveDetail');
  const output = state.output.get(executionId) || new Map();
  state.output.set(executionId, output);

  // Watch this execution's stream. The global subscription stays for the lists; a
  // scoped one is what carries its transient output. Reconnecting is main.js's job,
  // and the server's cursor keeps the resumed position honest.
  try { await bridge.startStream({ executionId }); } catch { /* status pill reports it */ }

  try {
    const record = await GET(`/executions/${encodeURIComponent(executionId)}`);
    renderExecutionDetail(host, record, output);
  } catch (error) {
    replace(host, el('div', { class: 'empty-state' }, [
      el('p', { text: 'This execution could not be read.' }),
      el('p', { class: 'fine', text: error.message }),
    ]));
  }
}

function nodeEntries(record) {
  const results = record.results && typeof record.results === 'object' ? record.results : {};
  const states = record.node_states && typeof record.node_states === 'object' ? record.node_states : {};
  const ids = new Set([...Object.keys(results), ...Object.keys(states)]);
  return [...ids].map((id) => ({
    id,
    state: (results[id] && results[id].state) || states[id] || 'unknown',
    view: results[id] || null,
  }));
}

function renderExecutionDetail(host, record, output) {
  const deviceLabel = record.device_name || record.device_id || 'this laptop';
  const head = el('div', { class: 'detail-head' }, [
    el('div', {}, [
      el('div', { class: 'detail-title', text: record.execution_id }),
      el('div', {
        class: 'detail-meta',
        text: `device ${deviceLabel} · actor ${record.actor_id} · started ${timeAgo(record.started_at)} · updated ${timeAgo(record.updated_at)}`,
      }),
    ]),
    el('div', { class: 'detail-actions' }, [stateChip(record.state)]),
  ]);

  const parts = [head];
  // "Copy Execution ID" copies what is on screen — the id the server minted.
  parts.push(el('dl', { class: 'kv' }, [
    el('dt', { text: 'Execution' }), el('dd', { text: record.execution_id }),
    el('dt', { text: 'Device' }), el('dd', { text: deviceLabel }),
    el('dt', { text: 'State' }), el('dd', { text: String(record.state).replace(/_/g, ' ') }),
    el('dt', { text: 'Cancel requested' }), el('dd', { text: record.cancel_requested ? 'yes' : 'no' }),
    el('dt', { text: 'Approval' }), el('dd', { text: approvalStatusText(record) }),
  ]));
  if (record.error) {
    parts.push(el('div', { class: 'notice bad', text: record.error }));
  }
  if (record.cancel_requested && !isTerminal(record.state)) {
    parts.push(el('div', { class: 'notice', text: 'Stop was requested. The action has not confirmed that it stopped yet.' }));
  }

  const rows = nodeEntries(record);
  if (!rows.length) {
    parts.push(el('div', { class: 'empty-state' }, [el('p', { text: 'No node results recorded yet.' })]));
  }
  for (const entry of rows) {
    parts.push(renderNode(entry));
  }

  const consoleBox = el('div', { class: 'console' });
  const consoleHead = el('div', { class: 'console-head' }, [
    el('span', { class: 'console-note', text: 'Live output — transient, never audited, never persisted' }),
  ]);
  const clearButton = el('button', { class: 'btn ghost small', type: 'button', text: 'Clear' });
  clearButton.addEventListener('click', () => {
    output.clear();
    renderOutputInto(outputHost, output);
  });
  consoleHead.append(clearButton);
  const outputHost = el('div');
  consoleBox.append(consoleHead, outputHost);
  renderOutputInto(outputHost, output);
  parts.push(consoleBox);

  const actions = el('div', { class: 'btn-row' });
  if (!isTerminal(record.state)) {
    const cancel = el('button', { class: 'btn danger ghost', type: 'button', text: 'Stop this execution' });
    cancel.addEventListener('click', async () => {
      cancel.disabled = true;
      try {
        const answer = await POST(`/executions/${encodeURIComponent(record.execution_id)}/cancel`);
        toast(answer.status === 'requested'
          ? 'Stop requested. The executor acts on it at its next check.'
          : `Stop: ${String(answer.status).replace(/_/g, ' ')}.`, answer.status === 'requested' ? '' : 'bad');
        loadExecution(record.execution_id);
      } catch (error) {
        toast(`The stop request failed: ${error.message}`, 'bad');
        cancel.disabled = false;
      }
    });
    actions.append(cancel);
  }
  const refresh = el('button', { class: 'btn ghost', type: 'button', text: 'Re-read record' });
  refresh.addEventListener('click', () => loadExecution(record.execution_id));
  actions.append(refresh);

  const openDetails = el('button', { class: 'btn ghost', type: 'button', text: 'Open Details' });
  openDetails.addEventListener('click', () => {
    showView('live');
    loadExecution(record.execution_id);
  });
  actions.append(openDetails);

  // The clipboard is a plain browser API and holds an id the server already minted —
  // nothing here reads or writes anything else.
  const copyId = el('button', { class: 'btn subtle', type: 'button', text: 'Copy Execution ID' });
  copyId.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(record.execution_id);
      toast('Execution id copied.', 'good');
    } catch (error) {
      toast(`The clipboard was not available: ${error.message}`, 'bad');
    }
  });
  actions.append(copyId);
  parts.push(actions);

  replace(host, parts);
}

/**
 * What the record itself says about approvals. Derived from the node states that are
 * present — it never claims an approval was granted when no node recorded one.
 */
function approvalStatusText(record) {
  const states = Object.values(record.node_states || {}).map(String);
  if (states.includes('awaiting_approval')) return 'waiting for you';
  if (states.includes('denied')) return 'denied';
  return 'none pending';
}

function renderNode(entry) {
  const view = entry.view || {};
  const rows = [];
  if (view.provider) rows.push(['provider', view.provider]);
  if (view.attempts !== undefined) rows.push(['attempts', String(view.attempts)]);
  if (view.duration_ms !== undefined && view.duration_ms !== null) rows.push(['duration', `${view.duration_ms} ms`]);
  if (view.simulated) rows.push(['simulated', 'yes — nothing touched the machine']);
  if (view.failure_kind) rows.push(['failure kind', view.failure_kind]);
  if (view.error) rows.push(['error', view.error]);
  if (view.verification) {
    rows.push(['verified', view.verification.verified ? 'yes' : 'no']);
    if (view.verification.method) rows.push(['method', view.verification.method]);
    if (view.verification.detail) rows.push(['detail', view.verification.detail]);
  }
  if (view.recovery && Object.keys(view.recovery).length) {
    rows.push(['recovery', JSON.stringify(view.recovery)]);
  }

  const body = el('div', { class: 'step-body' });
  if (rows.length) body.append(kvTable(rows));
  if (view.result && Object.keys(view.result).length) {
    body.append(el('div', { class: 'fine', text: 'Projected result (content never crosses this boundary):' }));
    body.append(el('pre', { class: 'mono', text: JSON.stringify(view.result, null, 2) }));
  }

  return el('li', { class: 'step', dataset: { s: entry.state } }, [
    el('div', { class: 'step-head' }, [
      el('span', { class: 'step-label', text: entry.id }),
      el('span', { class: 'step-action', text: view.action_type || '' }),
      stateChip(entry.state),
    ]),
    body,
  ]);
}

function renderOutputInto(host, output) {
  clear(host);
  if (!output.size) {
    host.append(el('div', { class: 'empty', text: 'No output has arrived for this execution.' }));
    return;
  }
  for (const [nodeId, buffers] of output) {
    for (const [streamName, text] of Object.entries(buffers)) {
      host.append(el('div', {
        class: 'console-note',
        text: `${nodeId} · ${streamName}`,
      }));
      host.append(el('pre', { class: streamName === 'stderr' ? 'stderr' : '', text }));
    }
  }
}

function handleStreamEvent(event) {
  const data = event && event.data;
  if (!data) return;
  const type = data.event_type || event.type;

  if (type === 'execution.output') {
    const executionId = data.execution_id;
    const payload = data.payload || {};
    if (!executionId || !payload.node_id) return;
    // The accessibility walk's element list arrives on the same transient channel a
    // file body does. It is not stdout, so it does not belong in the output console --
    // but it is the *only* place element names exist, because the result payload is
    // projected through a content allowlist that keeps metadata and drops names.
    if (payload.stream === 'elements') {
      applyInspectedElements(executionId, payload);
      return;
    }
    if (payload.stream === 'elements.note') {
      // A note about the walk, not program output: it says the walk stopped at its
      // own ceiling. Appending it to the stdout console would show the reader a
      // sentence in a pane that only ever holds what a command printed.
      applyElementsNote(executionId, payload.chunk);
      return;
    }
    const buffers = state.output.get(executionId) || {};
    const streamName = payload.stream === 'stderr' ? 'stderr' : 'stdout';
    const nodeBuffers = buffers[payload.node_id] || {};
    const previous = nodeBuffers[streamName] || '';
    let next = previous + String(payload.chunk || '');
    // A bounded transcript: this is a view, not an archive.
    if (next.length > 200_000) next = `…earlier output dropped…\n${next.slice(-200_000)}`;
    nodeBuffers[streamName] = next;
    buffers[payload.node_id] = nodeBuffers;
    state.output.set(executionId, buffers);
    if (state.view === 'live' && state.selectedExecution === executionId) {
      const host = byId('liveDetail');
      const box = host.querySelector('.console > div');
      if (box) renderOutputInto(box, new Map(Object.entries(buffers).map(([k, v]) => [k, v])));
    }
    return;
  }

  // Everything else is a fact about a record; the cheapest honest response is to
  // re-read the view, not to patch local state from an event payload.
  // The executions history (history.js) owns its own dedupe + transient rules;
  // delegating here keeps both views consistent without duplicating them.
  if (typeof ActionMeshHistory !== 'undefined' && ActionMeshHistory.handleStreamEvent) {
    ActionMeshHistory.handleStreamEvent(event);
  }
  if (state.view === 'live') {
    if (data.execution_id && state.selectedExecution === data.execution_id) {
      loadExecution(data.execution_id);
    }
    refreshLive();
  } else if (state.view === 'approvals') {
    refreshApprovals();
  } else if (state.view === 'tasks') {
    refreshTasks();
  } else if (state.view === 'home') {
    refreshHome();
  }
}

function applyStreamStatus(status) {
  const pill = byId('streamPill');
  const value = status && status.state ? status.state : 'closed';
  pill.dataset.state = value;
  if (value === 'open') pill.textContent = 'stream live';
  else if (value === 'reconnecting') pill.textContent = 'reconnecting…';
  else pill.textContent = 'stream closed';
}

// ---------------------------------------------------------------------------
// APPROVALS
// ---------------------------------------------------------------------------
async function refreshApprovals() {
  await refreshAppState();
  const host = byId('approvalList');
  if (!state.backendReady) {
    replace(host, el('p', { class: 'empty', text: 'Backend offline.' }));
    replace(byId('consentList'), el('li', { class: 'empty', text: 'Backend offline.' }));
    return;
  }
  // Both halves of this view are about the same question -- what has the user
  // already decided, and what is still waiting on them -- so they are read
  // together and a failure in one does not blank the other.
  await refreshConsents();
  try {
    const entries = await GET('/approvals');
    state.approvals = entries;
    const count = entries.reduce((sum, entry) => sum + (entry.actions || []).length, 0);
    byId('approvalCount').textContent = `${count} waiting`;
    setBadge('approvals', count);
    await refreshTasksBadgeOnly();
    if (!entries.length) {
      replace(host, el('p', { class: 'empty', text: 'Nothing is waiting. That is the normal state.' }));
      return;
    }
    // Two supporting reads, both optional: the execution record names the device the
    // run was routed to, and the capability registry names the permissions the action
    // asks for. When either is unavailable the card says "unknown" — it does not fill
    // the gap with a guess, because this is the card a person approves from.
    const [records, capabilities] = await Promise.all([
      Promise.allSettled(entries.map((entry) => GET(`/executions/${encodeURIComponent(entry.execution_id)}`))),
      GET('/capabilities').catch(() => []),
    ]);
    const byExecution = new Map();
    records.forEach((outcome, index) => {
      if (outcome.status === 'fulfilled') byExecution.set(entries[index].execution_id, outcome.value);
    });
    const permissionsFor = new Map();
    for (const cap of capabilities || []) {
      if (cap && cap.action) permissionsFor.set(cap.action, cap.permissions || []);
    }
    replace(host, entries.map((entry) => renderApproval(entry, {
      record: byExecution.get(entry.execution_id) || null,
      permissionsFor,
    })));
  } catch (error) {
    replace(host, el('p', { class: 'empty', text: `Could not read approvals: ${error.message}` }));
  }
}

function renderApproval(entry, context = {}) {
  const { record = null, permissionsFor = new Map() } = context;
  const card = el('div', { class: 'approval' });
  const deviceLabel = record
    ? (record.device_name || record.device_id || 'this laptop')
    : 'unknown — the record could not be read';
  card.append(el('div', { class: 'approval-head' }, [
    el('span', { class: 'approval-title', text: 'An execution is parked here' }),
    el('span', { class: 'chip', text: entry.execution_id }),
    el('span', { class: 'chip', text: `actor ${entry.actor}` }),
    el('span', { class: 'chip', text: `device ${deviceLabel}` }),
  ]));
  card.append(el('p', { class: 'fine', text: 'Approving is matched on the action id the server minted; nothing else in that run is authorised.' }));

  for (const action of entry.actions || []) {
    const block = el('div', { class: 'action-block' });
    block.append(el('div', { class: 'approval-head' }, [
      el('span', { class: 'approval-title', text: action.action_type }),
      riskChip(action.risk_level),
      el('span', { class: 'fine', text: `action ${action.action_id}` }),
    ]));
    // The permissions shown are the ones the registry declares for this action type,
    // read from `/capabilities`. An action the registry has no entry for is said to
    // be unknown rather than shown as "none".
    const declared = permissionsFor.get(action.action_type);
    block.append(el('dl', { class: 'kv' }, [
      el('dt', { text: 'Target device' }), el('dd', { text: deviceLabel }),
      el('dt', { text: 'Risk' }), el('dd', { text: action.risk_level }),
      el('dt', { text: 'Required permission' }), el('dd', {
        text: declared === undefined
          ? 'not declared by the registry'
          : (declared.length ? declared.join(', ') : 'none beyond the actor'),
      }),
      el('dt', { text: 'Execution identity' }), el('dd', { text: record ? record.actor_id : entry.actor }),
    ]));
    const params = el('div', { class: 'params' });
    params.append(el('div', { class: 'fine', text: 'What it would be asked to do (the request, not its result):' }));
    params.append(el('pre', { class: 'mono', text: JSON.stringify(action.parameters, null, 2) }));
    block.append(params);

    const row = el('div', { class: 'btn-row' });
    const approve = el('button', { class: 'btn primary', type: 'button', text: 'Approve this action' });
    approve.addEventListener('click', () => decide(entry, action, 'approved', approve));
    const reject = el('button', { class: 'btn danger ghost', type: 'button', text: 'Reject' });
    reject.addEventListener('click', () => decide(entry, action, 'rejected', reject));
    const viewRun = el('button', { class: 'btn ghost', type: 'button', text: 'View Details' });
    viewRun.addEventListener('click', () => {
      showView('live');
      loadExecution(entry.execution_id);
    });
    // Cancelling is the stop the executor already understands; it is the same
    // server-side operation as Live Control's, not a new authority.
    const cancelRun = el('button', { class: 'btn subtle', type: 'button', text: 'Cancel Execution' });
    cancelRun.addEventListener('click', async () => {
      cancelRun.disabled = true;
      try {
        const answer = await POST(`/executions/${encodeURIComponent(entry.execution_id)}/cancel`);
        toast(answer.status === 'requested' ? 'Cancel requested.' : `Cancel: ${answer.status}.`, '');
        await refreshApprovals();
      } catch (error) {
        toast(`The cancel request failed: ${error.message}`, 'bad');
        cancelRun.disabled = false;
      }
    });
    row.append(approve, reject, viewRun, cancelRun);
    block.append(row);
    card.append(block);
  }
  return card;
}

async function decide(entry, action, status, button) {
  if (!requireBackend()) return;
  button.disabled = true;
  try {
    let voiceSession = state.automaticApprovals.get(entry.execution_id);
    if (!voiceSession && status === 'approved') {
      const tasks = await GET('/voiceos/sessions?limit=100').catch(() => []);
      voiceSession = tasks.find((task) => task.actor_id === IDENTITY.id
        && task.automatic_approval_execution_id === entry.execution_id)?.session_id;
    }
    const answer = await POST('/approvals', {
      action_id: action.action_id,
      status,
      action_type: action.action_type,
      summary: status === 'approved' ? 'approved from the desktop shell' : 'rejected from the desktop shell',
      execution_id: entry.execution_id,
    });
    const resumedState = answer.execution ? answer.execution.state : 'not resumed';
    toast(
      status === 'approved'
        ? `Approved. Execution state: ${resumedState}.`
        : 'Rejected. The run will not proceed past that action.',
      status === 'approved' ? 'good' : '',
    );
    if (voiceSession) {
      state.automaticApprovals.delete(entry.execution_id);
      if (status === 'approved' && answer.execution) {
        const session = await GET(`/voiceos/sessions/${encodeURIComponent(voiceSession)}`).catch(() => null);
        if (session?.steps?.some((step) => step.execution_id === entry.execution_id && step.state === 'succeeded')
          && ['proposed', 'completed'].includes(session.status)) {
          if (!state.voiceTask) state.voiceTask = voiceSession;
          void runSession(voiceSession, null, true, true);
        }
      }
      else if (state.voiceTask === voiceSession) {
        renderReply(byId('voiceReply'), {
          kind: 'task', session_id: voiceSession, reply: 'Approval declined.',
        });
        await refreshVoiceResult();
      }
    }
    await refreshApprovals();
  } catch (error) {
    toast(`The decision was not recorded: ${error.message}`, 'bad');
    button.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// STANDING PERMISSIONS
// ---------------------------------------------------------------------------
// A consent is the user's standing answer to a question the policy would otherwise
// ask every run. It is separate from an approval on purpose: a grant says "this
// scope is fine up to this risk", while an approval authorises one named action.
// The page therefore never manufactures either -- it shows what the server holds
// and asks the server to change it.
async function refreshConsents() {
  const host = byId('consentList');
  if (!state.backendReady) {
    byId('consentCount').textContent = '0';
    replace(host, el('li', { class: 'empty', text: 'Backend offline.' }));
    return;
  }
  try {
    const records = await GET(`/consents?actor_id=${encodeURIComponent(IDENTITY.id)}`);
    byId('consentCount').textContent = String(records.length);
    if (!records.length) {
      replace(host, el('li', {
        class: 'empty',
        text: 'No standing permissions. Every action that needs one will wait for approval.',
      }));
      return;
    }
    replace(host, records.map((record) => {
      // The scope is the row's identity, and it is the only thing a revoke needs:
      // the server decides which actor's grant that scope names, and refuses one
      // that belongs to somebody else.
      const revoke = el('button', { class: 'btn danger ghost small', type: 'button', text: 'Remove' });
      revoke.addEventListener('click', () => revokeConsent(record.scope, revoke));
      return el('li', { class: 'row' }, [
        el('div', { class: 'row-main' }, [
          el('div', { class: 'row-title mono', text: record.scope }),
          el('div', {
            class: 'row-sub',
            text: `${record.granted_by} · up to ${record.max_risk}-risk · granted ${timeAgo(record.granted_at)}`,
          }),
        ]),
        el('div', { class: 'row-tail' }, [riskChip(record.max_risk), revoke]),
      ]);
    }));
  } catch (error) {
    replace(host, el('li', { class: 'empty', text: `Could not read permissions: ${error.message}` }));
  }
}

async function grantConsent(button) {
  if (!requireBackend()) return;
  const field = byId('consentScope');
  const scope = field.value.trim();
  if (!scope) {
    toast('Name the permission first — for example device.process.read.', 'bad');
    field.focus();
    return;
  }
  button.disabled = true;
  try {
    await POST('/consents', {
      scope,
      actor_id: IDENTITY.id,
      reason: 'granted from the desktop shell',
      max_risk: byId('consentRisk').value,
    });
    field.value = '';
    toast(`Granted ${scope}.`, 'good');
    await refreshConsents();
  } catch (error) {
    toast(`That permission was not granted: ${error.message}`, 'bad');
  } finally {
    button.disabled = false;
  }
}

async function revokeConsent(scope, button) {
  if (!requireBackend()) return;
  button.disabled = true;
  try {
    await DELETE(`/consents/${encodeURIComponent(scope)}?actor_id=${encodeURIComponent(IDENTITY.id)}`);
    toast(`Withdrew ${scope}. The next action that needs it will wait for you.`, 'good');
    await refreshConsents();
  } catch (error) {
    toast(`That permission was not removed: ${error.message}`, 'bad');
    button.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// AUTOMATIONS (C39)
// ---------------------------------------------------------------------------
//
// The whole view is built around one claim: saving a plan and running a plan are
// different acts. So a draft is never stored, saving is its own button, and running
// is a separate press that goes through the same policy engine as anything else.
// Nothing rendered here is a promise that a step succeeded — the run record is the
// only thing that says that, and it is read back from the server after the run.

/** Human words for what a step's recorded outcome means. Never optimistic. */
// The run states `automation_run.run_state_for` can produce, plus the scheduler's
// two: `claim` writes a row before a step is awaited, and `reconcile_claimed` turns
// one whose process never started into `missed`. Words rather than codes, because
// "denied" means *something* refused -- a missing grant, or a step with no computer --
// and the row below it carries the sentence saying which.
const RUN_STATE_WORDS = {
  succeeded: 'every step succeeded',
  failed: 'a step failed',
  denied: 'refused — see the step below',
  cancelled: 'cancelled',
  interrupted: 'interrupted by a restart',
  partial: 'some steps did not run',
  awaiting_approval: 'waiting for your approval',
  running: 'running now',
  claimed: 'claimed, not yet started',
  missed: 'missed — the occurrence passed while the app was not running',
  skipped: 'not run',
};

/**
 * A schedule in words. Both the draft review and the saved-plan list show a schedule,
 * and neither should print the stored shape's field names at a person: `kind: 'daily'`
 * is "every day at 09:00", not "every daily at 09:00". An unrecognised or empty shape
 * says so rather than inventing a cadence.
 */
function scheduleWords(schedule) {
  const s = schedule || {};
  if (!Object.keys(s).length) return '';
  if (s.kind === 'interval') {
    const seconds = Number(s.seconds) || 0;
    if (seconds % 3600 === 0 && seconds >= 3600) {
      const hours = seconds / 3600;
      return `every ${hours === 1 ? 'hour' : `${hours} hours`}`;
    }
    const minutes = Math.round(seconds / 60) || 1;
    return `every ${minutes === 1 ? 'minute' : `${minutes} minutes`}`;
  }
  if (s.kind === 'daily') return `every day at ${s.at || 'an unset time'}`;
  if (s.kind === 'weekly') {
    const days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
    const named = (s.days || []).map((d) => days[d] || d).join(', ');
    return named ? `${named} at ${s.at || 'an unset time'}` : 'weekly, with no day set';
  }
  return `a ${s.kind || 'unrecognised'} schedule`;
}

function triggerWords(workflow) {
  if (workflow.trigger_kind === 'event') {
    const event = workflow.event || {};
    const narrowed = event.state ? ` (state ${event.state})` : '';
    return `when ${event.event_type || 'an event'}${narrowed}`;
  }
  if (workflow.trigger_kind === 'schedule') {
    return scheduleWords(workflow.schedule) || 'a schedule with no cadence';
  }
  return 'run by hand only';
}

async function refreshAutomations() {
  await refreshAppState();
  const list = byId('automationList');
  if (!state.backendReady) {
    replace(list, el('li', { class: 'empty', text: 'Backend offline.' }));
    return;
  }
  try {
    const answer = await GET('/automations');
    const workflows = answer.workflows || [];
    byId('automationCount').textContent = String(workflows.length);
    setBadge('automations', workflows.filter((w) => w.enabled && w.trigger_kind !== 'manual').length);
    // The note is the server's own words about durability. Rendering it rather than
    // rewording it keeps the page from claiming a persistence the process lacks.
    byId('automationNotes').textContent = answer.note
      || 'A saved plan grants nothing. Every step is authorised again when it runs.';
    if (!workflows.length) {
      replace(list, el('li', {
        class: 'empty',
        text: 'No automations yet. Describe one on the right to see a draft.',
      }));
      return;
    }
    replace(list, workflows.map((workflow) => {
      const row = el('li', { class: 'row selectable', dataset: { id: workflow.workflow_id } }, [
        el('div', { class: 'row-main' }, [
          el('div', { class: 'row-title', text: workflow.name }),
          el('div', {
            class: 'row-sub',
            text: `${triggerWords(workflow)} · ${workflow.steps.length} step(s) · `
              + `${workflow.enabled ? 'armed' : 'disarmed'}`,
          }),
        ]),
        el('div', { class: 'row-tail' }, [
          workflow.last_state ? stateChip(workflow.last_state) : null,
          el('span', { class: 'chip', text: workflow.trigger_kind }),
        ]),
      ]);
      row.addEventListener('click', () => {
        state.selectedAutomation = workflow.workflow_id;
        renderAutomationDetail(workflow);
      });
      return row;
    }));
    if (state.selectedAutomation) {
      const still = workflows.find((w) => w.workflow_id === state.selectedAutomation);
      if (still) renderAutomationDetail(still);
    }
  } catch (error) {
    replace(list, el('li', { class: 'empty', text: `Could not read automations: ${error.message}` }));
  }
}

/**
 * The body a create or an edit sends.
 *
 * Built from the stored workflow and nothing else, so an edit can never widen a plan:
 * there is no field here for a grant, a permission, an actor or a risk ceiling, and
 * `POST`/`PUT /automations` refuse a body that carries one. `parameters` are copied
 * verbatim because they are what the plan actually does; the server re-validates every
 * one of them against the registry regardless of what this page believes.
 */
function workflowBody(workflow, overrides = {}) {
  return {
    name: overrides.name !== undefined ? overrides.name : workflow.name,
    description: overrides.description !== undefined ? overrides.description : (workflow.description || ''),
    steps: (overrides.steps || workflow.steps || []).map((step, index) => ({
      label: step.label,
      action: step.action_type || step.action,
      parameters: step.parameters || {},
      when: step.when || {},
      device_id: step.device_id || '',
      index,
    })),
    schedule: overrides.schedule !== undefined ? overrides.schedule : (workflow.schedule || {}),
    event: overrides.event !== undefined ? overrides.event : (workflow.event || {}),
    device_id: overrides.device_id !== undefined ? overrides.device_id : (workflow.device_id || ''),
    enabled: overrides.enabled !== undefined ? overrides.enabled : Boolean(workflow.enabled),
  };
}

/** The schedule editor's value, as the server's schedule shape. Empty = no trigger. */
function scheduleFromEditor(kindInput, atInput, intervalInput) {
  const kind = kindInput.value;
  if (kind === 'none') return {};
  if (kind === 'interval') {
    return { kind: 'interval', seconds: Number(intervalInput.value) || 0 };
  }
  const days = [...document.querySelectorAll('[data-sched-day]:checked')]
    .map((box) => Number(box.value));
  if (kind === 'weekly') return { kind: 'weekly', days, at: atInput.value.trim() };
  return { kind: 'daily', at: atInput.value.trim() };
}

/** Render one editable step: label, action id, and the parameters it stores. */
function stepEditor(step, index, removeSelf) {
  const parameters = Object.entries(step.parameters || {})
    .map(([key, value]) => `${key}=${Array.isArray(value) ? value.join(',') : value}`)
    .join(' ');
  const card = el('li', { class: 'row step-row' }, [
    el('div', { class: 'row-main' }, [
      el('div', { class: 'step-head' }, [
        el('span', { class: 'step-index', text: String(index + 1) }),
        el('div', { class: 'row-title', text: step.label || step.action_type }),
      ]),
      el('div', { class: 'row-sub', text: step.action_type }),
      parameters ? el('div', { class: 'row-sub mono', text: parameters }) : null,
      el('div', {
        class: 'row-sub',
        text: step.when && Object.keys(step.when).length
          ? `only after step ${Number(step.when.step) + 1}, when its evidence matches`
          : 'always runs',
      }),
    ]),
    el('div', { class: 'row-tail' }, [
      el('span', { class: 'chip', text: step.device_id || 'workflow target' }),
      removeSelf,
    ]),
  ]);
  return card;
}

function renderAutomationDetail(workflow) {
  const host = byId('automationSelected');
  const blocks = [
    el('div', { class: 'card-head' }, [
      el('h2', { class: 'card-title', text: workflow.name }),
      el('span', { class: 'chip', text: workflow.enabled ? 'armed' : 'disarmed' }),
      el('span', { class: 'chip', text: workflow.trigger_kind }),
    ]),
    el('p', { class: 'card-note', text: workflow.description_text || '' }),
  ];

  // --- the plan, as a visual step editor -----------------------------------
  // A copy is held here so editing is local until Save; the server's stored plan is
  // untouched by anything that happens in this list.
  let draftSteps = (workflow.steps || []).map((step) => ({
    label: step.label,
    action_type: step.action_type,
    parameters: { ...(step.parameters || {}) },
    when: { ...(step.when || {}) },
    device_id: step.device_id || '',
  }));
  const stepList = el('ul', { class: 'rows compact step-editor' });
  const renderSteps = () => {
    clear(stepList);
    if (!draftSteps.length) {
      stepList.append(el('li', {
        class: 'empty',
        text: 'Every step was removed. A plan needs one step to be saveable.',
      }));
      return;
    }
    draftSteps.forEach((step, index) => {
      const remove = el('button', {
        class: 'btn danger ghost small', type: 'button', text: 'Remove',
      });
      remove.addEventListener('click', () => {
        draftSteps = draftSteps.filter((_, position) => position !== index);
        renderSteps();
        markEdited();
      });
      stepList.append(stepEditor(step, index, remove));
    });
  };
  renderSteps();
  blocks.push(el('h3', { class: 'card-title spaced', text: 'Steps' }), stepList);

  // --- the schedule editor -------------------------------------------------
  const current = workflow.schedule || {};
  const kindInput = el('select', { class: 'form-input', id: 'autoSchedKind' }, [
    el('option', { value: 'none', text: 'Run by hand only' }),
    el('option', { value: 'interval', text: 'On an interval' }),
    el('option', { value: 'daily', text: 'Every day' }),
    el('option', { value: 'weekly', text: 'On chosen weekdays' }),
  ]);
  kindInput.value = current.kind || 'none';
  const atInput = el('input', {
    class: 'form-input', id: 'autoSchedAt', placeholder: 'HH:MM', value: current.at || '09:00',
    autocomplete: 'off',
  });
  const intervalInput = el('input', {
    class: 'form-input', id: 'autoSchedInterval', type: 'number',
    min: '60', step: '60', value: String(current.seconds || 3600),
  });
  const intervalRow = el('label', { class: 'form-row', id: 'autoSchedIntervalRow' }, [
    el('span', { class: 'form-label', text: 'Every (seconds, minimum 60)' }),
    intervalInput,
  ]);
  const atRow = el('label', { class: 'form-row', id: 'autoSchedAtRow' }, [
    el('span', { class: 'form-label', text: 'At (local time)' }), atInput,
  ]);
  const dayNames = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  const daysRow = el('div', { class: 'form-row', id: 'autoSchedDaysRow' }, [
    el('span', { class: 'form-label', text: 'Days' }),
    el('div', { class: 'btn-row' }, dayNames.map((name, day) => {
      const box = el('input', {
        type: 'checkbox', value: String(day), dataset: { schedDay: '1' }, id: `autoSchedDay${day}`,
      });
      box.checked = Array.isArray(current.days) && current.days.includes(day);
      return el('label', { class: 'chip check-chip', for: `autoSchedDay${day}` }, [box, name]);
    })),
  ]);
  const syncScheduleRows = () => {
    const kind = kindInput.value;
    intervalRow.hidden = kind !== 'interval';
    atRow.hidden = kind === 'interval' || kind === 'none';
    daysRow.hidden = kind !== 'weekly';
  };
  syncScheduleRows();
  kindInput.addEventListener('change', syncScheduleRows);
  for (const input of [kindInput, atInput, intervalInput]) {
    input.addEventListener('change', markEdited);
  }
  for (const box of daysRow.querySelectorAll('input[type=checkbox]')) {
    box.addEventListener('change', markEdited);
  }
  const scheduleBox = el('div', { class: 'card inset' }, [
    el('h3', { class: 'card-title spaced', text: 'When it runs' }),
    el('label', { class: 'form-row' }, [
      el('span', { class: 'form-label', text: 'Trigger' }), kindInput,
    ]),
    el('div', { class: 'form-grid' }, [atRow, intervalRow]),
    daysRow,
    workflow.next_run_at
      ? el('p', { class: 'fine', text: `Next scheduled occurrence: ${workflow.next_run_at}.` })
      : null,
    el('p', {
      class: 'fine',
      text: 'A schedule fires the plan; it does not authorise it. Each occurrence is '
        + 'decided again, and an approval is never carried from a previous run.',
    }),
  ]);
  blocks.push(scheduleBox);

  if (workflow.last_run_id) {
    blocks.push(el('p', {
      class: 'fine',
      text: `Last run ${timeAgo(workflow.last_run_at)}: ${workflow.last_state || 'no recorded state'}.`,
    }));
  }

  const save = el('button', { class: 'btn primary small', type: 'button', text: 'Save changes' });
  save.disabled = true;
  const dirtyNote = el('span', { class: 'fine', text: 'No unsaved changes.' });
  function markEdited() {
    save.disabled = false;
    dirtyNote.textContent = 'Unsaved changes. Saving replaces the stored plan.';
  }
  save.addEventListener('click', () => saveAutomationEdit(workflow, {
    steps: draftSteps,
    schedule: scheduleFromEditor(kindInput, atInput, intervalInput),
    event: kindInput.value === 'none' ? {} : workflow.event || {},
  }, save, dirtyNote));

  const row = el('div', { class: 'btn-row' });
  const run = el('button', { class: 'btn primary small', type: 'button', text: 'Run now' });
  run.addEventListener('click', () => runAutomation(workflow, run));
  const toggle = el('button', {
    class: 'btn ghost small', type: 'button', text: workflow.enabled ? 'Disable' : 'Enable',
  });
  toggle.addEventListener('click', () => toggleAutomation(workflow, !workflow.enabled, toggle));
  const duplicate = el('button', {
    class: 'btn ghost small', type: 'button', text: 'Duplicate',
  });
  duplicate.addEventListener('click', () => duplicateAutomation(workflow, duplicate));
  const remove = el('button', { class: 'btn danger ghost small', type: 'button', text: 'Delete' });
  remove.addEventListener('click', () => deleteAutomation(workflow, remove));
  const history = el('button', { class: 'btn ghost small', type: 'button', text: 'See its runs →' });
  history.addEventListener('click', () => showView('history'));
  row.append(save, run, toggle, duplicate, remove, history);
  blocks.push(row, dirtyNote);

  // The stored JSON, for reading the exact parameters the editor shows in short form.
  const details = el('details', { class: 'disclosure' }, [
    el('summary', { text: 'Stored definition' }),
    el('pre', { class: 'pre', text: JSON.stringify(workflowBody(workflow), null, 2) }),
  ]);
  blocks.push(details);

  blocks.push(el('p', {
    class: 'fine',
    text: 'Run now is a fresh authorisation, not a reuse of a previous approval: every '
      + 'step is decided again against the actor that owns this automation.',
  }));

  replace(host, blocks);
}

async function saveAutomationEdit(workflow, draft, button, note) {
  if (!draft.steps.length) {
    toast('A plan needs at least one step.', 'bad');
    return;
  }
  button.disabled = true;
  try {
    const body = workflowBody(workflow, {
      steps: draft.steps,
      schedule: draft.schedule,
      event: draft.event,
    });
    const saved = await PUT(`/automations/${encodeURIComponent(workflow.workflow_id)}`, body);
    toast(`Saved “${saved.name}”. Nothing has run.`, 'good');
    await refreshAutomations();
  } catch (error) {
    toast(`Could not save: ${error.message}`, 'bad');
    button.disabled = false;
    note.textContent = 'Unsaved changes. Saving replaces the stored plan.';
  }
}

/**
 * Duplicate a stored plan.
 *
 * The copy is *disarmed* and *manual* by default: duplicating an automation should not
 * silently create a second thing that runs on its own schedule. The name says where it
 * came from so a library with two similar entries can be told apart, and the whole
 * body is re-validated by the server exactly as a hand-written plan would be.
 */
async function duplicateAutomation(workflow, button) {
  button.disabled = true;
  try {
    const baseName = `${workflow.name} (copy)`;
    const created = await POST('/automations', workflowBody(workflow, {
      name: baseName.slice(0, 80),
      schedule: {},
      event: {},
      enabled: false,
    }));
    state.selectedAutomation = created.workflow_id;
    toast(`Duplicated as “${created.name}”, disarmed and set to run by hand only.`, 'good');
    await refreshAutomations();
  } catch (error) {
    toast(`Could not duplicate it: ${error.message}`, 'bad');
    button.disabled = false;
  }
}

async function draftAutomation() {
  const text = byId('automationText').value.trim();
  if (!text) return;
  if (!requireBackend()) return;
  try {
    const draft = await POST('/automations/draft', { text, actor: IDENTITY });
    state.automationDraft = draft;
    renderDraft(draft);
  } catch (error) {
    toast(`Could not read that: ${error.message}`, 'bad');
  }
}

function renderDraft(draft) {
  const host = byId('automationDraft');
  host.hidden = false;
  const schedule = draft.schedule || {};
  const event = draft.event || {};
  const parts = [
    el('h3', { class: 'card-title spaced', text: 'Draft — not saved' }),
    el('p', { class: 'card-note', text: `Understood: ${draft.understood || 'nothing'}` }),
    kvTable([
      ['Name', draft.name],
      ['Trigger', Object.keys(schedule).length
        ? scheduleWords(schedule)
        : (event.event_type ? `when ${event.event_type} fires` : 'none — run by hand')],
      ['Steps', String(draft.steps.length)],
      ['Where the steps came from', draft.source],
    ]),
  ];
  if (draft.reason) parts.push(el('p', { class: 'fine', text: draft.reason }));
  if (draft.refused_templates && draft.refused_templates.length) {
    parts.push(el('p', {
      class: 'fine',
      text: `Templates not offered: ${draft.refused_templates.join('; ')}`,
    }));
  }
  if (draft.steps.length) {
    const list = el('ul', { class: 'rows compact' });
    draft.steps.forEach((step, index) => {
      list.append(el('li', { class: 'row' }, [
        el('div', { class: 'row-main' }, [
          el('div', { class: 'row-title', text: `${index + 1}. ${step.label || step.action}` }),
          el('div', { class: 'row-sub', text: step.action }),
        ]),
      ]));
    });
    parts.push(list);
    const save = el('button', { class: 'btn primary small', type: 'button', text: 'Save this plan' });
    save.addEventListener('click', () => saveDraft(draft, save));
    parts.push(el('div', { class: 'btn-row' }, [save]));
    parts.push(el('p', {
      class: 'fine',
      text: 'Saving grants nothing and starts nothing. The plan is stored; it is '
        + 'authorised step by step each time it runs.',
    }));
  } else {
    parts.push(el('p', {
      class: 'fine',
      text: 'There are no steps to save. A step has to come from a phrase an operator '
        + 'authored, so this draft cannot become an automation as it stands.',
    }));
  }
  replace(host, parts);
}

async function saveDraft(draft, button) {
  button.disabled = true;
  try {
    const saved = await POST('/automations', draft.proposal);
    state.selectedAutomation = saved.workflow_id;
    toast(`Saved “${saved.name}”. Nothing has run.`, 'good');
    byId('automationDraft').hidden = true;
    await refreshAutomations();
  } catch (error) {
    toast(`Could not save: ${error.message}`, 'bad');
    button.disabled = false;
  }
}

async function runAutomation(workflow, button) {
  button.disabled = true;
  try {
    const run = await POST(`/automations/${encodeURIComponent(workflow.workflow_id)}/run`, {});
    const words = RUN_STATE_WORDS[run.state] || run.state;
    // "good" only for the one state that means every step actually succeeded and the
    // executor verified it. `partial` and `awaiting_approval` are not successes, and a
    // green toast for either would be the exact claim this page exists to avoid.
    toast(`Run finished: ${words}.`, run.state === 'succeeded' ? 'good' : 'bad');
    await refreshAutomations();
  } catch (error) {
    toast(`Could not run it: ${error.message}`, 'bad');
  } finally {
    button.disabled = false;
  }
}

async function toggleAutomation(workflow, enabled, button) {
  button.disabled = true;
  try {
    await POST(`/automations/${encodeURIComponent(workflow.workflow_id)}/enabled`, { enabled });
    toast(enabled ? 'Armed.' : 'Disarmed. A run in progress is not stopped.', 'good');
    await refreshAutomations();
  } catch (error) {
    toast(`Could not change it: ${error.message}`, 'bad');
  } finally {
    button.disabled = false;
  }
}

async function deleteAutomation(workflow, button) {
  if (!window.confirm(`Delete “${workflow.name}”? Its run history is kept.`)) return;
  button.disabled = true;
  try {
    await DELETE(`/automations/${encodeURIComponent(workflow.workflow_id)}`);
    if (state.selectedAutomation === workflow.workflow_id) state.selectedAutomation = '';
    replace(byId('automationSelected'), '');
    toast('The plan is gone; what it did is still in History.', 'good');
    await refreshAutomations();
  } catch (error) {
    toast(`Could not delete it: ${error.message}`, 'bad');
    button.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// HISTORY (C39)
// ---------------------------------------------------------------------------
async function refreshHistory() {
  await refreshAppState();
  const list = byId('historyList');
  if (!state.backendReady) {
    replace(list, el('li', { class: 'empty', text: 'Backend offline.' }));
    return;
  }
  try {
    const answer = await GET('/automations/runs?limit=100');
    const runs = answer.runs || [];
    byId('historyCount').textContent = String(runs.length);
    if (!runs.length) {
      replace(list, el('li', { class: 'empty', text: 'No automation has run yet.' }));
      return;
    }
    replace(list, runs.map((run) => {
      const steps = el('ul', { class: 'rows compact' });
      for (const step of run.steps || []) {
        steps.append(el('li', { class: 'row' }, [
          el('div', { class: 'row-main' }, [
            el('div', { class: 'row-title', text: `${step.index + 1}. ${step.label || step.action_type}` }),
            el('div', { class: 'row-sub', text: step.error || step.action_type }),
          ]),
          el('div', { class: 'row-tail' }, [
            step.verified === true ? el('span', { class: 'chip', text: 'verified' }) : null,
            stateChip(step.state),
          ]),
        ]));
      }
      return el('li', { class: 'row' }, [
        el('div', { class: 'row-main' }, [
          el('div', { class: 'row-title', text: run.workflow_name || run.workflow_id }),
          el('div', {
            class: 'row-sub',
            text: `${run.trigger}${run.scheduled_for ? ` for ${run.scheduled_for}` : ''} · `
              + `${RUN_STATE_WORDS[run.state] || run.state} · ${timeAgo(run.created_at)}`,
          }),
          steps,
          run.error ? el('p', { class: 'fine', text: run.error }) : null,
        ]),
        el('div', { class: 'row-tail' }, [stateChip(run.state)]),
      ]);
    }));
  } catch (error) {
    replace(list, el('li', { class: 'empty', text: `Could not read history: ${error.message}` }));
  }
}

// ---------------------------------------------------------------------------
// DEVICES
// ---------------------------------------------------------------------------
async function refreshDevices() {
  await refreshAppState();
  if (!state.backendReady) {
    replace(byId('deviceList'), el('li', { class: 'empty', text: 'Backend offline.' }));
    replace(byId('capabilityList'), el('li', { class: 'empty', text: 'Backend offline.' }));
    return;
  }
  const [devices, capabilities] = await Promise.allSettled([GET('/devices'), GET('/capabilities')]);
  // Route activity is counted from the real execution records, not a counter. If the
  // list cannot be read the cards say "unknown" rather than "0".
  const executions = await GET('/executions?limit=100').catch(() => null);
  const activeFor = (deviceId) => (executions
    ? executions.filter((record) => (record.device_id || '') === deviceId
      && ['running', 'pending', 'awaiting_approval'].includes(record.state)).length
    : null);

  if (devices.status === 'fulfilled') {
    const list = devices.value;
    setBadge('devices', list.filter((d) => d.online).length);
    // One card per device, with exactly the facts the record holds. The count is
    // whatever the registry says — this session has one laptop, so it shows one.
    replace(byId('deviceList'), list.length ? list.map((device) => {
      const active = activeFor(device.device_id);
      const card = el('li', { class: 'row device-card' }, [
        el('div', { class: 'row-main' }, [
          el('div', { class: 'row-title' }, [
            el('span', { class: `dot ${device.online ? 'online' : 'offline'}` }),
            el('span', { text: ` ${device.name || device.device_id}` }),
          ]),
          el('dl', { class: 'kv' }, [
            el('dt', { text: 'OS' }), el('dd', { text: `${device.platform || 'unknown'} ${device.os_version || ''}`.trim() }),
            el('dt', { text: 'Architecture' }), el('dd', { text: device.arch || 'unknown' }),
            el('dt', { text: 'Presence' }), el('dd', { text: device.online ? 'online' : 'offline' }),
            el('dt', { text: 'Last heartbeat' }), el('dd', { text: device.last_seen ? timeAgo(device.last_seen) : 'never' }),
            el('dt', { text: 'Capabilities' }), el('dd', { text: String((device.capabilities || []).length) }),
            el('dt', { text: 'Risk ceiling' }), el('dd', { text: device.risk_ceiling || 'unknown' }),
            el('dt', { text: 'Active executions' }), el('dd', { text: active === null ? 'unknown' : String(active) }),
            el('dt', { text: 'Device id' }), el('dd', { text: device.device_id }),
          ]),
        ]),
      ]);
      const select = el('button', { class: 'btn ghost small', type: 'button', text: 'Select' });
      select.addEventListener('click', () => {
        state.localDeviceId = device.device_id;
        renderDevicePill(list);
        toast(`${device.name || device.device_id} is the selected device.`, 'good');
      });
      const refresh = el('button', { class: 'btn ghost small', type: 'button', text: 'Refresh' });
      refresh.addEventListener('click', () => refreshDevices());
      const details = el('button', { class: 'btn ghost small', type: 'button', text: 'Details' });
      details.addEventListener('click', () => {
        toast(`${device.device_id} · ${device.kind} · enrolled ${device.enrolled ? 'yes' : 'no'} · agent ${device.agent_version || 'unknown'}`, '');
      });
      const pair = el('button', { class: 'btn ghost small', type: 'button', text: 'Pair' });
      pair.addEventListener('click', () => {
        byId('pairDeviceId').value = device.device_id;
        byId('pairDeviceId').scrollIntoView({ block: 'center', behavior: 'smooth' });
      });
      const revoke = el('button', { class: 'btn danger ghost small', type: 'button', text: 'Revoke' });
      revoke.addEventListener('click', async () => {
        revoke.disabled = true;
        try {
          await POST(`/devices/${encodeURIComponent(device.device_id)}/revoke`, { reason: 'revoked from the desktop shell' });
          toast(`${device.device_id} was revoked and its identity removed.`, 'good');
          refreshDevices();
        } catch (error) {
          toast(`Could not revoke: ${error.message}`, 'bad');
          revoke.disabled = false;
        }
      });
      card.append(el('div', { class: 'row-tail' }, [select, refresh, details, pair, revoke]));
      return card;
    }) : el('li', { class: 'empty', text: 'No devices are enrolled.' }));
  } else {
    replace(byId('deviceList'), el('li', { class: 'empty', text: `Could not read devices: ${devices.reason.message}` }));
  }

  if (capabilities.status === 'fulfilled') {
    const caps = capabilities.value;
    replace(byId('capabilityList'), caps.length ? caps.map((cap) => el('li', { class: 'row' }, [
      el('div', { class: 'row-main' }, [
        el('div', { class: 'row-title', text: cap.action }),
        el('div', { class: 'row-sub', text: `${cap.provider} · ${(cap.permissions || []).join(', ') || 'no extra permission'}` }),
      ]),
      riskChip(cap.risk_level),
    ])) : el('li', { class: 'empty', text: 'No capabilities are registered.' }));
  } else {
    replace(byId('capabilityList'), el('li', { class: 'empty', text: 'Could not read capabilities.' }));
  }

  // A freshly enrolled laptop is the only id a pairing code can name, so default the
  // field to it rather than to a guess.
  if (devices.status === 'fulfilled') {
    const laptops = devices.value.filter((d) => d.kind === 'laptop' || d.kind === 'server');
    const input = byId('pairDeviceId');
    if (laptops.length && !input.dataset.touched) input.value = laptops[0].device_id;
  }
}

async function mintPairing() {
  if (!requireBackend()) return;
  const deviceId = byId('pairDeviceId').value.trim();
  const maxRisk = byId('pairRisk').value;
  if (!deviceId) { toast('Name the laptop the phone may pair to.', 'bad'); return; }
  const button = byId('pairMint');
  button.disabled = true;
  try {
    const answer = await POST('/pairing/code', { device_id: deviceId, max_risk: maxRisk });
    const out = byId('pairOut');
    out.hidden = false;
    replace(out, [
      el('div', { class: 'fine', text: 'Read this to the phone. It is shown once and never logged.' }),
      el('div', { class: 'code-value', text: answer.code }),
      kvTable([
        ['Pairs to', answer.laptop_device_id],
        ['Ceiling', answer.max_risk],
        ['Expires', `${Math.round(answer.expires_in_s)}s`],
        ['Signing key', answer.pairing_durable ? 'durable (survives restart)' : 'in memory (lost on restart)'],
      ]),
    ]);
  } catch (error) {
    toast(`No code was minted: ${error.message}`, 'bad');
  } finally {
    button.disabled = false;
  }
}

async function revokePairings() {
  if (!requireBackend()) return;
  try {
    const answer = await POST('/pairing/revoke');
    byId('pairOut').hidden = true;
    toast(answer.revoked ? `Revoked ${answer.revoked} outstanding code(s).` : 'There were no outstanding codes.', 'good');
  } catch (error) {
    toast(`Could not revoke codes: ${error.message}`, 'bad');
  }
}

// ---------------------------------------------------------------------------
// SETTINGS
// ---------------------------------------------------------------------------
async function refreshSettings() {
  const appState = await bridge.state();
  applyAppState(appState);
  const backend = appState.backend || {};
  replace(byId('backendKv'), [
    el('dt', { text: 'Status' }), el('dd', { text: backend.status || 'stopped' }),
    el('dt', { text: 'Port' }), el('dd', { text: backend.port ? `127.0.0.1:${backend.port}` : '—' }),
    el('dt', { text: 'Runtime' }), el('dd', { text: backend.runtimeMode || '—' }),
    el('dt', { text: 'Data folder' }), el('dd', { text: backend.dataDir || appState.userData || '—' }),
    el('dt', { text: 'Key' }), el('dd', { text: 'minted in memory at launch; never written to disk' }),
    el('dt', { text: 'Last error' }), el('dd', { text: backend.lastError || 'none' }),
  ]);

  const settings = appState.settings || {};
  // The provider card reads the main-process status, not the raw settings: the words
  // "Connected" only appear once a probe said so.
  state.providerStatus = appState.provider || state.providerStatus;
  renderSettingsProviderCard();
  const shortcutInput = byId('setShortcut');
  if (!shortcutInput.dataset.touched) shortcutInput.value = settings.shortcut || '';
  byId('setShortcutEnabled').checked = Boolean(settings.shortcutEnabled);
  byId('setNotifications').checked = Boolean(settings.notificationsEnabled);
  byId('setAgent').checked = Boolean(settings.agentEnabled);
  byId('setDeviceName').value = settings.deviceName || '';
  byId('setWorkspace').value = settings.workspaceRoot || '';
  byId('setAllowShell').checked = Boolean(settings.allowShell);
  byId('setAllowDesktop').checked = Boolean(settings.allowDesktop);
  byId('setAllowBrowser').checked = Boolean(settings.allowBrowser);
  byId('setAllowMedia').checked = Boolean(settings.allowMedia);
  byId('setAllowedCommands').value = (settings.allowedCommands || []).join(', ');

  byId('shortcutStatus').textContent = appState.shortcutRegistered
    ? `${prettyAccelerator(settings.shortcut)} is registered system-wide.`
    : (settings.shortcutEnabled
      ? `${prettyAccelerator(settings.shortcut)} is not registered — another application may own it.`
      : 'The global shortcut is turned off.');

  // The old single-provider card is hidden in index.html and its fields are no longer
  // read: every provider setting now goes through the per-purpose surface above, and a
  // renderer that kept writing the legacy keys would let one screen silently disagree
  // with the lane cards. The keyring's availability is still worth stating, and each
  // lane already says it when it is false.

  replace(byId('aboutKv'), [
    el('dt', { text: 'Version' }), el('dd', { text: appState.version || '—' }),
    el('dt', { text: 'Bundle id' }), el('dd', { text: appState.appId || '—' }),
    el('dt', { text: 'Electron' }), el('dd', { text: appState.electron || '—' }),
    el('dt', { text: 'Chromium' }), el('dd', { text: appState.chrome || '—' }),
    el('dt', { text: 'Platform' }), el('dd', { text: `${appState.platform} ${appState.arch}` }),
    el('dt', { text: 'Packaged' }), el('dd', { text: appState.packaged ? 'yes' : 'no (development)' }),
  ]);
}

async function saveSettings() {
  const button = byId('settingsSave');
  button.disabled = true;
  try {
    const patch = {
      shortcut: byId('setShortcut').value.trim(),
      shortcutEnabled: byId('setShortcutEnabled').checked,
      notificationsEnabled: byId('setNotifications').checked,
      agentEnabled: byId('setAgent').checked,
      deviceName: byId('setDeviceName').value.trim(),
      workspaceRoot: byId('setWorkspace').value.trim(),
      allowShell: byId('setAllowShell').checked,
      allowDesktop: byId('setAllowDesktop').checked,
      allowBrowser: byId('setAllowBrowser').checked,
      allowMedia: byId('setAllowMedia').checked,
      allowedCommands: byId('setAllowedCommands').value.split(',').map((s) => s.trim()).filter(Boolean),
    };
    const answer = await bridge.updateSettings(patch);
    const shortcut = answer && answer.shortcut;
    byId('settingsSaved').textContent = shortcut && shortcut.registered
      ? 'Saved. The shortcut is registered.'
      : `Saved. Shortcut not registered: ${shortcut ? shortcut.reason : 'unknown reason'}.`;
    toast('Settings saved. Restart the backend to apply agent changes.', 'good');
    await refreshSettings();
  } catch (error) {
    toast(`Settings were not saved: ${error.message}`, 'bad');
  } finally {
    button.disabled = false;
  }
}

// The pre-C42 "Save provider" / "Clear key" pair used to write the legacy single
// provider slot — name, model, endpoint and credential — directly. Both jobs are now
// done by the purpose modal's Save (`cfgSave` → `purpose:configure`) and Remove
// (`cfgRemove` → `purpose:remove`), for one named job at a time, so the duplicated path
// is gone rather than merely hidden. The hidden markup it drove was removed from
// index.html too: a form nothing reads is a trap for the next person.

async function backendAction(action) {
  const label = { restart: 'Restarting', stop: 'Stopping', start: 'Starting' }[action] || 'Working';
  toast(`${label} the backend…`, '');
  try {
    const answer = await (action === 'restart' ? bridge.restartBackend()
      : action === 'stop' ? bridge.stopBackend()
        : bridge.startBackend());
    if (!answer || answer.ok === false || (answer.backend && answer.backend.status === 'failed')) {
      const detail = (answer && (answer.error || answer.backend && answer.backend.lastError)) || 'unknown error';
      toast(`${label} failed: ${detail}`, 'bad');
    } else {
      toast('Backend ' + (answer.backend ? answer.backend.status : 'ready') + '.', 'good');
    }
    await refreshSettings();
    await refreshCurrent();
  } catch (error) {
    toast(`Backend action failed: ${error.message}`, 'bad');
  }
}

async function loadBackendLog() {
  try {
    const answer = await bridge.backendLogs();
    const pre = byId('backendLog');
    pre.textContent = answer && answer.text ? answer.text : (answer && answer.error) || 'No log lines yet.';
    pre.scrollTop = pre.scrollHeight;
  } catch (error) {
    byId('backendLog').textContent = `Could not read the log: ${error.message}`;
  }
}

// ---------------------------------------------------------------------------
// Refresh dispatch, polling, and wiring
// ---------------------------------------------------------------------------
// ---------------------------------------------------------------------------
// VISUAL INTELLIGENCE (C38)
// ---------------------------------------------------------------------------
// This view is thin on purpose. It does not capture anything itself: every frame
// and every element comes from a `device.screen.*` action that went through the
// ordinary pipeline -- registry, policy, approval, executor, verification -- and
// the state kept here is *the pipeline's answer*, not a rendering of what we
// hoped would happen. "Capture Screen" and "Inspect Active Window" press a button;
// they do not mean "the screen is now being watched". Nothing runs on a timer,
// nothing runs on load, and nothing is retried.
//
// What this panel can honestly show depends on the machine:
//   * accessibility inspection is real anywhere AT-SPI exists;
//   * a capture is real only if the OS screenshot portal grants it -- on a session
//     where it refuses, the panel says "declined" and shows no image, which is a
//     true statement about the machine rather than a failure to fake past;
//   * AI interpretation is shown only when a multimodal credential is configured.
//     Sending pixels to a model is a separate act of sharing, so it never happens
//     as a side effect of a capture.
const visualState = {
  // The last real observation, as the pipeline reported it. `null` until asked.
  capture: null,   // {status, file, session, detail}
  inspect: null,   // {status, element_count, elements, truncated, window}
  frameUrl: null,  // an object URL for a captured frame, revoked on stop
  analysis: null,  // {status, text, model} -- only ever set from a real answer
  // Which execution's accessibility walk this page is currently willing to accept
  // element names from. Set when an inspection is submitted, so a chunk from an
  // earlier or unrelated execution cannot repaint this panel.
  inspectExecutionId: null,
  // The walk usually finishes before `/execute` returns, so its chunk can arrive
  // before there is an execution id to match it against. Held here, one entry only,
  // and adopted by the inspection request that names it -- never painted from here.
  pendingElements: null, // {executionId, payload}
  // The one action this page has staged against the inspected tree. `null` until an
  // element is picked. Staging is a local edit to this object and sends nothing: the
  // request leaves only when the person presses Approve Action.
  action: null,   // {kind, role, name, window, text, direction}
  // What came back from the last submission, keyed to the execution that answered.
  // `null` before anything was sent, so "no result yet" cannot read as "it worked".
  outcome: null,  // {phase, state, actionId, executionId, verified, method, detail, effect}
};

/** Forget the held frame and release the object URL. Never accumulates. */
function releaseVisualFrame() {
  if (visualState.frameUrl) {
    URL.revokeObjectURL(visualState.frameUrl);
    visualState.frameUrl = null;
  }
  visualState.capture = null;
}

function visualSharingOn() {
  return Boolean(visualState.frameUrl);
}

function renderSharing() {
  const chip = byId('visualSharing');
  const on = visualSharingOn();
  if (chip) {
    chip.textContent = on ? 'sharing a frame' : 'not sharing';
    chip.dataset.state = on ? 'on' : 'off';
  }
  const stop = byId('visualStop');
  if (stop) stop.disabled = !on;
  // The stop control exists precisely so that holding a frame is always undoable
  // with one press. If there is nothing held, there is nothing to stop.
  const badge = document.querySelector('[data-badge="visual"]');
  if (badge) badge.hidden = !on;
}

/** Turn a capture result into what this panel is allowed to assert. */
function applyCaptureResult(body) {
  releaseVisualFrame();
  const status = body && body.status;
  visualState.capture = body || null;
  if (status === 'captured') {
    // `file` describes a frame the *request* produced. Whether the bytes reached
    // this page is the next question; until they do, the status chip is honest that
    // nothing is being held, so a failed transfer cannot read as "sharing".
    const file = body.file || {};
    visualState.capture.file = file;
    renderPreviewPlaceholder(
      `The portal returned a ${file.width || '?'}×${file.height || '?'} frame.`,
      'The frame is not retained, and this panel does not display image bytes it has not received.',
    );
  } else {
    renderPreviewPlaceholder(
      status === 'cancelled' ? 'The OS declined the capture.' : 'No capture was produced.',
      (body && body.detail) || 'The screenshot portal did not answer with a frame.',
    );
  }
  renderSharing();
  renderObservation();
}

function renderPreviewPlaceholder(title, note) {
  const box = byId('visualPreview');
  if (!box) return;
  replace(box, el('div', { class: 'empty-state' }, [
    el('p', { text: title }),
    el('p', { class: 'fine', text: note }),
  ]));
}

function renderObservation() {
  const box = byId('visualObservation');
  if (!box) return;
  const rows = [];
  const cap = visualState.capture;
  const ins = visualState.inspect;

  if (cap) {
    const ok = cap.status === 'captured';
    const detail = ok
      ? `${cap.file.width || '?'}×${cap.file.height || '?'} ${cap.file.format || ''}`.trim()
      : (cap.detail || cap.status);
    rows.push(observationLine(ok ? 'ok' : 'warn',
      ok ? 'Screen captured' : `Capture ${cap.status}`, detail));
  }
  if (ins) {
    const ok = ins.status === 'ok';
    rows.push(observationLine(ok ? 'ok' : 'bad',
      ok ? 'Accessibility tree read' : `Inspection ${ins.status}`,
      ok ? `${ins.element_count} elements${ins.truncated ? ' (truncated)' : ''}`
        : (ins.detail || 'no tree was available')));
  }
  if (!rows.length) {
    replace(box, el('p', { class: 'fine', text: 'No observation yet. Capture the screen or inspect a window to begin.' }));
    return;
  }
  replace(box, rows);
}

function observationLine(state, title, detail) {
  return el('div', { class: 'status-line' }, [
    el('span', { class: 'dot', dataset: { state } }),
    el('div', {}, [
      el('div', { text: title }),
      el('div', { class: 'fine', text: detail || '' }),
    ]),
  ]);
}

/**
 * Take the element list off the transient channel.
 *
 * Arrives as one JSON array in a single `elements` chunk, addressed by execution id.
 * Only the execution this page most recently asked to inspect is accepted: the
 * channel is shared by every screen read the session made, and painting an older
 * walk over a newer one would show elements that are no longer on the screen.
 *
 * The runner labels the chunk itself: `final` with `truncated` means the publisher
 * refused the document rather than cut it, and `truncated` without `final` means the
 * walk stopped at its own ceiling. Those two are different facts and the panel says
 * so, because "no names" from a refused document is not "a small tree".
 */
function applyInspectedElements(executionId, payload) {
  if (!visualState.inspectExecutionId) {
    // No request has named an execution yet. The walk that just finished is very
    // likely the one about to be returned; hold its payload so the click that
    // follows can adopt it. Nothing is rendered from a guess.
    visualState.pendingElements = { executionId, payload };
    return;
  }
  if (executionId !== visualState.inspectExecutionId) return;
  if (!visualState.inspect) return;
  const final = payload.final === true;
  const truncated = payload.truncated === true;
  if (final && truncated && !payload.chunk) {
    // The document did not fit the channel's budget, so it was refused whole. Render
    // the reason instead of an empty list, which would read as an empty tree.
    visualState.inspect.namesRefused = true;
    visualState.inspect.namesDeliveredAt = Date.now();
    renderElements();
    renderObservation();
    return;
  }
  let parsed;
  try {
    parsed = JSON.parse(String(payload.chunk || '[]'));
  } catch {
    // A malformed payload is not an observation; leave the last real one.
    return;
  }
  if (!Array.isArray(parsed)) return;
  visualState.inspect.elements = parsed;
  visualState.inspect.namesRefused = false;
  // `truncated` on this chunk describes the *channel*, not the tree. The walk's own
  // ceiling came from the result's `truncated` and is not the same fact: a document
  // that arrives whole from a walk that stopped early is complete-as-delivered and
  // still incomplete as a tree. Never let the former overwrite the latter.
  if (truncated) visualState.inspect.walkTruncated = true;
  visualState.inspect.namesDeliveredAt = Date.now();
  renderElements();
  renderWindowInfo();
  renderObservation();
}

/** The walk reached its own element ceiling; the names that arrived are complete. */
function applyElementsNote(executionId, chunk) {
  if (!visualState.inspectExecutionId) {
    if (visualState.pendingElements) visualState.pendingElements.note = String(chunk || '');
    return;
  }
  if (executionId !== visualState.inspectExecutionId) return;
  if (!visualState.inspect) return;
  visualState.inspect.note = String(chunk || '');
  renderElements();
}

function renderElements() {
  const list = byId('visualElements');
  const count = byId('visualElementCount');
  if (!list || !count) return;
  const ins = visualState.inspect;
  if (!ins || ins.status !== 'ok') {
    count.textContent = '0';
    replace(list, el('li', { class: 'empty', text: 'Inspect a window to list its elements.' }));
    return;
  }
  count.textContent = String(ins.element_count);
  const nodes = (ins.elements || []).map((item) => {
    const children = [
      el('span', { class: 'el-role', text: item.role || 'unknown' }),
      el('span', { class: 'el-name', text: item.name || '(no name)' }),
    ];
    if (item.enabled === false) children.push(el('span', { class: 'el-bounds', text: 'disabled' }));
    const b = item.bounds || {};
    if (b.width) {
      children.push(el('span', { class: 'el-bounds',
        text: `${b.width}×${b.height} at ${b.x},${b.y}` }));
    }
    if ((item.actions || []).length) {
      children.push(el('span', { class: 'el-actionable', text: `${item.actions.length} actions` }));
    }
    // Which verbs this element can plausibly take. The accessibility tree publishes
    // its actions; an element with none is not offered a click it would refuse. A
    // text entry is recognised by the roles AT-SPI gives editable fields, so "Type
    // into" is offered where typing is possible and withheld everywhere else.
    const verbs = [];
    const actions = (item.actions || []).map((a) => String(a).toLowerCase());
    if (actions.some((a) => a.includes('click') || a.includes('tap') || a.includes('default'))) {
      verbs.push(['click', 'Activate']);
    }
    if (EDITABLE_ROLES.has(String(item.role || '').toLowerCase())) verbs.push(['type', 'Type']);
    if (SCROLL_ROLES.has(String(item.role || '').toLowerCase())) verbs.push(['scroll', 'Scroll']);
    if (verbs.length) {
      const row = el('div', { class: 'el-verbs' });
      for (const [kind, label] of verbs) {
        const btn = el('button', { class: 'btn ghost small', type: 'button', text: label });
        btn.addEventListener('click', () => stageAction(item, kind));
        row.append(btn);
      }
      children.push(row);
    }
    const staged = visualState.action
      && visualState.action.role === item.role
      && visualState.action.name === (item.name || '')
      && visualState.action.window === (item.app || '');
    return el('li', { class: staged ? 'row staged' : 'row' }, children);
  });
  if (!nodes.length) {
    // Only three things can produce an empty list here, and they are not the same
    // fact: the tree was empty, the names were refused whole, or they have not
    // arrived yet. Saying "the tree held nothing" over a non-zero count would
    // contradict the count printed right next to it.
    const expected = Number(ins.element_count) || 0;
    let text;
    if (ins.namesRefused) {
      text = `The walk read ${expected} elements, but their names did not fit the `
        + 'stream\'s size limit and were refused rather than cut short. Re-inspect '
        + 'with a smaller element cap to see them.';
    } else if (expected) {
      text = `The walk read ${expected} elements, but their names have not arrived on the stream.`;
    } else {
      text = 'The tree had no elements.';
    }
    nodes.push(el('li', { class: 'empty', text }));
  }
  // The walk's own ceiling is a statement about the tree, so it sits with the tree.
  // The provider's note is preferred when it arrived: it is what actually said so.
  if (ins.namesDeliveredAt && ins.walkTruncated && !ins.namesRefused) {
    nodes.push(el('li', { class: 'empty',
      text: ins.note
        || 'The walk reached its element ceiling; the tree holds more elements than are listed.' }));
  }
  // A run changes the screen. The list above still describes what was read before it,
  // so say so in place rather than let it read as current.
  if (ins.stale) {
    nodes.push(el('li', { class: 'empty stale',
      text: 'An action ran after this tree was read. Refresh the observation before '
        + 'staging a target from this list.' }));
  }
  replace(list, nodes);
}

// ---------------------------------------------------------------------------
// Interaction preview: staging a GUI action, submitting it, and reading back
// what actually happened.
//
// The separation this panel exists to preserve is between *naming* a target and
// *acting on it*. Picking an element from the accessibility list stages a request
// in this page's memory; it sends nothing and grants nothing. Approve Action sends
// one two-node graph for one action against one named element, and the server runs
// the same policy gate any other execution meets -- so a HIGH-risk GUI action comes
// back parked at `waiting_permission` with its effect *not* dispatched, and the
// approval that releases it is the server's, matched on the action id the run
// minted, not anything this page asserts.
// ---------------------------------------------------------------------------

// Which accessibility roles can plausibly take each verb. These are *offers*, not
// authority: the adapter re-locates the exact role+label and refuses anything that
// is not exactly it, and the server's policy gate still decides whether the action
// may run at all. Getting one of these wrong costs a refusal message, never an
// action aimed somewhere unintended -- which is why the target is always a name and
// never a coordinate.
const EDITABLE_ROLES = new Set([
  'text', 'entry', 'password text', 'document text', 'paragraph', 'terminal',
  'edit bar', 'text field',
]);
// Scroll verbs are offered on *content*, not on the scrolling container. The server's
// `scroll_element` takes the named element as the anchor that identifies which region
// is meant, then moves the bar that governs it -- a bar it looks for *above* the
// element. A `scroll pane` therefore has no bar above it and is always refused with
// `not_scrollable`, while a label inside one scrolls perfectly. Offering the verb on
// the pane and not on its contents was exactly backwards.
const SCROLL_ROLES = new Set([
  'list', 'tree', 'tree table', 'table', 'document frame', 'paragraph', 'label',
  'text', 'panel', 'viewport',
]);

const GUI_ACTION_TYPES = {
  click: 'device.gui.click',
  type: 'device.gui.type',
  scroll: 'device.gui.scroll',
};

/** The element a staged action names, as the inspection reported it. */
function stagedElementLabel(action) {
  if (!action) return 'nothing';
  const name = action.name ? `“${action.name}”` : '(no label)';
  return `${action.role || 'unknown'} ${name}`;
}

/** A one-line description of what Approve would ask the machine to do. */
function stagedActionSentence(action) {
  if (!action) return '';
  const target = stagedElementLabel(action);
  if (action.kind === 'type') {
    // The text is described by its length, never echoed into the panel: it may be a
    // message body or a credential, and this screen is the thing being screenshotted.
    const length = (action.text || '').length;
    return `Insert ${length} character${length === 1 ? '' : 's'} into ${target}.`;
  }
  if (action.kind === 'scroll') {
    return `Scroll the region holding ${target} until it reaches the ${action.direction} edge.`;
  }
  return `Activate ${target}.`;
}

/**
 * Stage an element. Resets any previous outcome, because a result that belongs to a
 * different target is not a result about this one.
 */
function stageAction(element, kind) {
  const previous = visualState.action;
  const sameTarget = previous
    && previous.role === element.role && previous.name === element.name
    && previous.window === element.app;
  visualState.action = {
    kind,
    role: element.role || '',
    name: element.name || '',
    window: element.app || '',
    // The controls below are bound to the staged action, so carry the choices
    // forward when the person only switched the verb.
    text: sameTarget && previous.text ? previous.text : '',
    direction: sameTarget && previous.direction ? previous.direction : 'bottom',
  };
  visualState.outcome = null;
  renderActionPreview();
  const chip = byId('visualActionChip');
  if (chip) chip.dataset.state = 'staged';
}

function clearStagedAction() {
  visualState.action = null;
  visualState.outcome = null;
  renderActionPreview();
  const chip = byId('visualActionChip');
  if (chip) chip.dataset.state = 'idle';
}

/** Repaint the preview card from the staged action and the last real outcome. */
function renderActionPreview() {
  const box = byId('visualActionPreview');
  const chip = byId('visualActionChip');
  const fields = byId('visualActionFields');
  const approve = byId('visualApprove');
  const cancel = byId('visualCancel');
  if (!box || !chip) return;
  const action = visualState.action;
  const outcome = visualState.outcome;
  syncActionFields();

  if (fields) fields.hidden = !action;
  if (cancel) cancel.disabled = !action && !outcome;

  if (!action) {
    chip.textContent = 'none';
    chip.dataset.state = 'idle';
    if (approve) approve.disabled = true;
    replace(box, el('p', { class: 'fine' },
      'Select an element above to stage an action. Nothing is sent to the machine '
      + 'until you press Approve Action, and the action still passes policy at that '
      + 'moment.'));
    return;
  }

  const rows = [];
  if (!outcome) {
    rows.push(el('p', { class: 'fine', text: 'Staged only — nothing has been sent to the machine.' }));
  }
  rows.push(el('p', { class: 'stage-sentence', text: stagedActionSentence(action) }));
  rows.push(el('div', { class: 'stage-target' }, [
    kv('role', action.role || '—'),
    kv('label', action.name || '(no label)'),
    kv('window', action.window || '—'),
  ]));

  if (!outcome) {
    chip.textContent = 'staged';
    chip.dataset.state = 'staged';
    if (approve) {
      approve.disabled = false;
      approve.textContent = 'Approve Action';
    }
    replace(box, rows);
    return;
  }

  rows.push(renderOutcome(outcome));
  switch (outcome.phase) {
    case 'waiting':
      chip.textContent = 'needs your approval';
      chip.dataset.state = 'waiting';
      if (approve) {
        approve.disabled = false;
        approve.textContent = 'Approve Action';
      }
      break;
    case 'running':
      chip.textContent = 'running';
      chip.dataset.state = 'running';
      if (approve) approve.disabled = true;
      break;
    default:
      chip.textContent = outcome.phase === 'done' ? 'ran' : 'refused';
      chip.dataset.state = outcome.phase === 'done' ? 'done' : 'refused';
      if (approve) {
        // A finished attempt is not re-runnable from here: the target may have moved,
        // and a second press would be a second action nobody staged again.
        approve.disabled = true;
        approve.textContent = 'Approve Action';
      }
  }
  replace(box, rows);
}

/** The outcome block: what the run reported, with the verifier's word kept separate. */
function renderOutcome(outcome) {
  const block = el('div', { class: 'stage-outcome' });
  const line = (state, title, detail) => el('div', { class: 'status-line' }, [
    el('span', { class: 'dot', dataset: { state } }),
    el('div', {}, [
      el('div', { text: title }),
      el('div', { class: 'fine', text: detail || '' }),
    ]),
  ]);

  if (outcome.phase === 'waiting') {
    block.append(line('warn', 'Waiting for approval — nothing has run',
      'The server parked this action at its permission gate. Approving here answers '
      + 'that request; it is matched on the action id below.'));
  } else if (outcome.phase === 'running') {
    block.append(line('warn', 'Approved — dispatching', 'The action is being applied now.'));
  } else if (outcome.phase === 'refused') {
    block.append(line('bad', 'The action did not run', outcome.detail || 'The run was refused.'));
    if (outcome.scope) {
      // Staging does not grant, and neither does this button. The most this panel may
      // do is name the scope and where a person grants it -- in Approvals, by their
      // own press, on their own record.
      block.append(el('p', { class: 'fine' },
        `It needs the standing permission ${outcome.scope}, which this page cannot grant `
        + 'and does not hold. Grant it in Approvals if you mean to, then stage the '
        + 'action again.'));
    }
  } else if (outcome.phase === 'done') {
    // Two independent facts, never merged: the machine reported the effect, and the
    // verifier went and looked. A click that was dispatched but not independently
    // confirmed is reported as exactly that.
    const effect = outcome.effect || {};
    const verified = outcome.verified === true;
    block.append(line(verified ? 'ok' : 'warn',
      verified ? 'Applied and independently verified' : 'Applied, but not independently verified',
      effect.effect || 'The run succeeded.'));
    block.append(el('div', { class: 'stage-verified' }, [
      kv('method', outcome.method || 'none'),
      kv('verified', String(outcome.verified)),
      kv('action', outcome.actionId || '—'),
    ]));
  }
  return block;
}

function renderAnalysis() {
  const box = byId('visualAnalysis');
  const chip = byId('visualAnalysisChip');
  if (!box || !chip) return;
  const a = visualState.analysis;
  if (!a) {
    chip.textContent = 'unavailable';
    replace(box, el('p', { class: 'fine' },
      'AI analysis of the screen is unavailable: no multimodal model credential is '
      + 'configured, so no visual interpretation is produced. Capture and accessibility '
      + 'inspection above are real.'));
    return;
  }
  if (a.status === 'unavailable') {
    chip.textContent = 'unavailable';
    replace(box, el('p', { class: 'fine', text: a.detail || 'No model was available.' }));
    return;
  }
  chip.textContent = a.model ? `by ${a.model}` : 'analysed';
  replace(box, el('p', { text: a.text || '' }));
}

/** Submit one screen capability as an ordinary two-node graph and return its output. */
async function runScreenAction(actionType, parameters) {
  // Which machine this is talking about. The id is not hardcoded: a laptop operator
  // may have named their device anything, and an execution addressed to a device that
  // does not exist is refused before any policy runs. `refreshDevices` records the
  // enrolled execution device here; until it has, this is the configured default.
  const target = state.localDeviceId;
  if (!target) {
    // No agent declared a machine. Sending the graph anyway would produce an
    // execution that cannot be routed, reported as a generic failure; saying why is
    // more useful and is the truth.
    throw new Error(
      'This backend has not declared a laptop agent, so there is no screen to look at. '
      + 'Enable the laptop agent in Settings and restart the backend.',
    );
  }
  const body = await POST('/execute', {
    graph: {
      intent: `view: ${actionType}`,
      target_device_id: target,
      nodes: [
        {
          node_id: 'screen',
          node_type: 'action',
          action: { type: actionType, provider: 'laptop', parameters: parameters || {} },
        },
        { node_id: 'done', node_type: 'end', depends_on: ['screen'] },
      ],
    },
    actor: IDENTITY,
    // One rule, naming exactly the one action this page can ask for. It is what the
    // operator is willing to *consider* — the same precedent the browser dashboard and
    // the voice orchestrator follow — and it authorises nothing by itself. Consent
    // (`device.screen.read`) lives in the server-side store; the policy engine's default
    // for an unlisted action is DENY, which without this rule is what every screen
    // action would meet.
    policy: { rules: [{ action: actionType, decision: 'allow' }] },
  }, { long: true });
  return body;
}

/**
 * Submit the staged action, or — once the server has asked — answer its request.
 *
 * One press does one of two things depending on where the run is, and this function
 * is explicit about which: with nothing submitted it sends the graph; when the last
 * response was `waiting_permission` it answers on the action id that response
 * reported. It never fabricates an approval: the id comes from the server's own
 * result, and a press with no such id does nothing but say so.
 */
async function submitStagedAction(button) {
  if (!requireBackend()) return;
  const action = visualState.action;
  if (!action) return;
  const outcome = visualState.outcome;

  if (outcome && outcome.phase === 'waiting') {
    if (!outcome.actionId || !outcome.executionId) {
      toast('The server did not report which action is waiting, so it cannot be answered here.', 'bad');
      return;
    }
    button.disabled = true;
    visualState.outcome = { ...outcome, phase: 'running' };
    renderActionPreview();
    try {
      const answer = await POST('/approvals', {
        action_id: outcome.actionId,
        status: 'approved',
        execution_id: outcome.executionId,
        summary: 'approved from the visual interaction preview',
      });
      applyGuiOutcome(answer.execution, outcome.actionId);
    } catch (error) {
      visualState.outcome = { ...outcome, phase: 'refused', detail: error.message };
      renderActionPreview();
      toast(`The approval was not recorded: ${error.message}`, 'bad');
    }
    return;
  }

  // Staging is discarded only after the run has an answer; until then the panel keeps
  // showing what was asked for.
  button.disabled = true;
  visualState.outcome = { phase: 'running', actionId: '', executionId: '', effect: {} };
  renderActionPreview();
  const parameters = { role: action.role, name: action.name };
  if (action.window) parameters.window = action.window;
  if (action.kind === 'type') parameters.text = action.text || '';
  if (action.kind === 'scroll') parameters.direction = action.direction || 'bottom';
  const actionType = GUI_ACTION_TYPES[action.kind] || GUI_ACTION_TYPES.click;

  try {
    const body = await runScreenAction(actionType, parameters);
    applyGuiOutcome(body, '');
  } catch (error) {
    visualState.outcome = { phase: 'refused', detail: error.message, effect: {} };
    renderActionPreview();
    toast(`The action could not be requested: ${error.message}`, 'bad');
  }
}

/**
 * Turn one `/execute` or `/approvals` response into what the panel is allowed to say.
 *
 * `waiting_permission` is the interesting branch and the reason this page cannot skip
 * the gate: the node is parked, `verified` is null, and nothing on the machine has
 * been touched. Reporting that as "ran" would be the single most damaging lie this
 * panel could tell, so it gets its own phase and its own sentence.
 */
function applyGuiOutcome(body, approvedActionId) {
  const record = screenNodeRecord(body || {});
  const output = record.result || {};
  const executionId = (body && body.execution_id) || '';
  const actionId = record.action_id || approvedActionId || '';
  // A graph can be refused before any node runs -- missing consent, an unroutable
  // device, a policy DENY. Then `nodes` is empty and the reason lives on the
  // envelope, so the envelope's state and error are what must be reported. Falling
  // through to "unknown" here would hide the one sentence that says which boundary
  // refused, which is the only thing that lets a person change it.
  const phase = record.state || (body && body.state) || 'unknown';
  const envelopeError = (body && body.error) || '';

  if (phase === 'waiting_permission' || phase === 'pending') {
    visualState.outcome = {
      phase: 'waiting', actionId, executionId, effect: {},
      detail: record.error || '',
    };
  } else if (phase === 'success') {
    const verification = record.verification || {};
    visualState.outcome = {
      phase: 'done',
      actionId,
      executionId,
      verified: verification.verified === true,
      method: verification.method || '',
      effect: {
        effect: describeGuiEffect(record.action_type, output),
        status: output.status,
      },
    };
    toast(verification.verified
      ? 'The action ran, and the effect was independently verified.'
      : 'The action ran, but its effect was not independently confirmed.', verification.verified ? 'good' : '');
    // The tree has almost certainly changed under the action; say so rather than
    // leaving a list that now describes the previous screen.
    markInspectionStale();
  } else {
    // The adapter's own sentence is the one a person can act on: it names what the
    // machine found ("no vertical scroll bar governs this element"). The node's error
    // is often a verification sentence written after the action was refused, so it is
    // the fallback rather than the first choice.
    const detail = output.detail || output.reason || record.error || envelopeError
      || (body && body.detail) || `The run ended in state ${phase}.`;
    // A missing standing scope is not a failure of the action; it is a permission
    // this page cannot grant and must not pretend it can. Name the scope and where
    // it is granted, rather than leaving a person to guess.
    const missing = /missing permission\(s\): \[([^\]]+)\]/.exec(detail);
    visualState.outcome = {
      phase: 'refused',
      actionId,
      executionId,
      effect: {},
      detail,
      scope: missing ? missing[1].replace(/['"]/g, '').trim() : '',
    };
    toast('The action did not run; the panel shows why.', 'bad');
  }
  renderActionPreview();
  renderObservation();
}

/** A sentence about the witnessed effect, or an honest "no effect was reported". */
function describeGuiEffect(actionType, output) {
  if (!output || typeof output !== 'object') return 'The run succeeded.';
  if (actionType === 'device.gui.click') {
    if (output.state_changed === true) return 'The element accepted the activation and its own state changed.';
    if (output.dispatched === true) return 'The toolkit accepted the activation; the element reported no state change.';
    return 'The run succeeded but reported no dispatch.';
  }
  if (actionType === 'device.gui.type') {
    if (output.typed !== true) return 'The run succeeded but reported no insertion.';
    // The result carries whether the insertion was accepted, never the text and never
    // its length. The adapter's own sentence says whether the field's character count
    // grew, which is the difference between "inserted" and "inserted somewhere".
    if (output.detail) return `The toolkit accepted the text: ${output.detail}.`;
    return 'The element accepted the text; whether the field grew was not reported.';
  }
  if (actionType === 'device.gui.scroll') {
    if (output.moved === true) return 'The scroll region moved.';
    if (output.already_at_edge === true) return 'The region was already at that edge; nothing moved.';
    return 'The region did not move.';
  }
  return 'The run succeeded.';
}

/** The tree the element list was read from is no longer the tree on screen. */
function markInspectionStale() {
  if (visualState.inspect) visualState.inspect.stale = true;
  renderElements();
}

/** The sentence a refusal deserves, from whichever gate said no.
 *
 * The engine reports a denial as a node whose `error` is already a complete sentence
 * ("device 'laptop-1' has risk ceiling low; this action is medium-risk"), and an HTTP
 * refusal arrives as a thrown `detail`. Both are shown verbatim rather than replaced
 * with "the capture failed", because *which* boundary refused is the thing a person
 * needs in order to change it -- and every one of them is a boundary they chose.
 */
function refusalDetail(body) {
  const record = screenNodeRecord((body && body.nodes) || {});
  const result = record && record.result;
  const candidates = [
    record && record.error,
    body && body.detail,
    result && result.reason,
  ];
  return candidates.find((value) => typeof value === 'string' && value.trim()) || '';
}

/** Pull the one action node's view out of an execution result.
 *
 * `nodes` is a mapping keyed by node id, not a list — the engine sorts it so the
 * record reads in order. An earlier version called `.find` on it and threw; the
 * thrown error surfaced as "the capture could not be requested", which is a
 * misleading sentence for a shape mistake in this page.
 */
function screenNodeRecord(result) {
  const nodes = (result && result.nodes) || {};
  const candidates = Array.isArray(nodes) ? nodes : Object.values(nodes);
  return candidates.find((n) => n && n.node_type === 'action') || candidates[0] || {};
}

function setVisualBusy(busy) {
  for (const id of ['visualCapture', 'visualInspect', 'visualAsk']) {
    const button = byId(id);
    if (button) button.disabled = Boolean(busy);
  }
}

async function captureScreen(button) {
  if (!requireBackend()) return;
  setVisualBusy(true);
  const original = button.textContent;
  button.textContent = 'Asking the OS…';
  try {
    const result = await runScreenAction('device.screen.capture', {});
    const record = screenNodeRecord(result);
    const output = record.result || {};
    applyCaptureResult(output);
    if (output.status === 'captured') {
      toast('The screen was captured by the OS portal. One frame; it is not kept.', 'good');
    } else if (output.status === 'cancelled') {
      toast('The OS declined the capture. Nothing was read.', '');
    } else {
      // `denied` is not the same as "no capture was possible": something upstream of
      // the portal said no, and its sentence names which boundary. Showing it is the
      // difference between a person fixing a setting and a person giving up.
      const why = refusalDetail(result);
      toast(why || 'No capture was possible on this machine.', why ? 'bad' : '');
    }
  } catch (error) {
    toast(`The capture could not be requested: ${error.message}`, 'bad');
  } finally {
    button.textContent = original;
    setVisualBusy(false);
  }
}

async function inspectWindow(button) {
  if (!requireBackend()) return;
  setVisualBusy(true);
  const original = button.textContent;
  button.textContent = 'Reading the tree…';
  const window = (byId('visualWindow') && byId('visualWindow').value || '').trim();
  try {
    // Forget the previous walk's id *before* the request leaves. The new chunk is
    // published while the execution is still running, so it can beat the response
    // back; if the old id were still standing, that chunk would be compared against
    // an execution it does not belong to and discarded -- leaving a non-zero count
    // beside an empty element list. With no id, the chunk is held and adopted by the
    // request that names it.
    visualState.inspectExecutionId = null;
    visualState.pendingElements = null;
    const parameters = { max_elements: 200 };
    // A named window is a narrower, more useful walk when the person knows which
    // application they mean; with no name the walk covers every top-level window,
    // which is what "inspect the active window" means without a compositor that
    // reports focus. The name is passed verbatim and matched exactly on the server.
    if (window) parameters.window = window;
    const result = await runScreenAction('device.screen.inspect', parameters);
    const record = screenNodeRecord(result);
    // The result payload is projected through a content allowlist: it carries the
    // count, the session and the refusal reason, and never the element names. Those
    // arrive separately on the transient `elements` chunk, keyed to this execution.
    visualState.inspectExecutionId = result.execution_id || null;
    visualState.inspect = record.result || {};
    visualState.inspect.elements = visualState.inspect.elements || [];
    visualState.inspect.namesRefused = false;
    visualState.inspect.namesDeliveredAt = null;
    visualState.inspect.walkTruncated = visualState.inspect.truncated === true;
    visualState.inspect.note = '';
    // The walk's chunk can beat this response back: it is published while the
    // execution is still finishing, and this promise resolves after it. Adopt a held
    // payload only when it belongs to *this* execution -- an unrelated walk from
    // another screen read must never be painted here.
    const held = visualState.pendingElements;
    visualState.pendingElements = null;
    if (held && held.executionId === visualState.inspectExecutionId) {
      applyInspectedElements(held.executionId, held.payload);
      if (held.note) applyElementsNote(held.executionId, held.note);
    }
    renderObservation();
    renderElements();
    renderWindowInfo();
    if (visualState.inspect.status === 'ok') {
      toast(`Read ${visualState.inspect.element_count} elements from the accessibility tree.`, 'good');
    } else {
      const why = refusalDetail(result);
      toast(why || 'No accessibility tree was available on this machine.', why ? 'bad' : '');
    }
  } catch (error) {
    toast(`The window could not be inspected: ${error.message}`, 'bad');
  } finally {
    button.textContent = original;
    setVisualBusy(false);
  }
}

function renderWindowInfo() {
  const box = byId('visualWindowInfo');
  if (!box) return;
  const ins = visualState.inspect;
  if (!ins || ins.status !== 'ok') { replace(box, []); return; }
  const apps = [...new Set((ins.elements || []).map((e) => e.app).filter(Boolean))];
  const parts = [
    kv('windows', String(apps.length || '—')),
    kv('elements', String(ins.element_count)),
    kv('session', ins.session || 'unknown'),
  ];
  if (apps.length) parts.push(kv('apps', apps.slice(0, 4).join(', ')));
  replace(box, parts);
}

function kv(key, value) {
  return el('span', { class: 'kv' }, [el('b', { text: `${key}:` }), el('span', { text: value })]);
}

// The "Ask About Screen" control is deliberately inert without a multimodal
// credential. It gathers the question, then states plainly that the analysis
// service is not configured -- it does not fall back to a text model guessing at
// what is on the screen, and it does not send pixels anywhere.
function wireAskAboutScreen() {
  const ask = byId('visualAsk');
  const wrap = byId('visualAskWrap');
  const input = byId('visualQuestion');
  if (!ask) return;
  ask.addEventListener('click', () => {
    if (!visualState.capture && !visualState.inspect) {
      toast('Capture the screen or inspect a window first.', '');
      return;
    }
    if (wrap) wrap.hidden = false;
    if (input) input.focus();
    // There is no multimodal model configured in this build. Rather than vanish,
    // the control says so, because "nothing happened" reads as a bug and "this is
    // not configured" reads as an answer.
    visualState.analysis = {
      status: 'unavailable',
      detail: 'Screen analysis needs a multimodal model credential, and none is configured. '
        + 'Nothing was sent anywhere. The observation above is real and can be read directly.',
    };
    renderAnalysis();
  });
  const submit = () => {
    const question = (input && input.value || '').trim();
    visualState.analysis = {
      status: 'unavailable',
      detail: question
        ? `"${question}" cannot be answered here: no multimodal model is configured, so no `
          + 'interpretation of the screen is produced. The observation is real and can be read directly.'
        : 'No multimodal model is configured, so no interpretation of the screen is produced.',
    };
    renderAnalysis();
  };
  if (input) input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') { event.preventDefault(); submit(); }
  });
}

function wireActionPreview() {
  const approve = byId('visualApprove');
  const cancel = byId('visualCancel');
  const refresh = byId('visualRefresh');
  const kind = byId('visualActionKind');
  const text = byId('visualActionText');
  const direction = byId('visualActionDirection');

  // The selectors are only ever *edits to the staged request*. Switching verbs here
  // after the server has already answered would describe something nobody asked for,
  // so a finished or parked attempt is re-staged from the element list instead.
  const editable = () => visualState.action && !visualState.outcome;
  if (kind) kind.addEventListener('change', () => {
    if (!visualState.action) return;
    if (!editable()) { clearStagedAction(); return; }
    visualState.action.kind = kind.value;
    renderActionPreview();
    renderElements();
  });
  if (text) text.addEventListener('input', () => {
    if (!editable()) return;
    visualState.action.text = text.value;
    // Repaint only the sentence that describes the request. A full render would
    // rewrite the input's own value on every keystroke and move the caret to the
    // end; the sentence is the part that changed.
    const sentence = document.querySelector('.visual-action .stage-sentence');
    if (sentence) sentence.textContent = stagedActionSentence(visualState.action);
  });
  if (direction) direction.addEventListener('change', () => {
    if (!editable()) return;
    visualState.action.direction = direction.value;
    // Same reason as the text field: only the sentence changed, and repainting the
    // whole block would fight the select the person just used.
    const sentence = document.querySelector('.visual-action .stage-sentence');
    if (sentence) sentence.textContent = stagedActionSentence(visualState.action);
  });

  if (approve) approve.addEventListener('click', () => submitStagedAction(approve));
  if (cancel) cancel.addEventListener('click', () => {
    clearStagedAction();
    renderElements();
    toast('The staged action was discarded. Nothing was sent.', '');
  });
  if (refresh) refresh.addEventListener('click', async () => {
    if (visualState.outcome && visualState.outcome.phase === 'waiting') {
      // Re-reading the tree would not answer the server's question, and leaving a
      // parked run behind while starting another is a way to lose track of which
      // action is which. Say so instead of pretending the refresh resolved it.
      toast('An action is still waiting for your decision. Approve or cancel it first.', '');
      return;
    }
    const inspect = byId('visualInspect');
    if (inspect) await inspectWindow(inspect);
    clearStagedAction();
    renderElements();
  });
  syncActionFields();
  renderActionPreview();
}

/** Mirror the staged action (or the absence of one) into the three field controls. */
function syncActionFields() {
  const action = visualState.action;
  const kind = byId('visualActionKind');
  const text = byId('visualActionText');
  const direction = byId('visualActionDirection');
  const textWrap = byId('visualTextWrap');
  const directionWrap = byId('visualDirectionWrap');
  const fields = byId('visualActionFields');
  if (!fields) return;
  fields.hidden = !action;
  if (!action) return;
  const locked = Boolean(visualState.outcome);
  if (kind) { kind.value = action.kind; kind.disabled = locked; }
  if (text) { text.value = action.text || ''; text.disabled = locked; }
  if (direction) { direction.value = action.direction || 'bottom'; direction.disabled = locked; }
  if (textWrap) textWrap.hidden = action.kind !== 'type';
  if (directionWrap) directionWrap.hidden = action.kind !== 'scroll';
}

function wireVisual() {
  wireAskAboutScreen();
  wireActionPreview();
  const cap = byId('visualCapture');
  const ins = byId('visualInspect');
  const stop = byId('visualStop');
  if (cap) cap.addEventListener('click', () => captureScreen(cap));
  if (ins) ins.addEventListener('click', () => inspectWindow(ins));
  if (stop) stop.addEventListener('click', () => {
    releaseVisualFrame();
    renderPreviewPlaceholder(
      'Sharing stopped.',
      'The frame was released. Nothing further will be captured until you ask again.',
    );
    renderSharing();
    renderObservation();
  });
  renderSharing();
  renderObservation();
  renderElements();
  renderAnalysis();
}

function refreshVisual() {
  // Entering the view reads nothing from the machine. It repaints whatever this
  // session last observed, and nothing else: a view that captured on open would be
  // the hidden continuous recording the brief forbids.
  renderSharing();
  renderObservation();
  renderElements();
  renderWindowInfo();
  renderAnalysis();
  // The one thing it does read is ActionMesh's own answer to "which machine is this?" —
  // a local HTTP call to the backend, not an observation of the desktop. Without it a
  // screen request would go out addressed to a device id this page guessed.
  if (state.backendReady && !state.localDeviceId) {
    GET('/ready')
      .then((ready) => {
        state.localDeviceId = ready.local_device_id || '';
        renderWindowInfo();
      })
      .catch(() => { /* the action itself will say the backend is unreachable */ });
  }
  return undefined;
}

// ---------------------------------------------------------------------------
// BROWSER (C42)
// ---------------------------------------------------------------------------
// Every control is a press, never a timer: each button invokes exactly one
// named preload method (`browserOpen`, `browserNavigate`, …) and renders the
// reply it returns. Replies and pushed events are turned into text with
// createElement + textContent — page content is displayed, never executed,
// and no page string is used as an element id or a selector. Refs are the
// opaque integers the snapshot stamped; the page never gets to name one.
const browserState = { lastReply: null, busy: false };

function browserRefInput() {
  const node = byId('browserRef');
  const ref = node ? Number(node.value) : NaN;
  return Number.isSafeInteger(ref) && ref >= 1 ? ref : null;
}

function renderBrowserObservation(reply) {
  const box = byId('browserObservation');
  if (!box) return;
  if (!reply) {
    replace(box, [el('p', { class: 'fine', text: 'No observation yet. Open a page to begin.' })]);
    return;
  }
  const rows = [['Status', reply.status || '—']];
  if (reply.url) rows.push(['URL', reply.url]);
  if (reply.title) rows.push(['Title', reply.title]);
  if (reply.note) rows.push(['Note', reply.note]);
  if (reply.changed !== undefined) rows.push(['Changed', reply.changed ? 'yes' : 'no']);
  replace(box, [kvTable(rows)]);
  const chip = byId('browserSessionChip');
  if (chip) {
    const live = reply.status === 'ok';
    chip.dataset.state = live ? 'on' : 'off';
    chip.textContent = live ? 'driving' : (reply.status === 'unavailable' ? 'no session' : String(reply.status || '—'));
  }
  const stateChipNode = byId('browserStateChip');
  if (stateChipNode) stateChipNode.textContent = reply.status || '—';
  const info = byId('browserWindowInfo');
  if (info) {
    replace(info, [kvTable([
      ['URL', reply.url || '—'],
      ['Epoch', reply.epoch !== undefined ? String(reply.epoch) : '—'],
    ])]);
  }
}

function renderBrowserElements(reply) {
  const list = byId('browserElements');
  const count = byId('browserElementCount');
  if (!list) return;
  const elements = reply && Array.isArray(reply.elements) ? reply.elements : [];
  if (count) count.textContent = String(elements.length);
  if (!elements.length) {
    replace(list, [el('li', { class: 'empty', text: 'No elements in the last snapshot.' })]);
    return;
  }
  const items = elements.map((item) => {
    const name = item.name ? ` — ${item.name}` : '';
    const password = item.password ? ' (password field)' : (item.disabled ? ' (disabled)' : '');
    return el('li', { text: `#${item.ref} ${item.tag}${name}${password}` });
  });
  replace(list, items);
}

async function runBrowserOp(label, makeCall) {
  if (browserState.busy) return;
  browserState.busy = true;
  try {
    const reply = await makeCall();
    browserState.lastReply = reply || null;
    renderBrowserObservation(reply);
    if (reply && Array.isArray(reply.elements)) renderBrowserElements(reply);
  } catch (error) {
    renderBrowserObservation({ status: 'error', note: error && error.message ? error.message : 'the drive refused' });
  } finally {
    browserState.busy = false;
  }
}

function wireBrowser() {
  const address = () => {
    const node = byId('browserAddress');
    return node ? node.value.trim() : '';
  };
  on('browserOpen', 'click', () => {
    const url = address();
    if (!url) { toast('Type an address first.', 'bad'); return; }
    runBrowserOp('open', () => bridge.browserOpen({ url }));
  });
  on('browserNavigate', 'click', () => {
    const url = address();
    if (!url) { toast('Type an address first.', 'bad'); return; }
    runBrowserOp('navigate', () => bridge.browserNavigate({ url }));
  });
  on('browserSearch', 'click', () => {
    const query = address();
    if (!query) { toast('Type search terms first.', 'bad'); return; }
    runBrowserOp('search', () => bridge.browserSearch({ query }));
  });
  on('browserBack', 'click', () => runBrowserOp('back', () => bridge.browserBack()));
  on('browserForward', 'click', () => runBrowserOp('forward', () => bridge.browserForward()));
  on('browserRefresh', 'click', () => runBrowserOp('refresh', () => bridge.browserRefresh()));
  on('browserWait', 'click', () => runBrowserOp('wait', () => bridge.browserWait({ ms: 500, until: 'load' })));
  on('browserStop', 'click', () => runBrowserOp('stop', () => bridge.browserStop()));
  on('browserInspect', 'click', () => runBrowserOp('inspect', () => bridge.browserInspect()));
  on('browserState', 'click', () => runBrowserOp('state', () => bridge.browserState()));
  on('browserClick', 'click', () => {
    const ref = browserRefInput();
    if (ref === null) { toast('Enter an element ref from the list (e.g. 3).', 'bad'); return; }
    runBrowserOp('click', () => bridge.browserClick({ ref }));
  });
  on('browserType', 'click', () => {
    const ref = browserRefInput();
    const textNode = byId('browserText');
    const text = textNode ? textNode.value : '';
    if (ref === null) { toast('Enter an element ref from the list (e.g. 3).', 'bad'); return; }
    runBrowserOp('type', () => bridge.browserType({ ref, text }));
    if (textNode) textNode.value = '';
  });
  on('browserSelect', 'click', () => {
    const ref = browserRefInput();
    const textNode = byId('browserText');
    const value = textNode ? textNode.value : '';
    if (ref === null) { toast('Enter an element ref from the list (e.g. 3).', 'bad'); return; }
    runBrowserOp('select', () => bridge.browserSelect({ ref, value }));
  });
  on('browserScroll', 'click', () => {
    const ref = browserRefInput();
    runBrowserOp('scroll', () => bridge.browserScroll({ direction: 'down', ref: ref === null ? undefined : ref, amount: 600 }));
  });
  if (typeof bridge.onBrowserEvent === 'function') {
    bridge.onBrowserEvent((event) => {
      if (!event) return;
      renderBrowserObservation({ status: 'ok', url: event.url, title: event.title, epoch: event.epoch });
      if (Array.isArray(event.elements)) renderBrowserElements(event);
    });
  }
}

function refreshBrowser() {
  // Entering the view reads nothing from the machine; it repaints the last reply.
  renderBrowserObservation(browserState.lastReply);
}

// ---------------------------------------------------------------------------
// VOICE COMMANDS
// ---------------------------------------------------------------------------
// Two honest facts govern this view:
//
//  1. Speech recognition is a browser feature, and Electron's renderer usually does
//     not have it. `VoiceOSVoice.supported` is read from the real object, so the
//     microphone reports that it is unavailable rather than pretending. The text
//     field beside it is the primary interface and always works.
//  2. A quick command is not a scripted result. Each one writes a sentence and sends
//     it through the same `POST /voiceos/messages` the typed box uses, so what comes
//     back is the planner's answer — including "I cannot do that here".
const VOICE_STATE_TEXT = {
  idle: 'Idle',
  requesting: 'Waiting for microphone access…',
  listening: 'Listening…',
  transcribing: 'Transcribing…',
  thinking: 'Thinking…',
  planning: 'Planning…',
  awaiting: 'Awaiting approval',
  executing: 'Executing…',
  speaking: 'Speaking…',
  error: 'Error',
};

const QUICK_COMMANDS = [
  { group: 'COMPUTER', label: 'Open Calculator', text: 'open calculator' },
  { group: 'WEB', label: 'Open Browser', text: 'open browser' },
  { group: 'WEB', label: 'Open Google Drive', text: 'open google drive' },
  { group: 'COMPUTER', label: 'Open Calculator and Clock', text: 'open calculator and clock' },
  { group: 'COMPUTER', label: 'Check if Clock Is Running', text: 'is clock running?' },
  { group: 'COMPUTER', label: 'Show Running Processes', text: 'show the running processes on this computer' },
  { group: 'COMPUTER', label: 'Show Servers', text: 'show which servers are running' },
  { group: 'DEVELOPMENT', label: 'Run Project Tests', text: 'run the project tests' },
  { group: 'DEVELOPMENT', label: 'Read Server Logs', text: 'read the server logs' },
];

// What the planner is actually able to route, and what it is not.
//
// The right-hand column used to be a hand-typed verdict, and it was wrong: on a service
// with no laptop agent, "show the running processes on this computer" matched no rule at
// all, the guide said "supported", and pressing it came back "I could not map that".
// A label nobody measures is a claim, not a fact. So the page asks the backend what its
// own planner does with each phrase (``POST /voiceos/guide``) and renders *that*.
//
// The literal stays as the list of phrases and as the fallback: `true` here means
// "ask the planner", never "supported". Only `false` and the two special kinds are
// verdicts the page makes itself, because they are not about the rule table.
const COMMAND_GUIDE = [
  {
    group: 'COMPUTER', items: [
      ['check my laptop is healthy', true],
      ['show the running processes on this computer', true],
      ['which servers are running', true],
      ['how much disk is free', true],
    ],
  },
  {
    group: 'DEVELOPMENT', items: [
      ['run the project tests', true],
      ['read the server logs', true],
      ['open my projects folder', true],
      ['start a dev server and keep it running', false],
    ],
  },
  {
    group: 'SCREEN', items: [
      ['capture my screen', 'screen'],
      ['inspect the active window', 'screen'],
      ['ask a question about the screen', 'analysis'],
      ['click that button for me', 'screen'],
    ],
  },
  {
    group: 'AUTOMATION', items: [
      ['every day at 9am check the build', true],
      ['duplicate an existing automation', true],
      ['run this automation now', true],
      ['notify me when a file appears', false],
    ],
  },
  {
    group: 'DEVICE', items: [
      ['show my active tasks', true],
      ['check the status of my devices', true],
      ['pair a new phone', true],
      ['move the work onto my other computer', false],
    ],
  },
];

//: Every phrase the page shows a verdict about: the guide's examples *and* the text of
//: each quick-command button. The buttons had their own delusion problem — a button
//: whose wording no rule matches was styled as a working command because it was never
//: measured at all. Both lists are read once here, so the request and every rendering
//: cannot disagree about what was measured.
const GUIDE_PHRASES = [
  ...COMMAND_GUIDE.flatMap((section) => section.items.map(([phrase]) => phrase)),
  ...QUICK_COMMANDS.map((command) => command.text),
].filter((phrase, index, all) => all.indexOf(phrase) === index);

let voiceWired = false;

function voiceController() {
  return window.VoiceOSVoice || null;
}

function renderVoiceState(name, detail) {
  const label = VOICE_STATE_TEXT[name] || name;
  const mic = byId('voiceMic');
  const stateLine = byId('voiceState');
  const commandStatus = byId('commandStatus');
  if (mic) mic.dataset.state = name;
  if (stateLine) {
    stateLine.dataset.state = name;
    stateLine.textContent = label;
  }
  if (commandStatus) {
    commandStatus.dataset.state = name;
    commandStatus.textContent = label;
  }
  if (detail && name === 'error') {
    toast(detail, 'bad');
  }
  const stopListening = byId('voiceStopListening');
  if (stopListening) stopListening.disabled = name !== 'listening';
  const stopSpeaking = byId('voiceStopSpeaking');
  if (stopSpeaking) stopSpeaking.disabled = name !== 'speaking';
}

function renderVoiceSupport() {
  const voice = voiceController();
  const host = byId('voiceSupport');
  const mic = byId('voiceMic');
  if (!host) return;

  if (!voice) {
    host.textContent = 'The voice module did not load. Type your command instead — everything works with text.';
    if (mic) mic.disabled = true;
    return;
  }

  const audio = state.appState?.purposes?.audio;
  const gateway = Boolean(audio?.provider === 'openai_compatible_stt' && audio.hasKey && audio.endpoint && audio.model);
  voice.setGatewayEnabled?.(gateway);
  if (mic) mic.disabled = !voice.supported;
  const parts = [];
  if (gateway && voice.supported) {
    parts.push('Press the microphone and speak. A pause ends the recording, or you can press the mic again. Turn off automatic running to review the transcript first.');
  } else if (voice.supported) {
    parts.push('Speech recognition is available in this window. Press the microphone to start one utterance; it stops on its own.');
  } else {
    parts.push('Configure speech transcription in Settings → Audio, or type your command.');
    if (mic) mic.disabled = true;
  }
  parts.push(voice.textToSpeechSupported
    ? 'Replies can be read aloud.'
    : 'Reading replies aloud is unavailable here.');
  host.textContent = parts.join(' ');
}

function renderQuickCommands() {
  const host = byId('quickGrid');
  if (!host) return;
  replace(host, QUICK_COMMANDS.map((command) => {
    // A button is an example request, not a capability claim. When the backend has
    // said its planner cannot route this wording, the button still sends the words —
    // that is the honest test — but it says first that the answer will be "I could
    // not map that", so nobody presses it expecting something to happen.
    const measured = state.guideVerdicts
      && Object.prototype.hasOwnProperty.call(state.guideVerdicts, command.text)
      ? state.guideVerdicts[command.text] : null;
    const routable = Boolean(measured && Array.isArray(measured.actions) && measured.actions.length);
    const unrouted = Boolean(measured && !routable && command.group !== 'WEB');
    const title = unrouted ? 'This command is unavailable on the current backend.' : '';
    const button = el('button', {
      class: 'quick-btn', type: 'button',
      dataset: { group: command.group, state: unrouted ? 'off' : 'ok' },
      title,
    }, [
      el('span', { class: 'quick-group', text: command.group }),
      el('span', { class: 'quick-label', text: command.label }),
    ]);
    button.addEventListener('click', () => runQuickCommand(command));
    return button;
  }));
}

function renderCommandGuide() {
  const host = byId('commandHelp');
  if (!host) return;
  // Reading the screen is a device capability; *asking a question about it* needs a
  // model, so it is available only when a provider has actually answered a probe.
  // Neither fact is assumed here.
  const capabilities = Array.isArray(state.capabilities) ? state.capabilities : [];
  const canReadScreen = capabilities.some((cap) => Array.isArray(cap.permissions)
    && cap.permissions.some((permission) => String(permission).startsWith('device.screen')));
  const providerConnected = Boolean(state.providerStatus && state.providerStatus.state === 'connected');

  // What the planner itself reports for one phrase. Three outcomes, and only the
  // first is a green "supported": a plan came back, the words were understood but the
  // capability is not registered here, or nothing matched at all. The middle case is
  // named separately because its answer is actionable ("enable the laptop agent") and
  // the last one is not.
  const verdictFor = (phrase) => {
    if (!state.guideVerdicts || !Object.prototype.hasOwnProperty.call(state.guideVerdicts, phrase)) {
      return { kind: 'unknown', note: 'not checked with this backend' };
    }
    const measured = state.guideVerdicts[phrase] || {};
    const actions = Array.isArray(measured.actions) ? measured.actions : [];
    if (actions.length) {
      return { kind: 'ok', note: `routes to ${actions.join(', ')}` };
    }
    // A live model is a second proposal source. The route measured the *rules*, so
    // "no rule" is not the same as "this cannot work" while a model answers — saying
    // otherwise would understate the system. It is still not a promise: the model's
    // proposal is validated against the same registry and policy as everything else.
    const modelLive = Boolean(state.voiceModel && state.voiceModel.mode === 'ai_model');
    const dropped = (Array.isArray(measured.notes) ? measured.notes : [])
      .filter((note) => String(note).startsWith('dropped unregistered action type'));
    if (dropped.length) {
      return { kind: 'off', note: 'planned, but this build does not register that capability' };
    }
    return modelLive
      ? { kind: 'unknown', note: 'no built-in rule; the configured model may still interpret it' }
      : { kind: 'off', note: 'the planner has no rule for this wording' };
  };

  replace(host, COMMAND_GUIDE.map((section) => el('div', { class: 'cmd-group' }, [
    el('h3', { class: 'cmd-group-title', text: section.group }),
    el('ul', { class: 'cmd-list' }, section.items.map(([phrase, status]) => {
      let verdict;
      if (status === false) verdict = { kind: 'off', note: 'not supported in this build' };
      else if (status === 'screen') {
        verdict = canReadScreen
          ? { kind: 'ok', note: 'the device advertises screen reading; "run" is still a separate press' }
          : { kind: 'off', note: 'no screen capability advertised by this device' };
      } else if (status === 'analysis') {
        verdict = providerConnected
          ? { kind: 'ok', note: 'supported — a connected provider can analyse what the screen showed' }
          : { kind: 'off', note: 'needs a connected AI provider' };
      } else {
        verdict = verdictFor(phrase);
      }
      return el('li', { class: 'cmd-item', dataset: { state: verdict.kind } }, [
        el('button', { class: 'cmd-phrase', type: 'button', text: `“${phrase}”` }),
        el('span', { class: 'cmd-status', text: verdict.note }),
      ]);
    })),
  ])));

  // Clicking a supported phrase is the same as typing it: one proposal, nothing runs.
  for (const button of host.querySelectorAll('.cmd-phrase')) {
    button.addEventListener('click', () => {
      const item = button.closest('.cmd-item');
      if (!item || item.dataset.state === 'off') {
        toast('This build cannot do that. Nothing was sent.', 'warn');
        return;
      }
      const phrase = button.textContent.replace(/[“”]/g, '');
      byId('voiceTranscript').value = phrase;
      sendVoiceTranscript({ autoRun: false });
    });
  }
}

function runQuickCommand(command) {
  byId('voiceTranscript').value = command.text;
  void sendVoiceTranscript();
}

async function sendVoiceTranscript({ autoRun = true } = {}) {
  if (state.voiceSubmitting) return false;
  const input = byId('voiceTranscript');
  const host = byId('voiceReply');
  const text = String(input.value || '').trim();
  if (!text) {
    toast('Type or dictate something first.', 'warn');
    return false;
  }
  const voice = voiceController();
  state.voiceSubmitting = true;
  if (voice) voice.setState('thinking');
  let answer;
  try { answer = await propose(text, host); }
  finally { state.voiceSubmitting = false; }
  if (!answer) {
    if (voice) voice.setState('error', '');
    return false;
  }
  if (voice) {
    voice.setState('idle');
    if (answer.reply && voice.textToSpeechSupported && (!autoRun || answer.kind !== 'task')) {
      speakVoiceReply(voice, answer.reply, true);
    }
  }
  input.value = '';
  state.voiceTask = answer.session_id || null;
  if (answer.session_id) {
    state.selectedTask = answer.session_id;
    refreshVoiceBadge();
    if (autoRun && answer.kind === 'task' && state.voiceTask === answer.session_id) {
      renderReply(host, {
        kind: 'task', session_id: answer.session_id, reply: 'Starting your task…',
      });
      replace(byId('voiceTaskResult'), el('p', { class: 'card-note', text: 'Running your task…' }));
      void runSession(answer.session_id, null, true, true);
      return true;
    }
  }
  await refreshVoiceResult();
  return true;
}

async function refreshVoiceResult() {
  const host = byId('voiceTaskResult');
  if (!host) return;
  const id = state.voiceTask;
  if (!id) {
    replace(host, el('p', { class: 'card-note', text: 'Send a task to see its plan and results here.' }));
    return;
  }
  if (state.runningTasks.has(id)) return;
  try {
    const session = await GET(`/voiceos/sessions/${encodeURIComponent(id)}`);
    if (state.voiceTask === id && !state.runningTasks.has(id)) renderTaskDetail(host, session);
  } catch (error) {
    if (state.voiceTask === id) replace(host, el('p', { class: 'card-note', text: error.message }));
  }
}

function refreshVoiceBadge() {
  const badge = document.querySelector('[data-badge="voice"]');
  if (badge) badge.hidden = true;
}

let voiceConversationActive = false;
let listenAfterReply = false;
let lastSpokenReply = "";

function endVoiceConversation() {
  listenAfterReply = false;
  if (!voiceConversationActive) return;
  voiceConversationActive = false;
  voiceController()?.cancel();
}

function speakVoiceReply(voice, text, continueConversation = false) {
  if (!continueConversation) endVoiceConversation();
  lastSpokenReply = text;
  listenAfterReply = voiceConversationActive;
  if (!voice?.speak(text)) endVoiceConversation();
}

function wireVoice() {
  if (voiceWired) return;
  voiceWired = true;
  const voice = voiceController();

  renderVoiceSupport();
  renderQuickCommands();
  byId('voiceGuide')?.addEventListener('toggle', () => {
    if (byId('voiceGuide').open) void refreshVoiceGuide();
  });
  byId('voiceDealShortcut')?.addEventListener('click', () => {
    const input = byId('voiceTranscript');
    input.value = 'Find the best deal for ';
    input.focus();
  });
  renderVoiceState('idle');

  if (voice) {
    voice.onStateChange((name, detail) => {
      renderVoiceState(name, detail);
      if (name === 'idle' && listenAfterReply) {
        listenAfterReply = false;
        if (state.view === 'voice' && voiceConversationActive) voice.startListening();
      }
      if (name === 'error') {
        endVoiceConversation();
        if (detail) toast(detail, 'bad');
      }
    });
    voice.onTranscript((text, info) => {
      if (info?.final && voiceConversationActive) {
        const command = text.trim().replace(/^(?:hey\s+)?jarvis[,!:\s]+/i, '').replace(/[.!?]+$/, '').trim();
        if (/^(stop listening|end conversation)$/i.test(command)) {
          endVoiceConversation();
          byId('voiceTranscript').value = '';
          return;
        }
        if (/^(repeat that|say that again)$/i.test(command)) {
          byId('voiceTranscript').value = '';
          speakVoiceReply(voice, lastSpokenReply || 'I have not spoken a reply yet.', true);
          return;
        }
      }
      byId('voiceTranscript').value = text;
      if (info?.final && byId('voiceHandsFree')?.checked) {
        void sendVoiceTranscript();
      }
    });
    voice.onError((message) => toast(message, 'bad'));
  }

  byId('voiceConversation')?.addEventListener('change', (event) => {
    if (!event.target.checked) endVoiceConversation();
  });
  const mic = byId('voiceMic');
  if (mic) {
    mic.addEventListener('click', () => {
      renderVoiceSupport();
      // Explicitly user-initiated: no timer, no ambient listening. A press either
      // opens the microphone for one utterance or says why it cannot.
      if (!voice) {
        toast('The voice module is not loaded. Type your command instead.', 'bad');
        return;
      }
      if (voiceConversationActive) {
        endVoiceConversation();
      } else if (voice.state === 'requesting') voice.cancel();
      else if (voice.state === 'listening') voice.stopListening();
      else {
        voiceConversationActive = Boolean(byId('voiceConversation')?.checked && byId('voiceHandsFree')?.checked);
        voice.startListening();
      }
    });
  }

  const stopListening = byId('voiceStopListening');
  if (stopListening) stopListening.addEventListener('click', () => {
    if (voiceConversationActive) endVoiceConversation();
    else voice?.stopListening();
  });
  const stopSpeaking = byId('voiceStopSpeaking');
  if (stopSpeaking) stopSpeaking.addEventListener('click', () => (voice ? voice.stopSpeaking() : null));
  const cancel = byId('voiceCancel');
  if (cancel) cancel.addEventListener('click', () => {
    endVoiceConversation();
    if (voice) voice.cancel();
    byId('voiceReply').hidden = true;
    renderVoiceState('idle');
  });
  const clear = byId('voiceClear');
  if (clear) clear.addEventListener('click', () => {
    byId('voiceTranscript').value = '';
    byId('voiceReply').hidden = true;
    state.voiceTask = null;
    void refreshVoiceResult();
  });
  const send = byId('voiceSend');
  if (send) send.addEventListener('click', sendVoiceTranscript);
  const transcript = byId('voiceTranscript');
  if (transcript) {
    transcript.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        sendVoiceTranscript();
      }
    });
  }

  // The persistent bottom bar is the same pipeline, reachable from every view.
  const barInput = byId('commandInput');
  const commandSend = byId('commandSend');
  if (commandSend) {
    commandSend.addEventListener('click', async () => {
      byId('voiceTranscript').value = barInput.value;
      showView('voice');
      const sent = barInput.value;
      await sendVoiceTranscript();
      // Only clear the bar when the request actually went out; a failed proposal
      // leaves the sentence where the person can retry it.
      if (sent && barInput.value === sent) barInput.value = '';
    });
  }
  if (barInput) {
    barInput.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        if (commandSend) commandSend.click();
      }
    });
  }
  const commandMic = byId('commandMic');
  if (commandMic) {
    commandMic.addEventListener('click', () => {
      showView('voice');
      renderVoiceSupport();
      if (voice?.supported) byId('voiceMic')?.click();
    });
  }
}

async function refreshVoice() {
  await refreshAppState();
  renderVoiceSupport();
  await Promise.all([refreshVoiceResult(), refreshVoiceModel()]);
  if (byId('voiceGuide')?.open) await refreshVoiceGuide();
}

async function refreshVoiceGuide() {
  // The capability list decides whether "ask about the screen" is offered as
  // supported. It is read, not guessed.
  if (state.backendReady && !state.capabilities) {
    try {
      state.capabilities = await GET('/capabilities');
    } catch { /* the guide falls back to "not supported" wording */ }
  }
  await refreshCommandGuideVerdicts();
  renderCommandGuide();
  renderQuickCommands();
}

/**
 * Ask the running backend what its own planner does with the guide's example phrasings.
 *
 * This is the difference between a documented capability and a measured one. The
 * route runs each phrase through the same planner a real turn uses, and the answer is
 * kept as-is: a phrase with no matching rule comes back with no actions, and the guide
 * says so. A backend that cannot be reached leaves the verdicts `null`, which renders
 * as "not checked with this backend" rather than as working.
 */
async function refreshCommandGuideVerdicts() {
  if (!state.backendReady) { state.guideVerdicts = null; return; }
  try {
    const answer = await POST('/voiceos/guide', { phrases: GUIDE_PHRASES });
    const byPhrase = {};
    for (const entry of (answer && Array.isArray(answer.phrases) ? answer.phrases : [])) {
      if (!entry || typeof entry.phrase !== 'string') continue;
      byPhrase[entry.phrase] = {
        actions: Array.isArray(entry.actions) ? entry.actions : [],
        notes: Array.isArray(entry.notes) ? entry.notes : [],
      };
    }
    state.guideVerdicts = byPhrase;
  } catch {
    // Not a fatal condition and not a silent one: the guide's own rows say the
    // measurement is missing, which is the honest reading of a failed request.
    state.guideVerdicts = null;
  }
}

/**
 * Which planner will answer the next turn — read from `/voiceos/status`, never assumed.
 *
 * The distinction matters at the point of use: a user who configured a provider needs
 * to know that the *next sentence* will reach it. A configured provider whose bridge
 * is down still answers, just not with a model, and the reason says why. The failure
 * to reach the status route is itself reported as unknown rather than as working.
 */
async function refreshVoiceModel() {
  const host = byId('voiceModel');
  if (!host) return;
  if (!state.backendReady) {
    setVoiceModel({ mode: 'unknown', reason: 'The backend is not running, so no planner can answer.' });
    return;
  }
  try {
    const status = await GET('/voiceos/status');
    state.voiceModel = status;
    setVoiceModel(status);
  } catch (error) {
    setVoiceModel({ mode: 'unknown', reason: `Its planning mode could not be read: ${error.message}` });
  }
}

function setVoiceModel(status) {
  const host = byId('voiceModel');
  if (!host) return;
  const mode = status && status.mode === 'ai_model' ? 'ai_model' : 'deterministic';
  const label = mode === 'ai_model'
    ? (status.mode_label || 'AI model active')
    : 'Deterministic fallback';
  const who = mode === 'ai_model' && (status.provider || status.model)
    ? ` — ${[status.provider, status.model].filter(Boolean).join(' / ')}`
    : '';
  host.dataset.mode = mode;
  host.textContent = `Planner: ${label}${who}`;
  // The reason is the whole point when a provider was configured and is not being used.
  // A page without it would show the fallback as if it were the intended behaviour.
  const reason = String((status && status.reason) || '').trim();
  host.title = reason || (mode === 'ai_model'
    ? 'The configured model plans the next turn; policy, approval and the executor still decide what happens.'
    : 'No model is configured, so the built-in rule-based planner answers. Add a provider in Settings to use a model.');
}

// ---------------------------------------------------------------------------
// AI PROVIDER — four purposes: chat | reasoning | image | audio
// ---------------------------------------------------------------------------
// The renderer draws one compact bar in the header (chat only) and four lane
// cards into #homeProviderKv and #settingsProviderKv. All four talk to the
// purpose-scoped IPC surface only — no key material ever crosses the bridge,
// and "Connected" is shown only when a test call actually said so.
const PURPOSES = ['chat', 'reasoning', 'image', 'audio'];

/**
 * The elements every screen depends on.
 *
 * Not every id in index.html belongs here — a control that is missing costs its own
 * feature and is guarded where it is wired. These are the anchors: the two view
 * headings, the rail, the toast, the lane hosts, and the provider modal. Without them
 * the page cannot describe itself truthfully, and starting anyway would paint a shell
 * whose every claim was about absent controls.
 */
const REQUIRED_IDS = [
  'app', 'viewEyebrow', 'viewTitle',
  'railDot', 'railStatus', 'railVersion', 'railShortcut', 'streamPill', 'toast',
  'homeProviderKv', 'settingsProviderKv', 'providerStatus',
  'providerModal', 'providerModalTitle', 'cfgProvider', 'cfgModel',
  'cfgTest', 'cfgSave', 'cfgRemove', 'cfgApiKey', 'cfgStatus',
];

const PURPOSE_LABELS = {
  chat: 'Chat',
  reasoning: 'Reasoning',
  image: 'Image',
  audio: 'Audio',
};

const PROVIDER_STATE_CHIP = {
  not_configured: 'Not configured',
  missing_key: 'Missing API key',
  connecting: 'Connecting…',
  connected: 'Connected',
  invalid_credentials: 'Invalid credentials',
  provider_unavailable: 'Provider unavailable',
  timeout: 'Timeout',
  rate_limited: 'Rate limited',
};

// The modal is one shared form; `#providerModal[data-purpose]` says which job it
// is configuring. Every save/test/remove the modal performs carries that string.
function currentModalPurpose() {
  const backdrop = byId('providerModal');
  const p = backdrop && backdrop.dataset ? backdrop.dataset.purpose : '';
  return PURPOSES.includes(p) ? p : 'chat';
}

async function loadPurposeSpecs() {
  // Static tables from the main process, read once per purpose before the first
  // render. A failure leaves that lane's list empty rather than inventing a
  // provider; the lanes still render with "not configured".
  for (const purpose of PURPOSES) {
    try {
      const answer = await bridge.purposeSpecs(purpose);
      state.purposeSpecs[purpose] = (answer && answer.ok && answer.specs) || [];
    } catch {
      state.purposeSpecs[purpose] = [];
    }
  }
}

async function refreshPurposes() {
  // Pull each purpose's status explicitly. `appState().purposes` pushes the same
  // objects on a backend change; this is the pull path used by boot, the poll, and
  // after any configure/test/remove so the lanes show the truth, not a guess.
  for (const purpose of PURPOSES) {
    try {
      const answer = await bridge.purposeStatus(purpose);
      if (answer && answer.ok && answer.status) {
        state.purposeStatuses[purpose] = answer.status;
        if (purpose === 'chat') state.providerStatus = answer.status;
      }
    } catch {
      // Leave the last-known status in place; a failed pull must not blank the UI
      // into implying something was removed.
    }
  }
  renderProviderBar(state.purposeStatuses.chat || state.providerStatus);
  renderHomeLanes();
  renderSettingsLanes();
}

function laneButton(action, purpose, label, extraAttrs) {
  return el('button', {
    type: 'button',
    class: 'btn ghost small',
    dataset: { laneAction: action, purpose, ...(extraAttrs || {}) },
    text: label,
  });
}

function laneCard(purpose) {
  const status = state.purposeStatuses[purpose] || {};
  const stateName = status.state || 'not_configured';
  const lines = [];
  const providerLine = status.provider
    ? `${status.providerLabel || status.provider}${status.model ? ` · ${status.model}` : ''}`
    : 'no provider chosen';
  lines.push(el('div', { class: 'purpose-lane-line', text: providerLine }));
  if (status.legacy) {
    lines.push(el('div', {
      class: 'purpose-lane-line',
      text: 'Using your earlier chat setup — saving here switches this job to its own configuration.',
    }));
  }
  const keyLine = status.hasKey
    ? 'Key held in the OS keyring.'
    : (status.encryptionAvailable === false
      ? 'No key can be stored: the OS keyring is unavailable.'
      : 'No key stored yet.');
  lines.push(el('div', { class: 'purpose-lane-line', text: keyLine }));
  if (status.detail) {
    lines.push(el('div', { class: 'purpose-lane-detail', text: status.detail }));
  }
  const tested = status.testedAt ? ` · tested ${timeAgo(status.testedAt)}` : '';
  lines.push(el('div', { class: 'purpose-lane-tested', text: tested.slice(3) || '' }));

  const actions = el('div', { class: 'purpose-lane-actions' }, [
    laneButton('configure', purpose, 'Configure'),
    laneButton('test', purpose, 'Test'),
  ]);
  if (purpose === 'audio' && status.provider && status.hasKey) {
    const record = el('button', { class: 'btn ghost small', type: 'button', text: 'Open Voice Commands' });
    record.addEventListener('click', () => showView('voice'));
    actions.append(record);
  }
  if (stateName === 'invalid_credentials') {
    lines.push(el('p', { class: 'fine', text: 'Open Configure, check the endpoint and replace the API key, then test again.' }));
  }
  if (purpose === 'chat') {
    // "Default" is the planner's lane preference, so the card says which state it is
    // in rather than offering the same press forever.
    const isDefault = Boolean(state.appState && state.appState.settings
      && state.appState.settings.providerDefault);
    if (isDefault) {
      lines.push(el('div', { class: 'purpose-lane-line', text: 'The planner uses this lane.' }));
    }
    actions.append(laneButton('default', 'chat', isDefault ? 'Planner default' : 'Set as default'));
  }

  return el('div', { class: 'purpose-lane', dataset: { purpose } }, [
    el('div', { class: 'purpose-lane-head' }, [
      el('span', { class: 'purpose-lane-name', text: PURPOSE_LABELS[purpose] || purpose }),
      stateChip(stateName),
    ]),
    ...lines,
    actions,
  ]);
}

function renderLanesInto(host) {
  if (!host) return;
  replace(host, PURPOSES.map(laneCard));
}

function renderHomeLanes() {
  renderLanesInto(byId('homeProviderKv'));
  const configure = byId('homeProviderConfigure');
  if (configure) configure.disabled = false;
  // "Set default" picks the *currently configured* chat provider as the planner's
  // remembered choice — it needs a configured chat lane, not a verified one. The
  // button is wired once; here it only tracks whether there is anything to set.
  const homeDefault = byId('homeProviderDefault');
  if (homeDefault) {
    if (!homeDefault._wired) {
      homeDefault._wired = true;
      homeDefault.addEventListener('click', () => laneSetDefault().catch((error) => toast(error.message, 'bad')));
    }
    const chat = state.purposeStatuses.chat || {};
    homeDefault.disabled = !chat.provider;
  }
}

function renderSettingsLanes() {
  renderLanesInto(byId('settingsProviderKv'));
  const note = byId('settingsProviderNote');
  if (note) {
    note.textContent = 'Use Test Connection for Chat and Reasoning. Audio is verified '
      + 'when a recording in Voice Commands returns a transcript. Image generation is unavailable.';
  }
}

// Kept as the name refreshHome() calls; it now renders the four lanes.
function renderHomeProviderKv() {
  renderHomeLanes();
}

// Kept as the name refreshSettings() calls; it now renders the four lanes.
function renderSettingsProviderCard() {
  renderSettingsLanes();
}

function renderProviderBar(status) {
  // The header bar is the chat lane in miniature: one provider select, one model
  // select, one verdict. The selects are looked up by class inside the chat bar,
  // and every lookup is guarded — a missing element must never crash boot().
  const bar = document.querySelector('.provider-bar[data-purpose="chat"]');
  if (!bar) return;
  const select = bar.querySelector('.purpose-provider-select');
  const modelSelect = bar.querySelector('.purpose-model-select');
  const chip = byId('providerStatus');

  const specs = state.purposeSpecs.chat || [];
  if (select) {
    if (select.options.length !== specs.length || select.dataset.signature !== specs.map((s) => s.id).join('|')) {
      select.dataset.signature = specs.map((s) => s.id).join('|');
      clear(select);
      for (const spec of specs) {
        select.append(el('option', {
          value: spec.id,
          text: spec.implemented ? spec.label : `${spec.label} — not implemented`,
          disabled: !spec.implemented,
        }));
      }
    }
    // Unimplemented options are present and disabled: hiding them would make the
    // list look like the whole world, and selecting one is refused at the main
    // process too.
    if (status && status.provider) select.value = status.provider;
  }

  if (modelSelect) {
    const currentSpec = specs.find((spec) => spec.id === (status && status.provider));
    const models = currentSpec ? [...currentSpec.models] : [];
    const currentModel = (status && status.model) || '';
    if (currentModel && !models.includes(currentModel)) models.unshift(currentModel);
    const modelSignature = models.join('|');
    if (modelSelect.dataset.signature !== modelSignature) {
      modelSelect.dataset.signature = modelSignature;
      clear(modelSelect);
      if (!models.length) {
        modelSelect.append(el('option', { value: '', text: 'no model' }));
      } else {
        for (const model of models) modelSelect.append(el('option', { value: model, text: model }));
      }
    }
    modelSelect.value = currentModel;
    modelSelect.disabled = !(status && status.provider);
  }

  if (chip) {
    const key = status ? status.state : 'not_configured';
    chip.dataset.state = key;
    chip.textContent = PROVIDER_STATE_CHIP[key] || 'Unknown';
    chip.title = status && status.detail ? status.detail : '';
  }
}

function openProviderModal(purpose) {
  const backdrop = byId('providerModal');
  if (!backdrop) return;
  const p = PURPOSES.includes(purpose) ? purpose : currentModalPurpose();
  backdrop.dataset.purpose = p;
  const title = byId('providerModalTitle');
  if (title) title.textContent = `AI provider configuration — ${PURPOSE_LABELS[p] || p}`;
  backdrop.hidden = false;
  fillProviderModal(p);
  const first = byId('cfgProvider');
  if (first) first.focus();
}

function closeProviderModal() {
  const backdrop = byId('providerModal');
  if (backdrop) backdrop.hidden = true;
  const key = byId('cfgApiKey');
  if (key) key.value = '';
}

function specsForModal(purpose) {
  return state.purposeSpecs[purpose] || [];
}

function fillProviderModal(purpose) {
  const p = purpose || currentModalPurpose();
  const specs = specsForModal(p);
  const status = state.purposeStatuses[p] || {};
  const select = byId('cfgProvider');
  if (!select) return;
  // The option list is rebuilt whenever the purpose changes or the table differs;
  // reusing a stale list from another purpose would offer providers that lane
  // cannot use.
  const signature = `${p}|${specs.map((s) => s.id).join('|')}`;
  if (select.dataset.signature !== signature) {
    select.dataset.signature = signature;
    clear(select);
    for (const spec of specs) {
      select.append(el('option', {
        value: spec.id,
        text: spec.implemented ? spec.label : `${spec.label} (not implemented)`,
        disabled: !spec.implemented,
      }));
    }
  }
  // With nothing configured, `status.provider` is empty. Falling back to the first
  // usable spec is the right *suggestion*, and the select must show exactly that,
  // because the form would otherwise read "nothing chosen" while a provider is
  // visibly selected — and Save would store it without the person choosing it.
  const firstUsable = specs.find((spec) => spec.implemented) || specs[0];
  select.value = status.provider || (firstUsable && firstUsable.id) || '';
  byId('cfgModel').value = status.model || '';
  // Show what is actually stored, so pressing Save without touching the form cannot
  // silently erase a custom endpoint or a non-default timeout. The endpoint belongs to
  // the stored provider; if the person has picked a different one it is not theirs to
  // reuse, so it stays blank.
  byId('cfgBaseUrl').value = select.value === status.provider ? (status.endpoint || '') : '';
  byId('cfgApiKey').value = '';
  byId('cfgTimeout').value = String(status.timeout || 12);
  applyProviderChoice();
  const keyHint = byId('cfgKeyHint');
  if (status.hasKey) {
    keyHint.textContent = 'A key is stored in the OS keyring for this job. It is never shown again; paste a new one to replace it.';
  } else if (status.encryptionAvailable === false) {
    keyHint.textContent = 'The OS keyring is unavailable, so no key can be stored here. Nothing is written in plain text.';
  } else {
    keyHint.textContent = 'Stored by the OS keyring, for this job only. It is never returned to this page.';
  }
  if (!status.provider) {
    setCfgStatus(
      'not_configured',
      'Nothing is configured for this job yet. The provider and model above are a suggestion; '
        + 'Save configuration stores the one you pick.',
    );
    return;
  }
  setCfgStatus(status.state || 'not_configured', status.detail || '');
}

function applyProviderChoice() {
  const p = currentModalPurpose();
  const id = byId('cfgProvider').value;
  const spec = specsForModal(p).find((entry) => entry.id === id);
  const baseRow = byId('cfgBaseUrlRow');
  if (baseRow) baseRow.hidden = !(spec && spec.requiresBaseUrl);

  const list = byId('cfgModelOptions');
  clear(list);
  if (spec) {
    for (const model of spec.models) list.append(el('option', { value: model }));
    const modelInput = byId('cfgModel');
    if (!modelInput.value && spec.defaultModel) modelInput.value = spec.defaultModel;
  }
  const hint = byId('cfgProviderHint');
  if (!spec) hint.textContent = '';
  else if (!spec.implemented) hint.textContent = spec.unavailableReason || 'Not implemented.';
  else hint.textContent = spec.requiresBaseUrl
    ? 'Any https endpoint that speaks the OpenAI chat-completions protocol.'
    : 'Test Connection calls the provider adapter for real — a stored key alone is not "connected".';
  byId('cfgTest').disabled = !spec || !spec.implemented;
  byId('cfgSave').disabled = !spec || !spec.implemented;
  // "Default" is the legacy single-provider notion — the planner's chat choice.
  // It is honest only for the chat lane.
  const def = byId('cfgDefault');
  if (def) def.disabled = p !== 'chat';
}

function setCfgStatus(stateName, detail) {
  const chip = byId('cfgStatus');
  if (!chip) return;
  chip.dataset.state = stateName;
  chip.textContent = (PROVIDER_STATE_CHIP[stateName] || stateName) + (detail ? ` — ${detail}` : '');
}

function providerPayload() {
  // Only these fields leave the renderer, plus the purpose they belong to.
  // No actor, no authority, no capability — and the key only ever leaves on a
  // save/test call, never back.
  return {
    purpose: currentModalPurpose(),
    provider: byId('cfgProvider').value,
    model: byId('cfgModel').value.trim(),
    baseUrl: byId('cfgBaseUrl').value.trim(),
    apiKey: byId('cfgApiKey').value,
    timeoutSeconds: Number(byId('cfgTimeout').value) || 12,
  };
}

async function testProviderConnection() {
  const button = byId('cfgTest');
  button.disabled = true;
  setCfgStatus('connecting', 'calling the provider');
  try {
    const answer = await bridge.purposeTest(providerPayload());
    if (!answer || !answer.ok) {
      setCfgStatus('provider_unavailable', (answer && answer.error) || 'the main process refused the request');
      return;
    }
    setCfgStatus(answer.state, answer.detail || '');
    // Testing does not save. A probe the main process recorded becomes the lane's
    // state, so the header, Home and Settings follow it; the modal is refilled only
    // when the stored status now agrees with what was just printed. Otherwise the
    // refill would wipe the verdict with "nothing configured yet" — reporting the
    // old state over a credential the provider just refused.
    await refreshPurposes();
    const refreshed = state.purposeStatuses[currentModalPurpose()];
    if (refreshed && refreshed.state === answer.state) fillProviderModal();
  } catch (error) {
    setCfgStatus('provider_unavailable', error.message);
  } finally {
    button.disabled = false;
  }
}

// Write the fields the person can see, and merge in the stored provider, model
// and endpoint this lane already uses when the modal left them blank. Without the
// merge, pressing Save after a provider switch would silently drop a configured
// model or custom endpoint.
function mergeConfigured(patch, configured) {
  const out = { ...patch };
  if (!out.provider) out.provider = configured.provider || '';
  if (!out.model) out.model = configured.model || '';
  if (!out.baseUrl) out.baseUrl = configured.endpoint || '';
  return out;
}

async function saveProviderConfig() {
  const button = byId('cfgSave');
  button.disabled = true;
  try {
    const payload = mergeConfigured(providerPayload(), state.purposeStatuses[currentModalPurpose()] || {});
    const answer = await bridge.purposeConfigure(payload);
    if (!answer || !answer.ok) {
      setCfgStatus('not_configured', (answer && answer.error) || 'not saved');
      return;
    }
    byId('cfgApiKey').value = '';
    toast(`${PURPOSE_LABELS[payload.purpose]} configuration saved. Run Test Connection to verify it.`, 'good');
    state.purposeStatuses[payload.purpose] = answer.status;
    if (payload.purpose === 'chat') state.providerStatus = answer.status;
    renderProviderBar(state.purposeStatuses.chat);
    fillProviderModal();
    renderHomeProviderKv();
    renderSettingsProviderCard();
  } catch (error) {
    setCfgStatus('not_configured', error.message);
  } finally {
    button.disabled = false;
  }
}

async function removeProviderKey() {
  const button = byId('cfgRemove');
  button.disabled = true;
  try {
    const payload = providerPayload();
    const answer = await bridge.purposeRemove({ purpose: payload.purpose });
    if (!answer || !answer.ok) {
      toast((answer && answer.error) || 'The key was not removed.', 'bad');
      return;
    }
    toast(`${PURPOSE_LABELS[payload.purpose]} key removed from the keyring.`, 'good');
    state.purposeStatuses[payload.purpose] = answer.status;
    if (payload.purpose === 'chat') state.providerStatus = answer.status;
    renderProviderBar(state.purposeStatuses.chat);
    fillProviderModal();
    renderHomeProviderKv();
    renderSettingsProviderCard();
  } catch (error) {
    toast(`The key was not removed: ${error.message}`, 'bad');
  } finally {
    button.disabled = false;
  }
}

async function setProviderAsDefault() {
  // "Default" is a real setting with one consumer: the planner prefers the chat
  // lane over the reasoning lane when both are configured. It grants nothing —
  // the backend's policy is unchanged. Only chat has a default to set.
  if (currentModalPurpose() !== 'chat') return;
  const answer = await bridge.purposeSetDefault({ purpose: 'chat' });
  if (answer && answer.ok) {
    toast('The planner will now use your chat lane.', 'good');
    state.providerStatus = answer.status;
    state.purposeStatuses.chat = answer.status;
    renderProviderBar(answer.status);
    renderHomeProviderKv();
    await refreshPurposes();
  } else {
    toast((answer && answer.error) || 'Could not set the default.', 'bad');
  }
}

async function laneTest(purpose) {
  // The lane's Test button runs the *stored* configuration for that job: an empty
  // override tells the main process to read the keyring itself.
  const status = state.purposeStatuses[purpose] || {};
  if (!status.provider) {
    toast(`${PURPOSE_LABELS[purpose]} has no provider to test yet.`, 'bad');
    return;
  }
  const answer = await bridge.purposeTest({ purpose });
  if (!answer || !answer.ok) {
    toast((answer && answer.error) || 'The test could not run.', 'bad');
    return;
  }
  toast(`${PURPOSE_LABELS[purpose]} — ${PROVIDER_STATE_CHIP[answer.state] || answer.state}.`,
    answer.state === 'connected' ? 'good' : 'bad');
  await refreshPurposes();
}

async function laneSetDefault() {
  const status = state.purposeStatuses.chat || {};
  if (!status.provider) {
    toast('Configure a chat provider first.', 'bad');
    return;
  }
  const answer = await bridge.purposeSetDefault({ purpose: 'chat' });
  if (answer && answer.ok) {
    toast('The planner will now use your chat lane.', 'good');
    state.providerStatus = answer.status;
    state.purposeStatuses.chat = answer.status;
    renderProviderBar(answer.status);
    renderHomeProviderKv();
    await refreshPurposes();
  } else {
    toast((answer && answer.error) || 'Could not set the default.', 'bad');
  }
}

function wireProvider() {
  // Header chat bar. The selects live inside `.provider-bar[data-purpose="chat"]`
  // and are addressed by class; every lookup is guarded so a layout change can
  // never take boot() down with it.
  const bar = document.querySelector('.provider-bar[data-purpose="chat"]');
  if (bar) {
    const modelSelect = bar.querySelector('.purpose-model-select');
    if (modelSelect) {
      // Changing the model is a real setting change, with no other effect.
      modelSelect.addEventListener('change', async (event) => {
        try {
          const answer = await bridge.purposeSetModel({ purpose: 'chat', model: event.target.value });
          if (answer && answer.ok && answer.status) {
            state.purposeStatuses.chat = answer.status;
            state.providerStatus = answer.status;
            renderProviderBar(answer.status);
            renderHomeProviderKv();
            toast('Model changed. Run Test Connection to verify it.', 'good');
          } else {
            toast((answer && answer.error) || 'The model was not changed.', 'bad');
          }
        } catch (error) {
          toast(`The model was not changed: ${error.message}`, 'bad');
        }
      });
    }
    const providerSelect = bar.querySelector('.purpose-provider-select');
    if (providerSelect) {
      providerSelect.addEventListener('change', (event) => {
        // Choosing a provider in the header only pre-fills the modal; it does not
        // save, because a provider without a key is not a configuration.
        openProviderModal('chat');
        const cfg = byId('cfgProvider');
        if (cfg) {
          cfg.value = event.target.value;
          applyProviderChoice();
        }
      });
    }
  }

  const configure = byId('providerConfigure');
  if (configure) {
    configure.addEventListener('click', () => openProviderModal(configure.dataset.configurePurpose || 'chat'));
  }
  const homeConfigure = byId('homeProviderConfigure');
  if (homeConfigure) homeConfigure.addEventListener('click', () => openProviderModal('chat'));

  // Lane buttons are event-delegated: the lane cards are re-rendered on every
  // status change, and a listener on the container survives that without
  // re-wiring or duplicating.
  for (const host of [byId('homeProviderKv'), byId('settingsProviderKv')]) {
    if (!host) continue;
    host.addEventListener('click', (event) => {
      const button = event.target.closest ? event.target.closest('button[data-lane-action]') : null;
      if (!button || !host.contains(button)) return;
      const action = button.dataset.laneAction;
      const purpose = button.dataset.purpose;
      if (action === 'configure') openProviderModal(purpose);
      else if (action === 'test') laneTest(purpose).catch((error) => toast(error.message, 'bad'));
      else if (action === 'default') laneSetDefault().catch((error) => toast(error.message, 'bad'));
    });
  }

  const close = byId('providerModalClose');
  if (close) close.addEventListener('click', closeProviderModal);
  const backdrop = byId('providerModal');
  if (backdrop) {
    backdrop.addEventListener('click', (event) => {
      if (event.target === backdrop) closeProviderModal();
    });
  }
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && backdrop && !backdrop.hidden) closeProviderModal();
  });

  on('cfgProvider', 'change', applyProviderChoice);
  on('cfgTest', 'click', testProviderConnection);
  on('cfgSave', 'click', saveProviderConfig);
  on('cfgRemove', 'click', removeProviderKey);
  on('cfgDefault', 'click', setProviderAsDefault);
}


// Sidebar collapse and the header's small controls.
function wireShell() {
  const toggle = byId('sidebarToggle');
  const sidebar = byId('sidebar');
  if (toggle && sidebar) {
    toggle.addEventListener('click', () => {
      const collapsed = sidebar.classList.toggle('collapsed');
      toggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    });
  }
  const devicePill = byId('devicePill');
  if (devicePill) devicePill.addEventListener('click', () => showView('devices'));
  const notify = byId('notifyBtn');
  if (notify) notify.addEventListener('click', () => showView('approvals'));
  const liveReconnect = byId('liveReconnect');
  if (liveReconnect) liveReconnect.addEventListener('click', () => {
    try {
      bridge.startStream({});
      toast('Reconnecting the event stream…', '');
    } catch (error) {
      toast(`Could not reconnect: ${error.message}`, 'bad');
    }
  });
}

function refreshCurrent() {
  switch (state.view) {
    case 'home': return refreshHome();
    case 'tasks': return refreshTasks();
    case 'automations': return refreshAutomations();
    case 'history': {
      // The automations history and the executions history share the view.
      // Each owns its own list; the executions list stays silent until its
      // hosts exist (history.js guards), so nothing here can break the panel.
      const primary = refreshHistory();
      if (typeof ActionMeshHistory !== 'undefined' && ActionMeshHistory.refreshExecHistory) {
        return Promise.allSettled([primary, ActionMeshHistory.refreshExecHistory()]);
      }
      return primary;
    }
    case 'live': return refreshLive();
    case 'approvals': return refreshApprovals();
    case 'devices': return refreshDevices();
    case 'visual': return refreshVisual();
    case 'browser': return refreshBrowser();
    case 'providers': return bridge.state().then(applyAppState);
    case 'settings': return refreshSettings();
    case 'voice': return refreshVoice();
    default: return undefined;
  }
}

function wire() {
  const taskFilters = document.querySelectorAll('[data-task-filter]');
  for (const button of taskFilters) {
    button.addEventListener('click', () => {
      for (const filter of taskFilters) filter.setAttribute('aria-pressed', String(filter === button));
      if (state.backendReady) renderFilteredTasks();
    });
  }
  byId('taskRefresh').addEventListener('click', () => void refreshTasks());
  byId('taskResetFilters').addEventListener('click', () => {
    const search = byId('taskSearch');
    search.value = '';
    document.querySelector('[data-task-filter="all"]').click();
    search.focus();
  });
  byId('taskSearch').addEventListener('input', () => {
    if (state.backendReady) renderFilteredTasks();
  });
  // Navigation
  for (const button of document.querySelectorAll('.nav-item')) {
    button.addEventListener('click', () => showView(button.dataset.view));
  }
  for (const button of document.querySelectorAll('[data-goto]')) {
    button.addEventListener('click', () => showView(button.dataset.goto));
  }
  on('refreshAll', 'click', () => {
    refreshCurrent();
    if (state.view === 'settings') loadBackendLog();
  });

  // Asking
  const homeInput = byId('homeAskText');
  const homeReply = byId('homeReply');
  const askHome = () => {
    propose(homeInput.value, homeReply).then((answer) => {
      if (answer) homeInput.value = '';
      refreshHome();
    });
  };
  on('homeAskSend', 'click', askHome);
  on('homeAsk', 'submit', (event) => { event.preventDefault(); askHome(); });
  wirePushToTalk(byId('homeAskMic'));

  const taskInput = byId('taskAskText');
  const taskReply = el('div', { class: 'reply', hidden: true });
  const taskForm = byId('taskAsk');
  if (taskForm) taskForm.after(taskReply);
  on('taskAsk', 'submit', (event) => event.preventDefault());
  const askTask = () => {
    propose(taskInput.value, taskReply, {
      onSession: (id) => { state.selectedTask = id; },
    }).then((answer) => {
      if (!answer) return;
      taskInput.value = '';
      const notes = byId('taskNotes');
      if (notes) notes.textContent = 'A task was proposed. Press Run on a step to carry it out.';
      refreshTasks();
      if (answer.session_id) loadTask(answer.session_id);
    });
  };
  on('taskAskSend', 'click', askTask);
  if (taskInput) {
    taskInput.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') { event.preventDefault(); askTask(); }
    });
  }
  wirePushToTalk(byId('taskAskMic'));

  // Visual Intelligence (C38): every control is a press, never a timer.
  wireVisual();
  wireBrowser();
  wireShell();
  wireVoice();
  wireProvider();

  // Approvals and the standing permissions that keep them rare
  on('consentGrant', 'click', (event) => {
    event.preventDefault();
    grantConsent(event.currentTarget);
  });
  on('consentScope', 'keydown', (event) => {
    if (event.key === 'Enter') { event.preventDefault(); grantConsent(byId('consentGrant')); }
  });

  // Automations
  on('automationRefresh', 'click', () => refreshAutomations());
  on('automationDraftGo', 'click', draftAutomation);
  on('automationText', 'keydown', (event) => {
    if (event.key === 'Enter') { event.preventDefault(); draftAutomation(); }
  });
  on('historyRefresh', 'click', () => refreshHistory());
  on('execHistoryRefresh', 'click', () => {
    if (typeof ActionMeshHistory !== 'undefined' && ActionMeshHistory.refreshExecHistory) {
      ActionMeshHistory.refreshExecHistory();
    }
  });

  // Devices
  on('devicesRefresh', 'click', () => refreshDevices());
  on('pairMint', 'click', mintPairing);
  on('pairRevoke', 'click', revokePairings);
  on('pairDeviceId', 'input', (event) => {
    event.target.dataset.touched = 'true';
  });

  // Settings
  on('settingsSave', 'click', (event) => { event.preventDefault(); saveSettings(); });
  on('settingsAbout', 'click', (event) => { event.preventDefault(); bridge.showAbout(); });
  on('backendRestart', 'click', () => backendAction('restart'));
  on('backendStop', 'click', () => backendAction('stop'));
  on('backendOpenData', 'click', async () => {
    try { await bridge.openDataDir(); } catch (error) { toast(error.message, 'bad'); }
  });
  const log = byId('backendLog');
  const logDetails = log ? log.closest('details') : null;
  if (logDetails) logDetails.addEventListener('toggle', () => { if (logDetails.open) loadBackendLog(); });
  on('setShortcut', 'input', (event) => { event.target.dataset.touched = 'true'; });
  on('openDashboard', 'click', () => bridge.openWebPage('dashboard'));
  on('openVoice', 'click', () => bridge.openWebPage('voice'));
  on('clearWeb', 'click', async () => {
    try {
      await bridge.clearWebSession();
      toast('The browser pages’ local data was cleared.', 'good');
    } catch (error) {
      toast(`Could not clear the page data: ${error.message}`, 'bad');
    }
  });

  // Main-process pushes
  bridge.onBackendChanged((next) => {
    applyAppState(next);
    if (next && next.lastError) toast(`Backend: ${next.lastError}`, 'bad');
    refreshCurrent();
  });
  bridge.onStreamStatus((status) => applyStreamStatus(status));
  bridge.onStreamEvent((event) => handleStreamEvent(event));
  bridge.onNavigate((payload) => {
    if (payload && payload.view) showView(payload.view);
  });

  // A slow poll keeps the lists honest when the stream is down; the stream is the
  // fast path, not the only one.
  setInterval(() => {
    if (document.hidden) return;
    refreshCurrent();
  }, 5000);
}

async function boot() {
  // The renderer and index.html are two files that have to agree. When they do not —
  // an older page cached, a half-finished edit, a build that dropped a section — the
  // failure otherwise arrives as a TypeError from somewhere deep in rendering, which
  // reads like a crash rather than like the markup being wrong. Naming the anchors
  // that are absent is the same refusal, stated early enough to act on.
  const missing = REQUIRED_IDS.filter((id) => !byId(id));
  if (missing.length) {
    const note = document.createElement('pre');
    note.className = 'boot-refusal';
    note.textContent = `This window is missing elements it cannot run without: ${missing.join(', ')}.\n`
      + 'Nothing below would be truthful, so the interface has not started. '
      + 'Reloading the app restores the page from index.html.';
    document.body.append(note);
    return { ok: false, missing };
  }
  wire();
  // The provider tables are static and come from the main process, so they are read
  // before the first render of the header; a failure leaves each lane's list empty
  // rather than inventing a provider.
  await loadPurposeSpecs();
  await refreshAppState();
  await refreshPurposes().catch(() => {});
  // First-run onboarding (onboarding.js). Mounted only on a fresh install:
  // the wizard itself writes onboardingCompleted once on Finish, so this
  // gate is re-checked on every boot and never flips silently. The host
  // is optional — its absence must never become a boot failure.
  const mount = byId('onboardingMount');
  if (mount && typeof ActionMeshOnboarding !== 'undefined' && ActionMeshOnboarding.renderOnboarding) {
    const settings = (state.appState && state.appState.settings) || {};
    if (settings.onboardingCompleted !== true) {
      mount.hidden = false;
      ActionMeshOnboarding.renderOnboarding(mount, bridge, { initialState: state.appState });
    }
  }
  // The stream is opened by the main process when the backend comes up, which can be
  // before this page has finished loading — its "open" status would then be pushed to
  // nobody, leaving the header saying "stream …" while events were arriving fine. A
  // reload subscribes first and then asks for a stream, so the pill reflects reality.
  // Starting one that is already open is a no-op with a fresh connection.
  if (state.backendReady) {
    try { await bridge.startStream({}); } catch { /* the pill reports the failure */ }
  }
  showView('home');
  if (!state.backendReady) {
    toast('The backend is not running. Settings → Restart backend.', 'bad');
  }
}

document.addEventListener('DOMContentLoaded', boot);
