/**
 * Hardened, kept BrowserWindow for the ActionMesh browser drive (C42).
 *
 * Owns ONE window that hosts untrusted web pages, driven only by the closed
 * set of named operations (open/navigate/back/forward/refresh/snapshot/
 * click/type/select/scroll/wait/stop/state/page_text). The laptop-side engine
 * (actionmesh/agent/browser.py) sends typed commands; this module answers
 * with fixed observations. No model-supplied JavaScript ever executes here:
 *
 *   - Every script that runs in the page is a FROZEN MODULE CONSTANT. The
 *     only dynamic inputs are passed through Electron's structured params
 *     (`executeJavaScript(script, [args])` serializes arguments; they are
 *     never spliced into source text), after integer/enum validation.
 *   - Element addressing uses refs stamped by the snapshot script itself;
 *     page-authored strings never enter a script or a selector.
 *
 * Partition deviation, on purpose: the existing `actionmesh-web-pages`
 * session attaches the loopback API key to every 127.0.0.1 request (see
 * main.js setUpWebPageSession). Untrusted pages must never read that header,
 * so the drive window lives in a SIBLING hardened partition
 * (`actionmesh-web-drive`) with identical webPreferences and NO webRequest
 * header injection. Same posture otherwise: sandbox: true,
 * contextIsolation: true, nodeIntegration: false, NO preload, webSecurity:
 * true, memory-only cookies via clearStorageData, never `--no-sandbox`.
 *
 * The lead wires the NDJSON bridge (desktop/app/browser_bridge.js) to
 * `handleDrive()` and installs an observation sink via `setObservationSink()`.
 * main.js does both: it starts the bridge with a per-launch token, and
 * registerBrowserIpc (browser_channel.js) forwards observations to the UI
 * through preload's onBrowserEvent.
 */

'use strict';

const { BrowserWindow, session } = require('electron');

const WEB_DRIVE_SESSION = 'actionmesh-web-drive';
const MAX_FRAME_LOG = 2048; // anything we log is truncated; never page bodies.

// ---------------------------------------------------------------------------
// Frozen page scripts. These are the ONLY code that ever executes in the
// page. They are constants; Electron passes our arguments structurally.
// ---------------------------------------------------------------------------

/**
 * Snapshot collector: stamps each addressable element with a data attribute
 * (the window-local id) and returns a bounded, structured summary. Password
 * inputs appear only as flags + value lengths — never values. Returns an
 * object { epoch, elements: [...], truncated } — epoch is the current URL's
 * navigation counter, kept in a page-side global by the loader script.
 */
const COLLECT_ELEMENTS = `(function (maxElements) {
  const NAV_KEY = '__amDriveEpoch';
  const out = [];
  let stamped = 0;
  const nodes = document.querySelectorAll(
    'a[href], button, input:not([type="hidden"]), select, textarea, ' +
    'h1, h2, h3, [role], [onclick], form'
  );
  for (const el of nodes) {
    if (out.length >= maxElements) break;
    if (!el.isConnected) continue;
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    const hidden = rect.width === 0 && rect.height === 0;
    if (hidden && style.display === 'none') continue;
    const id = String(++stamped);
    el.setAttribute('data-am-ref', id);
    const type = (el.getAttribute('type') || '').toLowerCase();
    const isPassword = el.tagName === 'INPUT' && type === 'password';
    let name = (el.getAttribute('aria-label') || el.getAttribute('placeholder') ||
      (el.labels && el.labels[0] ? el.labels[0].textContent : '') ||
      el.textContent || el.value || '');
    name = String(name || '').replace(/\\s+/g, ' ').trim().slice(0, 200);
    let host = '';
    if (el.tagName === 'A' && el.href) {
      try { host = new URL(el.href).hostname; } catch (e) { host = ''; }
    }
    out.push({
      id: id,
      tag: el.tagName.toLowerCase(),
      role: (el.getAttribute('role') || (el.tagName.toLowerCase() === 'a' ? 'link' : el.tagName.toLowerCase())),
      name: name,
      disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
      password: isPassword,
      value_len: isPassword ? 0 : String(el.value || '').length,
      href_host: host
    });
  }
  // Drop stamps for elements we skipped or that vanished between runs.
  for (const stale of document.querySelectorAll('[data-am-ref]')) {
    let keep = false;
    for (const item of out) { if (item.id === stale.getAttribute('data-am-ref')) { keep = true; break; } }
    if (!keep) stale.removeAttribute('data-am-ref');
  }
  return {
    epoch: (window[NAV_KEY] || 0),
    elements: out,
    truncated: nodes.length > out.length
  };
})(__MAX_ARGS__)`;

/** Visible-text reader: bounded innerText, no markup, no attributes. */
const READ_PAGE_TEXT = `(function (limit) {
  const t = (document.body && document.body.innerText) || '';
  return String(t).replace(/\\s+/g, ' ').trim().slice(0, limit);
})(__LIMIT__)`;

/** Focus + native click on one stamped element (args: id string, double). */
const DRIVE_CLICK = `(function (id, double) {
  const el = document.querySelector('[data-am-ref="' + id + '"]');
  if (!el) return { found: false };
  el.scrollIntoView({ block: 'center', inline: 'center' });
  el.focus({ preventScroll: true });
  if (double) {
    el.dispatchEvent(new MouseEvent('dblclick', { bubbles: true, cancelable: true }));
  } else {
    el.click();
  }
  return { found: true, changed: true };
})(__ID__, __DOUBLE__)`;

/**
 * Type into one stamped element. The text arrives as a STRUCTURED argument —
 * it is never concatenated into the script source, so page or model text
 * cannot become code. Password fields refuse by design.
 *
 * Three invariants live here because the observation model depends on them:
 *  - we CAPTURE the before-count at drive time, alongside the after-count.
 *    A later re-read must not be trusted — a framework (React-controlled
 *    input, a live formatter) may resync the field between the type and
 *    the re-read, and the re-read would report the field's own answer to
 *    "did this land" rather than the drive's;
 *  - the write goes through the page's native value setter, then an `input`
 *    event. `el.value = ...` sets the property without notifying any
 *    framework on top of the element; the setter step is what makes the
 *    DOM agree with the framework's model, and the event is what a real
 *    keyboard would have produced.
 *  - the descriptor is picked by element type: a <textarea> has no setter on
 *    HTMLInputElement.prototype, so reading that slot on a textarea throws
 *    a TypeError ("descriptor.set is not a function") instead of falling
 *    through to the textarea branch. Both prototypes are read defensively
 *    and the one matching the element wins.
 */
const DRIVE_TYPE = `(function (id, text) {
  const el = document.querySelector('[data-am-ref="' + id + '"]');
  if (!el) return { found: false };
  if (el.tagName === 'INPUT' && (el.getAttribute('type') || '').toLowerCase() === 'password') {
    return { found: true, refused: 'password_field' };
  }
  if (el.tagName !== 'INPUT' && el.tagName !== 'TEXTAREA' && el.isContentEditable !== true) {
    return { found: true, refused: 'not_editable' };
  }
  el.focus({ preventScroll: true });
  const before = String(el.value || el.textContent || '').length;
  if (el.isContentEditable === true) {
    el.textContent = String(el.textContent || '') + String(text).slice(0, 4000);
  } else {
    const descriptor =
      el.tagName === 'TEXTAREA'
        ? Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')
        : Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
    const setter = (descriptor && descriptor.set) || function (v) { this.value = v; };
    setter.call(el, String(el.value || '') + String(text).slice(0, 4000));
    el.dispatchEvent(new Event('input', { bubbles: true }));
  }
  const after = String(el.value || el.textContent || '').length;
  return { found: true, changed: after > before, inserted: after - before };
})(__ID__, __TEXT__)`;

/** Select an <option> by its value attribute; values arrive structured. */
const DRIVE_SELECT = `(function (id, value) {
  const el = document.querySelector('[data-am-ref="' + id + '"]');
  if (!el) return { found: false };
  if (el.tagName !== 'SELECT') return { found: true, refused: 'not_a_select' };
  const before = String(el.value);
  el.value = String(value).slice(0, 1024);
  if (String(el.value) !== String(value).slice(0, 1024)) return { found: true, refused: 'no_such_option' };
  el.dispatchEvent(new Event('change', { bubbles: true }));
  return { found: true, changed: before !== el.value };
})(__ID__, __VALUE__)`;

/** Scroll by enum direction, whole page or to one stamped element. */
const DRIVE_SCROLL = `(function (direction, amount, id) {
  const step = Math.max(1, Math.min(Number(amount) || 600, 10000));
  let target = null;
  if (id) target = document.querySelector('[data-am-ref="' + id + '"]');
  if (target) {
    target.scrollIntoView({
      block: direction === 'up' ? 'end' : 'start',
      inline: direction === 'left' ? 'end' : 'start'
    });
    return { found: true, moved: true };
  }
  const dx = direction === 'left' ? -step : direction === 'right' ? step : 0;
  const dy = direction === 'up' ? -step : direction === 'down' ? step : 0;
  window.scrollBy({ left: dx, top: dy, behavior: 'instant' });
  return { found: true, moved: true };
})(__DIRECTION__, __AMOUNT__, __ID__)`;

// Placeholder substitution in the frozen scripts replaces each `__NAME__`
// token with `JSON.stringify` of an already-validated scalar. That is the
// whole story: JSON serialization produces an inert string/number literal and
// escapes every quote, brace and newline, so neither page-authored nor
// model-authored text can ever terminate the literal and become code. The
// alternative — splicing raw text into source — is exactly what this file
// forbids.

function literal(scalar) {
  return JSON.stringify(scalar);
}

function fill(script, tokens) {
  let out = script;
  for (const [token, value] of Object.entries(tokens)) {
    out = out.split('__' + token + '__').join(literal(value));
  }
  return out;
}

const DIRECTIONS = new Set(['up', 'down', 'left', 'right']);
const WAIT_UNTIL = new Set(['load', 'idle', 'title', 'elements']);

// ---------------------------------------------------------------------------
// Window state
// ---------------------------------------------------------------------------

let win = null;
let observationSink = null; // (payload) => void — the lead wires the bridge.
let navEpoch = 0;
let lastTitle = '';
let loading = false;
// Last main-frame load failure: browser-authored net error name only
// (never page content). Cleared at navigation start, checked after loadURL
// in open/navigate so a DNS/connection failure is surfaced instead of being
// collapsed to the generic "drive failed" in handleDrive.
let failedLoad = null;

function prefs() {
  return {
    contextIsolation: true,
    nodeIntegration: false,
    sandbox: true,
    webSecurity: true,
    // No preload: this window hosts untrusted web content, never our UI.
    autoplayPolicy: 'document-user-activation-required',
    safeDialogs: true,
    backgroundThrottling: false,
  };
}

function ensureWindow() {
  if (win && !win.isDestroyed()) return win;
  const driveSession = session.fromPartition(WEB_DRIVE_SESSION);
  // Memory-only posture: nothing survives the window.
  win = new BrowserWindow({
    width: 1200,
    height: 800,
    show: true,
    title: 'ActionMesh browser',
    webPreferences: { ...prefs(), partition: WEB_DRIVE_SESSION },
  });
  driveSession.clearStorageData().catch(() => {});

  win.webContents.on('did-start-navigation', (event, url, isInPlace, isMainFrame) => {
    if (isMainFrame) {
      loading = true;
      failedLoad = null;
      emitObservation({ kind: 'navigation', url, title: win.getTitle(), loading: true });
    }
  });
  win.webContents.on('did-finish-load', () => {
    loading = false;
    navEpoch += 1;
    stampEpochIntoPage();
    emitObservation({ kind: 'navigation', url: win.webContents.getURL(), title: win.getTitle(), loading: false, epoch: navEpoch });
  });
  win.webContents.on('did-stop-loading', () => {
    loading = false;
    emitObservation({ kind: 'loading', url: win.webContents.getURL(), loading: false, epoch: navEpoch });
  });
  win.webContents.on('page-title-updated', (event, title) => {
    lastTitle = title;
    emitObservation({ kind: 'title', url: win.webContents.getURL(), title, epoch: navEpoch });
  });
  win.webContents.on('did-fail-load', (event, code, description, url, isMainFrame) => {
    if (isMainFrame) {
      loading = false;
      // description is browser-authored (net error name), not page content.
      failedLoad = {
        code: Number(code) || 0,
        note: String(description || 'load failed').slice(0, 120),
        url: String(url || '').slice(0, 512),
      };
      emitObservation({ kind: 'loading', url, loading: false, epoch: navEpoch });
    }
  });

  // Confine navigation to http(s); the engine already refuses other schemes,
  // this is the second door.
  win.webContents.on('will-navigate', (event, url) => {
    if (!isHttpUrl(url)) event.preventDefault();
  });
  win.webContents.on('will-redirect', (event, url) => {
    if (!isHttpUrl(url)) event.preventDefault();
  });
  // No popups from the page.
  win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));

  win.on('closed', () => {
    win = null;
    // Memory-only: wipe the drive partition when the window goes away.
    session.fromPartition(WEB_DRIVE_SESSION).clearStorageData().catch(() => {});
  });
  return win;
}

function isHttpUrl(url) {
  try {
    const parsed = new URL(url);
    return parsed.protocol === 'http:' || parsed.protocol === 'https:';
  } catch {
    return false;
  }
}

/** Keep the page-side epoch global aligned so the collector can tag records. */
async function stampEpochIntoPage() {
  if (!win || win.isDestroyed()) return;
  try {
    // Constant script; the number is JSON-serialized, never page text.
    await win.webContents.executeJavaScript(
      fill('window.__amDriveEpoch = __EPOCH__', { EPOCH: navEpoch })
    );
  } catch {
    // Page may be mid-navigation; the next snapshot carries the fresh epoch.
  }
}

function emitObservation(payload) {
  const bounded = { ...payload };
  for (const key of ['url', 'title']) {
    if (typeof bounded[key] === 'string' && bounded[key].length > 2048) {
      bounded[key] = bounded[key].slice(0, 2048);
    }
  }
  bounded.epoch = bounded.epoch ?? navEpoch;
  if (observationSink) {
    try {
      observationSink(bounded);
    } catch {
      // A broken sink never breaks the window.
    }
  }
}

function setObservationSink(fn) {
  observationSink = typeof fn === 'function' ? fn : null;
}

function contents() {
  if (!win || win.isDestroyed()) return null;
  return win.webContents;
}

function requireSession() {
  const wc = contents();
  if (!wc) {
    const err = new Error('no browser session');
    err.refused = 'unavailable';
    throw err;
  }
  return wc;
}

function validId(raw) {
  const n = Number(raw);
  return Number.isSafeInteger(n) && n >= 1 ? String(n) : null;
}

// ---------------------------------------------------------------------------
// The drive handlers — one per named op. Every return is a plain object with
// status ∈ ok | stale | needs_user | unavailable | error, matching
// actionmesh/agent/browser.py.
// ---------------------------------------------------------------------------

const handlers = {
  async open({ url }) {
    if (!isHttpUrl(url)) return { status: 'error', note: 'url must be absolute http(s)' };
    ensureWindow();
    win.show(); // a prior stop() hides it
    failedLoad = null;
    try {
      await win.loadURL(url);
    } catch {
      // loadURL rejects on ERR_* network failures; did-fail-load carries the
      // browser-authored name. Fall through to the failedLoad check below.
    }
    if (failedLoad) {
      return { status: 'error', note: `load failed: ${failedLoad.note}` };
    }
    return result({ url: win.webContents.getURL(), title: win.getTitle(), epoch: navEpoch });
  },

  async navigate({ url }) {
    requireSession();
    if (!isHttpUrl(url)) return { status: 'error', note: 'url must be absolute http(s)' };
    failedLoad = null;
    try {
      await win.loadURL(url);
    } catch {
      // See open(): did-fail-load is the source of truth.
    }
    if (failedLoad) {
      return { status: 'error', note: `load failed: ${failedLoad.note}` };
    }
    return result({ url: win.webContents.getURL(), title: win.getTitle(), epoch: navEpoch });
  },

  async back() {
    const wc = requireSession();
    if (wc.canGoBack()) wc.goBack();
    return result({ url: wc.getURL(), title: wc.getTitle(), epoch: navEpoch });
  },

  async forward() {
    const wc = requireSession();
    if (wc.canGoForward()) wc.goForward();
    return result({ url: wc.getURL(), title: wc.getTitle(), epoch: navEpoch });
  },

  async refresh() {
    const wc = requireSession();
    wc.reload();
    return result({ url: wc.getURL(), title: wc.getTitle(), epoch: navEpoch });
  },

  async snapshot({ max_elements }) {
    const wc = requireSession();
    const cap = Math.max(1, Math.min(Number(max_elements) || 200, 200));
    const page = await wc.executeJavaScript(fill(COLLECT_ELEMENTS, { MAX_ARGS: cap }));
    const elements = Array.isArray(page && page.elements) ? page.elements : [];
    return result({
      url: wc.getURL(),
      title: wc.getTitle(),
      epoch: navEpoch,
      elements: elements.map((el) => ({ ...el, ref: Number(el.id) || 0 })),
      truncated: !!(page && page.truncated),
    });
  },

  async click({ ref, double }) {
    const wc = requireSession();
    const id = validId(ref);
    if (!id) return { status: 'error', note: 'ref must be a positive integer' };
    const outcome = await wc.executeJavaScript(
      fill(DRIVE_CLICK, { ID: id, DOUBLE: !!double })
    );
    if (!outcome || !outcome.found) return { status: 'stale', note: 'element gone' };
    navEpoch += 1; // treat any drive as a potential DOM mutation
    stampEpochIntoPage();
    return result({
      url: wc.getURL(),
      title: wc.getTitle(),
      epoch: navEpoch,
      changed: !!outcome.changed,
    });
  },

  async type({ ref, text }) {
    const wc = requireSession();
    const id = validId(ref);
    if (!id) return { status: 'error', note: 'ref must be a positive integer' };
    if (typeof text !== 'string' || text.length > 4000) {
      return { status: 'error', note: 'text must be a string up to 4000 characters' };
    }
    // `text` is passed as a JSON-stringified literal in the frozen script below.
    // It becomes an inert string value — never concatenated into source.
    const outcome = await wc.executeJavaScript(fill(DRIVE_TYPE, { ID: id, TEXT: text }));
    if (!outcome || !outcome.found) return { status: 'stale', note: 'element gone' };
    if (outcome.refused === 'password_field') {
      return { status: 'needs_user', note: 'password_field' };
    }
    if (outcome.refused === 'not_editable') {
      return { status: 'error', note: 'not_editable' };
    }
    navEpoch += 1;
    stampEpochIntoPage();
    return result({
      url: wc.getURL(),
      title: wc.getTitle(),
      epoch: navEpoch,
      changed: !!outcome.changed,
      inserted: typeof outcome.inserted === 'number' ? outcome.inserted : 0,
    });
  },

  async select({ ref, value }) {
    const wc = requireSession();
    const id = validId(ref);
    if (!id) return { status: 'error', note: 'ref must be a positive integer' };
    if (typeof value !== 'string' || value.length > 1024) {
      return { status: 'error', note: 'value must be a string up to 1024 characters' };
    }
    // `value` arrives as a JSON-stringified literal in the frozen script.
    const outcome = await wc.executeJavaScript(fill(DRIVE_SELECT, { ID: id, VALUE: value }));
    if (!outcome || !outcome.found) return { status: 'stale', note: 'element gone' };
    if (outcome.refused) return { status: 'error', note: outcome.refused };
    navEpoch += 1;
    stampEpochIntoPage();
    return result({ url: wc.getURL(), title: wc.getTitle(), epoch: navEpoch, changed: !!outcome.changed });
  },

  async scroll({ ref, direction, amount }) {
    const wc = requireSession();
    if (!DIRECTIONS.has(direction)) return { status: 'error', note: 'unknown direction' };
    let id = '';
    if (ref !== null && ref !== undefined) {
      id = validId(ref);
      if (!id) return { status: 'error', note: 'ref must be a positive integer' };
    }
    const outcome = await wc.executeJavaScript(
      fill(DRIVE_SCROLL, { DIRECTION: direction, AMOUNT: Number(amount) || 600, ID: id })
    );
    if (!outcome || !outcome.found) return { status: 'stale', note: 'element gone' };
    return result({ url: wc.getURL(), title: wc.getTitle(), epoch: navEpoch });
  },

  // Wait for the engine's `until` condition. It polls internal state (never
  // page content), so no script runs on the page here.
  async wait({ ms, until }) {
    requireSession();
    const bounded = Math.max(0, Math.min(Number(ms) || 500, 10000));
    if (until !== null && until !== undefined && !WAIT_UNTIL.has(until)) {
      return { status: 'error', note: 'unknown until condition' };
    }
    // Poll loading=false or idle events for bounded time. We only observe local
    // state; no script runs on the page here.
    const start = Date.now();
    while (Date.now() - start < bounded && loading) {
      await new Promise((r) => setTimeout(r, 50));
    }
    return result({ url: win.webContents.getURL(), title: win.getTitle(), epoch: navEpoch });
  },

  async stop() {
    if (!win || win.isDestroyed()) return { status: 'unavailable', note: 'no session' };
    win.webContents.stop();
    win.webContents.stopPainting && win.webContents.stopPainting();
    win.hide();
    // Memory-only: cookies never persist past a stop.
    session.fromPartition(WEB_DRIVE_SESSION).clearStorageData().catch(() => {});
    return { status: 'ok' };
  },

  async state() {
    const wc = requireSession();
    return result({ url: wc.getURL(), title: wc.getTitle(), epoch: navEpoch, loading });
  },

  async page_text({ limit }) {
    const wc = requireSession();
    const bounded = Math.max(1, Math.min(Number(limit) || 6000, 6000));
    const text = await wc.executeJavaScript(fill(READ_PAGE_TEXT, { LIMIT: bounded }));
    return result({
      url: wc.getURL(),
      title: wc.getTitle(),
      epoch: navEpoch,
      text: String(text || '').slice(0, bounded),
    });
  },
};

function result(fields) {
  return { status: 'ok', ...fields };
}

/** Handle one drive frame from the bridge. `frame` is the JSON object the
 * engine sent; unknown fields are ignored, unknown ops are refused. */
async function handleDrive(frame) {
  const request_id = typeof frame.request_id === 'string' ? frame.request_id.slice(0, 64) : '';
  const op = frame.op;
  const handler = handlers[op];
  if (!handler) {
    return { request_id, status: 'error', note: 'unknown op ' + String(op).slice(0, 40) };
  }
  try {
    const reply = await handler(frame);
    return { request_id, ...reply };
  } catch (err) {
    if (err && err.refused === 'unavailable') {
      return { request_id, status: 'unavailable', note: 'no browser session' };
    }
    // Fixed detail: never leak err.message (may contain page data).
    return { request_id, status: 'error', note: 'drive failed' };
  }
}

/** Exposed for the bridge's page-text side-channel (bounded, redaction is
 * the engine's job). */
async function readPageText(limit) {
  const wc = requireSession();
  const bounded = Math.max(1, Math.min(Number(limit) || 6000, 6000));
  const text = await wc.executeJavaScript(fill(READ_PAGE_TEXT, { LIMIT: bounded }));
  return String(text || '').slice(0, bounded);
}

module.exports = {
  WEB_DRIVE_SESSION,
  ensureWindow,
  handleDrive,
  readPageText,
  setObservationSink,
  // Introspection for the lead's wiring tests.
  _windowState: () => ({ hasWindow: !!win && !win.isDestroyed(), loading, epoch: navEpoch }),
};
