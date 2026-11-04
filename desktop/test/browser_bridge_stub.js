'use strict';
/**
 * A stand-in for `app/browser_bridge.js`, for the main-process tests.
 *
 * The real bridge listens on a Unix socket in the profile directory and holds that
 * listener until the app quits — which is right, and which also means a unit test that
 * loaded it would never exit (a listening net.Server keeps the event loop alive after
 * the last test, and nothing in the test teardown closes it). The bridge's own
 * behaviour (token comparison, frame validation, one frame per connection) belongs to
 * its own socket test; here it only has to exist, so `main.js` boots and the purpose
 * handlers — which read settings, not the bridge — can be exercised. Mirrors
 * provider_bridge_stub.js exactly.
 */
async function startBridge({ socketPath }) {
  return {
    socketPath,
    closed: false,
    async close() { this.closed = true; },
    stats() { return {}; },
  };
}

const OP_NAMES = new Set([
  'open',
  'navigate',
  'back',
  'forward',
  'refresh',
  'snapshot',
  'click',
  'type',
  'select',
  'scroll',
  'wait',
  'stop',
  'state',
  'page_text',
]);

module.exports = { startBridge, OP_NAMES };
