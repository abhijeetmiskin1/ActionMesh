'use strict';
/**
 * ActionMesh — Connections Center (Phase 6, Agent 4).
 *
 * A read-only center that answers, per lane, "what is reachable right now
 * and why". It never configures, never tests a credential it was not asked
 * for, and never expects a secret back.
 *
 * Data comes from exactly three sources, matching the app's own lanes:
 *   1. `window.actionmesh.state()` — the snapshot the main process pushes
 *      (backend status + per-purpose verified provider states).
 *   2. `window.actionmesh.browserState()` — the read-only `state` op on the
 *      hardened browser window.
 *   3. `window.actionmesh.request(...)` — GET /ready, /capabilities, /devices
 *      and POST /pairing/code|revoke and /devices/{id}/revoke through the
 *      IPC channel allowlist main.js enforces.
 *
 * Expected DOM ids (Lead wires these in index.html after delivery — this
 * module creates #connectionsCenter inside <body> if absent):
 *   #connectionsCenter, #connectionsProviderKv, #connectionsBrowserKv,
 *   #connectionsMediaKv, #connectionsDevicesKv, #connectionsPairOut,
 *   #connectionsRefresh, #connectionsNote.
 * It deliberately does NOT write #settingsProviderKv, #deviceList,
 * #capabilityList, or #pairOut — app.js owns those; two renderers on one
 * node is how a lane ends up stale.
 *
 * Exported: loadConnections(), renderConnections(data), initConnections().
 *
 * Wrapped in an IIFE because index.html loads this file as a classic script,
 * and classic scripts share one global lexical scope: a top-level `const`
 * here that another script also declares is a SyntaxError that stops the
 * later script from loading at all. This module already collided with app.js
 * (`api`, `ApiError`, `GET`, `POST`, `on`, `kvTable`, `PURPOSE_LABELS`, …).
 * Everything the page needs is reached through window.actionmeshConnections.
 */
(function registerConnections() {
  'use strict';

const PURPOSE_LABELS = { chat: 'Chat', reasoning: 'Reasoning', image: 'Image', audio: 'Audio' };
const PURPOSE_ORDER = ['chat', 'reasoning', 'image', 'audio'];

/** The only six words this center may print. 'Needs authorization' is kept
 * distinct from 'Not configured': a lane that holds an unusable credential
 * is a different sentence than one where nothing was chosen yet. */
const CONNECTION_STATES = ['Connected', 'Ready', 'Not configured', 'Unavailable', 'Error', 'Needs authorization'];

/** Mirrors app.js PROVIDER_STATE_CHIP onto the six words. 'connecting' has
 * no honest six-word equivalent while the probe is in flight, so it reads
 * as 'Unavailable' until the probe settles — better to underclaim. */
const PROVIDER_STATE_WORD = {
  not_configured: 'Not configured',
  missing_key: 'Needs authorization',
  connecting: 'Unavailable',
  connected: 'Connected',
  invalid_credentials: 'Needs authorization',
  provider_unavailable: 'Unavailable',
  timeout: 'Error',
  rate_limited: 'Error',
};

function bridge() { return window.actionmesh; }

// ---------------------------------------------------------------------------
// Small DOM helpers (mirrors app.js: text in as text, tolerant of missing ids)
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

function byId(id) { return document.getElementById(id); }

function replace(node, children) {
  if (!node) return;
  while (node.firstChild) node.removeChild(node.firstChild);
  for (const child of [].concat(children || [])) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

function on(node, type, handler) {
  if (node && typeof node.addEventListener === 'function') node.addEventListener(type, handler);
}

/** A chip carries both its six-word text and a slug of that word, so the
 * stylesheet can tint it without the JS and CSS agreeing on a second enum. */
function stateChip(word) {
  const slug = String(word || 'Unavailable').toLowerCase().replace(/[^a-z]+/g, '_').replace(/^_|_$/g, '');
  return el('span', { class: `connections-chip ${slug}`, text: word });
}

// ---------------------------------------------------------------------------
// Transport (same contract as app.js: bridge.request, ApiError on !ok)
// ---------------------------------------------------------------------------
class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

async function api(method, path, body) {
  const result = await bridge().request({ method, path, body: body === undefined ? null : body, long: false });
  if (!result || !result.ok) {
    const detail = (result && result.body && result.body.detail)
      || (result && result.error)
      || (result && typeof result.text === 'string' ? result.text : '')
      || 'request failed';
    throw new ApiError(detail, result ? result.status : 0);
  }
  return result.body;
}

const GET = (path) => api('GET', path);
const POST = (path, body) => api('POST', path, body);

// ---------------------------------------------------------------------------
// Pure state words (unit-tested)
// ---------------------------------------------------------------------------
function providerWord(status) {
  const state = status && status.state ? status.state : 'not_configured';
  return PROVIDER_STATE_WORD[state] || 'Error';
}

function providerReason(status) {
  if (!status || !status.detail) return 'Nothing is configured for this job yet.';
  return status.detail;
}

function browserWord(reply, error) {
  if (error) return 'Error';
  if (!reply) return 'Unavailable';
  return reply.status === 'ok' ? 'Connected' : 'Unavailable';
}

function browserReason(reply, error) {
  if (error) return `The browser window did not answer: ${error.message}`;
  if (!reply) return 'No reply from the browser bridge.';
  if (reply.status === 'ok') return 'The window answered its state probe.';
  return reply.note || 'No browser session: the bridge socket was not started by the desktop main process.';
}

function mediaAdvertised(names) { return Array.isArray(names) && names.includes('media.status'); }

const MEDIA_NOT_ADVERTISED_REASON =
  'The media boundary is closed on this machine (allow_media is off), so no media capability is advertised.';
const MEDIA_ADVERTISED_REASON =
  'The media capability is advertised, which means playback control is permitted — not that playback is under way. A player state is reported only from a live media.status read-back by the engine.';
const MEDIA_LIVE_NOTE =
  'Live MPRIS verification has not been performed in this build. If no player is reachable, the engine answers "unavailable"; a fake player, where one is used in tests, is labelled as a fake.';

function devicesWord(devices, error, backendRunning) {
  if (!backendRunning) return 'Unavailable';
  if (error) return 'Error';
  if (!Array.isArray(devices) || devices.length === 0) return 'Not configured';
  return 'Ready';
}

function devicesReason(devices, error, backendRunning) {
  if (!backendRunning) return 'The backend is not running, so no device list can be read.';
  if (error) return `Could not read the device list: ${error.message}`;
  if (!Array.isArray(devices) || devices.length === 0) {
    return 'No devices are enrolled. Pair one below — a device only appears here after it has paired.';
  }
  const online = devices.filter((d) => d && d.online).length;
  return `${devices.length} device(s) enrolled, ${online} online. Presence is derived by the server from heartbeats, not claimed by this page.`;
}

// ---------------------------------------------------------------------------
// Data load — read-only except for the user's own actions
// ---------------------------------------------------------------------------
async function loadConnections() {
  const data = {
    loadedAt: null, snapshot: null, snapshotError: null,
    backendRunning: false, ready: null, readyError: null,
    capabilityNames: null, capabilitiesError: null,
    devices: null, devicesError: null, purposes: {},
    browserState: null, browserError: null,
  };

  try { data.snapshot = await bridge().state(); }
  catch (error) { data.snapshotError = error; }
  if (data.snapshot && data.snapshot.purposes) data.purposes = data.snapshot.purposes;
  if (data.snapshot && data.snapshot.backend) {
    data.backendRunning = data.snapshot.backend.status === 'running';
  }

  try { data.browserState = await bridge().browserState(); }
  catch (error) { data.browserError = error; }

  if (!data.backendRunning) {
    // The snapshot can lag one boot behind; /ready is the truth.
    try { data.ready = await GET('/ready'); data.backendRunning = true; }
    catch (error) { data.readyError = error; }
  }
  if (data.backendRunning) {
    for (const [key, path] of [['capabilities', '/capabilities'], ['devices', '/devices']]) {
      try {
        const body = await GET(path);
        data[key] = key === 'capabilities'
          ? (Array.isArray(body) ? body.map((e) => (e && e.name) || e).filter(Boolean) : body)
          : body;
        if (key === 'capabilities') data.capabilityNames = data.capabilities;
      } catch (error) { data[`${key}Error`] = error; }
    }
    if (!data.ready) {
      try { data.ready = await GET('/ready'); } catch (error) { data.readyError = error; }
    }
  }

  data.loadedAt = new Date().toISOString();
  data_holder = data;
  data_holder_pairShown = false;
  renderConnections(data);
  return data;
}

// ---------------------------------------------------------------------------
// Render
// ---------------------------------------------------------------------------
// The latest loaded data lives here so event handlers can re-render the
// whole center after a Test / probe / revoke, like app.js re-renders lanes.
let data_holder = null;
let data_holder_pairShown = false;

function ensureLane(containerId) {
  let node = byId(containerId);
  if (!node) {
    const center = byId('connectionsCenter');
    if (!center) return null;
    node = el('div', { id: containerId, class: 'connections-rows' });
    center.append(node);
  }
  return node;
}

function kvTable(rows) {
  const table = el('dl', { class: 'connections-kv' });
  for (const [label, value] of rows) {
    table.append(el('div', { class: 'connections-kv-row' }, [
      el('dt', { text: label }),
      el('dd', { text: (value === undefined || value === null || value === '') ? '—' : String(value) }),
    ]));
  }
  return table;
}

function lane(title, word, reason, rows, actions) {
  const parts = [
    el('div', { class: 'connections-lane-head' }, [
      el('h3', { class: 'connections-lane-title', text: title }),
      stateChip(word),
    ]),
    el('p', { class: 'connections-reason', text: reason }),
  ];
  if (rows && rows.length) parts.push(kvTable(rows));
  if (actions && actions.length) parts.push(el('div', { class: 'connections-actions' }, actions));
  return el('section', { class: 'connections-lane' }, parts);
}

function renderProviderLane(purpose, status) {
  const label = PURPOSE_LABELS[purpose] || purpose;
  const word = providerWord(status);
  const reason = providerReason(status);
  const rows = status ? [
    ['Provider', status.providerLabel || status.provider || '—'],
    ['Model', status.model || '—'],
    ['Key', status.hasKey ? 'held by the OS keyring (never shown)' : 'no key stored'],
    ['Encryption', status.encryptionAvailable ? 'OS keyring encryption available' : 'keyring encryption unavailable — the main process refuses to store keys'],
    ['Last verified', status.testedAt || 'never tested'],
  ] : [];
  const testable = Boolean(status) && status.state !== 'not_configured' && status.state !== 'provider_unavailable';
  const button = el('button', {
    class: 'connections-btn', type: 'button',
    disabled: testable ? null : '',
    title: testable ? 'Call the real provider test for the stored configuration' : 'Nothing to test: this lane has no configured provider to probe.',
    text: 'Test Connection',
  });
  const hint = testable
    ? 'Calls the real, metered 1-token probe against the stored configuration. Click only for a lane you expect to use.'
    : 'Test is disabled until a provider is configured for this lane in Home/Settings.';
  on(button, 'click', async () => {
    button.disabled = true;
    try {
      const answer = await bridge().purposeTest({ purpose });
      const fresh = await bridge().purposeStatus(purpose);
      data_holder.purposes[purpose] = (fresh && fresh.status) || (answer && answer.status) || status;
      data_holder_pairShown = false;
      renderConnections(data_holder);
    } catch (error) {
      button.disabled = false;
      button.after(el('span', { class: 'connections-action-result', text: `Test Connection failed: ${error.message}` }));
    }
  });
  return lane(`${label} provider`, word, reason, rows, [button, el('span', { class: 'connections-hint', text: hint })]);
}

function renderBrowserLane(data) {
  const word = browserWord(data.browserState, data.browserError);
  const reason = browserReason(data.browserState, data.browserError);
  const rows = [['Bridge', 'desktop main process → hardened window (per-launch token on the socket)']];
  if (data.browserState && data.browserState.status === 'ok') {
    rows.push(['Page', data.browserState.url || 'about:blank']);
    if (data.browserState.title) rows.push(['Title', data.browserState.title]);
  }
  const probe = el('button', { class: 'connections-btn', type: 'button', text: 'Re-check state' });
  on(probe, 'click', async () => {
    probe.disabled = true;
    try { data_holder.browserState = await bridge().browserState(); data_holder.browserError = null; }
    catch (error) { data_holder.browserError = error; }
    probe.disabled = false;
    renderConnections(data_holder);
  });
  return lane('Browser bridge', word, reason, rows, [probe]);
}

function renderMediaLane(data) {
  const advertised = mediaAdvertised(data.capabilityNames);
  const word = advertised ? 'Ready' : 'Not configured';
  const reason = !data.backendRunning
    ? 'The backend is not running, so no capability list can be read.'
    : (data.capabilitiesError
        ? `Could not read capabilities: ${data.capabilitiesError.message}`
        : (advertised ? MEDIA_ADVERTISED_REASON : MEDIA_NOT_ADVERTISED_REASON));
  const rows = [
    ['Capability advertised', advertised ? 'media.status present in /capabilities' : 'media.status absent from /capabilities'],
    ['Playback state', 'Not reported by this lane (only a live engine read-back may report it)'],
    ['Live status', MEDIA_LIVE_NOTE],
  ];
  return lane('Media', word, reason, rows, []);
}

function renderDevicesLane(data) {
  const word = devicesWord(data.devices, data.devicesError, data.backendRunning);
  const reason = devicesReason(data.devices, data.devicesError, data.backendRunning);
  const children = [lane('Devices', word, reason, [], [])];
  if (Array.isArray(data.devices)) {
    for (const device of data.devices) {
      if (!device || !device.device_id) continue;
      // Allowlist only: the backend deliberately omits identity handles,
      // secrets, and tokens; even if a future route added one, it is not
      // rendered here.
      const revoke = el('button', { class: 'connections-btn', type: 'button', text: 'Revoke' });
      on(revoke, 'click', async () => {
        revoke.disabled = true;
        try {
          await POST(`/devices/${encodeURIComponent(device.device_id)}/revoke`, { reason: 'revoked from the connections center' });
          await loadConnections();
        } catch (error) {
          revoke.disabled = false;
          revoke.after(el('span', { class: 'connections-action-result', text: `Revoke failed: ${error.message}` }));
        }
      });
      children.push(lane(
        String(device.name || device.device_id),
        device.online ? 'Connected' : 'Unavailable',
        device.online ? 'Seen recently.' : 'Not seen recently.',
        [
          ['Device id', device.device_id],
          ['Kind', device.kind || 'unknown'],
          ['Platform', device.platform || 'unknown'],
          ['Presence', device.online ? 'online' : 'offline'],
          ['Last seen', device.last_seen || 'never'],
          ['Risk ceiling', device.risk_ceiling || 'low'],
          ['Identity', 'enrolled'],
        ],
        [revoke],
      ));
    }
  }
  children.push(renderPairing(data));
  return el('div', {}, children);
}

function renderPairing(data) {
  const localId = data.ready && typeof data.ready === 'object' ? data.ready.local_device_id : null;
  const canMint = Boolean(data.backendRunning && localId);
  const select = el('select', {
    class: 'connections-select', 'aria-label': 'Pairing risk ceiling',
    disabled: canMint ? null : '',
  }, [
    el('option', { value: 'low', text: 'low' }),
    el('option', { value: 'medium', text: 'medium' }),
    el('option', { value: 'high', text: 'high' }),
    el('option', { value: 'critical', text: 'critical — run programs' }),
  ]);
  select.value = 'low';
  const mint = el('button', {
    class: 'connections-btn connections-btn-primary', type: 'button',
    disabled: canMint ? null : '',
    title: canMint ? 'Mint a pairing code on this machine' : 'Pairing needs the backend running and a known local device id.',
    text: 'Pair New Device',
  });
  const out = el('div', { id: 'connectionsPairOut', class: 'connections-pair-out', hidden: data_holder_pairShown ? null : '' });
  on(mint, 'click', async () => {
    mint.disabled = true;
    try {
      const answer = await POST('/pairing/code', { device_id: localId, max_risk: select.value || 'low' });
      data_holder_pairShown = true;
      out.hidden = false;
      replace(out, [
        el('p', { class: 'connections-reason', text: 'Shown once — enter it on the phone within the seconds below. The app does not keep this code.' }),
        el('p', { class: 'connections-code', text: answer.code }),
        kvTable([
          ['Pairs to', answer.laptop_device_id || localId],
          ['Ceiling', answer.max_risk || 'low'],
          ['Expires in', answer.expires_in_s ? `${Math.round(answer.expires_in_s)}s` : 'unknown'],
          ['Signing key', answer.pairing_durable ? 'durable (survives restart)' : 'in memory (lost on restart)'],
        ]),
      ]);
    } catch (error) {
      data_holder_pairShown = true;
      out.hidden = false;
      replace(out, [el('p', { class: 'connections-reason', text: `Could not mint a code: ${error.message}` })]);
    } finally {
      mint.disabled = !canMint;
    }
  });
  const revokeAll = el('button', {
    class: 'connections-btn', type: 'button',
    disabled: data.backendRunning ? null : '',
    title: data.backendRunning ? 'Burn every live pairing code' : 'Needs the backend running.',
    text: 'Revoke All Codes',
  });
  on(revokeAll, 'click', async () => {
    revokeAll.disabled = true;
    try {
      const answer = await POST('/pairing/revoke', {});
      data_holder_pairShown = true;
      out.hidden = false;
      replace(out, [el('p', { class: 'connections-reason', text: `Revoked ${answer.revoked || 0} live code(s).` })]);
    } catch (error) {
      data_holder_pairShown = true;
      out.hidden = false;
      replace(out, [el('p', { class: 'connections-reason', text: `Revoke failed: ${error.message}` })]);
    } finally {
      revokeAll.disabled = !data.backendRunning;
    }
  });
  const intro = canMint
    ? `Minting for ${localId}. A code is minted on this machine only; the ceiling chosen here is the visible act — the phone cannot raise it.`
    : 'Pairing is unavailable: the backend is not running, or this machine does not know its local device id yet.';
  return el('div', { class: 'connections-pairing' }, [
    el('h4', { class: 'connections-lane-title', text: 'Pair a phone' }),
    el('p', { class: 'connections-reason', text: intro }),
    el('div', { class: 'connections-actions' }, [select, mint, revokeAll]),
    out,
  ]);
}

function renderSummary(data) {
  const note = byId('connectionsNote');
  if (!note) return;
  const when = data.loadedAt ? new Date(data.loadedAt).toLocaleTimeString() : 'never';
  const parts = [
    `Backend: ${data.backendRunning ? 'running' : 'not running'}`,
    `Browser: ${browserWord(data.browserState, data.browserError)}`,
    `Media: ${mediaAdvertised(data.capabilityNames) ? 'Ready' : 'Not configured'}`,
    `Devices: ${devicesWord(data.devices, data.devicesError, data.backendRunning)}`,
  ];
  replace(note, [el('p', { class: 'connections-reason', text: `Last checked ${when}. ${parts.join(' · ')}.` })]);
}

function renderConnections(data) {
  if (!data) data = data_holder;
  if (!data) return;
  data_holder = data;
  const providerHost = ensureLane('connectionsProviderKv');
  if (providerHost) replace(providerHost, PURPOSE_ORDER.map((purpose) => renderProviderLane(purpose, data.purposes && data.purposes[purpose])));
  const browserHost = ensureLane('connectionsBrowserKv');
  if (browserHost) replace(browserHost, [renderBrowserLane(data)]);
  const mediaHost = ensureLane('connectionsMediaKv');
  if (mediaHost) replace(mediaHost, [renderMediaLane(data)]);
  const devicesHost = ensureLane('connectionsDevicesKv');
  if (devicesHost) replace(devicesHost, [renderDevicesLane(data)]);
  renderSummary(data);
}

function ensureCenter() {
  let center = byId('connectionsCenter');
  if (center) return center;
  center = el('section', { id: 'connectionsCenter', class: 'connections-wrap' }, [
    el('h2', { class: 'connections-heading', text: 'Connections' }),
    el('p', { class: 'connections-intro', text:
      'Every lane below is a fact the app just read — a provider probe, a window reply, a capability list, a device list. Nothing here is guessed, and no lane shows a green word it did not earn.' }),
    el('div', { class: 'connections-actions' }, [
      el('button', { id: 'connectionsRefresh', class: 'connections-btn', type: 'button', text: 'Refresh' }),
    ]),
    el('div', { id: 'connectionsNote' }),
    el('div', { id: 'connectionsProviderKv', class: 'connections-rows' }),
    el('div', { id: 'connectionsBrowserKv', class: 'connections-rows' }),
    el('div', { id: 'connectionsMediaKv', class: 'connections-rows' }),
    el('div', { id: 'connectionsDevicesKv', class: 'connections-rows' }),
  ]);
  document.body.append(center);
  return center;
}

function initConnections() {
  ensureCenter();
  on(byId('connectionsRefresh'), 'click', () => { loadConnections(); });
  return loadConnections();
}

const api_export = { loadConnections, renderConnections, initConnections, CONNECTION_STATES, _internals: {
  providerWord, providerReason, browserWord, browserReason,
  mediaAdvertised, devicesWord, devicesReason, PROVIDER_STATE_WORD,
} };
if (typeof window !== 'undefined') window.actionmeshConnections = api_export;
if (typeof module !== 'undefined' && module.exports) module.exports = api_export;
})();
