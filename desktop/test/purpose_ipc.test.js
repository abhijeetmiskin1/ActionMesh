'use strict';
/**
 * The per-purpose IPC surface, tested by loading the real `app/main.js`.
 *
 * There is nothing to unit-test about an IPC handler except the handler itself, so this
 * loads the actual main module under a fake `electron` (see electron_stub.js). That is
 * also the cheapest check on the thing most likely to break after a refactor: that the
 * module boots, registers exactly the channels preload invokes, and answers each with
 * the shape the renderer reads.
 *
 * The security claims are asserted here too, because this is the last place the main
 * process is visible without a display server: no answer carries key material, an
 * unknown purpose is refused before it touches settings, and one purpose's credential
 * is never spent by another.
 */
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const { boot, APP_DIR } = require('./main_harness.js');

const USER_DATA = fs.mkdtempSync(path.join(os.tmpdir(), 'actionmesh-purpose-'));
process.env.ACTIONMESH_TEST_USERDATA = USER_DATA;

const KEY = 'sk-purpose-test-0123456789abcdef';
const OTHER_KEY = 'sk-reasoning-test-0123456789abcdef';

let loaded = null;

/** One boot for the whole file: main.js is a singleton and re-loading it re-binds IPC. */
async function app() {
  if (!loaded) loaded = await boot();
  return loaded;
}

function settingsFile() {
  return path.join(USER_DATA, 'settings.json');
}

/**
 * Boot leaves the event stream running: the real main process starts it after the
 * backend reports ready, and it retries against a backend that is not there. That is
 * correct in the app and would keep this process alive forever, so the last thing this
 * file does is close it the same way the UI's Stop button would.
 */
test.after(async () => {
  if (!loaded) return;
  await loaded.invoke('stream:stop');
  await loaded.invoke('backend:stop');
});

test('observation analysis sends only bounded labels to the reasoning provider and never runs an action', async () => {
  const { invoke } = await app();
  const providers = require('../app/providers');
  const original = providers.generate;
  let seen;
  providers.generate = async (config, request) => {
    seen = { config, request };
    return { state: 'ok', content: { finding: 'A settings dialog with Save and Cancel.' } };
  };
  try {
    await invoke('purpose:configure', { purpose: 'reasoning', provider: 'openai_compatible',
      model: 'power', baseUrl: 'http://127.0.0.1:20128/api/v1', apiKey: KEY });
    const answer = await invoke('screen:analyze', { question: 'What is this?',
      elements: [{ role: 'button', name: 'Save', value: 'never send field values', bounds: { x: 0 } }] });
    assert.equal(answer.ok, true);
    assert.equal(answer.source, 'accessibility observation');
    assert.equal(seen.request.purpose, 'diagnose');
    assert.equal(seen.request.user.includes('never send field values'), false);
    assert.equal(seen.request.user.includes('bounds'), false);
    assert.equal(JSON.stringify(answer).includes(KEY), false);
    assert.equal((await invoke('screen:analyze', { question: 'Question', elements: [] })).ok, false);
  } finally {
    providers.generate = original;
    await invoke('purpose:remove', { purpose: 'reasoning' });
    // Restore the empty initial selection expected by the remaining scenarios.
    await invoke('settings:update', { purpose_reasoning_provider: '', purpose_reasoning_model: '' });
  }
});

test('the main process boots and registers every channel preload invokes', async () => {
  const { stub } = await app();
  if (process.platform === 'linux') assert.ok(stub.__store.commandLineSwitches.has('enable-speech-dispatcher'));
  // Read preload.js as text: the point is that the two lists agree, and a test that
  // imported the bridge would be asserting against itself.
  const preload = fs.readFileSync(path.join(APP_DIR, 'preload.js'), 'utf8');
  const invoked = [...preload.matchAll(/ipcRenderer\.invoke\('([^']+)'/g)].map((m) => m[1]);
  assert.ok(invoked.length >= 20, `preload exposes ${invoked.length} channels`);

  const missing = invoked.filter((channel) => !stub.__store.handlers.has(channel));
  assert.deepEqual(missing, [], 'every preload call has a main-process handler');

  // Boot also created the main window without throwing. The page load itself is the
  // Electron run's job; here the assertion is that the module got that far.
  assert.equal(stub.__store.windows.length >= 1, true);
});

test('every purpose reports a status, and an unknown purpose is refused', async () => {
  const { invoke } = await app();
  for (const purpose of ['chat', 'reasoning', 'image', 'audio']) {
    const answer = await invoke('purpose:status', purpose);
    assert.equal(answer.ok, true);
    assert.equal(answer.status.purpose, purpose);
    assert.equal(answer.status.state, 'not_configured');
    assert.equal(answer.status.hasKey, false);
    assert.equal(answer.status.encryptionAvailable, true);
    // No key material: the object has no field that could hold one.
    assert.deepEqual(
      Object.keys(answer.status).sort(),
      ['detail', 'encryptionAvailable', 'endpoint', 'hasKey', 'legacy', 'model', 'provider',
        'providerLabel', 'purpose', 'state', 'testedAt', 'text', 'timeout'],
    );
  }
  for (const bad of ['admin', 'CHAT', '', null, 7, 'chat ']) {
    const answer = await invoke('purpose:status', bad);
    assert.equal(answer.ok, false, `${JSON.stringify(bad)} must be refused`);
    assert.equal(answer.error, 'unknown purpose');
  }
});

test('an unknown purpose never reaches settings or a secret slot', async () => {
  const { invoke } = await app();
  // Touch the file first: a refusal must not be the reason it appears.
  await invoke('settings:update', { deviceName: 'This laptop' });
  const before = fs.readFileSync(settingsFile(), 'utf8');

  for (const bad of ['admin', '__proto__', 'chat;rm', '*']) {
    assert.equal((await invoke('purpose:configure', { purpose: bad, provider: 'anthropic', apiKey: KEY })).ok, false);
    assert.equal((await invoke('purpose:remove', { purpose: bad })).ok, false);
    assert.equal((await invoke('purpose:test', { purpose: bad, provider: 'anthropic' })).ok, false);
    assert.equal((await invoke('purpose:model', { purpose: bad, model: 'x' })).ok, false);
    assert.equal((await invoke('purpose:default', { purpose: bad })).ok, false);
  }

  assert.equal(fs.readFileSync(settingsFile(), 'utf8'), before, 'a refusal writes nothing');
  assert.equal(JSON.stringify(before).includes('__proto__'), false);
});

test('two purposes keep independent provider and model choices', async () => {
  const { invoke } = await app();

  const chat = await invoke('purpose:configure', {
    purpose: 'chat', provider: 'anthropic', model: 'claude-sonnet-5-5', apiKey: KEY,
  });
  assert.equal(chat.ok, true);
  const reasoning = await invoke('purpose:configure', {
    purpose: 'reasoning', provider: 'openai', model: 'gpt-4o', apiKey: OTHER_KEY,
  });
  assert.equal(reasoning.ok, true);

  const chatStatus = (await invoke('purpose:status', 'chat')).status;
  const reasonStatus = (await invoke('purpose:status', 'reasoning')).status;
  assert.equal(chatStatus.provider, 'anthropic');
  assert.equal(chatStatus.model, 'claude-sonnet-5-5');
  assert.equal(reasonStatus.provider, 'openai');
  assert.equal(reasonStatus.model, 'gpt-4o');
  // Both are configured and keyed, and neither is claimed as connected until tested.
  assert.equal(chatStatus.state, 'not_configured');
  assert.equal(reasonStatus.state, 'not_configured');
  assert.equal(chatStatus.hasKey, true);
  assert.equal(reasonStatus.hasKey, true);

  // Changing chat's model leaves reasoning's alone, and vice versa.
  await invoke('purpose:model', { purpose: 'chat', model: 'claude-haiku-4-5-20251001' });
  assert.equal((await invoke('purpose:status', 'chat')).status.model, 'claude-haiku-4-5-20251001');
  assert.equal((await invoke('purpose:status', 'reasoning')).status.model, 'gpt-4o');
  await invoke('purpose:model', { purpose: 'reasoning', model: 'gpt-4o-mini' });
  assert.equal((await invoke('purpose:status', 'chat')).status.model, 'claude-haiku-4-5-20251001');
});

test('removing one purpose key leaves the other purpose keyed', async () => {
  const { invoke } = await app();

  const removed = await invoke('purpose:remove', { purpose: 'chat' });
  assert.equal(removed.ok, true);
  assert.equal(removed.status.hasKey, false);
  assert.equal(removed.status.state, 'missing_key');

  const reasoning = (await invoke('purpose:status', 'reasoning')).status;
  assert.equal(reasoning.hasKey, true, 'the reasoning key is still there');
  assert.equal(reasoning.provider, 'openai');
});

test('a replacement key is stored, and is not the old one', async () => {
  const { invoke } = await app();
  const replacement = 'sk-chat-replacement-0123456789';

  const answer = await invoke('purpose:configure', {
    purpose: 'chat', provider: 'anthropic', model: 'claude-sonnet-5-5', apiKey: replacement,
  });
  assert.equal(answer.ok, true);
  assert.equal(answer.status.hasKey, true);

  // The disk holds ciphertext, not the key.
  const raw = fs.readFileSync(settingsFile(), 'utf8');
  assert.equal(raw.includes(replacement), false);
  const stored = JSON.parse(raw).secrets.chatApiKey;
  assert.ok(stored, 'the chat key was persisted');

  // And a second save without a key leaves the stored one alone.
  await invoke('purpose:configure', { purpose: 'chat', provider: 'anthropic', model: 'claude-haiku-4-5-20251001' });
  assert.equal((await invoke('purpose:status', 'chat')).status.hasKey, true);
});

test('no IPC answer anywhere carries a credential', async () => {
  const { invoke } = await app();
  const answers = await Promise.all([
    invoke('app:state'),
    invoke('purpose:status', 'chat'),
    invoke('purpose:status', 'reasoning'),
    invoke('purpose:status', 'audio'),
    invoke('purpose:specs', 'chat'),
    invoke('purpose:specs', 'reasoning'),
  ]);
  const serialized = JSON.stringify(answers);
  for (const secret of [KEY, OTHER_KEY, 'sk-chat-replacement-0123456789']) {
    assert.equal(serialized.includes(secret), false);
  }
  // `secrets` may say present:true; it may not carry a value.
  const chatSecret = answers[0].settings.secrets.chatApiKey;
  assert.deepEqual(Object.keys(chatSecret).sort(), ['present', 'status']);
});

test('a provider this build does not implement cannot be saved for image or audio', async () => {
  const { invoke } = await app();
  for (const purpose of ['image', 'audio']) {
    const specs = await invoke('purpose:specs', purpose);
    assert.equal(specs.ok, true);
    assert.ok(specs.specs.length > 0, `${purpose} has a table`);
    const unavailable = specs.specs.filter((spec) => !spec.implemented);
    assert.ok(unavailable.length);
    assert.ok(unavailable.every((spec) => typeof spec.unavailableReason === 'string' && spec.unavailableReason.length > 10));

    // Even a real-looking id is refused, and nothing is written.
    const refused = await invoke('purpose:configure', {
      purpose, provider: unavailable[0].id, model: 'x', apiKey: KEY,
    });
    assert.equal(refused.ok, false);
    assert.match(refused.error, /not wired|not implemented/i);
    const status = (await invoke('purpose:status', purpose)).status;
    assert.equal(status.provider, '');
    assert.equal(status.state, 'not_configured');
  }
});

test('an unimplemented provider cannot be named for a text lane either', async () => {
  const { invoke } = await app();
  const answer = await invoke('purpose:configure', {
    purpose: 'chat', provider: 'elevenlabs', model: 'x', apiKey: KEY,
  });
  assert.equal(answer.ok, false);
  assert.equal(answer.error, 'unknown provider');
});

test('text lanes can independently select the planner default; media lanes cannot', async () => {
  const { invoke } = await app();

  const reasoning = await invoke('purpose:default', { purpose: 'reasoning' });
  assert.equal(reasoning.ok, true);
  assert.equal((await invoke('app:state')).settings.providerDefault, false);
  for (const purpose of ['image', 'audio']) {
    assert.equal((await invoke('purpose:default', {purpose})).ok, false);
  }
  await invoke('purpose:configure', {purpose:'reasoning',provider:'openai',model:'gpt-4o',apiKey:OTHER_KEY});
  assert.equal((await invoke('purpose:default', {purpose:'reasoning'})).ok, true);
  assert.equal((await invoke('app:state')).settings.providerDefault, false);

  // chat is configured by now, so the default is accepted and visible in app state.
  const chat = await invoke('purpose:default', { purpose: 'chat' });
  assert.equal(chat.ok, true);
  const state = await invoke('app:state');
  assert.equal(state.settings.providerDefault, true);
});

test('a purpose saved with a custom endpoint does not lose it on the next save', async () => {
  const { invoke } = await app();
  const saved = await invoke('purpose:configure', {
    purpose: 'reasoning', provider: 'openai_compatible', model: 'local',
    baseUrl: 'https://llm.example/v1', apiKey: OTHER_KEY, timeoutSeconds: 40,
  });
  assert.equal(saved.ok, true);
  assert.equal(saved.status.endpoint, 'https://llm.example/v1');
  assert.equal(saved.status.timeout, 40);

  const again = await invoke('purpose:configure', {
    purpose: 'reasoning', provider: 'openai_compatible', model: 'local-2',
  });
  assert.equal(again.status.endpoint, 'https://llm.example/v1', 'endpoint survives a keyless save');
  assert.equal(again.status.timeout, 40);

  // An explicit empty string is a deliberate clear, and does take effect.
  const cleared = await invoke('purpose:configure', {
    purpose: 'reasoning', provider: 'openai_compatible', model: 'local-2', baseUrl: '',
  });
  assert.equal(cleared.status.endpoint, '');
});

test('a keyless configure reports missing_key, not connected', async () => {
  const { invoke } = await app();
  const stored = await invoke('purpose:configure', {
    purpose: 'audio', provider: 'elevenlabs', model: 'x',
  });
  // audio has no implemented provider, so even that is refused — the point is that
  // neither path can produce a green state.
  assert.equal(stored.ok, false);

  await invoke('purpose:remove', { purpose: 'chat' });
  const status = (await invoke('purpose:status', 'chat')).status;
  assert.equal(status.state, 'missing_key');
  assert.notEqual(status.state, 'connected');
});

test('the settings file and the crash log never receive plaintext key material', async () => {
  await app();
  const raw = fs.readFileSync(settingsFile(), 'utf8');
  for (const secret of [KEY, OTHER_KEY, 'sk-chat-replacement-0123456789']) {
    assert.equal(raw.includes(secret), false);
  }
  assert.equal(fs.existsSync(path.join(USER_DATA, 'crash.log')), false);
});

test('preload exposes no credential getter at all', () => {
  const text = fs.readFileSync(path.join(APP_DIR, 'preload.js'), 'utf8');
  const names = [...text.matchAll(/^ {2}([a-zA-Z]+):/gm)].map((m) => m[1]);
  assert.ok(names.includes('purposeConfigure'));
  for (const name of names) {
    assert.doesNotMatch(name, /key|secret|credential|password/i, `preload method ${name}`);
  }
  const channels = [...text.matchAll(/ipcRenderer\.invoke\('([^']+)'/g)].map((m) => m[1]);
  for (const channel of channels) {
    assert.doesNotMatch(channel, /key|secret/i, `IPC channel ${channel}`);
  }
});

test('every modified JS file is syntactically valid', () => {
  const files = ['main.js', 'preload.js', 'settings.js', 'providers.js', 'provider_bridge.js', 'backend.js'];
  for (const file of files) {
    execFileSync(process.execPath, ['--check', path.join(APP_DIR, file)], { stdio: 'pipe' });
  }
  execFileSync(process.execPath, ['--check', path.join(APP_DIR, '..', 'ui', 'app.js')], { stdio: 'pipe' });
  execFileSync(process.execPath, ['--check', path.join(APP_DIR, 'browser_window.js')], { stdio: 'pipe' });
  execFileSync(process.execPath, ['--check', path.join(APP_DIR, 'browser_channel.js')], { stdio: 'pipe' });
});

test('provider lanes retain independent verified states and edits invalidate only their own lane', async () => {
  const { invoke } = await app();
  const providers = require('../app/providers');
  const original = providers.test;
  providers.test = async () => ({ state: 'connected', detail: 'fixture probe answered' });
  try {
    for (const purpose of ['chat', 'reasoning']) {
      await invoke('purpose:configure', { purpose, provider: 'openai_compatible', model: 'fixture',
        baseUrl: 'http://127.0.0.1:20128/api/v1', apiKey: KEY });
      await invoke('purpose:test', { purpose });
    }
    assert.equal((await invoke('purpose:status', 'chat')).status.state, 'connected');
    assert.equal((await invoke('purpose:status', 'reasoning')).status.state, 'connected');
    await invoke('purpose:configure', { purpose: 'reasoning', provider: 'openai_compatible',
      model: 'changed', apiKey: OTHER_KEY });
    assert.equal((await invoke('purpose:status', 'chat')).status.state, 'connected');
    assert.equal((await invoke('purpose:status', 'reasoning')).status.state, 'not_configured');
    await invoke('purpose:test', { purpose: 'reasoning', apiKey: KEY });
    assert.equal((await invoke('purpose:status', 'reasoning')).status.state, 'not_configured',
      'testing an unsaved credential must not certify the stored credential');
  } finally { providers.test = original; }
});

test('each provider purpose keeps its own API endpoint, model and credential', async () => {
  const { invoke } = await app();
  const configurations = [
    ['chat', 'openai_compatible', 'chat-model', 'https://chat.example/v1'],
    ['reasoning', 'openai_compatible', 'reasoning-model', 'http://localhost:20128/api/v1'],
    ['audio', 'router_audio', 'speech-model', 'https://audio.example/v1'],
    ['image', 'router_image', 'image-model', 'https://image.example/v1'],
  ];
  for (const [purpose, provider, model, baseUrl] of configurations) {
    const saved = await invoke('purpose:configure', {purpose, provider, model, baseUrl,
      apiKey: KEY + '-' + purpose, timeoutSeconds: 20});
    assert.equal(saved.ok, true);
  }
  for (const [purpose, provider, model, endpoint] of configurations) {
    const result = await invoke('purpose:status', purpose);
    assert.equal(result.status.provider, provider);
    assert.equal(result.status.model, model);
    assert.equal(result.status.endpoint, endpoint);
    assert.equal(result.status.hasKey, true);
    assert.equal(JSON.stringify(result).includes(KEY), false);
  }
});

test('Todoist verifies before saving, exposes no token, and disconnects', async () => {
  const { invoke } = await app();
  const providers = require('../app/providers');
  const original = providers.todoistTasks;
  let accepted = false;
  providers.todoistTasks = async (token) => token === KEY && accepted
    ? { ok: true, tasks: [{ title: 'Read an article', due: '' }], hasMore: false }
    : { ok: false, detail: 'No usable token.' };
  try {
    assert.equal((await invoke('todoist:configure', KEY)).ok, false);
    assert.equal((await invoke('todoist:list')).ok, false);
    await invoke('settings:update', { todoistApiToken: KEY });
    accepted = true;
    assert.equal((await invoke('todoist:list')).ok, false);
    const saved = await invoke('todoist:configure', KEY);
    assert.deepEqual(saved, { ok: true, detail: 'Todoist verified and saved.' });
    assert.equal(JSON.stringify(await invoke('app:state')).includes(KEY), false);
    assert.equal((await invoke('todoist:list')).tasks[0].title, 'Read an article');
    assert.equal((await invoke('todoist:disconnect')).ok, true);
    assert.equal((await invoke('todoist:list')).ok, false);
  } finally {
    await invoke('todoist:disconnect');
    providers.todoistTasks = original;
  }
});
