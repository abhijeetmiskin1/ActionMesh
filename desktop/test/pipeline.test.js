'use strict';
/**
 * C41: the bridge's end of the model round trip.
 *
 * VoiceOS (Python) asks Electron main for a plan over a private Unix socket. Two
 * properties have to hold at this seam, and both are about what comes *back*:
 *
 *   1. a successful generation hands the caller the model's JSON object and nothing
 *      else — no risk level, no permission, no approval, no device. The bridge is a
 *      transport, not an authority; if it ever "helpfully" annotated the answer the
 *      Python validator would be policing a shape the bridge had already changed.
 *   2. the provider API key goes in and never comes out — not in the frame, not in a
 *      detail sentence, not in a state name.
 *
 * The provider is a fake `generate`; no network is contacted and no real key is used.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const { startBridge } = require('../app/provider_bridge.js');

const TOKEN = 'test-token-not-a-key';
// Distinctive enough that a substring search proves a leak rather than coincidence.
const API_KEY = 'sk-test-DO-NOT-LEAK-1c9f4a';

/** SettingsStore-shaped, with the provider pointed at the fake. */
function fakeSettings({ key = API_KEY } = {}) {
  const values = {
    providerName: 'anthropic',
    providerModel: 'claude-sonnet-5-5',
    providerEndpoint: '',
    providerTimeout: 20,
  };
  return {
    get: (name) => values[name],
    secret: (name) => (name === 'providerApiKey' ? key : ''),
    hasSecret: (name) => name === 'providerApiKey' && Boolean(key),
  };
}

/** One socket for the duration of a test, cleaned up even on failure. */
async function withBridge(t, { generate, settings = fakeSettings() } = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'actionmesh-bridge-'));
  const socketPath = path.join(dir, 'provider-bridge.sock');
  const calls = [];
  const providers = {
    specFor: () => ({ provider: 'anthropic', requiresKey: true, implemented: true }),
    generate: async (config, request) => {
      calls.push({ config, request });
      return generate(config, request);
    },
  };
  const bridge = await startBridge({ socketPath, token: TOKEN, settings, providers });
  t.after(async () => {
    await bridge.close();
    fs.rmSync(dir, { recursive: true, force: true });
  });
  return { bridge, socketPath, calls };
}

/** Send one frame, collect exactly one newline-delimited reply, close. */
function roundTrip(socketPath, frame) {
  return new Promise((resolve, reject) => {
    const socket = net.connect({ path: socketPath });
    let buffer = '';
    const timer = setTimeout(() => {
      socket.destroy();
      reject(new Error('the bridge did not answer'));
    }, 5000);
    socket.once('connect', () => socket.write(`${JSON.stringify(frame)}\n`));
    socket.on('data', (chunk) => {
      buffer += chunk.toString('utf8');
      const newline = buffer.indexOf('\n');
      if (newline === -1) return;
      clearTimeout(timer);
      socket.destroy();
      try {
        resolve(JSON.parse(buffer.slice(0, newline)));
      } catch (error) {
        reject(new Error(`the bridge answered something that is not JSON: ${error.message}`));
      }
    });
    socket.once('error', (error) => {
      clearTimeout(timer);
      reject(error);
    });
  });
}

function planFrame(purpose = 'plan') {
  return {
    token: TOKEN,
    op: 'generate',
    purpose,
    system: 'You propose actions. You have no authority.',
    user: 'tell mom I will be late',
    timeout_s: 20,
  };
}

test('a successful generation returns the model JSON unchanged and adds no authority', async (t) => {
  // Deliberately includes a field a model should not be able to assert. The bridge must
  // pass it through (the Python validator is what refuses it) rather than laundering it:
  // an answer rewritten in transit would make the validator's refusal untestable.
  const modelAnswer = {
    actions: [{ type: 'message.send', parameters: { to: 'mom', message: 'late' } }],
    notes: ['nothing else applies'],
    risk_level: 'low',
  };
  const { socketPath, calls } = await withBridge(t, {
    generate: async () => ({ state: 'ok', content: modelAnswer, detail: '' }),
  });

  const frame = await roundTrip(socketPath, planFrame());

  assert.equal(frame.ok, true, 'a prior `ok` state must be reported as ok');
  assert.equal(frame.state, undefined, 'an ok frame carries no failure state');
  assert.deepEqual(frame.content, modelAnswer, 'the model object must arrive unchanged');
  assert.deepEqual(Object.keys(frame).sort(), ['content', 'ok'], 'the frame adds no fields');

  // The bridge must not have invented authority fields the model did not send.
  for (const field of ['risk_level', 'permissions', 'grant', 'approved', 'actor', 'device_id']) {
    if (!(field in modelAnswer)) {
      assert.equal(field in frame.content, false, `the bridge added ${field}`);
    }
  }

  // The provider was actually asked, with the purpose and both prompts, and with the
  // key read from settings — this is the one direction the key is allowed to travel.
  assert.equal(calls.length, 1);
  assert.equal(calls[0].request.purpose, 'plan');
  assert.equal(calls[0].request.user, planFrame().user);
  assert.equal(calls[0].config.apiKey, API_KEY);
});

test('the API key never appears in the response frame', async (t) => {
  const { socketPath, bridge } = await withBridge(t, {
    generate: async () => ({ state: 'ok', content: { actions: [] }, detail: '' }),
  });

  const raw = await new Promise((resolve, reject) => {
    const socket = net.connect({ path: socketPath });
    let buffer = '';
    const timer = setTimeout(() => {
      socket.destroy();
      reject(new Error('the bridge did not answer'));
    }, 5000);
    socket.once('connect', () => socket.write(`${JSON.stringify(planFrame())}\n`));
    socket.on('data', (chunk) => {
      buffer += chunk.toString('utf8');
      if (buffer.includes('\n')) {
        clearTimeout(timer);
        socket.destroy();
        resolve(buffer);
      }
    });
    socket.once('error', (error) => {
      clearTimeout(timer);
      reject(error);
    });
  });

  assert.equal(raw.includes(API_KEY), false, 'the key must not be in the reply frame');
  assert.equal(/authorization|x-api-key/i.test(raw), false, 'no credential header echoed');
  assert.equal(JSON.stringify(bridge.stats()).includes(API_KEY), false, 'the counters are key-free');
});

test('invalid credentials yield a state-only refusal with no content', async (t) => {
  const { socketPath } = await withBridge(t, {
    generate: async () => ({
      state: 'invalid_credentials',
      detail: `401 from provider; key ${API_KEY} was rejected`,
    }),
  });

  const frame = await roundTrip(socketPath, planFrame());

  assert.deepEqual(Object.keys(frame).sort(), ['detail', 'ok', 'state']);
  assert.equal(frame.ok, false);
  assert.equal(frame.state, 'invalid_credentials');
  assert.equal('content' in frame, false, 'a failed generation must return no content');
  assert.equal(typeof frame.detail, 'string');
  // The provider's own detail is not relayed: a provider error body can quote the
  // request, and a guard that echoes its input is not a guard.
  assert.equal(JSON.stringify(frame).includes(API_KEY), false, 'the key must not reach the caller');
  assert.equal(
    JSON.stringify(frame).includes('401'),
    false,
    'provider text must not be relayed verbatim',
  );
});

test('a provider state outside the contract is reported as provider_unavailable', async (t) => {
  const { socketPath } = await withBridge(t, {
    generate: async () => ({ state: 'something_new', content: { actions: [] } }),
  });

  const frame = await roundTrip(socketPath, planFrame());

  assert.equal(frame.ok, false);
  assert.equal(frame.state, 'provider_unavailable');
  assert.equal('content' in frame, false);
});