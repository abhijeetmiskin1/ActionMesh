/**
 * IPC channels for the browser drive (C42) — the renderer-facing surface.
 *
 * Registers `ipcMain.handle` endpoints the dashboard UI invokes to drive the
 * kept browser window (desktop/app/browser_window.js), and bridges page
 * observations back to the UI as bounded events. This is the UI path; the
 * engine's own path is the NDJSON bridge (browser_bridge.js) calling
 * `handleDrive` directly — both go through the SAME handlers, so there is one
 * drive surface, not two.
 *
 * Payloads mirror actionmesh/agent/browser_ipc.py exactly: fixed keys, no
 * arbitrary JS, refs are integers. Everything the UI may send is validated
 * again here — a handler that trusts its renderer is not a hardened boundary.
 *
 * Lead wiring (main.js): require both modules, call
 * `registerBrowserIpc(browserWindow)` after app.whenReady(), and forward
 * `browser:*` events to the UI via preload's `onBrowserEvent` subscription.
 */

'use strict';

const { ipcMain } = require('electron');

const SCROLL_DIRECTIONS = new Set(['up', 'down', 'left', 'right']);
const WAIT_UNTIL = new Set(['load', 'idle', 'title', 'elements']);

function bad(reason) {
  const err = new Error(reason); // the UI sees a fixed reason, never page data
  throw err;
}

function needString(value, key, max) {
  if (typeof value !== 'string' || value.trim() === '' || value.length > max) {
    bad(key + ' must be a string up to ' + max + ' characters');
  }
  return value;
}

function needRef(value) {
  const n = Number(value);
  if (!Number.isSafeInteger(n) || n < 1) bad('ref must be a positive integer');
  return n;
}

function optionalRef(value) {
  if (value === undefined || value === null) return undefined;
  return needRef(value);
}

/**
 * @param {object} browserWindow module surface from desktop/app/browser_window.js
 *        (must expose handleDrive, setObservationSink, readPageText).
 * @param {object} mainWindowGetter () => BrowserWindow|null — where to push
 *        observations. Never captured eagerly: the UI window can be closed.
 */
function registerBrowserIpc(browserWindow, mainWindowGetter) {
  const drive = async (frame) => {
    const reply = await browserWindow.handleDrive(frame);
    return reply; // already { request_id, status, ... } with bounded fields
  };

  ipcMain.handle('browser:open', (_event, payload) =>
    drive({ op: 'open', url: needString(payload && payload.url, 'url', 4096), request_id: 'ui' }));

  ipcMain.handle('browser:navigate', (_event, payload) =>
    drive({ op: 'navigate', url: needString(payload && payload.url, 'url', 4096), request_id: 'ui' }));

  ipcMain.handle('browser:search', async (_event, payload) => {
    // Read-only from the UI's point of view too: the UI may search, but the
    // channel exposes no click/buy affordance beyond the generic drive ops.
    const query = needString(payload && payload.query, 'query', 512);
    const engine = payload && payload.engine !== undefined
      ? needString(payload.engine, 'engine', 32)
      : undefined;
    return drive({ op: 'navigate', url: searchUrl(query, engine), request_id: 'ui' });
  });

  ipcMain.handle('browser:inspect', () => drive({ op: 'snapshot', request_id: 'ui', max_elements: 200 }));
  ipcMain.handle('browser:snapshot', () => drive({ op: 'snapshot', request_id: 'ui', max_elements: 200 }));

  ipcMain.handle('browser:click', (_event, payload) =>
    drive({
      op: 'click',
      ref: needRef(payload && payload.ref),
      double: !!(payload && payload.double),
      request_id: 'ui',
    }));

  ipcMain.handle('browser:type', (_event, payload) =>
    drive({
      op: 'type',
      ref: needRef(payload && payload.ref),
      text: typeof (payload && payload.text) === 'string' ? payload.text : bad('text must be a string'),
      request_id: 'ui',
    }));

  ipcMain.handle('browser:select', (_event, payload) =>
    drive({
      op: 'select',
      ref: needRef(payload && payload.ref),
      value: typeof (payload && payload.value) === 'string' ? payload.value : bad('value must be a string'),
      request_id: 'ui',
    }));

  ipcMain.handle('browser:scroll', (_event, payload) => {
    const direction = payload && payload.direction;
    if (!SCROLL_DIRECTIONS.has(direction)) bad('direction must be up/down/left/right');
    return drive({
      op: 'scroll',
      direction,
      ref: optionalRef(payload && payload.ref),
      amount: payload && payload.amount !== undefined ? Number(payload.amount) : undefined,
      request_id: 'ui',
    });
  });

  ipcMain.handle('browser:back', () => drive({ op: 'back', request_id: 'ui' }));
  ipcMain.handle('browser:forward', () => drive({ op: 'forward', request_id: 'ui' }));
  ipcMain.handle('browser:refresh', () => drive({ op: 'refresh', request_id: 'ui' }));

  ipcMain.handle('browser:wait', (_event, payload) => {
    const until = payload && payload.until;
    if (until !== undefined && !WAIT_UNTIL.has(until)) bad('until must be load/idle/title/elements');
    const ms = payload && payload.ms !== undefined ? Number(payload.ms) : undefined;
    if (ms !== undefined && (!Number.isFinite(ms) || ms < 0 || ms > 10000)) bad('ms must be 0..10000');
    return drive({ op: 'wait', ms, until, request_id: 'ui' });
  });

  ipcMain.handle('browser:stop', () => drive({ op: 'stop', request_id: 'ui' }));
  ipcMain.handle('browser:state', () => drive({ op: 'state', request_id: 'ui' }));

  ipcMain.handle('browser:page_text', (_event, payload) => {
    const limit = payload && payload.limit !== undefined ? Number(payload.limit) : 6000;
    if (!Number.isFinite(limit) || limit < 1 || limit > 6000) bad('limit must be 1..6000');
    return drive({ op: 'page_text', limit, request_id: 'ui' });
  });

  // Observations: the window pushes facts, we relay a bounded copy to the UI
  // shell. Page-derived strings are forwarded as-is for *display* only — the
  // renderer must treat them as untrusted text (the UI never evals them;
  // contextIsolation keeps it honest).
  browserWindow.setObservationSink((observation) => {
    const target = mainWindowGetter ? mainWindowGetter() : null;
    if (!target || target.isDestroyed()) return;
    const bounded = { ...observation };
    if (Array.isArray(bounded.elements) && bounded.elements.length > 50) {
      bounded.elements = bounded.elements.slice(0, 50);
      bounded.truncated = true;
    }
    target.webContents.send('browser:event', bounded);
  });
}

const SEARCH_ENGINES = {
  duckduckgo: 'https://duckduckgo.com/?q=',
  google: 'https://www.google.com/search?q=',
  bing: 'https://www.bing.com/search?q=',
};

function searchUrl(query, engine) {
  const name = String(engine || 'duckduckgo').toLowerCase();
  const base = SEARCH_ENGINES[name] || SEARCH_ENGINES.duckduckgo;
  return base + encodeURIComponent(query);
}

module.exports = { registerBrowserIpc };
