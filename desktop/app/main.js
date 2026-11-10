'use strict';
/**
 * ActionMesh desktop — main process.
 *
 * The window is a shell over the *real* control plane. It has no policy engine, no
 * approval store and no OS access of its own; it starts the backend the dashboard
 * already serves and talks to it over loopback.
 *
 * Two decisions worth stating because they are the security-relevant ones:
 *
 *  1. **The renderer never holds the loopback key and never makes an HTTP call.**
 *     Every request goes renderer -> IPC -> here -> 127.0.0.1. That removes the key
 *     from the DOM, from devtools, from any injected string, and removes the need
 *     for CORS on the server entirely (which is why the server has none).
 *
 *  2. **The stream is opened here, not in the renderer.** An EventSource in the page
 *     would need the key in a URL or a ticket flow; an async iterator in the main
 *     process needs neither. Events are forwarded as parsed JSON, transient output
 *     included, and nothing is written to disk.
 */
const {
  app, BrowserWindow, Menu, Tray, Notification, globalShortcut, ipcMain,
  nativeImage, session, shell, safeStorage, screen, systemPreferences,
} = require('electron');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const { pathToFileURL } = require('node:url');

const { Backend, newLoopbackKey, os } = require('./backend');
const { SettingsStore } = require('./settings');
const providers = require('./providers');
const { startBridge } = require('./provider_bridge');
const browserWindow = require('./browser_window');
const { registerBrowserIpc } = require('./browser_channel');
const { startBridge: startBrowserBridge } = require('./browser_bridge');

const APP_ID = 'com.actionmesh.voiceos';
const PRODUCT = 'ActionMesh';
const VERSION = require('../package.json').version;
const MAIN_UI_PATH = path.join(__dirname, '..', 'ui', 'index.html');
const MAIN_UI_URL = pathToFileURL(MAIN_UI_PATH).href;
const REQUEST_TIMEOUT_MS = 30_000;
const LONG_REQUEST_TIMEOUT_MS = 15 * 60 * 1000;

// Only these paths may be reached from the renderer. A shell that proxies arbitrary
// paths is a shell that can be steered anywhere by whatever ends up in the DOM.
const ALLOWED_PREFIXES = [
  '/health', '/ready', '/capabilities', '/metrics',
  '/devices', '/pairing', '/pair', '/plan', '/execute',
  '/executions', '/approvals', '/consents', '/audit',
  // Automations are a plan the operator wrote and a run history; both are read and
  // edited from the Automations and History views. The whole family lives under this
  // one prefix, so it is admitted once and the segment boundary in `assertAllowed`
  // keeps `/automationsX` out.
  '/automations',
  '/voiceos', '/manifest.webmanifest',
];

app.setAppUserModelId(APP_ID);
if (process.platform === 'linux') app.setName(PRODUCT);

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  // A second copy has nothing to do: it hands the window to the copy that holds the
  // lock via `second-instance` below and leaves. It must not reach `whenReady`,
  // because that path spawns a backend -- and two backends on one data directory
  // share a SQLite audit store, where the second one's in-memory chain collides with
  // the first one's rows (`UNIQUE constraint failed: audit_records.seq`) and every
  // execution starts failing. `app.quit()` is asynchronous, so the flag is checked
  // again in the readiness handler rather than trusted to win the race.
  app.quit();
}

let mainWindow = null;
let aboutWindow = null;
let tray = null;
let backend = null;
let settings = null;

// The last connection probe's outcome, kept in memory so the header can show what was
// actually verified rather than inferring "connected" from the presence of a key. It is
// never persisted: a restart re-tests, and a stale green tick is worse than none.
const lastPurposeTests = new Map();
// C41: the socket server VoiceOS calls to reach a model. `bridgeToken` is minted per
// launch and handed only to the backend child's environment; it is worth exactly one
// thing — permission to talk to this socket while this process lives.
let providerBridge = null;
let bridgeToken = '';
// C42: the drive socket VoiceOS's BrowserEngine calls to reach the hardened
// window. Its token authorises "speak the drive protocol" and nothing more —
// no credential crosses it, and the window itself carries none.
let browserBridge = null;
let browserBridgeToken = '';
let streamAbort = null;
let streamGeneration = 0;
const pendingNotifications = new Map();

// ---------------------------------------------------------------------------
// Window state: remembered, but validated against the displays actually attached
// (a monitor unplugged between launches must not put the window off-screen).
// ---------------------------------------------------------------------------
function windowStateFile() {
  return path.join(app.getPath('userData'), 'window-state.json');
}

function loadWindowState() {
  const fallback = { width: 1280, height: 860, x: undefined, y: undefined, maximized: false };
  let saved = {};
  try {
    saved = JSON.parse(fs.readFileSync(windowStateFile(), 'utf8'));
  } catch {
    return fallback;
  }
  const state = { ...fallback, ...saved };
  if (typeof state.x === 'number' && typeof state.y === 'number') {
    const visible = screen.getAllDisplays().some((display) => {
      const b = display.workArea;
      return state.x < b.x + b.width && state.x + 200 > b.x
        && state.y < b.y + b.height && state.y + 120 > b.y;
    });
    if (!visible) {
      state.x = undefined;
      state.y = undefined;
    }
  }
  return state;
}

function saveWindowState() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  const bounds = mainWindow.getNormalBounds();
  try {
    fs.writeFileSync(windowStateFile(), JSON.stringify({
      ...bounds,
      maximized: mainWindow.isMaximized(),
    }));
  } catch {
    /* a failed window-state write is not worth interrupting anyone over */
  }
}

// ---------------------------------------------------------------------------
// The API proxy. The renderer names a path and a body; nothing else crosses.
// ---------------------------------------------------------------------------
function assertAllowed(routePath) {
  const clean = String(routePath || '');
  if (!clean.startsWith('/') || clean.startsWith('//')) {
    throw new Error('path must be relative to the API root');
  }
  if (clean.includes('..') || clean.includes('\0')) {
    throw new Error('path rejected');
  }
  const bare = clean.split('?')[0];
  const ok = ALLOWED_PREFIXES.some(
    (prefix) => bare === prefix || bare.startsWith(prefix + '/'),
  );
  if (!ok) throw new Error(`path ${bare} is not reachable from the app`);
  return clean;
}

async function apiRequest({ method = 'GET', path: routePath, body = null, long = false }) {
  if (!backend || !backend.baseUrl) throw new Error('the backend is not running');
  const clean = assertAllowed(routePath);
  const headers = { 'x-api-key': backend.key };
  const init = {
    method,
    headers,
    signal: AbortSignal.timeout(long ? LONG_REQUEST_TIMEOUT_MS : REQUEST_TIMEOUT_MS),
  };
  if (body !== null && body !== undefined) {
    headers['content-type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  const response = await fetch(backend.baseUrl + clean, init);
  const text = await response.text();
  let parsed = null;
  try {
    parsed = text ? JSON.parse(text) : null;
  } catch {
    parsed = null;
  }
  return {
    ok: response.ok,
    status: response.status,
    body: parsed,
    text: parsed === null ? text.slice(0, 4000) : undefined,
  };
}

/**
 * The browser pages (/dashboard, /voice) in their own hardened session.
 *
 * A work order constraint says both must keep working. They are ordinary web pages:
 * they fetch same-origin and hold their own dialogue state, so proxying them through
 * IPC would mean reimplementing them. Instead this session injects the loopback key
 * at the *network layer*, below JavaScript: the page never calls a function that can
 * hand it the key, and `webRequest` attaches the header to loopback requests only.
 *
 * This is the same session for viewing those pages; it is deliberately separate from
 * the default session so this header can never ride along on anything else.
 */
const WEB_PAGES_SESSION = 'actionmesh-web-pages';

function setUpWebPageSession() {
  const pages = session.fromPartition(WEB_PAGES_SESSION);
  pages.webRequest.onBeforeSendHeaders((details, callback) => {
    let host = '';
    try {
      host = new URL(details.url).hostname;
    } catch {
      /* a request without a parseable URL is not ours to help */
    }
    const headers = { ...details.requestHeaders };
    if (host === '127.0.0.1' && backend && backend.key) {
      headers['x-api-key'] = backend.key;
    }
    callback({ requestHeaders: headers });
  });
  return pages;
}

/** The session cookie jar is memory-only; a page cannot leave data behind. */
async function clearPageSession() {
  try {
    await session.fromPartition(WEB_PAGES_SESSION).clearStorageData();
  } catch {
    /* nothing to clear */
  }
}

function openWebPage(routePath) {
  if (!backend || !backend.baseUrl) return false;
  const url = backend.baseUrl + routePath;
  const pages = setUpWebPageSession();
  const win = new BrowserWindow({
    width: 1220,
    height: 900,
    title: routePath === '/voice' ? `VoiceOS — ${PRODUCT}` : `Dashboard — ${PRODUCT}`,
    parent: mainWindow || undefined,
    backgroundColor: '#070a16',
    autoHideMenuBar: true,
    show: false,
    icon: path.join(__dirname, '..', 'assets', 'icon.png'),
    webPreferences: {
      partition: WEB_PAGES_SESSION,
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      devTools: !app.isPackaged,
    },
  });
  win.once('ready-to-show', () => win.show());
  win.webContents.setWindowOpenHandler(({ url: target }) => {
    if (/^https?:\/\//i.test(target) && !target.startsWith(backend.baseUrl)) {
      shell.openExternal(target);
    }
    return { action: 'deny' };
  });
  win.webContents.on('will-navigate', (event, target) => {
    if (!backend || !target.startsWith(backend.baseUrl)) {
      event.preventDefault();
      if (/^https?:\/\//i.test(target)) shell.openExternal(target);
    }
  });
  win.loadURL(url).catch(() => {
    // A backend that is not up cannot serve the page; say so rather than showing
    // Chromium's own error page, which mentions a port and nothing useful.
    win.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(
      '<body style="background:#070a16;color:#edf1ff;font:15px system-ui;padding:40px">'
      + 'The ActionMesh backend is not running. Open Settings and start it, then try again.</body>',
    ));
  });
  void pages;
  return true;
}

// ---------------------------------------------------------------------------
// The event stream, held here so no credential ever reaches a page.
// ---------------------------------------------------------------------------
async function startStream(scope) {
  stopStream();
  const generation = ++streamGeneration;
  const controller = new AbortController();
  streamAbort = controller;
  const query = scope && scope.executionId
    ? `?execution_id=${encodeURIComponent(scope.executionId)}`
    : '';
  const since = scope && scope.lastEventId
    ? { 'last-event-id': String(scope.lastEventId) }
    : {};
  let attempt = 0;
  while (!controller.signal.aborted && generation === streamGeneration) {
    try {
      if (!backend || !backend.baseUrl) throw new Error('backend not running');
      const response = await fetch(`${backend.baseUrl}/events${query}`, {
        headers: { 'x-api-key': backend.key, accept: 'text/event-stream', ...since },
        signal: controller.signal,
      });
      if (!response.ok || !response.body) throw new Error(`stream answered ${response.status}`);
      attempt = 0;
      send('stream:status', { state: 'open' });
      const decoder = new TextDecoder();
      let buffer = '';
      for await (const chunk of response.body) {
        buffer += decoder.decode(chunk, { stream: true });
        // Frames end at a blank line. A chunk boundary never splits a frame's parse
        // because the buffer only hands over complete frames.
        let split;
        while ((split = buffer.indexOf('\n\n')) !== -1) {
          const frame = buffer.slice(0, split);
          buffer = buffer.slice(split + 2);
          const event = parseFrame(frame);
          if (event) {
            send('stream:event', event);
            // Native notifications are raised here, not in the page: the page may not
            // be open (the window can be hidden to the tray) and a notification is a
            // system fact, not a rendering detail.
            onEvent(event);
          }
        }
      }
      throw new Error('stream ended');
    } catch (error) {
      if (controller.signal.aborted || generation !== streamGeneration) return;
      attempt += 1;
      send('stream:status', {
        state: 'reconnecting',
        attempt,
        detail: String(error && error.message ? error.message : error),
      });
      // Backoff, capped: a backend that is restarting should be waited out, not
      // hammered. Last-Event-ID is carried by the server's own cursor, so a resume
      // picks up where it left off rather than replaying.
      const delay = Math.min(500 * 2 ** Math.min(attempt, 4), 8000);
      await new Promise((r) => setTimeout(r, delay));
    }
  }
}

function stopStream() {
  streamGeneration += 1;
  if (streamAbort) {
    try { streamAbort.abort(); } catch { /* already aborted */ }
  }
  streamAbort = null;
}

function parseFrame(frame) {
  const event = { data: null, id: null, type: 'message' };
  const dataLines = [];
  for (const line of frame.split('\n')) {
    if (!line || line.startsWith(':')) continue;
    const colon = line.indexOf(':');
    const field = colon === -1 ? line : line.slice(0, colon);
    const value = colon === -1 ? '' : line.slice(colon + 1).replace(/^ /, '');
    if (field === 'data') dataLines.push(value);
    else if (field === 'id') event.id = value;
    else if (field === 'event') event.type = value;
  }
  if (!dataLines.length) return null;
  try {
    event.data = JSON.parse(dataLines.join('\n'));
  } catch {
    event.data = { raw: dataLines.join('\n') };
  }
  return event;
}

// ---------------------------------------------------------------------------
// Notifications. Only a short, server-produced summary ever leaves the window;
// parameters, command lines and output chunks never do.
// ---------------------------------------------------------------------------
function notify(title, body, { key, onClick } = {}) {
  if (!settings.get('notificationsEnabled')) return;
  if (!Notification.isSupported()) return;
  if (key) {
    const now = Date.now();
    const last = pendingNotifications.get(key) || 0;
    if (now - last < 15_000) return; // same fact, same minute, said once
    pendingNotifications.set(key, now);
  }
  const notification = new Notification({ title, body, silent: false });
  notification.on('click', () => {
    showMainWindow();
    if (onClick) onClick();
  });
  notification.show();
}

function summarise(state) {
  switch (state) {
    case 'success': return 'Finished and verified.';
    case 'failed': return 'Finished but the check did not pass.';
    case 'denied': return 'The policy refused this action.';
    case 'cancelled': return 'Stopped on request.';
    case 'interrupted': return 'The run was cut short; whether it took effect is unknown.';
    case 'waiting_permission': return 'Waiting for your approval.';
    case 'partially_completed': return 'Some steps finished and some did not.';
    default: return '';
  }
}

/**
 * Turn one stream event into at most one notification.
 *
 * `event.type` is the SSE `event:` field (the MeshEvent's event_type) and the
 * MeshEvent itself is in `event.data`; the execution state lives in `payload.state`.
 * Only a fixed sentence per state ever leaves the process — an event payload carries
 * node ids and counts, never parameters or command output, and none of it is quoted.
 */
function onEvent(event) {
  const type = (event && event.type) || '';
  const envelope = (event && event.data) || {};
  const payload = envelope.payload || {};
  const executionId = String(envelope.execution_id || '');

  if (type === 'approval.required') {
    notify('ActionMesh — approval needed',
      'An action is parked until you decide. Open Approvals to see what it would do.',
      { key: `approval:${executionId}`, onClick: () => send('ui:navigate', { view: 'approvals' }) });
    return;
  }
  if (type === 'execution.completed' || type === 'execution.denied' || type === 'execution.cancelled') {
    const summary = summarise(payload.state);
    if (summary) {
      notify('ActionMesh — run finished', summary, {
        key: `done:${executionId}`,
        onClick: () => send('ui:navigate', { view: 'live', executionId }),
      });
    }
    return;
  }
  if (type === 'execution.interrupted') {
    notify('ActionMesh — run interrupted', summarise('interrupted'), {
      key: `done:${executionId}`,
      onClick: () => send('ui:navigate', { view: 'live', executionId }),
    });
  }
}

// ---------------------------------------------------------------------------
// IPC surface. Named channels only; the preload exposes exactly these.
// ---------------------------------------------------------------------------
function send(channel, payload) {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send(channel, payload);
  }
}

/**
 * One place to announce a backend lifecycle change, so the renderer and the tray
 * cannot disagree about what is running. Every start/stop/restart path calls this
 * instead of sending `backend:changed` by hand.
 */
function afterBackendChange(extra) {
  send('backend:changed', { ...appState(), ...(extra || {}) });
  refreshTray();
}

function appState() {
  return {
    product: PRODUCT,
    version: VERSION,
    appId: APP_ID,
    electron: process.versions.electron,
    chrome: process.versions.chrome,
    node: process.versions.node,
    platform: process.platform,
    microphonePermission: process.platform === 'darwin'
      ? systemPreferences?.getMediaAccessStatus?.('microphone') || 'unknown' : 'unknown',
    arch: process.arch,
    backend: backend ? backend.snapshot() : { status: 'stopped' },
    settings: settings ? settings.public() : null,
    provider: settings ? providerStatus() : null,
    userData: app.getPath('userData'),
    logsDir: path.join(app.getPath('userData'), 'logs'),
    packaged: app.isPackaged,
    shortcutRegistered: globalShortcut.isRegistered(currentShortcut()),
    tray: Boolean(tray),
    purposes: settings ? Object.fromEntries(PURPOSES.map(p => [p, purposeStatus(p)])) : null,
  };
}

/**
 * What one purpose bar may know.
 *
 * `configured` means a provider and model are recorded. `state` is the *verified*
 * connection state: absent a test, it is `missing_key` or `not_configured`, never
 * `connected` — a key on disk is not a provider that answers.
 */
const PURPOSES = ['chat', 'reasoning', 'image', 'audio'];

function purposeStatus(purpose) {
  const purposeId = String(purpose || '');
  if (!PURPOSES.includes(purposeId)) return null;
  const fields = purposeFields(purposeId);
  const secretKey = purposeSecretKey(purposeId);
  const spec = providers.specFor(fields.provider, purposeId);
  const model = fields.model || (spec ? spec.defaultModel : '');
  const hasKey = Boolean(settings && settings.hasSecret(secretKey));
  const tested = lastPurposeTests.get(purposeId);
  const last = tested?.provider === fields.provider && tested.model === fields.model
    && tested.baseUrl === fields.endpoint ? tested : null;
  const legacy = fields.legacy;

  let state;
  let detail;
  if (!spec) {
    state = 'not_configured';
    detail = 'No provider has been chosen.';
  } else if (!spec.implemented) {
    state = 'provider_unavailable';
    detail = spec.unavailableReason || 'This provider is not implemented.';
  } else if (spec.requiresKey && !hasKey) {
    state = 'missing_key';
    detail = 'No API key is stored for this provider.';
  } else if (last) {
    state = last.state;
    detail = last.detail || '';
  } else {
    // Configured and keyed, but nothing has been verified: say so rather than
    // showing a green dot the app has not earned.
    state = 'not_configured';
    detail = 'Configured but not tested. Press Test Connection.';
  }

  return {
    purpose: purposeId,
    provider: fields.provider || '',
    providerLabel: spec ? spec.label : '',
    model,
    // The renderer needs these to pre-fill the modal. Without them, a Save that
    // leaves the base URL blank would write '' over a stored custom endpoint.
    endpoint: fields.endpoint || '',
    timeout: fields.timeout,
    state,
    text: providers.STATE_TEXT[state] || state,
    detail,
    hasKey,
    legacy,
    encryptionAvailable: Boolean(settings && settings.encryptionAvailable()),
    testedAt: last ? last.at : null,
  };
}

/**
 * The legacy single bar. It reports the chat lane — the one purpose that has always
 * had an implementation — and whether the configuration it shows is a stored chat
 * setup or the preserved pre-C42 one. The old settings are never rewritten.
 *
 * The bridge also calls this with the lane it just resolved (`reasoning`, `chat`, or
 * `legacy`); the shell answers for that lane so /voiceos/status cannot claim a state
 * that belongs to a different card than the one the planner reads.
 */
function providerStatus(lane) {
  const wanted = typeof lane === 'string' && PURPOSES.includes(lane) ? lane : 'chat';
  const status = purposeStatus(wanted);
  return status ? { ...status, purpose: wanted } : null;
}

/**
 * Legacy names the chat purpose falls back to when its own has never been set. The
 * Chat bar is where an existing single-provider setup keeps working: no migration
 * script rewrites somebody's configuration behind their back, and nothing about the
 * old keys is deleted, so rolling back is just this build pointed the other way.
 */
function purposeFields(purpose) {
  const own = {
    provider: String((settings && settings.get(`purpose_${purpose}_provider`)) || ''),
    model: String((settings && settings.get(`purpose_${purpose}_model`)) || ''),
    endpoint: String((settings && settings.get(`purpose_${purpose}_endpoint`)) || ''),
    timeout: Number((settings && settings.get(`purpose_${purpose}_timeout`)) || 12),
  };
  if (purpose === 'chat'
      && !own.provider && !own.model
      && settings && String(settings.get('providerName') || '')) {
    return {
      provider: String(settings.get('providerName') || ''),
      model: String(settings.get('providerModel') || ''),
      endpoint: String(settings.get('providerEndpoint') || ''),
      timeout: Number(settings.get('providerTimeout') || 12) || 12,
      legacy: true,
    };
  }
  return { ...own, legacy: false };
}

/** Where the key actually lives for this purpose: its own secret, else legacy chat. */
function purposeSecretKey(purpose) {
  if (purpose === 'chat' && !settings.hasSecret('chatApiKey') && settings.hasSecret('providerApiKey')) {
    return 'providerApiKey';
  }
  return `${purpose}ApiKey`;
}

function currentShortcut() {
  const value = settings ? settings.get('shortcut') : 'CommandOrControl+Shift+Space';
  return String(value || '').trim();
}

function registerShortcut() {
  globalShortcut.unregisterAll();
  if (!settings.get('shortcutEnabled')) return { registered: false, reason: 'disabled' };
  const accelerator = currentShortcut();
  if (!accelerator) return { registered: false, reason: 'no shortcut configured' };
  try {
    const ok = globalShortcut.register(accelerator, () => toggleMainWindow());
    if (!ok) {
      // A conflict is a fact to report, not a crash: another application owns this
      // combination. The settings screen says so and offers a different one.
      return { registered: false, reason: `${accelerator} is already used by another application` };
    }
    return { registered: true, accelerator };
  } catch (error) {
    return { registered: false, reason: `invalid shortcut: ${error.message}` };
  }
}

function registerIpc() {
  ipcMain.handle('app:state', () => appState());

  ipcMain.handle('settings:update', (_event, patch) => {
    const previousShortcut = currentShortcut();
    const previousEnabled = settings.get('shortcutEnabled');
    if (patch && Object.keys(patch).some((key) => key.endsWith('ApiKey'))) {
      lastPurposeTests.clear();
    }
    settings.update(patch);
    let shortcut = { registered: globalShortcut.isRegistered(currentShortcut()) };
    if (currentShortcut() !== previousShortcut || settings.get('shortcutEnabled') !== previousEnabled) {
      shortcut = registerShortcut();
    }
    if (previousEnabled !== settings.get('shortcutEnabled') || shortcut.registered === false) {
      // re-register defensively when the toggle changed but the value did not
    }
    return { settings: settings.public(), shortcut };
  });

  ipcMain.handle('backend:start', async () => {
    try {
      const snapshot = await backend.start();
      startStream({});
      afterBackendChange();
      return { ok: true, backend: snapshot };
    } catch (error) {
      afterBackendChange({ lastError: error.message });
      return { ok: false, backend: backend.snapshot(), error: error.message };
    }
  });

  ipcMain.handle('backend:stop', async () => {
    stopStream();
    await backend.stop();
    afterBackendChange();
    return { ok: true, backend: backend.snapshot() };
  });

  ipcMain.handle('backend:restart', async () => {
    try {
      const snapshot = await backend.restart();
      startStream({});
      afterBackendChange();
      return { ok: true, backend: snapshot };
    } catch (error) {
      afterBackendChange({ lastError: error.message });
      return { ok: false, backend: backend.snapshot(), error: error.message };
    }
  });

  ipcMain.handle('backend:logs', () => {
    const logPath = path.join(app.getPath('userData'), 'logs', 'backend.log');
    try {
      const stats = fs.statSync(logPath);
      const handle = fs.openSync(logPath, 'r');
      const length = Math.min(stats.size, 32 * 1024);
      const buffer = Buffer.alloc(length);
      fs.readSync(handle, buffer, 0, length, stats.size - length);
      fs.closeSync(handle);
      return { ok: true, path: logPath, text: buffer.toString('utf8') };
    } catch (error) {
      return { ok: false, path: logPath, text: '', error: error.message };
    }
  });

  ipcMain.handle('backend:open-data-dir', async () => {
    const dir = app.getPath('userData');
    await shell.openPath(dir);
    return dir;
  });

  ipcMain.handle('api:request', async (_event, request) => {
    try {
      const result = await apiRequest(request || {});
      return { ok: true, ...result };
    } catch (error) {
      return {
        ok: false,
        status: 0,
        error: String(error && error.message ? error.message : error),
      };
    }
  });

  ipcMain.handle('stream:start', async (_event, scope) => {
    startStream(scope || {});
    return { ok: true };
  });

  // -- AI provider -------------------------------------------------------
  // Narrowly scoped IPC, and deliberately not a general proxy:
  //   * the renderer may set a key, never read one back;
  //   * provider settings are stored here and are NOT backend permissions — nothing
  //     in this handler can grant a capability or approve an action;
  //   * `provider:test` reads the stored key in this process and passes it straight
  //     into providers.test(); it is never returned, logged, or written to disk.
  ipcMain.handle('provider:specs', () => ({ specs: providers.publicSpecs() }));

  ipcMain.handle('provider:status', () => providerStatus());

  ipcMain.handle('provider:configure', (_event, patch) => {
    if (!patch || typeof patch !== 'object') return { ok: false, error: 'no configuration' };
    const spec = providers.specFor(String(patch.provider || ''));
    if (!spec) return { ok: false, error: 'unknown provider' };
    if (!spec.implemented) return { ok: false, error: spec.unavailableReason || 'not implemented' };

    const update = {
      providerName: spec.id,
      providerModel: String(patch.model || '').slice(0, 120),
      providerEndpoint: spec.requiresBaseUrl ? String(patch.baseUrl || '').slice(0, 500) : '',
    };
    const timeout = Number(patch.timeoutSeconds);
    if (Number.isFinite(timeout)) update.providerTimeout = Math.min(Math.max(Math.round(timeout), 1), 60);
    if (patch.setDefault) update.providerDefault = true;

    // An empty box means "leave the stored key alone"; removal is its own call.
    const key = typeof patch.apiKey === 'string' ? patch.apiKey.trim() : '';
    if (key) {
      if (!settings.encryptionAvailable()) {
        return { ok: false, error: 'The OS keyring is unavailable, so the key was not stored. Nothing was written in plain text.' };
      }
      update.providerApiKey = key;
    }
    const answer = settings.update(update);
    return { ok: true, settings: answer ? settings.public() : settings.public(), status: providerStatus() };
  });

  ipcMain.handle('provider:remove', () => {
    settings.update({ providerApiKey: '' });
    return { ok: true, settings: settings.public(), status: providerStatus() };
  });

  ipcMain.handle('provider:test', async (_event, overrides) => {
    const patch = overrides && typeof overrides === 'object' ? overrides : {};
    const provider = String(patch.provider || settings.get('providerName') || '');
    const spec = providers.specFor(provider);
    if (!spec) return providers.test({ provider: '', model: '' });

    // A key typed into the modal but not yet saved is tested without being stored:
    // the value goes from the IPC argument to providers.test() and nowhere else.
    const typed = typeof patch.apiKey === 'string' ? patch.apiKey.trim() : '';
    const apiKey = typed || settings.secret('providerApiKey');
    const outcome = await providers.test({
      provider: spec.id,
      model: String(patch.model || settings.get('providerModel') || ''),
      baseUrl: String(patch.baseUrl || settings.get('providerEndpoint') || ''),
      apiKey,
      timeoutSeconds: Number(patch.timeoutSeconds || settings.get('providerTimeout') || 12),
    });
    return { ok: true, provider: spec.id, ...outcome };
  });

  ipcMain.handle('provider:model', (_event, model) => {
    settings.update({ providerModel: String(model || '').slice(0, 120) });
    return { ok: true, status: providerStatus() };
  });

  // -- Per-purpose providers (C42) --------------------------------------
  // chat | reasoning | image | audio, each with its own provider choice and its own
  // keyring secret. Same discipline as the single-provider handlers above, plus one
  // rule: `purpose` is validated against a closed list before it touches settings, so
  // a renderer string can never name an arbitrary settings key or secret slot.
  const validPurpose = (value) => {
    const p = String(value || '');
    return PURPOSES.includes(p) ? p : null;
  };

  ipcMain.handle('purpose:specs', (_event, purpose) => {
    const p = validPurpose(purpose);
    if (!p) return { ok: false, error: 'unknown purpose' };
    return { ok: true, purpose: p, specs: providers.publicSpecs(p) };
  });

  ipcMain.handle('purpose:status', (_event, purpose) => {
    const p = validPurpose(purpose);
    if (!p) return { ok: false, error: 'unknown purpose' };
    return { ok: true, status: purposeStatus(p) };
  });

  let transcriptionInFlight = false;
  const fromVoicePage = (event) => event.sender === mainWindow?.webContents
    && event.senderFrame?.url === MAIN_UI_URL && !event.senderFrame.parent;
  ipcMain.handle('voice:request-mic', async (event) => {
    if (!fromVoicePage(event)) return false;
    if (process.platform !== 'darwin') return true;
    return systemPreferences.askForMediaAccess('microphone');
  });
  ipcMain.handle('voice:transcribe', async (event, recording) => {
    if (!fromVoicePage(event) || transcriptionInFlight) {
      return { state: 'provider_unavailable', detail: 'A transcription request cannot start right now.' };
    }
    const { provider, model, endpoint: baseUrl, timeout } = purposeFields('audio');
    if (provider !== 'openai_compatible_stt') {
      return { state: 'not_configured', detail: 'Configure speech transcription in the Audio settings.' };
    }
    const apiKey = settings.secret('audioApiKey');
    transcriptionInFlight = true;
    try {
      const language = settings.get('voiceTranscriptionLanguage');
      const answer = await providers.transcribe({ model, baseUrl, apiKey, timeoutSeconds: timeout, language },
        recording?.bytes, recording?.mime);
      const current = purposeFields('audio');
      if (current.provider === provider && current.model === model && current.endpoint === baseUrl
          && settings.secret('audioApiKey') === apiKey) {
        const { transcript, ...status } = answer;
        lastPurposeTests.set('audio', { provider, model, baseUrl, ...status, at: new Date().toISOString() });
      }
      return answer;
    } finally {
      transcriptionInFlight = false;
    }
  });

  ipcMain.handle('purpose:configure', (_event, payload) => {
    if (!payload || typeof payload !== 'object') return { ok: false, error: 'no configuration' };
    const p = validPurpose(payload.purpose);
    if (!p) return { ok: false, error: 'unknown purpose' };
    const spec = providers.specFor(String(payload.provider || ''), p);
    if (!spec) return { ok: false, error: 'unknown provider' };
    if (!spec.implemented) return { ok: false, error: spec.unavailableReason || 'not implemented' };

    const update = {
      [`purpose_${p}_provider`]: spec.id,
      [`purpose_${p}_model`]: String(payload.model || '').slice(0, 120),
    };
    // A save that omits `baseUrl` leaves the stored endpoint alone; an explicit empty
    // string clears it. The renderer pre-fills the box, so the omission case is a
    // caller that only meant to change the model — and silently erasing a custom
    // endpoint there would make the very next request go to the wrong host.
    if (payload.baseUrl !== undefined || !spec.requiresBaseUrl) {
      update[`purpose_${p}_endpoint`] = spec.requiresBaseUrl ? String(payload.baseUrl || '').slice(0, 500) : '';
    }
    const timeout = Number(payload.timeoutSeconds);
    if (Number.isFinite(timeout)) update[`purpose_${p}_timeout`] = Math.min(Math.max(Math.round(timeout), 1), 60);

    // An empty box means "leave the stored key alone"; removal is its own call. The
    // key is written only when the keyring can encrypt it — never as plain text.
    const key = typeof payload.apiKey === 'string' ? payload.apiKey.trim() : '';
    if (key) {
      if (!settings.encryptionAvailable()) {
        return { ok: false, error: 'The OS keyring is unavailable, so the key was not stored. Nothing was written in plain text.' };
      }
      update[`${p}ApiKey`] = key;
    }
    settings.update(update);
    lastPurposeTests.delete(p);
    return { ok: true, purpose: p, settings: settings.public(), status: purposeStatus(p) };
  });

  ipcMain.handle('purpose:remove', (_event, payload) => {
    const p = validPurpose(payload && payload.purpose);
    if (!p) return { ok: false, error: 'unknown purpose' };
    // Remove from the purpose's own slot, and from the legacy chat slot if that is
    // where the chat key actually lives, so "Remove credentials" never leaves a
    // second copy behind.
    const update = { [`${p}ApiKey`]: '' };
    if (p === 'chat') update.providerApiKey = '';
    settings.update(update);
    lastPurposeTests.delete(p);
    return { ok: true, purpose: p, settings: settings.public(), status: purposeStatus(p) };
  });

  ipcMain.handle('purpose:test', async (_event, payload) => {
    if (!payload || typeof payload !== 'object') return { ok: false, error: 'no configuration' };
    const p = validPurpose(payload.purpose);
    if (!p) return { ok: false, error: 'unknown purpose' };
    const fields = purposeFields(p);
    const provider = String(payload.provider || fields.provider || '');
    const spec = providers.specFor(provider, p);
    if (!spec || !spec.implemented) {
      // Nothing to call: report it without touching the network or the keyring.
      return { ok: true, purpose: p, provider, ...await providers.test({ provider, purpose: p }) };
    }
    // A key typed but not saved is tested ephemerally; otherwise the stored key for
    // this exact purpose is read here and passed straight into the adapter. It never
    // goes to the renderer.
    const typed = typeof payload.apiKey === 'string' ? payload.apiKey.trim() : '';
    const apiKey = typed || settings.secret(purposeSecretKey(p));
    const model = String(payload.model || fields.model || '');
    const baseUrl = String(payload.baseUrl || fields.endpoint || '');
    const outcome = await providers.test({
      provider: spec.id,
      model,
      baseUrl,
      apiKey,
      timeoutSeconds: Number(payload.timeoutSeconds || fields.timeout || 12),
      purpose: p,
    });
    const current = purposeFields(p);
    if (current.provider === spec.id && current.model === model && current.endpoint === baseUrl
        && settings.secret(purposeSecretKey(p)) === apiKey) {
      lastPurposeTests.set(p, { provider: spec.id, model, baseUrl, ...outcome, at: new Date().toISOString() });
    }
    return { ok: true, purpose: p, provider: spec.id, ...outcome };
  });

  ipcMain.handle('purpose:model', (_event, payload) => {
    const p = validPurpose(payload && payload.purpose);
    if (!p) return { ok: false, error: 'unknown purpose' };
    settings.update({ [`purpose_${p}_model`]: String(payload.model || '').slice(0, 120) });
    lastPurposeTests.delete(p);
    return { ok: true, purpose: p, status: purposeStatus(p) };
  });

  /**
   * "Set as default" — a real setting, not a badge.
   *
   * There is one consumer that has a notion of a default: the planner. It resolves a
   * lane from the reasoning settings, then chat, then the legacy keys. This flag is
   * what makes the *chat* lane win that resolution when both are configured, so the
   * button does something a person can observe. It grants nothing: the planner still
   * goes through validate_proposal → registry → policy → approval.
   *
   * Only chat can be the default, because chat is the only lane whose purpose is
   * generic conversation. A "default reasoning" would be a second name for the same
   * setting and a way for the two to disagree.
   */
  ipcMain.handle('purpose:default', (_event, payload) => {
    const p = validPurpose(payload && payload.purpose);
    if (!p) return { ok: false, error: 'unknown purpose' };
    if (p !== 'chat') return { ok: false, error: 'Only the chat lane can be the default.' };
    const fields = purposeFields('chat');
    if (!fields.provider) return { ok: false, error: 'Configure a chat provider first.' };
    settings.update({ providerDefault: true });
    return { ok: true, purpose: 'chat', status: purposeStatus('chat') };
  });

  ipcMain.handle('stream:stop', () => {
    stopStream();
    return { ok: true };
  });

  ipcMain.handle('notify', (_event, payload) => {
    if (!payload || typeof payload !== 'object') return { ok: false };
    notify(String(payload.title || PRODUCT), String(payload.body || ''), {});
    return { ok: true };
  });

  ipcMain.handle('window:about', () => openAbout());
  ipcMain.handle('window:minimize', () => mainWindow && mainWindow.minimize());
  ipcMain.handle('window:open-web', (_event, which) => {
    // Closed set: the app can open its own two pages, nothing else.
    if (which === 'dashboard') return { ok: openWebPage('/dashboard') };
    if (which === 'voice') return { ok: openWebPage('/voice') };
    return { ok: false, error: 'unknown page' };
  });
  ipcMain.handle('window:clear-web-session', async () => {
    await clearPageSession();
    return { ok: true };
  });
  ipcMain.handle('window:toggle-maximize', () => {
    if (!mainWindow) return false;
    if (mainWindow.isMaximized()) mainWindow.unmaximize();
    else mainWindow.maximize();
    return mainWindow.isMaximized();
  });
  ipcMain.handle('window:close', () => mainWindow && mainWindow.close());
  ipcMain.handle('external:open', async (_event, url) => {
    const target = String(url || '');
    if (!/^https?:\/\//i.test(target)) return { ok: false, error: 'only http(s) links open' };
    await shell.openExternal(target);
    return { ok: true };
  });
}

// ---------------------------------------------------------------------------
// Windows
// ---------------------------------------------------------------------------
function createMainWindow() {
  const state = loadWindowState();
  mainWindow = new BrowserWindow({
    width: state.width,
    height: state.height,
    x: state.x,
    y: state.y,
    minWidth: 960,
    minHeight: 640,
    show: false,
    backgroundColor: '#070a16',
    title: PRODUCT,
    icon: path.join(__dirname, '..', 'assets', process.platform === 'win32' ? 'icon.ico' : 'icon.png'),
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      allowRunningInsecureContent: false,
      spellcheck: false,
      devTools: !app.isPackaged,
    },
  });

  if (state.maximized) mainWindow.maximize();

  // The window only ever shows the app's own file. Anything else — a link in a
  // rendered string, a redirect — is refused here rather than trusted to the page.
  const appOrigin = 'file://';
  mainWindow.webContents.on('will-navigate', (event, url) => {
    if (!url.startsWith(appOrigin)) {
      event.preventDefault();
      if (/^https?:\/\//i.test(url)) shell.openExternal(url);
    }
  });
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//i.test(url)) shell.openExternal(url);
    return { action: 'deny' };
  });
  mainWindow.webContents.session.setPermissionRequestHandler((wc, permission, callback, details) => {
    // The microphone is requested by the VoiceOS page via getUserMedia. Nothing else
    // is granted, and the request itself is what the page's own push-to-talk does —
    // the app never opens the microphone on its own.
    callback(wc === mainWindow?.webContents && permission === 'media'
      && details?.isMainFrame === true && details.requestingUrl === MAIN_UI_URL
      && details.mediaTypes?.length === 1
      && details.mediaTypes[0] === 'audio');
  });

  mainWindow.once('ready-to-show', () => mainWindow.show());
  mainWindow.on('resize', saveWindowState);
  mainWindow.on('move', saveWindowState);
  mainWindow.on('close', saveWindowState);
  mainWindow.on('closed', () => {
    mainWindow = null;
  });

  mainWindow.loadFile(MAIN_UI_PATH);
}

function openAbout() {
  if (aboutWindow && !aboutWindow.isDestroyed()) {
    aboutWindow.focus();
    return true;
  }
  aboutWindow = new BrowserWindow({
    width: 480,
    height: 460,
    resizable: false,
    minimizable: false,
    maximizable: false,
    title: `About ${PRODUCT}`,
    parent: mainWindow || undefined,
    modal: false,
    show: false,
    autoHideMenuBar: true,
    backgroundColor: '#070a16',
    icon: path.join(__dirname, '..', 'assets', process.platform === 'win32' ? 'icon.ico' : 'icon.png'),
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  aboutWindow.once('ready-to-show', () => aboutWindow.show());
  aboutWindow.on('closed', () => {
    aboutWindow = null;
  });
  aboutWindow.loadFile(path.join(__dirname, '..', 'ui', 'about.html'));
  return true;
}

function showMainWindow() {
  if (!mainWindow || mainWindow.isDestroyed()) {
    createMainWindow();
    return;
  }
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
}

function toggleMainWindow() {
  if (mainWindow && !mainWindow.isDestroyed() && mainWindow.isVisible() && mainWindow.isFocused()) {
    mainWindow.hide();
  } else {
    showMainWindow();
  }
}

function trayImage() {
  const file = path.join(__dirname, '..', 'assets', 'tray.png');
  const image = nativeImage.createFromPath(file);
  return image.isEmpty()
    ? nativeImage.createFromPath(path.join(__dirname, '..', 'assets', 'icon.png')).resize({ width: 22, height: 22 })
    : image;
}

function trayMenu() {
  return Menu.buildFromTemplate([
    { label: 'Open ActionMesh', click: showMainWindow },
    { label: 'Ask (VoiceOS)', click: () => { showMainWindow(); send('ui:navigate', { view: 'voice' }); } },
    { label: 'Approvals', click: () => { showMainWindow(); send('ui:navigate', { view: 'approvals' }); } },
    { type: 'separator' },
    {
      label: backend && backend.status === 'ready' ? `Backend: ready (${backend.port})` : 'Backend: stopped',
      enabled: false,
    },
    {
      label: 'Restart backend',
      click: async () => {
        try {
          await backend.restart();
          startStream({});
          afterBackendChange();
        } catch (error) {
          afterBackendChange({ lastError: error.message });
        }
      },
    },
    { type: 'separator' },
    { label: `About ${PRODUCT}`, click: openAbout },
    { label: 'Quit', click: () => app.quit() },
  ]);
}

function createTray() {
  try {
    tray = new Tray(trayImage());
  } catch {
    return;
  }
  tray.setToolTip(PRODUCT);
  tray.setContextMenu(trayMenu());
  tray.on('click', toggleMainWindow);
}

/**
 * A menu's labels are a snapshot taken when it is built, so the "Backend: ready"
 * line would keep saying "stopped" for the rest of the session. Rebuild after any
 * lifecycle change: it is cheap, and a tray that lies about the backend is worse
 * than no tray at all.
 */
function refreshTray() {
  if (!tray) return;
  try {
    tray.setContextMenu(trayMenu());
  } catch {
    /* the tray was destroyed by the desktop environment */
  }
}

function buildAppMenu() {
  const template = [
    ...(process.platform === 'darwin' ? [{ role: 'appMenu' }] : []),
    {
      label: 'File',
      submenu: [
        { label: 'Settings', accelerator: 'CmdOrCtrl+,', click: () => send('ui:navigate', { view: 'settings' }) },
        { type: 'separator' },
        { role: process.platform === 'darwin' ? 'close' : 'quit' },
      ],
    },
    { role: 'editMenu' },
    {
      label: 'View',
      submenu: [
        { label: 'Home', accelerator: 'CmdOrCtrl+1', click: () => send('ui:navigate', { view: 'home' }) },
        { label: 'Tasks', accelerator: 'CmdOrCtrl+2', click: () => send('ui:navigate', { view: 'tasks' }) },
        { label: 'Live', accelerator: 'CmdOrCtrl+3', click: () => send('ui:navigate', { view: 'live' }) },
        { label: 'Approvals', accelerator: 'CmdOrCtrl+4', click: () => send('ui:navigate', { view: 'approvals' }) },
        { label: 'Devices', accelerator: 'CmdOrCtrl+5', click: () => send('ui:navigate', { view: 'devices' }) },
        { label: 'Settings', accelerator: 'CmdOrCtrl+6', click: () => send('ui:navigate', { view: 'settings' }) },
        { type: 'separator' },
        { role: 'reload' },
        { role: 'resetZoom' },
        { role: 'zoomIn' },
        { role: 'zoomOut' },
        { type: 'separator' },
        { role: 'togglefullscreen' },
      ],
    },
    {
      label: 'Help',
      submenu: [
        { label: `About ${PRODUCT}`, click: openAbout },
        {
          label: 'Open data folder',
          click: () => shell.openPath(app.getPath('userData')),
        },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

// ---------------------------------------------------------------------------
// Lifecycle
// ---------------------------------------------------------------------------
app.on('second-instance', () => showMainWindow());

app.whenReady().then(async () => {
  // See the lock check above: a copy that lost the race quit while this handler was
  // already queued. Starting here anyway would spawn the second backend that check
  // exists to prevent.
  if (!gotLock) return;
  settings = new SettingsStore({
    userDataDir: app.getPath('userData'),
    safeStorage,
  });
  // C41: the bridge comes up *before* the backend, because the backend's environment is
  // built once at spawn and the socket it dials must already be under way. A failure to
  // listen is not fatal: the app runs, VoiceOS reports why it is on the deterministic
  // planner, and no model call is silently attempted.
  bridgeToken = crypto.randomBytes(32).toString('base64url');
  const bridgeSocket = path.join(app.getPath('userData'), 'provider-bridge.sock');
  try {
    providerBridge = await startBridge({
      socketPath: bridgeSocket,
      token: bridgeToken,
      settings,
      providers,
      getStatus: providerStatus,
      noteTestResult(state, { purpose, provider, model, baseUrl }) {
        const lane = purpose === 'legacy' ? 'chat' : purpose;
        lastPurposeTests.set(lane, { provider, model, baseUrl, state, at: new Date().toISOString() });
      },
    });
  } catch (error) {
    providerBridge = null;
    bridgeToken = '';
    console.error('model bridge did not start:', error && error.message);
  }
  // C42: the drive bridge comes up *before* the backend, for the same reason:
  // the backend's environment is built once at spawn and the socket it dials
  // must already be under way. The bundle that VoiceOS dials (the Python
  // BridgeDriver's `request` callable) is the backend-side counterpart; this
  // socket is what it calls. A failure to listen is not fatal: the browser
  // capability answers "unavailable" honestly and nothing is silently
  // attempted. The socket lives in the profile directory; Backend.prepare()
  // creates that directory before start(), so we create it eagerly here too —
  // the drive bridge must exist when the backend's environment is built.
  browserBridgeToken = crypto.randomBytes(32).toString('base64url');
  const browserBridgeSocket = path.join(app.getPath('userData'), 'browser-bridge.sock');
  try {
    fs.mkdirSync(app.getPath('userData'), { recursive: true });
    browserBridge = await startBrowserBridge({
      socketPath: browserBridgeSocket,
      token: browserBridgeToken,
      drive: browserWindow,
    });
  } catch (error) {
    browserBridge = null;
    browserBridgeToken = '';
    console.error('browser bridge did not start:', error && error.message);
  }
  backend = new Backend({
    dataDir: app.getPath('userData'),
    appRoot: app.isPackaged
      ? path.join(process.resourcesPath, 'app')
      : path.resolve(__dirname, '..', '..'),
    isPackaged: app.isPackaged,
    resourcesPath: process.resourcesPath,
    settings: settings.values,
    connectorCredentials: () => ({
      github: settings.secret('githubReadToken'),
      drive: settings.secret('driveReadToken'),
    }),
    key: newLoopbackKey(),
    bridge: bridgeToken ? { socketPath: bridgeSocket, token: bridgeToken } : null,
    // Env only: socket path + per-launch token, never a credential. The backend
    // hands them to the agent (ACTIONMESH_ALLOW_BROWSER gates the capability;
    // the provider core never opens IPC itself).
    browserBridge: browserBridgeToken ? { socketPath: browserBridgeSocket, token: browserBridgeToken } : null,
  });
  backend.prepare();
  registerIpc();
  // The browser IPC surface is the renderer's drive path. Registering it here
  // — after registerIpc() — keeps every ipcMain.handle in one lifecycle.
  registerBrowserIpc(browserWindow, () => mainWindow);
  buildAppMenu();
  createMainWindow();
  createTray();
  registerShortcut();

  try {
    await backend.start();
    afterBackendChange();
    startStream({});
  } catch (error) {
    afterBackendChange({ lastError: error.message });
  }
});

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) createMainWindow();
  else showMainWindow();
});

app.on('window-all-closed', () => {
  // A tray icon is the whole point of a background app; quitting on window close
  // would make the global shortcut point at nothing. macOS keeps the convention too.
  if (process.platform !== 'darwin' && !tray) app.quit();
});

let quitting = false;
app.on('before-quit', async (event) => {
  if (quitting) return;
  event.preventDefault();
  quitting = true;
  stopStream();
  try {
    await backend.stop();
  } finally {
    // The bridge holds a listening socket in the profile directory; leaving it behind
    // would make the next launch have to clean up after this one.
    if (providerBridge) {
      try {
        await providerBridge.close();
      } catch {
        /* already gone */
      }
      providerBridge = null;
    }
    if (browserBridge) {
      try {
        await browserBridge.close();
      } catch {
        /* already gone */
      }
      browserBridge = null;
    }
    globalShortcut.unregisterAll();
    app.quit();
  }
});

app.on('will-quit', () => {
  globalShortcut.unregisterAll();
});

process.on('uncaughtException', (error) => {
  try {
    const file = path.join(app.getPath('userData'), 'crash.log');
    fs.appendFileSync(file, `${new Date().toISOString()} ${error && error.stack}\n`);
  } catch {
    /* nothing else to do */
  }
});

module.exports = { os };
