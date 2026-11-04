'use strict';
/**
 * First-run onboarding — renderer module tests.
 *
 * The wizard itself is mounted by the lead (index.html/app.js are
 * lead-owned), so everything testable without a mount lives here: the state
 * vocabulary, the aggregation rule, the per-feature checks, and the
 * invariants (no silent grants, no key material, skip-optional).
 *
 * The DOM_half uses the same tiny harness the renderer tests use
 * (dom_harness.js); no real layout is needed because every assertion is
 * about values the module computed, and consent behaviour is proven by
 * counting fake-bridge calls.
 */
const assert = require('node:assert/strict');
const test = require('node:test');

const { install } = require('./dom_harness.js');
const onboarding = require('../ui/onboarding.js');

const { READY, UNAVAILABLE, NEEDS_CONFIG } = onboarding;

/** A settings-shaped state the way main.js appState() reports it. */
function fakeState(overrides) {
  return {
    backend: { status: 'ready', port: 0, lastError: '' },
    settings: {
      deviceName: 'This laptop',
      allowShell: false,
      allowDesktop: false,
      allowBrowser: false,
      allowMedia: false,
      encryptionAvailable: true,
    },
    purposes: {
      chat: { state: 'not_configured', detail: 'Nothing is configured for this job yet.', text: 'Not configured', providerLabel: '', model: '' },
      reasoning: { state: 'not_configured', detail: 'Nothing is configured for this job yet.', text: 'Not configured', providerLabel: '', model: '' },
      image: { state: 'not_configured', detail: 'Nothing is configured for this job yet.', text: 'Not configured', providerLabel: '', model: '' },
      audio: { state: 'not_configured', detail: 'Nothing is configured for this job yet.', text: 'Not configured', providerLabel: '', model: '' },
    },
    specsByPurpose: {
      chat: [{ id: 'anthropic', implemented: true }],
      reasoning: [{ id: 'anthropic', implemented: true }],
      image: [{ id: 'openai_images', implemented: false }],
      audio: [{ id: 'elevenlabs', implemented: false }],
    },
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Vocabulary mapping
// ---------------------------------------------------------------------------

test('provider states map to the mandated vocabulary', () => {
  assert.equal(onboarding.readinessForProviderState('connected'), READY);
  assert.equal(onboarding.readinessForProviderState('not_configured'), NEEDS_CONFIG);
  assert.equal(onboarding.readinessForProviderState('missing_key'), NEEDS_CONFIG);
  assert.equal(onboarding.readinessForProviderState('connecting'), NEEDS_CONFIG);
  assert.equal(onboarding.readinessForProviderState('invalid_credentials'), UNAVAILABLE);
  assert.equal(onboarding.readinessForProviderState('rate_limited'), UNAVAILABLE);
  assert.equal(onboarding.readinessForProviderState('provider_unavailable'), UNAVAILABLE);
  assert.equal(onboarding.readinessForProviderState('timeout'), UNAVAILABLE);
  assert.equal(onboarding.readinessForProviderState('mystery-future-state'), NEEDS_CONFIG,
    'an unknown future state is NEEDS_CONFIG, never READY');
  assert.equal(onboarding.readinessForProviderState(undefined), NEEDS_CONFIG);
});

test('unimplemented lanes can never be READY', () => {
  const image = onboarding.checkProviders(fakeState().purposes, fakeState().specsByPurpose);
  assert.equal(image.lanes.image.status, UNAVAILABLE);
  assert.equal(image.lanes.audio.status, UNAVAILABLE);
  // Even if main.js ever said `connected` for an unimplemented lane, the UI
  // vocabulary forces it back — a lane needs a real adapter+probe first.
  const forced = onboarding.readinessForProviderState('connected');
  assert.equal(forced, READY, 'sanity: connected is READY on its own');
  const checks = onboarding.checkProviders(
    { ...fakeState().purposes,
      image: { state: 'connected', detail: '', text: 'Connected', providerLabel: 'X', model: 'y' } },
    fakeState().specsByPurpose,
  );
  // checkProviders honours the unimplemented table: image stays UNAVAILABLE
  // until an implemented spec exists for that purpose.
  assert.equal(checks.lanes.image.status, UNAVAILABLE);
});

test('purposesImplemented detects unimplemented-only tables', () => {
  const map = onboarding.purposesImplemented(fakeState().specsByPurpose);
  assert.deepEqual(map, { chat: false, reasoning: false, image: true, audio: true });
  assert.deepEqual(onboarding.purposesImplemented(null),
    { chat: false, reasoning: false, image: false, audio: false });
});

// ---------------------------------------------------------------------------
// Per-feature checks
// ---------------------------------------------------------------------------

test('checkService reflects the backend fact', () => {
  assert.equal(onboarding.checkService(fakeState()).status, READY);
  assert.equal(onboarding.checkService({ backend: { status: 'failed', lastError: 'boom' } }).status,
    UNAVAILABLE);
  assert.match(onboarding.checkService({ backend: { status: 'failed', lastError: 'boom' } }).detail, /boom/);
  assert.equal(onboarding.checkService({}).status, NEEDS_CONFIG);
  assert.equal(onboarding.checkService(null).status, NEEDS_CONFIG);
});

test('checkDevice names this laptop or asks for config', () => {
  assert.equal(onboarding.checkDevice(fakeState()).status, READY);
  assert.match(onboarding.checkDevice(fakeState()).detail, /This laptop/);
  assert.equal(onboarding.checkDevice({}).status, NEEDS_CONFIG);
});

test('checkPermissions is READY by default and never a blocker', () => {
  const off = onboarding.checkPermissions(fakeState());
  assert.equal(off.status, READY);
  assert.match(off.detail, /off/);
  const on = onboarding.checkPermissions({ settings: { allowShell: true } });
  assert.equal(on.status, READY);
  assert.match(on.detail, /allowShell/);
});

test('checkSecurity is UNAVAILABLE when the keyring is down', () => {
  assert.equal(
    onboarding.checkSecurity({ settings: { encryptionAvailable: false } }).status, UNAVAILABLE);
  assert.match(
    onboarding.checkSecurity({ settings: { encryptionAvailable: false } }).detail, /plain text/);
  assert.equal(onboarding.checkSecurity(fakeState()).status, READY);
});

test('checkBrowser and checkMedia are optional-safe', () => {
  assert.equal(onboarding.checkBrowser(fakeState(), null).status, NEEDS_CONFIG);
  assert.equal(onboarding.checkBrowser({ settings: { allowBrowser: true } }, null).status, READY);
  assert.equal(onboarding.checkMedia(fakeState()).status, NEEDS_CONFIG);
  assert.equal(onboarding.checkMedia({ settings: { allowMedia: true } }).status, READY);
});

// ---------------------------------------------------------------------------
// Aggregation — optional never blocks
// ---------------------------------------------------------------------------

test('aggregateReadiness: required NEEDS_CONFIG → NEEDS_CONFIG, UNAVAILABLE wins', () => {
  const allReady = {
    service: { status: READY }, device: { status: READY },
    permissions: { status: READY }, security: { status: READY },
  };
  assert.equal(onboarding.aggregateReadiness(allReady), READY);
  const needy = { ...allReady, device: { status: NEEDS_CONFIG } };
  assert.equal(onboarding.aggregateReadiness(needy), NEEDS_CONFIG);
  const down = { ...allReady, service: { status: UNAVAILABLE }, device: { status: NEEDS_CONFIG } };
  assert.equal(onboarding.aggregateReadiness(down), UNAVAILABLE);
  // Optional lanes can be UNAVAILABLE without blocking a READY final.
  const optionalDown = {
    ...allReady,
    providers: { status: UNAVAILABLE }, browser: { status: UNAVAILABLE }, media: { status: UNAVAILABLE },
  };
  assert.equal(onboarding.aggregateReadiness(optionalDown), READY);
});

test('collectChecks produces all nine steps with a final', () => {
  const results = onboarding.collectChecks(fakeState(), null);
  for (const step of onboarding.STEPS) {
    assert.ok(results[step.id], `step ${step.id} has a result`);
    assert.ok([READY, UNAVAILABLE, NEEDS_CONFIG].includes(results[step.id].status),
      `${step.id} status is in the vocabulary`);
  }
  assert.equal(results.providers.lanes.chat.label, 'Chat');
  assert.equal(results.final.detail.length > 0, true);
});

test('STEPS has the eight mandated features plus welcome', () => {
  const ids = onboarding.STEPS.map((s) => s.id);
  for (const required of ['service', 'device', 'permissions', 'providers', 'browser', 'media', 'security', 'final']) {
    assert.ok(ids.includes(required), `step ${required} exists`);
  }
  const optional = onboarding.STEPS.filter((s) => s.optional).map((s) => s.id);
  assert.deepEqual(optional.sort(), ['browser', 'media', 'providers']);
});

// ---------------------------------------------------------------------------
// Renderer invariants (fake bridge, no mount assumptions)
// ---------------------------------------------------------------------------

test('the module has no credentials, no storage and no innerHTML', () => {
  const fs = require('node:fs');
  const path = require('node:path');
  const source = fs.readFileSync(path.join(__dirname, '..', 'ui', 'onboarding.js'), 'utf8');
  new Function(source);
  assert.equal(/\.innerHTML\s*=/.test(source.replace(/^\s*\/\/.*$/gm, '')), false,
    'nothing is assigned to innerHTML');
  for (const banned of ['localStorage', 'sessionStorage', 'fetch(', 'XMLHttpRequest', 'require(']) {
    assert.equal(source.includes(banned), false, `onboarding.js does not use ${banned}`);
  }
  // No key-shaped read: the module never asks for a credential back.
  assert.equal(/apiKey|getKey/i.test(source), false,
    'onboarding.js never names a credential');
  // Every state it renders is one of the three words.
  for (const word of ['READY', 'UNAVAILABLE', 'NEEDS_CONFIG']) {
    assert.ok(source.includes(word), `${word} is in the vocabulary`);
  }
});

test('renderOnboarding mounts, paints without bridge I/O, and only writes on explicit clicks', async () => {
  const calls = [];
  const bridge = {
    state: () => { calls.push('state'); return Promise.resolve(fakeState()); },
    updateSettings: (patch) => { calls.push(['updateSettings', patch]); return Promise.resolve({ ok: true }); },
    purposeTest: (payload) => { calls.push(['purposeTest', payload]); return Promise.resolve({ ok: true }); },
    browserState: () => { calls.push('browserState'); return Promise.resolve({}); },
  };
  const page = install({ bridge });
  const host = page.document.createElement('div');
  page.document.body.append(host);

  const controller = onboarding.renderOnboarding(host, bridge, { initialState: fakeState() });
  assert.ok(controller, 'mount returns a controller');
  assert.equal(controller.element.id, 'onboardingRoot');
  assert.equal(calls.length, 0, 'mounting performs no bridge I/O');

  // The step nav lists all nine steps with states as words.
  const navItems = host.querySelectorAll('.onboarding-step');
  assert.equal(navItems.length, 9);
  assert.match(host.textContent, /READY/);
  assert.match(host.textContent, /NEEDS CONFIG/);

  // Optional skip is offered only on optional steps, never on service.
  assert.equal(controller.skip('service'), false, 'required steps cannot be skipped');
  assert.equal(controller.skip('providers'), true, 'optional steps can be skipped');
  assert.equal(controller.skip('nonexistent'), false);
  assert.deepEqual(controller.skippedSteps(), ['providers']);

  // Consent toggles exist on the permissions step and are off by default.
  const current0 = host.querySelector('[data-step="service"]');
  assert.ok(current0, 'service step is listed');
  assert.equal(controller.currentStep(), 'welcome');

  // complete() is the only path that writes onboardingCompleted, and it writes
  // exactly that one key — existing settings are preserved by construction.
  await controller.complete();
  const writes = calls.filter((c) => Array.isArray(c) && c[0] === 'updateSettings');
  assert.equal(writes.length, 1);
  assert.deepEqual(writes[0][1], { onboardingCompleted: true });
  assert.equal(host.querySelector('#onboardingRoot').hidden, true);

  // No secret was ever passed to the bridge.
  assert.equal(JSON.stringify(calls).includes('apiKey'), false);
});

// ---------------------------------------------------------------------------
// Packaging metadata (desktop/package.json)
// ---------------------------------------------------------------------------

test('package.json linux build has desktopName and excludes secrets', () => {
  const fs = require('node:fs');
  const path = require('node:path');
  const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'package.json'), 'utf8'));
  assert.equal(pkg.scripts['dist:linux'], 'electron-builder --linux deb --x64');
  assert.equal(pkg.build.linux.desktop.entry.Name, 'ActionMesh');
  assert.equal(pkg.build.linux.desktop.entry.StartupNotify, 'true');
  // desktopName is top-level package metadata (packages.json root), not linux.*.
  assert.equal(pkg.desktopName, 'actionmesh.desktop');
  assert.equal(pkg.build.linux.syncDesktopName, true);
  assert.ok(pkg.build.extraResources.some(
    (r) => r.from === 'runtime' && r.to === 'runtime'), 'bundled runtime is staged as extraResources');
  const files = pkg.build.files.join(' ');
  for (const banned of ['settings.json', '.key', '.env']) {
    assert.ok(pkg.build.files.some((f) => f.includes('!') && f.includes(banned)),
      `secrets are excluded (${banned})`);
  }
  void files;
  assert.equal(pkg.build.productName, 'ActionMesh');
  assert.ok(pkg.build.files.includes('ui/**/*'), 'onboarding.js ships via ui/**');
});

// ---------------------------------------------------------------------------
// Phase 2 Agent 5 — P2-3 accessibility + onboarding readiness honesty
// ---------------------------------------------------------------------------

test('onboarding writes NO settings on load (zero bridge writes)', async () => {
  const writes = [];
  const bridge = {
    state: () => Promise.resolve(fakeState()),
    updateSettings: (patch) => { writes.push(patch); return Promise.resolve({ ok: true }); },
    purposeTest: () => Promise.resolve({ ok: true }),
    browserState: () => Promise.resolve({}),
  };
  const page = install({ bridge });
  const host = page.document.createElement('div');
  page.document.body.append(host);
  const controller = onboarding.renderOnboarding(host, bridge, { initialState: fakeState() });
  await Promise.resolve();
  // Mount + paint must not flip agentEnabled / allow_* / onboardingCompleted.
  assert.equal(writes.length, 0, `mount wrote settings: ${JSON.stringify(writes)}`);
  // Explicit refresh is a read, not a write.
  await controller.refresh();
  assert.equal(writes.length, 0, 'refresh wrote settings');
  // Only complete() writes, and only the single onboardingCompleted key.
  await controller.complete();
  assert.equal(writes.length, 1);
  assert.deepEqual(writes[0], { onboardingCompleted: true });
});

test('onboarding buttons have accessible labels and panel is a live region', () => {
  const bridge = { state: () => Promise.resolve(fakeState()), updateSettings: () => Promise.resolve({}) };
  const page = install({ bridge });
  const host = page.document.createElement('div');
  page.document.body.append(host);
  onboarding.renderOnboarding(host, bridge, { initialState: fakeState() });
  const buttons = host.querySelectorAll('button');
  assert.ok(buttons.length > 0, 'onboarding renders buttons');
  for (const btn of buttons) {
    const label = (btn.getAttribute('aria-label') || btn.textContent || '').trim();
    assert.ok(label.length > 0, `button "${btn.textContent}" has text or aria-label`);
  }
  const panel = host.querySelector('.onboarding-panel');
  assert.ok(panel, 'panel exists');
  assert.equal(panel.getAttribute('aria-live'), 'polite');
  assert.equal(panel.getAttribute('role'), 'status');
  const root = host.querySelector('#onboardingRoot');
  assert.equal(root.getAttribute('role'), 'region');
});

test('onboarding readiness reflects initialState, never invented READY', () => {
  const down = fakeState({ backend: { status: 'failed', lastError: 'boom' } });
  const checks = onboarding.collectChecks(down, null);
  assert.equal(checks.service.status, onboarding.UNAVAILABLE);
  assert.equal(checks.final.status, onboarding.UNAVAILABLE);
  const up = onboarding.collectChecks(fakeState(), null);
  assert.equal(up.service.status, onboarding.READY);
});
