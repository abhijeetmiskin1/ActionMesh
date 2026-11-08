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
const vm = require('node:vm');

const { install } = require('./dom_harness.js');

test('the scripts in index.html can register the renderer boot together', () => {
  const uiDir = path.join(__dirname, '..', 'ui');
  const html = fs.readFileSync(path.join(uiDir, 'index.html'), 'utf8');
  const scripts = html.matchAll(/<script\s+src="([^"]+)"><\/script>/g);
  const listeners = new Map();
  const context = vm.createContext({
    document: { addEventListener: (event, listener) => listeners.set(event, listener) },
  });
  context.window = context;
  for (const [, filename] of scripts) {
    const source = fs.readFileSync(path.join(uiDir, filename), 'utf8');
    vm.runInContext(source, context, { filename });
  }
  assert.equal(typeof listeners.get('DOMContentLoaded'), 'function');
  assert.equal(typeof context.ActionMeshHistory?.refreshExecHistory, 'function');
});

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
async function bootPage({ beforeLoad, setupWindow } = {}) {
  const bridge = fakeBridge();
  if (beforeLoad) beforeLoad(bridge);
  const page = install({ bridge });
  if (setupWindow) setupWindow(page.window);
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
  assert.match(note, /Test Connection for Chat and Reasoning/);
  assert.match(note, /Audio is verified when a recording/);
});

test('hands-free dictation runs a final transcript and speaks the result', async () => {
  const calls = [];
  const spoken = [];
  let transcript;
  let ran = false;
  const { document } = await bootPage({ setupWindow(window) {
    window.VoiceOSVoice = {
      supported: true, textToSpeechSupported: true, setState() {},
      onStateChange() {}, onError() {}, onTranscript(fn) { transcript = fn; },
      speak(text) { spoken.push(text); },
    };
  }, beforeLoad(bridge) {
    const readState = bridge.state;
    bridge.state = async () => {
      const value = await readState();
      return { ...value, backend: { ...value.backend, status: 'ready' } };
    };
    bridge.request = async ({ method, path, body }) => {
      calls.push([method, path, body]);
      if (path === '/voiceos/messages') {
        return { ok: true, body: { kind: 'task', session_id: 'task-1', reply: 'Review this plan.', ran: false } };
      }
      if (path === '/voiceos/sessions/task-1/run') ran = true;
      if (path.startsWith('/voiceos/sessions/task-1')) {
        return { ok: true, body: {
          session_id: 'task-1', status: ran ? 'completed' : 'planned', request: 'List workspace',
          steps: [{
            label: 'List workspace', action_type: 'device.file.list', state: ran ? 'succeeded' : 'pending',
            verified: ran ? true : null, observed: ran ? { count: 1 } : null,
          }],
        } };
      }
      return { ok: false, status: 503, error: 'fixture route unavailable' };
    };
  } });
  document.getElementById('voiceHandsFree').checked = true;
  transcript('List workspace', { final: false });
  assert.equal(calls.some(([, path]) => path === '/voiceos/messages'), false);
  transcript('List workspace', { final: true });
  await new Promise(setImmediate);
  const result = document.getElementById('voiceTaskResult');
  const runs = calls.filter(([, path]) => path === '/voiceos/sessions/task-1/run');
  assert.deepEqual(runs, [['POST', '/voiceos/sessions/task-1/run', {
    actor: { id: 'voiceos-local' }, stop_on_unverified: true,
  }]]);
  assert.match(result.textContent, /verifiedyes/);
  assert.match(result.textContent, /"count": 1/);
  assert.match(document.getElementById('voiceReply').textContent, /Task completed/);
  assert.doesNotMatch(document.getElementById('voiceReply').textContent, /Review this plan/);
  assert.deepEqual(spoken, ['Task completed.']);
});

test('automatic voice tasks continue after their specific approval', async () => {
  let approved = false;
  const runs = [];
  const waiting = {
    session_id: 'voice-approval', status: 'awaiting_approval', request: 'Run a task',
    steps: [{ label: 'Run a step', state: 'awaiting_approval', execution_id: 'approval-run' }],
  };
  const completed = { ...waiting, status: 'completed', steps: [
    { ...waiting.steps[0], state: 'succeeded', verified: true },
  ] };
  const options = { beforeLoad(bridge) {
    const readState = bridge.state;
    bridge.state = async () => {
      const value = await readState();
      return { ...value, backend: { ...value.backend, status: 'ready' } };
    };
    bridge.request = async ({ method, path, body }) => {
      if (path === '/voiceos/messages') {
        return { ok: true, body: { kind: 'task', session_id: waiting.session_id } };
      }
      if (path === '/voiceos/sessions/voice-approval/run') {
        runs.push(body);
        return { ok: true, body: approved ? completed : waiting };
      }
      if (path === '/voiceos/sessions?limit=100') {
        return { ok: true, body: [{
          actor_id: 'voiceos-local', session_id: waiting.session_id,
          automatic_approval_execution_id: 'approval-run',
        }] };
      }
      if (path === '/voiceos/sessions/voice-approval') {
        return { ok: true, body: approved ? completed : waiting };
      }
      if (path === '/approvals' && method === 'POST') {
        assert.equal(body.action_id, 'action-to-approve');
        approved = body.status === 'approved';
        return { ok: true, body: { execution: { state: 'completed' } } };
      }
      if (path === '/approvals') {
        return { ok: true, body: approved ? [] : [{
          execution_id: 'approval-run', actions: [{
            action_type: 'device.file.write', action_id: 'action-to-approve',
            parameters: {}, risk_level: 'high',
          }],
        }] };
      }
      if (path.startsWith('/consents') || path === '/capabilities') return { ok: true, body: [] };
      return { ok: false, status: 503, error: 'fixture route unavailable' };
    };
  } };
  const { document } = await bootPage(options);
  document.getElementById('voiceTranscript').value = 'Run a task';
  document.getElementById('voiceSend').click();
  await new Promise(setImmediate);
  assert.match(document.getElementById('voiceReply').textContent, /Waiting for your approval/);
  assert.equal(runs.length, 1);
  const result = document.getElementById('voiceTaskResult');
  assert.doesNotMatch(result.textContent, /Check again after approving/);
  const reopened = (await bootPage(options)).document;
  reopened.querySelector('.nav-item[data-view="approvals"]').click();
  await new Promise(setImmediate);
  reopened.querySelector('#approvalList .btn.primary').click();
  await new Promise(setImmediate);
  assert.equal(runs.length, 2);
  assert.ok(runs.every((body) => body.stop_on_unverified === true));
  assert.match(reopened.getElementById('voiceReply').textContent, /Task completed/);
});

test('voice reads the verified app status in plain language', async () => {
  const spoken = [];
  const status = {
    session_id: 'clock-status', status: 'completed', request: 'Is Clock running?',
    steps: [{
      label: 'Check Clock', action_type: 'device.app.status_native', state: 'succeeded',
      verified: true, observed: { app: 'clock', running: false },
    }],
  };
  const { document } = await bootPage({ setupWindow(window) {
    window.VoiceOSVoice = {
      textToSpeechSupported: true, setState() {}, onStateChange() {}, onError() {},
      onTranscript() {}, speak(text) { spoken.push(text); },
    };
  }, beforeLoad(bridge) {
    const readState = bridge.state;
    bridge.state = async () => {
      const value = await readState();
      return { ...value, backend: { ...value.backend, status: 'ready' } };
    };
    bridge.request = async ({ path }) => {
      if (path === '/voiceos/messages') {
        return { ok: true, body: { kind: 'task', session_id: status.session_id } };
      }
      if (path.startsWith('/voiceos/sessions/clock-status')) return { ok: true, body: status };
      return { ok: false, status: 503, error: 'fixture route unavailable' };
    };
  } });
  document.getElementById('voiceTranscript').value = 'Is Clock running?';
  document.getElementById('voiceSend').click();
  await new Promise(setImmediate);
  assert.match(document.getElementById('voiceReply').textContent, /Clock is closed/);
  assert.match(document.getElementById('voiceTaskResult').textContent, /verifiedyes/);
  assert.deepEqual(spoken, ['Clock is closed.']);
  const edit = Array.from(document.getElementById('voiceTaskResult').querySelectorAll('button'))
    .find((button) => button.textContent === 'Edit command');
  edit.click();
  assert.equal(document.getElementById('voiceTranscript').value, 'Is Clock running?');
  assert.deepEqual(spoken, ['Clock is closed.']);
});

test('conversation listens again after speaking and stops when leaving Voice Commands', async () => {
  let transcript;
  let stateChanged;
  let starts = 0;
  let stops = 0;
  const spoken = [];
  const { document } = await bootPage({ setupWindow(window) {
    window.VoiceOSVoice = {
      supported: true, textToSpeechSupported: true, state: 'idle', setState() {},
      onStateChange(fn) { stateChanged = fn; }, onTranscript(fn) { transcript = fn; },
      onError() {}, startListening() { starts++; }, cancel() { stops++; },
      speak(text) { spoken.push(text); return true; },
    };
  }, beforeLoad(bridge) {
    const readState = bridge.state;
    bridge.state = async () => {
      const value = await readState();
      return { ...value, backend: { ...value.backend, status: 'ready' } };
    };
    bridge.request = async ({ path }) => path === '/voiceos/messages'
      ? { ok: true, body: { kind: 'answer', reply: 'Hello.' } }
      : { ok: false, status: 503, error: 'fixture route unavailable' };
  } });
  document.querySelector('.nav-item[data-view="voice"]').click();
  document.getElementById('voiceConversation').checked = true;
  document.getElementById('voiceHandsFree').checked = true;
  document.getElementById('voiceMic').click();
  assert.equal(starts, 1);
  transcript('Hello', { final: true });
  await new Promise(setImmediate);
  assert.equal(starts, 1);
  stateChanged('idle');
  assert.equal(starts, 2);
  transcript('Jarvis, repeat that.', { final: true });
  assert.deepEqual(spoken, ['Hello.', 'Hello.']);
  stateChanged('idle');
  assert.equal(starts, 3);
  transcript('Jarvis, stop listening.', { final: true });
  assert.equal(stops, 1);
  stateChanged('idle');
  assert.equal(starts, 3);
  document.getElementById('voiceMic').click();
  document.querySelector('.nav-item[data-view="settings"]').click();
  assert.equal(stops, 2);
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

test('Providers has its own tab with all four model lanes', async () => {
  const { document } = await bootPage();
  assert.ok(document.querySelector('.nav-item[data-view="providers"]'));
  document.querySelector('[data-goto="providers"]').click();
  await new Promise(setImmediate);
  const view = document.querySelector('section[data-view="providers"]');
  assert.equal(view.hidden, false);
  for (const purpose of ['chat', 'reasoning', 'image', 'audio']) {
    assert.ok(view.querySelector(`[data-lane-action="configure"][data-purpose="${purpose}"]`));
  }
});

test('deal shortcut prepares an editable command without running it', async () => {
  const { document } = await bootPage();
  document.querySelector('.nav-item[data-view="voice"]').click();
  document.getElementById('voiceDealShortcut').click();
  assert.equal(document.getElementById('voiceTranscript').value, 'Find the best deal for ');
  assert.equal(document.getElementById('voiceReply').hidden, true);
});

test('the header approval shortcut opens the approval workspace', async () => {
  const { document } = await bootPage();
  document.getElementById('notifyBtn').click();
  assert.equal(document.querySelector('section[data-view="approvals"]').hidden, false);
  assert.equal(document.querySelector('section[data-view="home"]').hidden, true);
});

test('Task Center filters pending approvals without refetching tasks', async () => {
  let reads = 0;
  const { document } = await bootPage({ beforeLoad(bridge) {
    const readState = bridge.state;
    bridge.state = async () => {
      const value = await readState();
      return { ...value, backend: { ...value.backend, status: 'ready' } };
    };
    bridge.request = async ({ path }) => {
      if (path === '/voiceos/sessions?limit=50') {
        reads++;
        return { ok: true, body: [
          { session_id: 'waiting', request: 'Waiting request', status: 'awaiting_approval' },
          { session_id: 'done', request: 'Finished request', status: 'completed' },
        ] };
      }
      return { ok: false, status: 503, error: 'fixture route unavailable' };
    };
  } });
  document.querySelector('.nav-item[data-view="tasks"]').click();
  await new Promise(setImmediate);
  const list = document.getElementById('taskList');
  assert.match(list.textContent, /Finished request/);
  document.querySelector('[data-task-filter="approval"]').click();
  assert.match(list.textContent, /Waiting request/);
  assert.doesNotMatch(list.textContent, /Finished request/);
  assert.equal(list.querySelector('[role="button"]').tabIndex, 0);
  document.getElementById('taskResetFilters').click();
  assert.match(list.textContent, /Finished request/);
  assert.equal(reads, 1);
});

test('a completed task can be reused as an editable voice command', async () => {
  const { document } = await bootPage({ beforeLoad(bridge) {
    const readState = bridge.state;
    bridge.state = async () => {
      const value = await readState();
      return { ...value, backend: { ...value.backend, status: 'ready' } };
    };
    const task = { session_id: 'done', request: 'Open Calculator', status: 'completed', steps: [] };
    bridge.request = async ({ path }) => {
      if (path === '/voiceos/sessions?limit=50') return { ok: true, body: [task] };
      if (path === '/voiceos/sessions/done') return { ok: true, body: task };
      return { ok: false, status: 503, error: 'fixture route unavailable' };
    };
  } });
  document.querySelector('.nav-item[data-view="tasks"]').click();
  await new Promise(setImmediate);
  document.querySelector('#taskList [data-id="done"]').click();
  await new Promise(setImmediate);
  Array.from(document.getElementById('taskDetail').querySelectorAll('button'))
    .find((button) => button.textContent === 'Reuse command').click();
  assert.equal(document.querySelector('section[data-view="voice"]').hidden, false);
  assert.equal(document.getElementById('voiceTranscript').value, 'Open Calculator');
  assert.equal(document.getElementById('voiceReply').hidden, true);
});
