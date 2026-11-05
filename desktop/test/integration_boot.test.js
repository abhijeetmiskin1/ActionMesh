'use strict';
/**
 * test/integration_boot.test.js — Phase 6 integration: the page as the browser runs it.
 *
 * The module tests (workbench.test.js, activity.test.js, connections.test.js,
 * savedtasks.test.js) load each file in isolation through `require`. That proves a
 * module's pure helpers but not the thing §1 of the brief asks for: that all nine
 * classic scripts coexist in ONE global lexical scope, that index.html's script
 * order loads them in a workable sequence, and that boot() actually initialises
 * the Phase 6 panels.
 *
 * Scope note, so the evidence is not overstated: this is a DOM HARNESS, not a live
 * GUI session. It proves wiring — script order, globals present, init called once,
 * controls bound to real markup, no duplicate handlers. It does not prove pixel
 * rendering, and it is never reported as a live graphical acceptance.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const { install } = require('./dom_harness.js');

const UI_DIR = path.resolve(__dirname, '..', 'ui');

/** The script order index.html declares, read from index.html itself. */
function indexScriptOrder() {
  const html = fs.readFileSync(path.join(UI_DIR, 'index.html'), 'utf8');
  return [...html.matchAll(/<script\s+src="([^"]+)"><\/script>/g)].map((m) => m[1]);
}

/**
 * Build a context that behaves like the renderer window: the harness document and
 * bridge, plus `window` pointing at itself so `window.ActionMeshX` assignments land
 * somewhere the next script can read — exactly the sharing the browser gives.
 */
function rendererContext(bridgeOverrides) {
  const page = install({ bridge: bridgeOverrides });
  const win = page.window;
  const context = vm.createContext(win);
  // vm gives the context its own global object. Point `globalThis`/`window` at the
  // shared object so classic scripts see the same bindings a page would.
  vm.runInContext('globalThis.window = globalThis; globalThis.self = globalThis;', context);
  return { page, context };
}

/** Load one page script the way a classic <script src> does: evaluate it in the shared global. */
function loadScript(context, file) {
  const source = fs.readFileSync(path.join(UI_DIR, file), 'utf8');
  vm.runInContext(source, context, { filename: file });
}

function loadAll(context) {
  const order = indexScriptOrder();
  for (const file of order) loadScript(context, file);
  return order;
}

test('index.html declares every Phase 6 module before app.js', () => {
  const order = indexScriptOrder();
  for (const file of ['history.js', 'activity.js', 'workbench.js', 'connections.js', 'savedtasks.js']) {
    assert.ok(order.includes(file), `${file} is not referenced by index.html`);
  }
  assert.equal(order[order.length - 1], 'app.js', 'app.js must boot last');
});

test('all nine classic scripts evaluate in one shared scope without colliding', () => {
  const { context } = rendererContext();
  // A SyntaxError here is the exact failure this guards: one top-level `const`
  // declared twice stops the later file from loading at all.
  const order = loadAll(context);
  assert.equal(order.length, 9, 'expected nine scripts, as index.html declares');
});

test('every module publishes its window surface after load', () => {
  const { context } = rendererContext();
  loadAll(context);
  const win = context.window || context;
  const expected = {
    ActionMeshHistory: 'refreshExecHistory',
    ActionMeshActivity: 'initActivity',
    ActionMeshWorkbench: 'initWorkbench',
    actionmeshConnections: 'initConnections',
    ActionMeshSavedTasks: 'initSavedTasks',
    ActionMeshApp: 'showView',
  };
  for (const [name, method] of Object.entries(expected)) {
    assert.ok(win[name], `${name} was not published`);
    assert.equal(typeof win[name][method], 'function', `${name}.${method} is not callable`);
  }
});

test('boot initialises Workbench, Activity and Connections exactly once', async () => {
  const { context, page } = rendererContext();
  loadAll(context);

  const counts = { initWorkbench: 0, initActivity: 0, initConnections: 0, initSavedTasks: 0 };
  const win = context.window || context;
  for (const name of Object.keys(counts)) {
    for (const owner of ['ActionMeshWorkbench', 'ActionMeshActivity', 'actionmeshConnections', 'ActionMeshSavedTasks']) {
      if (win[owner] && typeof win[owner][name] === 'function') {
        const original = win[owner][name];
        win[owner][name] = function wrapped(...args) {
          counts[name] += 1;
          return original.apply(this, args);
        };
      }
    }
  }

  const app = win.ActionMeshApp;
  assert.equal(typeof app, 'object', 'ActionMeshApp must be exported for cross-module navigation');

  await vm.runInContext('boot()', context);

  assert.equal(counts.initWorkbench, 1, 'Workbench must initialise exactly once');
  assert.equal(counts.initActivity, 1, 'Activity must initialise exactly once');
  assert.equal(counts.initConnections, 1, 'Connections must initialise exactly once');
  // app.js owns the automations surface; savedtasks.js must stay dormant.
  assert.equal(counts.initSavedTasks, 0, 'savedtasks must not double-bind the automations surface');
});

test('boot reaches the shell rather than refusing on missing anchors', async () => {
  const { context, page } = rendererContext();
  loadAll(context);
  const result = await vm.runInContext('boot()', context);
  // boot() returns undefined on the success path and only returns
  // {ok:false, missing:[…]} when index.html and app.js disagree. Assert both the
  // return value and the refusal artefact it would append, so a silent early
  // return cannot pass as success.
  assert.notEqual(result && result.ok, false, `boot refused: ${JSON.stringify(result && result.missing)}`);
  const refusal = page.document.querySelector('.boot-refusal');
  assert.equal(refusal, null, 'boot appended a refusal note instead of starting');
});

// ---------------------------------------------------------------------------
// The guided tour. onboarding_flow.js used to be loaded and then never called: a
// module that publishes `ActionMeshFirstRun.renderFirstRun` and has no caller is
// dead weight, not a feature. These pin the call site to the control that opens it.
// ---------------------------------------------------------------------------

test('the guided tour is reachable from Settings and mounts on its own host', async () => {
  const { context, page } = rendererContext();
  loadAll(context);
  await vm.runInContext('boot()', context);
  const doc = page.document;

  const doc2 = doc.getElementById('firstRunMount');
  assert.ok(doc2, 'index.html must declare #firstRunMount');
  assert.equal(doc2.hidden, true, 'the tour starts closed');
  // A second fixed overlay at boot would cover the wizard onboarding.js already owns.
  assert.equal(doc2.childNodes.length, 0, 'nothing is mounted until it is asked for');

  const open = doc.getElementById('firstRunOpen');
  assert.ok(open, 'Settings must offer a control that opens the tour');
  open.click();
  assert.equal(doc2.hidden, false, 'pressing the control shows the tour');
  const text = (doc2.textContent || '').replace(/\s+/g, ' ');
  assert.match(text, /Welcome/, 'the tour opened at its first step');
  assert.match(text, /grants nothing/i, 'the first step says what it will not do');
});

test('opening the tour twice does not stack a second copy', async () => {
  const { context, page } = rendererContext();
  loadAll(context);
  await vm.runInContext('boot()', context);
  const doc = page.document;
  const mount = doc.getElementById('firstRunMount');
  const open = doc.getElementById('firstRunOpen');
  open.click();
  const roots = () => mount.childNodes.filter((n) => n.id === 'firstRunRoot');
  assert.equal(roots().length, 1, 'one tour after one press');
  open.click();
  assert.equal(roots().length, 1, 'pressing again must reuse the mounted tour, not append another');
});

test('closing the tour decides nothing', async () => {
  const { context, page } = rendererContext();
  loadAll(context);
  await vm.runInContext('boot()', context);
  const doc = page.document;
  const mount = doc.getElementById('firstRunMount');
  doc.getElementById('firstRunOpen').click();
  const before = page.calls.filter((c) => c.name === 'updateSettings').length;
  // Escape is the exit a keyboard user has. Closing is not completing: the setting
  // that hides the boot overlay must be untouched by it.
  doc.dispatch('keydown', { key: 'Escape' });
  assert.equal(mount.hidden, true, 'Escape puts the overlay away');
  const after = page.calls.filter((c) => c.name === 'updateSettings').length;
  assert.equal(after, before, 'closing the tour wrote no setting');
});