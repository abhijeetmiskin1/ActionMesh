'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const { boot } = require('./main_harness');

let loaded;
test.after(async () => {
  if (!loaded) return;
  await loaded.invoke('stream:stop');
  await loaded.invoke('backend:stop');
});

test('desktop API proxy admits versioned project routes and keeps path boundaries', async () => {
  loaded = await boot();
  const { invoke } = loaded;
  const originalFetch = global.fetch;
  let requested = '';
  global.fetch = async (url) => {
    requested = String(url);
    return { ok: true, status: 200, text: async () => '{"projects":[]}' };
  };
  try {
    const result = await invoke('api:request', {
      method: 'GET', path: '/api/v1/projects',
    });
    assert.equal(result.ok, true);
    assert.match(requested, /\/api\/v1\/projects$/);
    const refused = await invoke('api:request', {
      method: 'GET', path: '/api/v1evil/projects',
    });
    assert.equal(refused.ok, false);
    assert.match(refused.error, /not reachable/);
  } finally {
    global.fetch = originalFetch;
  }
});
