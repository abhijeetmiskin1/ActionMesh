'use strict';
/**
 * A stand-in for `app/provider_bridge.js`, for the main-process tests.
 *
 * The real bridge listens on a Unix socket in the profile directory and holds that
 * listener until the app quits — which is right, and which also means a unit test that
 * loaded it would never exit. The bridge's own behaviour (one request per connection,
 * token comparison, lane resolution) is covered against a real socket in
 * app/provider_bridge.test.js. Here it only has to exist, so `main.js` boots and the
 * purpose handlers — which read settings, not the bridge — can be exercised.
 */
async function startBridge({ socketPath }) {
  return {
    socketPath,
    closed: false,
    async close() { this.closed = true; },
  };
}

const MAX_FRAME_BYTES = 256 * 1024;

module.exports = { startBridge, MAX_FRAME_BYTES };