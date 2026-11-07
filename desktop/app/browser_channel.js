/**
 * IPC channels for the browser drive (C42) — the renderer-facing surface.
 *
 * Registers renderer controls which propose ordinary backend actions. The backend
 * alone authorizes and executes them through the existing private drive bridge.
 * Renderer controls never invoke handleDrive directly.
 *
 * Payloads mirror actionmesh/agent/browser_ipc.py exactly: fixed keys, no
 * arbitrary JS, refs are integers. Everything the UI may send is validated
 * again here — a handler that trusts its renderer is not a hardened boundary.
 *
 * Lead wiring (main.js): require both modules, call
 * `registerBrowserIpc(browserWindow, getMainWindow, runAction)` after app.whenReady().
 * Observations reach the UI through the backend's scoped execution output stream.
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
function registerBrowserIpc(browserWindow, mainWindowGetter, runAction) {
  const drive = async (frame) => {
    if (typeof runAction !== 'function') bad('the browser execution pipeline is not configured');
    return runAction(frame);
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
    return drive({ op: 'search', query, engine, request_id: 'ui' });
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

  // Driver IDs are not the backend's session refs. Page content reaches the UI
  // only through actor-scoped, redacted execution output from the backend.
  browserWindow.setObservationSink(() => {});

}

module.exports = { registerBrowserIpc };
