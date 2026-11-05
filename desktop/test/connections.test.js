'use strict';
/**
 * The Connections Center lanes, as the person meets them and as the probe
 * answers.
 *
 * app.js owns the page's lanes; this module is the second renderer (Phase 6,
 * Agent 4) that Lead wires in after delivery. Its job is different from the
 * sidebar's: it never configures anything, so its three checks are that it
 * (1) prints only the six honest words a lane may use, (2) never writes
 * `innerHTML` (provider and device strings are untrusted display data), and
 * (3) never renders a key, a token, or a device identity it was not given.
 *
 * No jsdom is installed, so `dom_harness.js` supplies a deliberately small
 * document — enough for the module's `el()`/`byId()`/`querySelectorAll`
 * usage — plus a fake `window.actionmesh`. See `ui_render.test.js` for the
 * sibling renderer suite this follows.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const { install } = require('./dom_harness.js');

const SIX_WORDS = ['Connected', 'Ready', 'Not configured', 'Unavailable', 'Error', 'Needs authorization'];

/** The static state vocabulary `providers.js` publishes, copied here so the
 * fake is plain data. The module must map exactly these onto the six words. */
const PROVIDER_STATES = [
  'not_configured',
  'missing_key',
  'connecting',
  'connected',
  'invalid_credentials',
  'provider_unavailable',
  'timeout',
  'rate_limited',
];

function statusFor(purpose, overrides) {
  return {
    purpose,
    provider: '',
    providerLabel: '',
    model: '',
    endpoint: '',
    hasKey: false,
    encryptionAvailable: true,
    legacy: false,
    state: 'not_configured',
    detail: 'Nothing is configured for this job yet.',
    text: 'Not configured',
    testedAt: '',
    ...overrides,
  };
}

/**
 * A fake preload narrowed to what the module touches. It answers like the
 * real one does (records every call, moves its statuses on configure/test,
 * answers `state` with the same snapshot shape) and — crucially — its device
 * list carries the private fields a row must never print, so the test can
 * prove the renderer allowlists what it renders.
 */
function fakeBridge(overrides = {}) {
  const statuses = {
    chat: statusFor('chat'),
    reasoning: statusFor('reasoning'),
    image: statusFor('image'),
    audio: statusFor('audio'),
  };
  const calls = [];
  const record = (name, answer) => (...args) => {
    calls.push({ name, args });
    return Promise.resolve(typeof answer === 'function'
      ? answer(...args)
      : answer);
  };

  const appState = () => ({
    product: 'ActionMesh',
    version: '0.1.0',
    backend: { status: 'running', port: 47821, runtimeMode: 'development-venv', dataDir: '/tmp/actionmesh', lastError: '' },
    provider: statuses.chat,
    purposes: { ...statuses },
  });

  const ready = () => ({ status: 'ready', mode: 'durable', online_devices: 1, local_device_id: 'laptop-1' });

  const devices = () => ([
    {
      device_id: 'laptop-1', name: 'This Laptop', kind: 'laptop', platform: 'linux',
      online: true, enrolled: true, risk_ceiling: 'low', last_seen: '2026-11-04T12:00:00Z',
      // The real route deliberately never sends these. Sending them here
      // proves the renderer allowlists itself instead of trusts the route.
      identity_id: 'identity-sentinel-should-not-print',
      secret: 'secret-sentinel-should-not-print',
      token: 'token-sentinel-should-not-print',
    },
  ]);

  const capabilities = () => ([
    { name: 'device.files.read' },
    { name: 'media.status' },
  ]);

  const map = {
    state: () => appState(),
    browserState: () => ({ status: 'ok', url: 'https://example.com/', title: 'Example', epoch: 3 }),
    request: record('request', (payload) => {
      if (payload.method === 'GET' && payload.path === '/ready') return { ok: true, status: 200, body: ready() };
      if (payload.method === 'GET' && payload.path === '/devices') return { ok: true, status: 200, body: devices() };
      if (payload.method === 'GET' && payload.path === '/capabilities') return { ok: true, status: 200, body: capabilities() };
      if (payload.method === 'POST' && payload.path === '/pairing/code') {
        return { ok: true, status: 200, body: {
          code: '456-789', laptop_device_id: 'laptop-1', expires_in_s: 300.0,
          expires_at: '2026-11-05T12:05:00Z', max_risk: (payload.body && payload.body.max_risk) || 'low',
          pairing_durable: false,
        } };
      }
      if (payload.method === 'POST' && payload.path === '/pairing/revoke') {
        return { ok: true, status: 200, body: { revoked: 2 } };
      }
      if (payload.method === 'POST' && /^\/devices\/[^/]+\/revoke$/.test(payload.path)) {
        const id = payload.path.split('/')[2];
        return { ok: true, status: 200, body: { device_id: id, revoked: true, identity_id: 'identity-sentinel-must-not-print' } };
      }
      return { ok: false, status: 0, error: `${payload.method} ${payload.path} not mocked` };
    }),
    purposeStatus: record('purposeStatus', (purpose) => ({ ok: true, status: { ...statuses[purpose] } })),
    purposeTest: record('purposeTest', (payload) => ({ ok: true, state: 'connected', detail: 'The provider accepted the request.' })),
    ...overrides,
  };
  return { statuses, calls, bridge: map };
}

function loadConnectionsModule(bridge) {
  const page = install({ bridge });
  const source = fs.readFileSync(path.join(__dirname, '..', 'ui', 'connections.js'), 'utf8');
  // Capture the export from window: connections.js is wrapped in an IIFE
  // (classic scripts share one global lexical scope, so it cannot leak a
  // top-level binding), and window.actionmeshConnections is the surface the
  // page actually uses. module.exports is assigned inside the same IIFE.
  const evaluate = new Function(
    'window', 'document', 'navigator', 'setTimeout', 'setInterval',
    'clearTimeout', 'clearInterval', 'confirm', 'alert', 'URL', 'fetch',
    'Node', 'CSS', 'module',
    `${source}\nreturn (window && window.actionmeshConnections) || (module && module.exports) || null;`,
  );
  const api = evaluate(
    page.window, page.document, page.window.navigator || {}, page.timers.setTimeout,
    page.timers.setInterval, page.timers.clearTimeout, page.timers.clearInterval,
    () => false, () => {}, page.window.URL, page.window.fetch,
    page.window.Node, page.window.CSS, { exports: {} },
  );
  return { ...page, api };
}

async function boot(overrides) {
  const fake = fakeBridge(overrides);
  const page = loadConnectionsModule(fake.bridge);
  const data = await page.api.initConnections();
  return { ...page, ...fake, data };
}

test('exactly six honest words are published', async () => {
  const { api } = await boot();
  assert.deepEqual(api.CONNECTION_STATES, SIX_WORDS);
});

test('the module never writes innerHTML', async () => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'ui', 'connections.js'), 'utf8');
  const commentless = source.replace(/\/\*[\s\S]*?\*\//g, '');
  assert.equal(/\.innerHTML\s*=/.test(commentless), false, 'no innerHTML assignments');
  const { document } = await boot();
  assert.deepEqual(document.__innerHTMLWrites, []);
});

test('the browser lane states a session only when the window answered ok', async () => {
  const { api } = await boot();
  const internals = api._internals;
  assert.equal(internals.browserWord({ status: 'ok' }, null), 'Connected');
  assert.equal(internals.browserWord({ status: 'unavailable', note: 'no socket' }, null), 'Unavailable');
  assert.equal(internals.browserWord(null, null), 'Unavailable');
  assert.equal(internals.browserWord(null, new Error('no socket')), 'Error');
});

test('MEDIA never prints a playback state on its own', async () => {
  const { api } = await boot();
  const { mediaAdvertised } = api._internals;
  assert.equal(mediaAdvertised(['media.status', 'device.files.read']), true);
  assert.equal(mediaAdvertised(['device.files.read']), false);
  assert.equal(mediaAdvertised(null), false);
  // Crucially: an advertised media boundary is "Ready" (configurable), never
  // "Connected" — Connected is the provider probe's word only.
  const { document } = await boot();
  const mediaHost = document.getElementById('connectionsMediaKv');
  assert.notEqual(mediaHost, null);
  assert.match(mediaHost.textContent, /Not reported by this lane/);
  assert.match(mediaHost.textContent, /MPRIS/);
  assert.match(mediaHost.textContent, /not been performed/);
  // The one word this lane may never mint on its own is 'playing' as a state.
  // The advertised-boundary copy explains nothing here claims it.
  assert.equal(/playing/.test(mediaHost.textContent), false);
});

test('a device row never prints the identity handle, secret, or token', async () => {
  const { document } = await boot();
  const devicesHost = document.getElementById('connectionsDevicesKv');
  assert.notEqual(devicesHost, null);
  const text = devicesHost.textContent;
  assert.equal(text.includes('identity-sentinel-should-not-print'), false);
  assert.equal(text.includes('secret-sentinel-should-not-print'), false);
  assert.equal(text.includes('token-sentinel-should-not-print'), false);
  assert.match(text, /laptop-1/);
});

test('pairing mint shows the server code once, from the mint response', async () => {
  const { document } = await boot();
  const out = document.getElementById('connectionsPairOut');
  assert.equal(out.hidden, true, 'PairOut starts hidden: the code only appears after a mint');
  // The mint button is the primary button in the pairing block.
  const mint = [...document.querySelectorAll('#connectionsDevicesKv button')]
    .find((button) => button.textContent === 'Pair New Device');
  assert.notEqual(mint, undefined, 'mint button exists');
  mint.click();
  // Flush the async POST handler.
  for (let turn = 0; turn < 5; turn += 1) await new Promise((resolve) => setImmediate(resolve));
  assert.equal(document.getElementById('connectionsPairOut').hidden, false);
  assert.equal(document.getElementById('connectionsPairOut').textContent.includes('456-789'), true);
});

test('the provider vocabulary maps onto exactly the six words', async () => {
  const { api } = await boot();
  const { providerWord } = api._internals;
  const words = new Set();
  for (const state of PROVIDER_STATES) {
    const word = providerWord(state ? { state } : null);
    words.add(word);
    assert.equal(SIX_WORDS.includes(word), true, `${state} maps onto a published word`);
  }
  assert.equal(providerWord({ state: 'connected' }), 'Connected');
  assert.equal(providerWord({ state: 'missing_key' }), 'Needs authorization');
  assert.equal(providerWord({ state: 'invalid_credentials' }), 'Needs authorization');
});

test('a disconnected backend renders its lane as unavailable rather than crashing', async () => {
  const failing = fakeBridge({
    state: () => Promise.reject(new Error('backend offline')),
    request: () => Promise.resolve({ ok: false, status: 0, error: 'backend offline' }),
    browserState: () => Promise.reject(new Error('no browser socket')),
  });
  const page = loadConnectionsModule(failing.bridge);
  await page.api.initConnections();
  const devicesHost = page.document.getElementById('connectionsDevicesKv');
  // loadConnections() never created the center — the center is created by
  // initConnections(); loadConnections only renders into hosts that exist.
  // So the disconnected case is exercised through init, which ensures them.
  assert.notEqual(devicesHost, null);
  assert.match(devicesHost.textContent, /not running/);
  const providerHost = page.document.getElementById('connectionsProviderKv');
  assert.match(providerHost.textContent, /Not configured/);
});

test('Test Connection is disabled when the lane has nothing to probe', async () => {
  const { document } = await boot();
  const buttons = [...document.querySelectorAll('#connectionsProviderKv button')];
  assert.equal(buttons.length >= 4, true);
  // The default fixture has every lane at not_configured: no button may be
  // enabled, because there is no stored configuration to probe.
  for (const button of buttons) {
    if (button.textContent === 'Test Connection') {
      assert.equal(button.disabled, true, 'disabled Test Connection explains why');
    }
  }
});
