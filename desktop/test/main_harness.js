'use strict';
/**
 * Boot the real `app/main.js` under fake Electron modules.
 *
 * `main.js` is a singleton: it takes the single-instance lock, registers IPC handlers
 * once, and starts a backend. That means a test file gets one boot, and a scenario that
 * needs a *different* starting state (say, a pre-C42 settings file) has to run in its
 * own process. This module is the shared half of both: it installs the resolver hooks,
 * requires main, and waits for `whenReady`'s chain to finish registering.
 */
const path = require('node:path');
const { registerHooks } = require('node:module');

const APP_DIR = path.resolve(__dirname, '..', 'app');

function boot() {
  const electron = require.resolve('./electron_stub.js');
  const backend = require.resolve('./backend_stub.js');
  const bridge = require.resolve('./provider_bridge_stub.js');
  const browserBridge = require.resolve('./browser_bridge_stub.js');

  // Only main.js's own imports are redirected. The modules replaced here are the
  // ones that spawn a child process, listen on a socket, or need a display server;
  // everything else — settings, providers, the purpose logic — is the real thing.
  // The browser bridge is stubbed for the same reason the model bridge is: its real
  // net.Server would hold the test process's event loop open after the last test,
  // and the teardown only stops the stream and the backend.
  registerHooks({
    resolve(specifier, context, nextResolve) {
      if (specifier === 'electron') return { url: `file://${electron}`, shortCircuit: true };
      if (context.parentURL && context.parentURL.endsWith('/desktop/app/main.js')) {
        if (specifier === './backend') return { url: `file://${backend}`, shortCircuit: true };
        if (specifier === './provider_bridge') return { url: `file://${bridge}`, shortCircuit: true };
        if (specifier === './browser_bridge') return { url: `file://${browserBridge}`, shortCircuit: true };
      }
      return nextResolve(specifier, context);
    },
  });

  delete require.cache[path.join(APP_DIR, 'main.js')];
  const main = require(path.join(APP_DIR, 'main.js'));
  const stub = require('./electron_stub.js');

  return stub.__appState.ready
    // whenReady's `.then(async …)` — the handler registration — is queued behind this
    // promise. Two turns of the event loop is enough for it to run to its first await.
    .then(() => new Promise((resolve) => setImmediate(resolve)))
    .then(() => ({ main, stub, invoke: stub.__invoke, store: stub.__store }));
}

module.exports = { boot, APP_DIR };