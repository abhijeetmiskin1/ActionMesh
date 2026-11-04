'use strict';
/**
 * The four provider lanes, as the person actually meets them.
 *
 * The main process is covered elsewhere (`purpose_ipc.test.js`) and the planner's lane
 * choice by the bridge's own suite. What is left is the renderer: does it boot, does it
 * show one card per job, does the shared modal retarget itself when a different lane is
 * opened, and does a Save empty the key box without ever asking the main process for a
 * credential back.
 *
 * No jsdom is installed, so `dom_harness.js` supplies a deliberately small document —
 * enough for the renderer's own `el()`/`byId()`/`querySelectorAll` usage and a fake
 * `window.actionmesh` that answers like preload does. It renders nothing and lays
 * nothing out; every assertion below is about values the page computed.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const { install } = require('./dom_harness.js');

const PURPOSE_LABELS = { chat: 'Chat', reasoning: 'Reasoning', image: 'Image', audio: 'Audio' };

/** The static tables `providers.js` publishes, copied here so the fake is plain data. */
const TEXT_MODELS = {
  anthropic: ['claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-4-5-20251001'],
  openai: ['gpt-4o', 'gpt-4o-mini', 'gpt-4.1'],
  gemini: ['gemini-2.0-flash', 'gemini-1.5-pro', 'gemini-1.5-flash'],
  openai_compatible: [],
  ollama: [],
};

const SPECS = {
  chat: [
    { id: 'anthropic', label: 'Anthropic', implemented: true, requiresKey: true, requiresBaseUrl: false, defaultModel: 'claude-sonnet-5-5', models: TEXT_MODELS.anthropic, unavailableReason: '' },
    { id: 'openai', label: 'OpenAI', implemented: true, requiresKey: true, requiresBaseUrl: false, defaultModel: 'gpt-4o', models: TEXT_MODELS.openai, unavailableReason: '' },
    { id: 'gemini', label: 'Google Gemini', implemented: true, requiresKey: true, requiresBaseUrl: false, defaultModel: 'gemini-2.0-flash', models: TEXT_MODELS.gemini, unavailableReason: '' },
    { id: 'openai_compatible', label: 'OpenAI-compatible endpoint', implemented: true, requiresKey: true, requiresBaseUrl: true, defaultModel: '', models: [], unavailableReason: '' },
    { id: 'ollama', label: 'Ollama (local)', implemented: false, requiresKey: false, requiresBaseUrl: false, defaultModel: '', models: [], unavailableReason: 'No local runtime probe is wired in this build.' },
  ],
  reasoning: null, // same table as chat, filled below
  image: [
    { id: 'openai_images', label: 'OpenAI Images', implemented: false, requiresKey: true, requiresBaseUrl: false, defaultModel: '', models: [], unavailableReason: 'Not wired in this build. An image adapter and a real probe must exist before a key is accepted.' },
    { id: 'stability', label: 'Stability AI', implemented: false, requiresKey: true, requiresBaseUrl: false, defaultModel: '', models: [], unavailableReason: 'Not wired in this build. An image adapter and a real probe must exist before a key is accepted.' },
  ],
  audio: [
    { id: 'elevenlabs', label: 'ElevenLabs', implemented: false, requiresKey: true, requiresBaseUrl: false, defaultModel: '', models: [], unavailableReason: 'Not wired in this build. An audio adapter and a real probe must exist before a key is accepted.' },
  ],
};
SPECS.reasoning = SPECS.chat;

function statusFor(purpose, overrides) {
  return {
    purpose,
    provider: '',
    providerLabel: '',
    model: '',
    endpoint: '',
    timeout: 12,
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
 * A fake preload. It records the order and arguments of every call, and its mutable
 * `statuses` is what a configure/remove/test changes — the same relationship the real
 * main process has, so the renderer is exercised against a moving truth rather than a
 * fixed fixture.
 */
function fakeBridge() {
  const statuses = {
    chat: statusFor('chat'),
    reasoning: statusFor('reasoning'),
    image: statusFor('image'),
    audio: statusFor('audio'),
  };
  const calls = [];
  const record = (name, answer) => (...args) => {
    calls.push({ name, args });
    return Promise.resolve(typeof answer === 'function' ? answer(...args) : answer);
  };

  const appState = () => ({
    version: '1.2.3',
    appId: 'com.actionmesh.desktop',
    electron: '33.0.0',
    chrome: '130.0.0',
    platform: 'linux',
    arch: 'x64',
    packaged: false,
    userData: '/tmp/actionmesh',
    backend: { status: 'stopped', port: 0, runtimeMode: 'bundled', dataDir: '/tmp/actionmesh', lastError: '' },
    settings: {
      shortcut: 'CommandOrControl+Shift+Space',
      shortcutEnabled: true,
      notificationsEnabled: true,
      agentEnabled: true,
      deviceName: 'This laptop',
      workspaceRoot: '',
      allowShell: false,
      allowDesktop: false,
      allowedCommands: [],
      encryptionAvailable: true,
      providerDefault: false,
      secrets: { chatApiKey: { present: false, status: 'empty' } },
    },
    shortcutRegistered: true,
    provider: statuses.chat,
    purposes: { ...statuses },
  });

  const bridge = {
    statuses,
    calls,
    state: record('state', () => appState()),
    purposeSpecs: record('purposeSpecs', (purpose) => ({ ok: true, purpose, specs: SPECS[purpose] || [] })),
    purposeStatus: record('purposeStatus', (purpose) => ({ ok: true, status: { ...statuses[purpose] } })),
    purposeTest: record('purposeTest', (payload) => ({ ok: true, state: 'invalid_credentials', detail: 'The provider rejected the credential.' })),
    purposeConfigure: record('purposeConfigure', (payload) => {
      const target = statuses[payload.purpose];
      statuses[payload.purpose] = {
        ...target,
        provider: payload.provider || '',
        providerLabel: ((SPECS[payload.purpose] || []).find((s) => s.id === payload.provider) || {}).label || '',
        model: payload.model || '',
        endpoint: payload.baseUrl || '',
        timeout: payload.timeoutSeconds || 12,
        hasKey: Boolean(payload.apiKey) || target.hasKey,
        state: 'not_configured',
        detail: 'Configured but not tested. Press Test Connection.',
        text: 'Not configured',
        testedAt: '',
      };
      return { ok: true, status: { ...statuses[payload.purpose] } };
    }),
    purposeRemove: record('purposeRemove', (payload) => {
      statuses[payload.purpose] = { ...statuses[payload.purpose], hasKey: false, state: 'missing_key', detail: 'No key stored', text: 'Missing credential' };
      return { ok: true, status: { ...statuses[payload.purpose] } };
    }),
    purposeSetModel: record('purposeSetModel', (payload) => {
      statuses[payload.purpose] = { ...statuses[payload.purpose], model: payload.model };
      return { ok: true, status: { ...statuses[payload.purpose] } };
    }),
    purposeSetDefault: record('purposeSetDefault', (payload) => ({ ok: true, status: { ...statuses[payload.purpose] } })),
    // The backend is stopped in this fake, so every route read is refused the way an
    // offline backend would be. Home must still render its lanes.
    request: record('request', () => ({ ok: false, status: 0, error: 'backend offline' })),
    updateSettings: record('updateSettings', () => ({ ok: true, settings: appState().settings })),
    startStream: record('startStream', () => ({ ok: true })),
    stopStream: record('stopStream', () => ({ ok: true })),
    startBackend: record('startBackend', () => ({ ok: true })),
    stopBackend: record('stopBackend', () => ({ ok: true })),
    restartBackend: record('restartBackend', () => ({ ok: true })),
    backendLogs: record('backendLogs', () => ({ ok: true, lines: [] })),
    openDataDir: record('openDataDir', () => ({ ok: true })),
    notify: record('notify', () => ({ ok: true })),
    showAbout: record('showAbout', () => ({ ok: true })),
    openExternal: record('openExternal', () => Promise.resolve()),
    openWebPage: record('openWebPage', () => Promise.resolve()),
    clearWebSession: record('clearWebSession', () => Promise.resolve()),
    minimize: () => {}, toggleMaximize: () => {}, close: () => {},
    onStreamEvent: () => () => {},
    onStreamStatus: () => () => {},
    onBackendChanged: () => () => {},
    onNavigate: () => () => {},
  };
  return bridge;
}

/**
 * Boot one page against its own fake bridge.
 *
 * `beforeLoad` may adjust the fake first (say, a lane that is already configured) —
 * which is the only way to test a starting state, since the renderer reads the bridge
 * at load and each test gets a fresh module instance.
 */
async function bootPage({ beforeLoad } = {}) {
  const bridge = fakeBridge();
  if (beforeLoad) beforeLoad(bridge);
  const page = install({ bridge });
  const app = page.load();
  await app.boot();
  return { ...page, app, bridge };
}

function laneCards(document) {
  return document.querySelectorAll('#homeProviderKv .purpose-lane');
}

function laneText(card) { return card.textContent; }

test('the renderer boots with no exception and shows one lane per job', async () => {
  const { document } = await bootPage();

  const cards = laneCards(document);
  assert.equal(cards.length, 4, 'home lists chat, reasoning, image and audio');
  assert.deepEqual(cards.map((card) => card.dataset.purpose), ['chat', 'reasoning', 'image', 'audio']);
  for (const card of cards) {
    assert.match(laneText(card), /Not configured/i, `${card.dataset.purpose} starts unconfigured`);
  }
  // Settings has its own copy of the same four lanes.
  assert.equal(document.querySelectorAll('#settingsProviderKv .purpose-lane').length, 4);
  // The page never reaches for innerHTML, which is how untrusted provider text would
  // become markup. This is the lock the file's header comment promises.
  assert.deepEqual(document.__innerHTMLWrites, []);
  // And the header's chat bar was populated from the chat lane's provider table only.
  const select = document.querySelector('.provider-bar[data-purpose="chat"] .purpose-provider-select');
  assert.deepEqual(select.options.map((option) => option.value), SPECS.chat.map((spec) => spec.id));
});

test('a lane that is configured but untested says so rather than looking connected', async () => {
  const { document } = await bootPage({
    beforeLoad(bridge) {
      bridge.statuses.reasoning = statusFor('reasoning', {
        provider: 'openai', providerLabel: 'OpenAI', model: 'gpt-4o', hasKey: true,
        detail: 'Configured but not tested. Press Test Connection.',
      });
    },
  });

  const reasoning = laneCards(document).find((card) => card.dataset.purpose === 'reasoning');
  assert.match(laneText(reasoning), /OpenAI · gpt-4o/);
  assert.match(laneText(reasoning), /Key held in the OS keyring/);
  assert.match(laneText(reasoning), /Configured but not tested/);
  // The chip carries the state word; "connected" is not among them.
  assert.match(laneText(reasoning), /not configured/i);
  assert.doesNotMatch(laneText(reasoning), /connected/i);
});

test('the image and audio lanes show unimplemented providers with a reason, and cannot be saved', async () => {
  const { document, app } = await bootPage();
  // Open the modal on the image lane the way the card's Configure button does.
  document.querySelector('#homeProviderKv [data-lane-action="configure"][data-purpose="image"]').click();
  await new Promise((resolve) => setImmediate(resolve));

  const modal = document.getElementById('providerModal');
  assert.equal(modal.hidden, false);
  assert.equal(modal.dataset.purpose, 'image');
  assert.match(document.getElementById('providerModalTitle').textContent, /Image/);

  const select = document.getElementById('cfgProvider');
  assert.deepEqual(select.options.map((option) => option.value), SPECS.image.map((spec) => spec.id));
  for (const option of select.options) {
    assert.equal(option.disabled, true, `${option.value} is not selectable`);
    assert.match(option.textContent, /not implemented/i);
  }
  // Save and Test are disabled, and the reason is on screen.
  assert.equal(document.getElementById('cfgSave').disabled, true);
  assert.equal(document.getElementById('cfgTest').disabled, true);
  assert.match(document.getElementById('cfgProviderHint').textContent, /Not wired in this build/);
  void app;
});

test('opening the modal on a different job swaps the provider list with it', async () => {
  const { document } = await bootPage();
  const open = async (purpose) => {
    document.querySelector(`#homeProviderKv [data-lane-action="configure"][data-purpose="${purpose}"]`).click();
    await new Promise((resolve) => setImmediate(resolve));
  };

  await open('chat');
  const chatOptions = document.getElementById('cfgProvider').options.map((option) => option.value);
  const chatSignature = document.getElementById('cfgProvider').dataset.signature;
  assert.deepEqual(chatOptions, SPECS.chat.map((spec) => spec.id));

  await open('audio');
  const audioOptions = document.getElementById('cfgProvider').options.map((option) => option.value);
  assert.deepEqual(audioOptions, SPECS.audio.map((spec) => spec.id));
  assert.notEqual(document.getElementById('cfgProvider').dataset.signature, chatSignature);
  assert.equal(document.getElementById('providerModal').dataset.purpose, 'audio');
  // The default button belongs to the chat lane only.
  assert.equal(document.getElementById('cfgDefault').disabled, true);

  await open('chat');
  assert.equal(document.getElementById('cfgDefault').disabled, false);
  assert.deepEqual(document.getElementById('cfgProvider').options.map((option) => option.value),
    SPECS.chat.map((spec) => spec.id), 'the list comes back with the chat table');
});

test('Save sends the credential for the open job, then clears the box', async () => {
  const { document, bridge } = await bootPage();
  document.querySelector('#homeProviderKv [data-lane-action="configure"][data-purpose="reasoning"]').click();
  await new Promise((resolve) => setImmediate(resolve));

  document.getElementById('cfgProvider').value = 'openai';
  document.getElementById('cfgModel').value = 'gpt-4o-mini';
  const keyBox = document.getElementById('cfgApiKey');
  keyBox.value = 'sk-typed-into-the-box-0123456789';

  document.getElementById('cfgSave').click();
  // The handler is async: let its two awaits (configure, refresh) settle.
  for (let turn = 0; turn < 4; turn += 1) await new Promise((resolve) => setImmediate(resolve));

  const saved = bridge.calls.filter((call) => call.name === 'purposeConfigure');
  assert.equal(saved.length, 1);
  assert.equal(saved[0].args[0].purpose, 'reasoning', 'the save is scoped to the open job');
  assert.equal(saved[0].args[0].provider, 'openai');
  assert.equal(saved[0].args[0].model, 'gpt-4o-mini');
  assert.equal(saved[0].args[0].apiKey, 'sk-typed-into-the-box-0123456789');

  // The page keeps no copy in the field, and the lane it updated is the one that was
  // saved — not chat, which shares this modal.
  assert.equal(keyBox.value, '', 'the key box is emptied after saving');
  assert.equal(bridge.statuses.reasoning.provider, 'openai');
  assert.equal(bridge.statuses.chat.provider, '');
  // And the credential never came back: only a hasKey boolean was ever held.
  assert.equal(JSON.stringify(bridge.statuses).includes('sk-typed-into-the-box'), false);
});

test('closing the modal discards a typed key', async () => {
  const { document } = await bootPage();
  document.querySelector('#homeProviderKv [data-lane-action="configure"][data-purpose="chat"]').click();
  await new Promise((resolve) => setImmediate(resolve));
  document.getElementById('cfgApiKey').value = 'sk-half-typed-0123456789';

  document.getElementById('providerModalClose').click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(document.getElementById('providerModal').hidden, true);
  assert.equal(document.getElementById('cfgApiKey').value, '');
});

test('Test Connection reports the failure verbatim and does not save anything', async () => {
  const { document, bridge } = await bootPage();
  document.querySelector('#homeProviderKv [data-lane-action="configure"][data-purpose="chat"]').click();
  await new Promise((resolve) => setImmediate(resolve));
  document.getElementById('cfgProvider').value = 'anthropic';
  document.getElementById('cfgModel').value = 'claude-sonnet-5-5';
  document.getElementById('cfgApiKey').value = 'sk-rejected-0123456789';

  document.getElementById('cfgTest').click();
  for (let turn = 0; turn < 6; turn += 1) await new Promise((resolve) => setImmediate(resolve));

  assert.equal(bridge.calls.filter((call) => call.name === 'purposeConfigure').length, 0, 'test is not save');
  const status = document.getElementById('cfgStatus');
  assert.match(status.textContent, /Invalid credentials/);
  assert.match(status.textContent, /rejected the credential/);
});

test('a missing element is a stated refusal, not an exception', async () => {
  const page = install({ bridge: fakeBridge() });
  // Remove every id the page looks up by name. If the renderer assumed one existed, the
  // boot below would throw instead of returning.
  page.document.nodes.clear();
  const app = page.load();
  await assert.doesNotReject(() => app.boot());
});

test('a Configure click on a lane reaches the main process for that purpose only', async () => {
  const { document, bridge } = await bootPage();
  document.querySelector('#homeProviderKv [data-lane-action="configure"][data-purpose="audio"]').click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(document.getElementById('providerModal').dataset.purpose, 'audio');
  // Opening the modal reads no status on its own; the lane table was loaded once per
  // purpose at boot, and the modal filled from that.
  const read = bridge.calls.filter((call) => call.name === 'purposeSpecs').map((call) => call.args[0]);
  assert.deepEqual(read.sort(), ['audio', 'chat', 'image', 'reasoning']);
});

test('the Settings lane card names the keyring boundary honestly', async () => {
  const { document } = await bootPage();
  const note = document.getElementById('settingsProviderNote').textContent;
  assert.match(note, /own Test Connection/);
  assert.match(note, /Image and audio adapters are not implemented/);
});

test('the renderer source is syntactically valid and keeps its two promises', async () => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'ui', 'app.js'), 'utf8');
  // `node --check` runs the same parse the page does; this is the cheap half of the
  // gate and the full command is in package.json's test script.
  new Function(source);
  assert.equal(/\.innerHTML\s*=/.test(source.replace(/^\s*\/\/.*$/gm, '')), false,
    'nothing is assigned to innerHTML');
  assert.equal(/localStorage|sessionStorage/.test(source), false,
    'no credential can be parked in web storage');
  // The legacy save path is gone, not merely hidden: none of the identifiers that drove
  // the pre-C42 single-provider form are in the renderer, and the markup it read is gone
  // from the page, so no screen can write that slot behind the lane cards' back.
  const html = fs.readFileSync(path.join(__dirname, '..', 'ui', 'index.html'), 'utf8');
  for (const legacy of ['setProviderName', 'setProviderEndpoint', 'setProviderModel',
    'setProviderKey', 'providerSave', 'providerClear', 'legacyProviderCard']) {
    assert.equal(source.includes(legacy), false, `app.js still drives ${legacy}`);
    assert.equal(html.includes(legacy), false, `index.html still contains ${legacy}`);
  }
  for (const purpose of ['chat', 'reasoning', 'image', 'audio']) {
    assert.ok(source.includes(`'${purpose}'`) || source.includes(`"${purpose}"`), `${purpose} is named`);
  }
  for (const label of Object.values(PURPOSE_LABELS)) {
    assert.ok(source.includes(label), `${label} has a label`);
  }
});