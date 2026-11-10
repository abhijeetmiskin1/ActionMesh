'use strict';
/**
 * The provider bridge, over a real Unix socket.
 *
 * The point of this file is the boundary, not the happy path: a wrong token must not
 * reach the provider, an oversized frame must not be buffered, a status reply must
 * carry no key material, and a change to the stored provider must be visible on the
 * next request. The settings and providers are fakes because those objects are
 * Agent A's and main.js's; the socket is real because that is the part this module
 * owns.
 */
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const { startBridge, MAX_FRAME_BYTES } = require('./provider_bridge.js');

const TOKEN = 'test-token-0123456789abcdef0123456789abcdef';
const API_KEY = 'sk-secret-should-never-appear';

// -- fixtures -------------------------------------------------------------------

function tempSocketPath() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'am-bridge-'));
  return { dir, socketPath: path.join(dir, 'provider-bridge.sock') };
}

/**
 * A SettingsStore-shaped fake. `values` and `secrets` are plain objects the test can
 * mutate between requests — that is how a provider "change" is simulated.
 */
function fakeSettings({ values = {}, secrets = {} } = {}) {
  const state = {
    values: { providerName: 'anthropic', providerModel: 'claude-sonnet-5-5', providerEndpoint: '', providerTimeout: 20, ...values },
    secrets: { providerApiKey: API_KEY, ...secrets },
    getCalls: 0,
  };
  return {
    state,
    get(key) { state.getCalls += 1; return state.values[key]; },
    secret(key) { return state.secrets[key] || ''; },
    hasSecret(key) { return Boolean(state.secrets[key]); },
  };
}

/** A providers module with a spec table and a recording generate(). */
function fakeProviders({ generateImpl } = {}) {
  const calls = [];
  const specs = [
    { id: 'anthropic', implemented: true, requiresKey: true, defaultModel: 'claude-sonnet-5-5' },
    { id: 'openai', implemented: true, requiresKey: true, defaultModel: 'gpt-4o' },
    { id: 'openai_compatible', implemented: true, requiresKey: true, defaultModel: 'local-model' },
  ];
  return {
    calls,
    specFor(id) { return specs.find((spec) => spec.id === id) || null; },
    async generate(config, request) {
      calls.push({ config: { ...config }, request: { ...request } });
      if (generateImpl) return generateImpl(config, request, calls.length);
      return { state: 'ok', content: { actions: [], notes: [] }, detail: '' };
    },
  };
}

function frameFor(extra) {
  return `${JSON.stringify({ token: TOKEN, op: 'generate', purpose: 'plan', system: 'sys', user: 'usr', ...extra })}\n`;
}

/** Send one frame, collect one reply (or nothing, on a refused connection). */
function roundTrip(socketPath, payload) {
  return new Promise((resolve, reject) => {
    let data = '';
    const socket = net.connect({ path: socketPath });
    const timer = setTimeout(() => reject(new Error('no reply within 5s')), 5000);
    socket.on('connect', () => socket.write(payload));
    socket.on('data', (chunk) => { data += chunk.toString('utf8'); });
    socket.on('end', () => {
      clearTimeout(timer);
      const line = data.trim();
      resolve({ raw: data, frame: line ? JSON.parse(line) : null });
    });
    socket.on('error', (error) => { clearTimeout(timer); reject(error); });
  });
}

async function withBridge(t, options) {
  const { dir, socketPath } = tempSocketPath();
  const bridge = await startBridge({
    socketPath,
    token: TOKEN,
    settings: options.settings,
    providers: options.providers,
    log: options.log,
    getStatus: options.getStatus,
    noteTestResult: options.noteTestResult,
  });
  t.after(async () => {
    await bridge.close();
    try { fs.rmSync(dir, { recursive: true, force: true }); } catch { /* best effort */ }
  });
  return { bridge, socketPath, dir };
}

// -- generate -------------------------------------------------------------------

test('plan generate returns the provider content', async (t) => {
  const settings = fakeSettings();
  const providers = fakeProviders({
    generateImpl: () => ({ state: 'ok', content: { actions: [{ type: 'open_app', parameters: { app: 'x' } }], notes: [] }, detail: '' }),
  });
  const { socketPath } = await withBridge(t, { settings, providers });

  const { frame, raw } = await roundTrip(socketPath, frameFor({}));
  assert.equal(frame.ok, true);
  assert.deepEqual(frame.content, { actions: [{ type: 'open_app', parameters: { app: 'x' } }], notes: [] });
  assert.equal(providers.calls.length, 1);
  assert.equal(providers.calls[0].request.purpose, 'plan');
  assert.equal(providers.calls[0].config.apiKey, API_KEY);
  // The key went to the provider, never back over the wire.
  assert.ok(!raw.includes(API_KEY));
  assert.ok(!raw.includes(TOKEN));
});

test('a bad token is refused and no provider call is made', async (t) => {
  const settings = fakeSettings();
  const providers = fakeProviders();
  const { socketPath, bridge } = await withBridge(t, { settings, providers });

  const { frame, raw } = await roundTrip(socketPath, frameFor({ token: `${TOKEN}x` }));
  assert.equal(frame, null, 'a refused connection gets no reply frame');
  assert.equal(raw, '');
  assert.equal(providers.calls.length, 0);
  assert.equal(bridge.stats().authFailures, 1);
});

test('an oversized frame is refused without reaching the provider', async (t) => {
  const settings = fakeSettings();
  const providers = fakeProviders();
  const { socketPath, bridge } = await withBridge(t, { settings, providers });

  const big = 'x'.repeat(MAX_FRAME_BYTES + 1024);
  const payload = `${JSON.stringify({ token: TOKEN, op: 'generate', purpose: 'plan', system: 's', user: big })}\n`;
  const { raw } = await roundTrip(socketPath, payload);
  assert.equal(raw, '');
  assert.equal(providers.calls.length, 0);
  assert.equal(bridge.stats().oversized, 1);
});

test('an oversized frame without a newline is dropped', async (t) => {
  const settings = fakeSettings();
  const providers = fakeProviders();
  const { socketPath, bridge } = await withBridge(t, { settings, providers });

  const payload = `{"token":"${TOKEN}","op":"status","pad":"${'y'.repeat(MAX_FRAME_BYTES + 10)}"`;
  const { raw } = await roundTrip(socketPath, payload);
  assert.equal(raw, '');
  assert.equal(bridge.stats().oversized, 1);
});

// -- status ---------------------------------------------------------------------

test('status exposes no key material', async (t) => {
  const settings = fakeSettings();
  const providers = fakeProviders();
  const { socketPath } = await withBridge(t, {
    settings,
    providers,
    getStatus: () => ({ state: 'connected' }),
  });

  const { raw, frame } = await roundTrip(socketPath, `${JSON.stringify({ token: TOKEN, op: 'status' })}\n`);
  assert.equal(frame.ok, true);
  assert.equal(frame.configured, true);
  assert.equal(frame.provider, 'anthropic');
  assert.equal(frame.model, 'claude-sonnet-5-5');
  assert.equal(frame.implemented, true);
  assert.equal(frame.hasKey, true);
  assert.equal(frame.state, 'connected');
  assert.equal(typeof frame.hasKey, 'boolean');

  assert.ok(!raw.includes(API_KEY), 'the key must not be serialised');
  assert.ok(!raw.includes(TOKEN), 'the token must not be serialised');
  // 'apiKey' must not appear as a *value* either — the object has no such field.
  for (const value of Object.values(frame)) {
    if (typeof value === 'string') assert.notEqual(value, 'apiKey');
  }
  assert.equal('apiKey' in frame, false);
});

test('status without an injected getStatus reports the conservative state', async (t) => {
  const settings = fakeSettings({ secrets: { providerApiKey: '' } });
  const providers = fakeProviders();
  const { socketPath } = await withBridge(t, { settings, providers });

  const { frame } = await roundTrip(socketPath, `${JSON.stringify({ token: TOKEN, op: 'status' })}\n`);
  assert.equal(frame.hasKey, false);
  assert.equal(frame.state, 'missing_key');
});

test('status records the last verified state over the inferred one', async (t) => {
  const settings = fakeSettings();
  const providers = fakeProviders();
  const { socketPath } = await withBridge(t, { settings, providers });

  await roundTrip(socketPath, frameFor({}));
  const { frame } = await roundTrip(socketPath, `${JSON.stringify({ token: TOKEN, op: 'status' })}\n`);
  assert.equal(frame.state, 'connected');
});

test('status narrates the injected verified state to main when it is supplied', async (t) => {
  const settings = fakeSettings({
    values: {
      purpose_reasoning_provider: 'openai_compatible',
      purpose_reasoning_model: 'local-model',
      purpose_reasoning_endpoint: 'http://localhost:20128/v1',
    },
    secrets: { reasoningApiKey: API_KEY },
  });
  const providers = fakeProviders();
  const seen = [];
  const { socketPath } = await withBridge(t, {
    settings,
    providers,
    getStatus: () => ({ state: seen.at(-1)?.state || 'not_configured' }),
    noteTestResult: (state, lane) => seen.push({ state, ...lane }),
  });

  await roundTrip(socketPath, frameFor({}));
  assert.deepEqual(seen, [{
    state: 'connected', purpose: 'reasoning', provider: 'openai_compatible',
    model: 'local-model', baseUrl: 'http://localhost:20128/v1',
  }]);
  const { frame } = await roundTrip(socketPath, frameFor({ op: 'status' }));
  assert.equal(frame.state, 'connected');
});

// -- live settings --------------------------------------------------------------

test('a provider change between two requests is honoured without a restart', async (t) => {
  const settings = fakeSettings();
  const providers = fakeProviders();
  const { socketPath } = await withBridge(t, { settings, providers });

  await roundTrip(socketPath, frameFor({}));
  assert.equal(providers.calls[0].config.provider, 'anthropic');

  // What the UI writes when the user picks a different provider.
  settings.state.values.providerName = 'openai';
  settings.state.values.providerModel = 'gpt-4o';
  settings.state.secrets.providerApiKey = `${API_KEY}-second`;

  await roundTrip(socketPath, frameFor({}));
  assert.equal(providers.calls.length, 2);
  assert.equal(providers.calls[1].config.provider, 'openai');
  assert.equal(providers.calls[1].config.model, 'gpt-4o');
  assert.equal(providers.calls[1].config.apiKey, `${API_KEY}-second`);
  assert.ok(settings.state.getCalls > 0);
});

test('a missing key produces missing_key and never calls the provider', async (t) => {
  const settings = fakeSettings({ secrets: { providerApiKey: '' } });
  const providers = fakeProviders();
  const { socketPath } = await withBridge(t, { settings, providers });

  const { frame, raw } = await roundTrip(socketPath, frameFor({}));
  assert.equal(frame.ok, false);
  assert.equal(frame.state, 'missing_key');
  assert.equal(typeof frame.detail, 'string');
  assert.equal(providers.calls.length, 0);
  assert.ok(!raw.includes(TOKEN));
});

test('a provider exception becomes a guard-authored detail, never the error text', async (t) => {
  const settings = fakeSettings();
  const providers = fakeProviders({
    generateImpl: () => { throw new Error(`upstream said: ${API_KEY} at /v1/messages`); },
  });
  const { socketPath } = await withBridge(t, { settings, providers });

  const { frame, raw } = await roundTrip(socketPath, frameFor({}));
  assert.equal(frame.ok, false);
  assert.equal(frame.state, 'provider_unavailable');
  assert.ok(!raw.includes(API_KEY), 'the provider error must not be relayed');
  assert.ok(!frame.detail.includes('/v1/messages'));
});

test('an out-of-contract provider state is not trusted', async (t) => {
  const settings = fakeSettings();
  const providers = fakeProviders({
    generateImpl: () => ({ state: 'weird', content: {}, detail: 'should not reach the peer' }),
  });
  const { socketPath } = await withBridge(t, { settings, providers });

  const { frame } = await roundTrip(socketPath, frameFor({}));
  assert.equal(frame.ok, false);
  assert.equal(frame.state, 'provider_unavailable');
  assert.equal(frame.detail.includes('should not reach the peer'), false);
});

test('an unknown op is answered with a guard-authored state', async (t) => {
  const settings = fakeSettings();
  const providers = fakeProviders();
  const { socketPath, bridge } = await withBridge(t, { settings, providers });

  const { frame } = await roundTrip(socketPath, `${JSON.stringify({ token: TOKEN, op: 'delete_everything' })}\n`);
  assert.equal(frame.ok, false);
  assert.equal(frame.state, 'malformed');
  assert.equal(bridge.stats().unknownOp, 1);
});

test('timeout_s is passed through to the provider config', async (t) => {
  const settings = fakeSettings();
  const providers = fakeProviders();
  const { socketPath } = await withBridge(t, { settings, providers });

  await roundTrip(socketPath, frameFor({ purpose: 'diagnose', timeout_s: 7 }));
  assert.equal(providers.calls[0].config.timeoutSeconds, 7);
  assert.equal(providers.calls[0].request.purpose, 'diagnose');
});

// -- concurrency and lifecycle --------------------------------------------------

test('two concurrent connections are both answered', async (t) => {
  const settings = fakeSettings();
  const providers = fakeProviders({
    generateImpl: async (config, request, n) => {
      await new Promise((resolve) => setTimeout(resolve, n === 1 ? 60 : 10));
      return { state: 'ok', content: { actions: [], notes: [], n } };
    },
  });
  const { socketPath } = await withBridge(t, { settings, providers });

  const [a, b] = await Promise.all([
    roundTrip(socketPath, frameFor({})),
    roundTrip(socketPath, frameFor({})),
  ]);
  assert.equal(a.frame.ok, true);
  assert.equal(b.frame.ok, true);
  assert.equal(providers.calls.length, 2);
});

test('close() removes the socket file', async (t) => {
  const settings = fakeSettings();
  const providers = fakeProviders();
  const { dir, socketPath } = tempSocketPath();
  const bridge = await startBridge({ socketPath, token: TOKEN, settings, providers });
  assert.ok(fs.existsSync(socketPath));

  await bridge.close();
  assert.equal(fs.existsSync(socketPath), false);
  // Closing twice is not an error.
  await bridge.close();
  fs.rmSync(dir, { recursive: true, force: true });
});

test('a stale socket file is replaced on start', async (t) => {
  const { dir, socketPath } = tempSocketPath();
  // A socket file left by a process that exited without closing its server — which is
  // what a crash leaves behind. Node unlinks on an orderly close, so a child that calls
  // process.exit() is the honest fixture.
  execFileSync(process.execPath, [
    '-e',
    'const net=require("net");'
      + `const s=net.createServer();s.listen(${JSON.stringify(socketPath)},()=>process.exit(0));`,
  ]);
  assert.ok(fs.existsSync(socketPath), 'the crashed child must have left its socket file');

  const settings = fakeSettings();
  const providers = fakeProviders();
  const bridge = await startBridge({ socketPath, token: TOKEN, settings, providers });
  t.after(async () => {
    await bridge.close();
    fs.rmSync(dir, { recursive: true, force: true });
  });

  const { frame } = await roundTrip(socketPath, `${JSON.stringify({ token: TOKEN, op: 'status' })}\n`);
  assert.equal(frame.ok, true);
});

test('a non-socket at the path is not deleted', async (t) => {
  const { dir, socketPath } = tempSocketPath();
  fs.writeFileSync(socketPath, 'not mine');

  await assert.rejects(
    () => startBridge({ socketPath, token: TOKEN, settings: fakeSettings(), providers: fakeProviders() }),
    /not a socket/,
  );
  assert.equal(fs.readFileSync(socketPath, 'utf8'), 'not mine');
  fs.rmSync(dir, { recursive: true, force: true });
});

test('a live server on the path refuses a second bridge', async (t) => {
  const settings = fakeSettings();
  const providers = fakeProviders();
  const first = await withBridge(t, { settings, providers });

  await assert.rejects(
    () => startBridge({ socketPath: first.socketPath, token: TOKEN, settings, providers }),
    /already listening/,
  );
});

test('the socket is mode 0600 after listen', async (t) => {
  const settings = fakeSettings();
  const providers = fakeProviders();
  const { dir, socketPath } = tempSocketPath();
  const bridge = await startBridge({ socketPath, token: TOKEN, settings, providers });
  t.after(async () => {
    await bridge.close();
    fs.rmSync(dir, { recursive: true, force: true });
  });
  const mode = fs.statSync(socketPath).mode & 0o777;
  assert.equal(mode, 0o600);
});
// -- per-purpose routing ---------------------------------------------------------

// The bridge used to read the single pre-C42 provider slot, so choosing a Reasoning
// provider in Settings changed a card and nothing else: the planner kept using
// whatever chat had. These tests pin the routing rule itself.

function laneSettings({ lanes = {}, legacy = {} } = {}) {
  const values = {
    providerName: '', providerModel: '', providerEndpoint: '', providerTimeout: 12,
    purpose_chat_provider: '', purpose_chat_model: '', purpose_chat_endpoint: '', purpose_chat_timeout: 12,
    purpose_reasoning_provider: '', purpose_reasoning_model: '', purpose_reasoning_endpoint: '', purpose_reasoning_timeout: 12,
    ...legacy,
  };
  const secrets = {};
  for (const [lane, config] of Object.entries(lanes)) {
    values[`purpose_${lane}_provider`] = config.provider || '';
    values[`purpose_${lane}_model`] = config.model || '';
    values[`purpose_${lane}_endpoint`] = config.endpoint || '';
    if (config.timeout) values[`purpose_${lane}_timeout`] = config.timeout;
    if (config.key) secrets[`${lane}ApiKey`] = config.key;
  }
  return fakeSettings({ values, secrets });
}

async function statusOf(socketPath) {
  const { frame } = await roundTrip(socketPath, `${JSON.stringify({ token: TOKEN, op: 'status' })}\n`);
  return frame;
}

test('the planner uses the reasoning lane when it is configured', async (t) => {
  const settings = laneSettings({
    lanes: {
      chat: { provider: 'anthropic', model: 'claude-sonnet-5-5', key: 'chat-key' },
      reasoning: { provider: 'openai', model: 'gpt-4o', key: 'reasoning-key' },
    },
  });
  const providers = fakeProviders();
  const { socketPath } = await withBridge(t, { settings, providers });

  await roundTrip(socketPath, frameFor({}));
  assert.equal(providers.calls[0].config.provider, 'openai');
  assert.equal(providers.calls[0].config.model, 'gpt-4o');
  assert.equal(providers.calls[0].config.apiKey, 'reasoning-key');
});

test('changing chat does not move the planner while reasoning is configured', async (t) => {
  const settings = laneSettings({
    lanes: {
      chat: { provider: 'anthropic', model: 'claude-sonnet-5-5', key: 'chat-key' },
      reasoning: { provider: 'openai', model: 'gpt-4o', key: 'reasoning-key' },
    },
  });
  const providers = fakeProviders();
  const { socketPath } = await withBridge(t, { settings, providers });

  await roundTrip(socketPath, frameFor({}));
  // What the Chat card writes.
  settings.state.values.purpose_chat_provider = 'openai';
  settings.state.values.purpose_chat_model = 'gpt-4o';
  settings.state.secrets.chatApiKey = 'new-chat-key';

  await roundTrip(socketPath, frameFor({}));
  assert.equal(providers.calls[1].config.provider, 'openai');
  assert.equal(providers.calls[1].config.model, 'gpt-4o');
  assert.equal(providers.calls[1].config.apiKey, 'reasoning-key');
});

test('with only a chat lane the planner uses it, key and all', async (t) => {
  const settings = laneSettings({
    lanes: { chat: { provider: 'openai', model: 'gpt-4o', key: 'chat-key' } },
  });
  const providers = fakeProviders();
  const { socketPath } = await withBridge(t, { settings, providers });

  await roundTrip(socketPath, frameFor({}));
  assert.equal(providers.calls[0].config.provider, 'openai');
  assert.equal(providers.calls[0].config.apiKey, 'chat-key');
});

test('a reasoning lane with no key falls through to a configured chat lane', async (t) => {
  const settings = laneSettings({
    lanes: {
      chat: { provider: 'anthropic', model: 'claude-sonnet-5-5', key: 'chat-key' },
      reasoning: { provider: 'openai', model: 'gpt-4o' },
    },
  });
  const providers = fakeProviders();
  const { socketPath } = await withBridge(t, { settings, providers });

  await roundTrip(socketPath, frameFor({}));
  assert.equal(providers.calls[0].config.provider, 'anthropic');
  assert.equal(providers.calls[0].config.apiKey, 'chat-key');
});

test('neither lane configured still uses the pre-C42 provider', async (t) => {
  const settings = laneSettings({
    legacy: { providerName: 'anthropic', providerModel: 'claude-sonnet-5-5' },
  });
  settings.state.secrets.providerApiKey = 'legacy-key';
  const providers = fakeProviders();
  const { socketPath } = await withBridge(t, { settings, providers });

  await roundTrip(socketPath, frameFor({}));
  assert.equal(providers.calls[0].config.provider, 'anthropic');
  assert.equal(providers.calls[0].config.apiKey, 'legacy-key');
});

test('a key entered for one lane is never spent by another', async (t) => {
  // Reasoning has a key, chat does not. Falling through to chat must not lend it
  // reasoning's credential: that would be one lane's key paid out under another's
  // provider, and the user would have no way to see it happened.
  const settings = laneSettings({
    lanes: {
      chat: { provider: 'anthropic', model: 'claude-sonnet-5-5' },
      reasoning: { provider: 'openai', model: 'gpt-4o', key: 'reasoning-key' },
    },
  });
  const providers = fakeProviders();
  const { socketPath } = await withBridge(t, { settings, providers });

  await roundTrip(socketPath, frameFor({}));
  assert.equal(providers.calls.length, 1);
  assert.equal(providers.calls[0].config.provider, 'openai');
  assert.equal(providers.calls[0].config.apiKey, 'reasoning-key');
});

test('the default switch moves the planner to the chat lane', async (t) => {
  const settings = laneSettings({
    lanes: {
      chat: { provider: 'anthropic', model: 'claude-sonnet-5-5', key: 'chat-key' },
      reasoning: { provider: 'openai', model: 'gpt-4o', key: 'reasoning-key' },
    },
  });
  const providers = fakeProviders();
  const { socketPath } = await withBridge(t, { settings, providers });

  await roundTrip(socketPath, frameFor({}));
  assert.equal(providers.calls[0].config.provider, 'openai');

  settings.state.values.providerDefault = true;
  await roundTrip(socketPath, frameFor({}));
  assert.equal(providers.calls[1].config.provider, 'anthropic');
  assert.equal(providers.calls[1].config.apiKey, 'chat-key');
});

test('status reports the lane the planner would actually use', async (t) => {
  const settings = laneSettings({
    lanes: {
      chat: { provider: 'anthropic', model: 'claude-sonnet-5-5', key: 'chat-key' },
      reasoning: { provider: 'openai', model: 'gpt-4o', key: 'reasoning-key' },
    },
  });
  const providers = fakeProviders();
  const { socketPath } = await withBridge(t, { settings, providers });

  const frame = await statusOf(socketPath);
  assert.equal(frame.purpose, 'reasoning');
  assert.equal(frame.provider, 'openai');
  assert.equal(frame.model, 'gpt-4o');
  assert.equal(frame.hasKey, true);
  assert.equal(frame.configured, true);
});

test('status asks main about the same lane it reports', async (t) => {
  const settings = laneSettings({
    lanes: { reasoning: { provider: 'openai', model: 'gpt-4o', key: 'reasoning-key' } },
  });
  const providers = fakeProviders();
  const asked = [];
  const { socketPath } = await withBridge(t, {
    settings,
    providers,
    getStatus: (lane) => { asked.push(lane); return { state: 'invalid_credentials' }; },
  });

  const frame = await statusOf(socketPath);
  assert.deepEqual(asked, ['reasoning']);
  assert.equal(frame.state, 'invalid_credentials');
});

test('status with no lane configured reports the legacy chat lane', async (t) => {
  const settings = laneSettings({
    legacy: { providerName: 'anthropic', providerModel: 'claude-sonnet-5-5' },
  });
  settings.state.secrets.providerApiKey = API_KEY;
  const providers = fakeProviders();
  const { socketPath } = await withBridge(t, { settings, providers });

  const frame = await statusOf(socketPath);
  assert.equal(frame.purpose, 'legacy');
  assert.equal(frame.provider, 'anthropic');
  assert.ok(!JSON.stringify(frame).includes(API_KEY));
});

test('a per-lane endpoint and timeout reach the provider config', async (t) => {
  const settings = laneSettings({
    lanes: {
      reasoning: {
        provider: 'openai_compatible', model: 'local-model',
        endpoint: 'https://example.invalid/v1', key: 'local-key', timeout: 33,
      },
    },
  });
  const providers = fakeProviders();
  const { socketPath } = await withBridge(t, { settings, providers });

  await roundTrip(socketPath, frameFor({}));
  assert.equal(providers.calls[0].config.baseUrl, 'https://example.invalid/v1');
  assert.equal(providers.calls[0].config.timeoutSeconds, 33);
});

test('an unconfigured lane produces a stated refusal, not a provider call', async (t) => {
  const settings = laneSettings();
  const providers = fakeProviders();
  const { socketPath } = await withBridge(t, { settings, providers });

  const { frame } = await roundTrip(socketPath, frameFor({}));
  assert.equal(frame.ok, false);
  assert.equal(frame.state, 'not_configured');
  assert.equal(typeof frame.detail, 'string');
  assert.equal(providers.calls.length, 0);
});
