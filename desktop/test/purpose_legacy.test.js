'use strict';
/**
 * The pre-C42 single-provider setup, read by the C42 per-purpose surface.
 *
 * This is a separate process because it is a separate *boot*: `main.js` loads settings
 * once, so the only honest way to test "what does the app show somebody who configured
 * a provider before C42 existed" is to write that settings file first and start the
 * real module against it. Nothing migrates the old keys, and nothing deletes them —
 * that is the claim being checked.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const LEGACY_KEY = 'sk-legacy-chat-0123456789abcdef';

const USER_DATA = fs.mkdtempSync(path.join(os.tmpdir(), 'actionmesh-legacy-'));
process.env.ACTIONMESH_TEST_USERDATA = USER_DATA;

// The pre-C42 profile: providerName/providerModel plus a sealed providerApiKey, and no
// purpose_* keys at all. `sealed:` is what the electron stub's safeStorage unwraps.
fs.writeFileSync(path.join(USER_DATA, 'settings.json'), JSON.stringify({
  providerName: 'anthropic',
  providerModel: 'claude-sonnet-5-5',
  providerEndpoint: '',
  providerTimeout: 18,
  secrets: { providerApiKey: Buffer.from(`sealed:${LEGACY_KEY}`, 'utf8').toString('base64') },
}, null, 2), { mode: 0o600 });

const { boot } = require('./main_harness.js');

// Same reason as purpose_ipc.test.js: boot starts the event stream, and its retry loop
// would otherwise keep this process alive after the last assertion.
let appInstance = null;

/** One boot: main.js loads settings once and registers its handlers once. */
function app() {
  if (!appInstance) appInstance = boot();
  return appInstance;
}

test.after(async () => {
  if (!appInstance) return;
  const loaded = await appInstance;
  await loaded.invoke('stream:stop');
  await loaded.invoke('backend:stop');
});

test('the legacy provider answers as the chat lane, and only the chat lane', async () => {
  const { invoke } = await app();

  const chat = (await invoke('purpose:status', 'chat')).status;
  assert.equal(chat.legacy, true, 'chat says this is the preserved setup');
  assert.equal(chat.provider, 'anthropic');
  assert.equal(chat.model, 'claude-sonnet-5-5');
  assert.equal(chat.timeout, 18);
  assert.equal(chat.hasKey, true);
  // Configured and keyed, still not "connected": no test has run in this process.
  assert.equal(chat.state, 'not_configured');
  assert.match(chat.detail, /not tested/i);

  for (const purpose of ['reasoning', 'image', 'audio']) {
    const status = (await invoke('purpose:status', purpose)).status;
    assert.equal(status.provider, '', `${purpose} must not borrow the legacy provider`);
    assert.equal(status.legacy, false);
    assert.equal(status.hasKey, false);
    assert.equal(status.state, 'not_configured');
  }
});

test('the legacy key is not reported to the renderer, only that it exists', async () => {
  const { invoke } = await app();
  const state = await invoke('app:state');
  assert.equal(state.settings.secrets.providerApiKey.present, true);
  assert.equal(JSON.stringify(state).includes(LEGACY_KEY), false);
});

test('naming a chat provider explicitly takes over from the legacy slot, without deleting it', async () => {
  const { invoke } = await app();
  const NEW_KEY = 'sk-new-chat-0123456789abcdef';

  const saved = await invoke('purpose:configure', {
    purpose: 'chat', provider: 'openai', model: 'gpt-4o', apiKey: NEW_KEY,
  });
  assert.equal(saved.ok, true);
  assert.equal(saved.status.legacy, false);
  assert.equal(saved.status.provider, 'openai');
  assert.equal(saved.status.model, 'gpt-4o');

  // The old values are still on disk: rolling the app back is pointing it at them.
  const raw = JSON.parse(fs.readFileSync(path.join(USER_DATA, 'settings.json'), 'utf8'));
  assert.equal(raw.providerName, 'anthropic');
  assert.equal(raw.providerModel, 'claude-sonnet-5-5');
  assert.ok(raw.secrets.providerApiKey, 'the legacy credential was not deleted');
  assert.notEqual(raw.secrets.providerApiKey, raw.secrets.chatApiKey);
});

test('removing the chat credentials clears both the new slot and the legacy one', async () => {
  const { invoke } = await app();
  const removed = await invoke('purpose:remove', { purpose: 'chat' });
  assert.equal(removed.ok, true);
  assert.equal(removed.status.hasKey, false);

  const raw = JSON.parse(fs.readFileSync(path.join(USER_DATA, 'settings.json'), 'utf8'));
  assert.equal(raw.secrets.providerApiKey, undefined, 'no second copy is left behind');
});