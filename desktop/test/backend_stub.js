'use strict';
/**
 * A stand-in for `app/backend.js`, for the main-process tests.
 *
 * The real `Backend.start()` spawns the FastAPI control plane. That is deliberate and
 * correct in the app, and it is the wrong thing to do from a unit test: it needs a
 * python runtime, a free port, and a data directory, and it would leave a child
 * process behind if an assertion failed. Everything the purpose handlers touch is the
 * *settings* store, not the backend, so this stub keeps the same shape and starts
 * nothing.
 *
 * `snapshot()` reports the same fields the real one does, because `appState()` and the
 * tray menu both read them.
 */
const crypto = require('node:crypto');

function newLoopbackKey() {
  return crypto.randomBytes(32).toString('base64url');
}

class Backend {
  constructor(options = {}) {
    this.dataDir = options.dataDir || '';
    this.appRoot = options.appRoot || '';
    this.isPackaged = Boolean(options.isPackaged);
    this.resourcesPath = options.resourcesPath || '';
    this.settings = options.settings || {};
    this.key = options.key || '';
    this.bridge = options.bridge || null;
    this.browserBridge = options.browserBridge || null;
    this.child = null;
    this.port = 0;
    this.baseUrl = '';
    this.status = 'stopped';
    this.lastError = '';
    this.startedAt = null;
    this.dirs = null;
    this.runtime = { mode: 'stub' };
    this.claimed = false;
    this.prepared = false;
  }

  snapshot() {
    return {
      status: this.status,
      port: this.port,
      baseUrl: this.baseUrl,
      startedAt: this.startedAt,
      lastError: this.lastError,
      runtimeMode: this.runtime ? this.runtime.mode : 'unknown',
      dataDir: this.dataDir,
    };
  }

  prepare() {
    this.prepared = true;
    return {};
  }

  async start() {
    this.status = 'ready';
    this.port = 65535;
    this.baseUrl = `http://127.0.0.1:${this.port}`;
    this.startedAt = new Date().toISOString();
    return this.snapshot();
  }

  async stop() {
    this.status = 'stopped';
    this.baseUrl = '';
    return this.snapshot();
  }

  async restart() {
    await this.stop();
    return this.start();
  }
}

module.exports = { Backend, newLoopbackKey };