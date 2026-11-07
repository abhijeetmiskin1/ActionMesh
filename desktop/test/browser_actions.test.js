'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createBrowserActionRunner } = require('../app/browser_actions');

test('manual browser controls create ordinary graphs with no caller-defined authority', async () => {
  const requests = [];
  const run = createBrowserActionRunner({ getDeviceId: async () => 'my-laptop',
    request: async (request) => { requests.push(request); return {
      ok: true, body: { execution_id: 'run-1', state: 'success', nodes: { browser: { verified: true } } },
    }; } });
  const result = await run({ op: 'type', ref: 17, text: 'a command', grants: ['*'], approved: true });
  const body = requests[0].body;
  assert.equal(requests[0].path, '/execute');
  assert.equal(body.graph.target_device_id, 'my-laptop');
  assert.deepEqual(body.graph.nodes[0].action, {
    type: 'browser.type', provider: 'laptop', parameters: { ref: '17', text: 'a command' },
  });
  assert.equal('grants' in body, false);
  assert.equal('approved' in body.graph.nodes[0].action, false);
  assert.deepEqual(body.policy.rules, [{ action: 'browser.type', decision: 'allow' }]);
  assert.equal(result.verified, true);
});

test('backend refusal and unverified outcomes never become browser success', async () => {
  for (const state of ['denied', 'waiting_permission', 'requires_user', 'failed']) {
    const run = createBrowserActionRunner({ getDeviceId: () => 'laptop', request: async () => ({
      ok: true, body: { execution_id: 'run', state, nodes: { browser: { verified: false, error: 'refused' } } },
    }) });
    const result = await run({ op: 'open', url: 'https://example.com' });
    assert.equal(result.status, state);
    assert.equal(result.verified, false);
  }
});

test('search remains a search capability and does not silently become navigation', async () => {
  let type;
  const run = createBrowserActionRunner({ getDeviceId: () => 'laptop', request: async ({ body }) => {
    type = body.graph.nodes[0].action.type;
    return { ok: true, body: { state: 'denied', nodes: {} } };
  } });
  await run({ op: 'search', query: 'pizza', engine: 'duckduckgo' });
  assert.equal(type, 'browser.search');
});

test('missing device and unsupported raw operations never dispatch', async () => {
  let calls = 0;
  const run = createBrowserActionRunner({ getDeviceId: () => '', request: async () => { calls++; } });
  assert.equal((await run({ op: 'open', url: 'https://example.com' })).status, 'unavailable');
  assert.equal((await run({ op: 'page_text' })).status, 'unsupported');
  assert.equal(calls, 0);
});

test('renderer browser IPC cannot drive the window without the execution pipeline', async () => {
  const handlers = new Map();
  const source = fs.readFileSync(path.join(__dirname, '../app/browser_channel.js'), 'utf8');
  const module = { exports: {} };
  vm.runInNewContext(source, { module, exports: module.exports, require: () => ({
    ipcMain: { handle: (name, handler) => handlers.set(name, handler) },
  }) });
  let rawCalls = 0;
  const driver = { handleDrive: () => { rawCalls++; }, setObservationSink() {} };
  module.exports.registerBrowserIpc(driver, () => null);
  await assert.rejects(handlers.get('browser:open')({}, { url: 'https://example.com' }), /pipeline/);
  assert.equal(rawCalls, 0);
  const frames = [];
  module.exports.registerBrowserIpc(driver, () => null, async (frame) => { frames.push(frame); return { status: 'denied' }; });
  const reply = await handlers.get('browser:search')({}, { query: 'pizza' });
  assert.equal(reply.status, 'denied');
  assert.equal(frames[0].op, 'search');
  assert.equal(rawCalls, 0);
});


test('a success envelope without independent verification is explicitly unverified', async () => {
  const run = createBrowserActionRunner({ getDeviceId: () => 'laptop', request: async () => ({
    ok: true, body: { state: 'success', nodes: { browser: { verified: false } } },
  }) });
  assert.equal((await run({ op: 'type', ref: 1, text: 'hello' })).status, 'succeeded_unverified');
});
