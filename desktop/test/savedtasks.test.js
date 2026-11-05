'use strict';
/**
 * Saved tasks + first-run flow — renderer module tests.
 *
 * Pattern: the same discipline as onboarding.test.js / history tests —
 * node:test + assert/strict, the tiny dom_harness.js shim (no jsdom), and
 * a fake bridge that records every call. Every assertion is about values
 * the module computed or the exact HTTP paths it hit, so tightening the
 * backend contract breaks these tests rather than letting them drift.
 *
 * Placement note for Lead: `desktop/package.json` `scripts.test` is
 *   node --test test/*.test.js app/providers.test.js app/provider_bridge.test.js
 * which does NOT glob `ui/*.test.js`. This file lives at
 * `desktop/ui/savedtasks.test.js` per ownership; the lead needs to either
 * move it to `desktop/test/savedtasks.test.js` or extend the test glob to
 * include `ui/savedtasks.test.js`. Running it directly:
 *   node --test ui/savedtasks.test.js   (from desktop/)
 */
const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');

const { install } = require('./dom_harness.js');
const saved = require('../ui/savedtasks.js');
const flow = require('../ui/onboarding_flow.js');

/** A bridge that answers like the real /automations API (actor-scoped). */
function fakeBridge(handlers) {
  const calls = [];
  const store = new Map();
  let seq = 0;
  const snapshot = (w) => ({ ...w });
  const route = handlers || {};
  return {
    calls,
    store,
    request: async ({ method, path, body }) => {
      calls.push({ method, path, body });
      if (route[`${method} ${path}`]) return route[`${method} ${path}`](body);
      if (method === 'GET' && path === '/automations') {
        return { ok: true, status: 200, body: { workflows: [...store.values()].map(snapshot) } };
      }
      if (method === 'POST' && path === '/automations') {
        if (body && (body.grants || body.permissions)) {
          return { ok: false, status: 400, body: { detail: 'an automation cannot carry permissions' } };
        }
        const id = `wf_${++seq}`;
        const wf = {
          workflow_id: id, name: body.name, description: body.description || '',
          steps: body.steps || [], schedule: body.schedule || {}, event: body.event || {},
          trigger_kind: body.schedule && Object.keys(body.schedule).length ? 'schedule'
            : body.event && Object.keys(body.event).length ? 'event' : 'manual',
          enabled: Boolean(body.enabled), actor_id: 'voiceos-local', last_state: null,
        };
        store.set(id, wf);
        return {
          ok: true, status: 200,
          body: { ...snapshot(wf), runs_nothing: 'saved. Nothing has run and no permission was granted; a run is authorised when it happens.' },
        };
      }
      if (method === 'GET' && path.startsWith('/automations/')) {
        const id = decodeURIComponent(path.slice('/automations/'.length));
        const wf = store.get(id);
        if (!wf) return { ok: false, status: 404, body: { detail: 'not found' } };
        return { ok: true, status: 200, body: snapshot(wf) };
      }
      if (method === 'PUT' && path.startsWith('/automations/')) {
        const id = decodeURIComponent(path.slice('/automations/'.length));
        const wf = store.get(id);
        if (!wf) return { ok: false, status: 404, body: { detail: 'not found' } };
        if (body && (body.grants || body.permissions)) {
          return { ok: false, status: 400, body: { detail: 'an automation cannot carry permissions' } };
        }
        Object.assign(wf, { name: body.name, description: body.description, steps: body.steps, schedule: body.schedule, event: body.event, enabled: body.enabled });
        return { ok: true, status: 200, body: snapshot(wf) };
      }
      if (method === 'POST' && path.startsWith('/automations/') && path.endsWith('/enabled')) {
        const id = decodeURIComponent(path.slice('/automations/'.length, -'/enabled'.length));
        const wf = store.get(id);
        if (!wf) return { ok: false, status: 404, body: { detail: 'not found' } };
        wf.enabled = Boolean(body.enabled);
        return { ok: true, status: 200, body: snapshot(wf) };
      }
      if (method === 'POST' && path.startsWith('/automations/') && path.endsWith('/run')) {
        const id = decodeURIComponent(path.slice('/automations/'.length, -'/run'.length));
        const wf = store.get(id);
        if (!wf) return { ok: false, status: 404, body: { detail: 'not found' } };
        // No consent was granted for anything: the honest run is a denial,
        // exactly like the real backend's test fixture.
        wf.last_state = 'denied';
        return { ok: true, status: 200, body: { workflow_id: id, state: 'denied', steps: [{ device_id: 'laptop-1', state: 'denied' }] } };
      }
      if (method === 'DELETE' && path.startsWith('/automations/')) {
        const id = decodeURIComponent(path.slice('/automations/'.length));
        if (!store.delete(id)) return { ok: false, status: 404, body: { detail: 'not found' } };
        return { ok: true, status: 200, body: { deleted: id, runs_kept: true } };
      }
      if (method === 'POST' && path === '/automations/draft') {
        const text = String(body.text || '').toLowerCase();
        const matched = saved.EXAMPLES.find((e) => text.includes(e.text.split(' ').slice(-2).join(' ')));
        if (matched && /check the disk|daily check/.test(text)) {
          const sched = /every day at 9am/.test(text) ? { kind: 'daily', at: '09:00' }
            : /every 30 minutes/.test(text) ? { kind: 'interval', seconds: 1800 } : {};
          const ev = /when a run fails/.test(text)
            ? { event_type: 'execution.completed', state: 'failed' } : {};
          const proposal = {
            name: matched.label, steps: [{ label: 'Search', action: 'search.web', parameters: { query: 'disk' } }],
            ...(Object.keys(sched).length ? { schedule: sched } : {}),
            ...(Object.keys(ev).length ? { event: ev } : {}),
          };
          return {
            ok: true, status: 200,
            body: { name: proposal.name, steps: proposal.steps, schedule: sched, event: ev, source: 'template:disk-check', reason: '', created: false, proposal },
          };
        }
        return {
          ok: true, status: 200,
          body: {
            name: text || 'Untitled', steps: [], schedule: {}, event: {}, source: 'none',
            reason: 'no operator template matched this sentence, so no steps were written',
            created: false,
            proposal: { name: text || 'Untitled', steps: [] },
          },
        };
      }
      return { ok: false, status: 404, body: { detail: `no fake route for ${method} ${path}` } };
    },
  };
}

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------

test('isSaveable: only a draft with steps and a savable proposal', () => {
  assert.equal(saved.isSaveable(null), false);
  assert.equal(saved.isSaveable({ steps: [], proposal: { name: 'x' } }), false);
  assert.equal(saved.isSaveable({ steps: [{ action: 'message.send' }], proposal: { name: 'x', steps: [{ action: 'message.send' }] } }), true);
  assert.equal(saved.isSaveable({ steps: [{ action: 'message.send' }], proposal: { steps: [] } }), false);
});

test('exampleStatus: template source is real, none is not', () => {
  assert.deepEqual(
    saved.exampleStatus({ source: 'template:disk-check', steps: [{}] }),
    { real: true, template: 'disk-check' },
  );
  const none = saved.exampleStatus({ source: 'none', steps: [], reason: 'no template' });
  assert.equal(none.real, false);
  assert.match(none.reason, /no template/);
});

test('taskBody never carries a grant, permission, actor, or risk field', () => {
  const body = saved.taskBody(
    { name: 'n', steps: [{ action_type: 'message.send', parameters: { message: 'hi' } }] },
    { actor_id: 'evil', grants: [{ scope: 'x' }], permissions: ['x'], max_risk: 'critical' },
  );
  for (const banned of ['grants', 'permissions', 'actor_id', 'actor', 'max_risk', 'approval']) {
    assert.equal(banned in body, false, `taskBody must not emit ${banned}`);
  }
  assert.deepEqual(body.steps, [{
    label: undefined, action: 'message.send', parameters: { message: 'hi' }, when: {}, device_id: '', index: 0,
  }]);
});

test('runStateWord never upgrades a waiting or refused run to success', () => {
  assert.match(saved.runStateWord('awaiting_approval'), /approval/);
  assert.match(saved.runStateWord('denied'), /refused/);
  assert.equal(saved.isSuccess({ state: 'awaiting_approval' }), false);
  assert.equal(saved.isSuccess({ state: 'denied' }), false);
  assert.equal(saved.isSuccess({ state: 'succeeded' }), true);
  assert.equal(saved.isSuccess(null), false);
});

test('EXAMPLES all draft through the real route shape (fake bridge)', async () => {
  const bridge = fakeBridge();
  for (const ex of saved.EXAMPLES) {
    const draft = await saved.draftTask(bridge, ex.text);
    assert.equal(draft.source.startsWith('template:'), true, `example "${ex.text}" drafts steps`);
    assert.equal(saved.exampleStatus(draft).real, true);
    assert.equal(saved.isSaveable(draft), true);
  }
});

// ---------------------------------------------------------------------------
// API wiring (fake bridge records exact paths/bodies)
// ---------------------------------------------------------------------------

test('save -> list -> get -> rename -> run -> delete hits the real paths', async () => {
  const bridge = fakeBridge();
  const created = await saved.saveTask(bridge, {
    name: 'nightly check',
    steps: [{ label: 'Search', action: 'search.web', parameters: { query: 'disk' } }],
  });
  assert.match(created.runs_nothing, /Nothing has run/);
  assert.equal(bridge.calls[0].method, 'POST');
  assert.equal(bridge.calls[0].path, '/automations');
  assert.equal('grants' in bridge.calls[0].body, false);

  const list = await saved.listSavedTasks(bridge);
  assert.equal(list.length, 1);
  assert.equal(list[0].name, 'nightly check');

  const got = await saved.getSavedTask(bridge, created.workflow_id);
  assert.equal(got.workflow_id, created.workflow_id);

  const renamed = await saved.renameTask(bridge, created.workflow_id, got, 'morning check');
  assert.equal(renamed.name, 'morning check');
  assert.equal(bridge.calls.at(-1).method, 'PUT');

  // A run with no consent is a denial — surfaced, never tidied into success.
  const run = await saved.runTask(bridge, created.workflow_id);
  assert.equal(run.state, 'denied');
  assert.equal(saved.runStateWord(run.state).includes('succeeded'), false);
  assert.deepEqual(bridge.calls.at(-1).body, {}, 'run posts an empty body: the server stamps the owner');

  const deleted = await saved.deleteTask(bridge, created.workflow_id);
  assert.equal(deleted.runs_kept, true);
  assert.equal(bridge.calls.at(-1).method, 'DELETE');
  assert.equal((await saved.listSavedTasks(bridge)).length, 0);
});

test('draftTask with no matching phrase surfaces the planner reason, with no save offered', async () => {
  const bridge = fakeBridge();
  const draft = await saved.draftTask(bridge, 'do something improbable');
  assert.equal(draft.source, 'none');
  assert.equal(saved.isSaveable(draft), false);
  assert.match(draft.reason, /no operator template/);
  // draftAndSave refuses rather than inventing a task
  await assert.rejects(() => saved.draftAndSave(bridge, 'do something improbable'), /no operator template/);
});

test('draftAndSave saves the planner proposal verbatim', async () => {
  const bridge = fakeBridge();
  const { draft, saved: task } = await saved.draftAndSave(bridge, 'check the disk');
  assert.equal(saved.exampleStatus(draft).real, true);
  assert.equal(task.workflow_id.startsWith('wf_'), true);
  const post = bridge.calls.find((c) => c.method === 'POST' && c.path === '/automations');
  assert.deepEqual(post.body, draft.proposal, 'the saved body is the proposal, not a re-typed plan');
});

test('setTaskEnabled rejects a non-boolean and hits the enabled route', async () => {
  const bridge = fakeBridge();
  const created = await saved.saveTask(bridge, { name: 't', steps: [{ label: 's', action: 'search.web', parameters: { query: 'x' } }] });
  await assert.rejects(() => saved.setTaskEnabled(bridge, created.workflow_id, 'yes'), /must be true or false/);
  const updated = await saved.setTaskEnabled(bridge, created.workflow_id, true);
  assert.equal(updated.enabled, true);
  assert.equal(bridge.calls.at(-1).path, `/automations/${created.workflow_id}/enabled`);
});

// ---------------------------------------------------------------------------
// Renderer invariants
// ---------------------------------------------------------------------------

test('savedtasks.js has no innerHTML, no storage, no fetch, no grants', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'ui', 'savedtasks.js'), 'utf8');
  new Function(source);
  const stripped = source.replace(/^\s*\/\/.*$/gm, '');
  assert.equal(/\.innerHTML\s*=/.test(stripped), false, 'nothing is assigned to innerHTML');
  for (const banned of ['localStorage', 'sessionStorage', 'fetch(', 'XMLHttpRequest']) {
    assert.equal(source.includes(banned), false, `savedtasks.js does not use ${banned}`);
  }
  // The word "grants" may appear only in comments explaining it is NOT sent.
  const codeOnly = source.split('\n').filter((l) => !l.trim().startsWith('*') && !l.trim().startsWith('//')).join('\n');
  assert.equal(/grants\s*:/.test(codeOnly), false, 'no body ever carries a grants field');
});

test('renderSavedTaskList paints into the existing automations hosts', async () => {
  const bridge = fakeBridge();
  await saved.saveTask(bridge, { name: 'first task', steps: [{ label: 'Search', action: 'search.web', parameters: { query: 'x' } }] });
  const page = install({ bridge });
  const tasks = await saved.renderSavedTaskList(bridge, {
    hosts: {
      automationList: page.document.getElementById('automationList'),
      automationCount: page.document.getElementById('automationCount'),
      automationNotes: page.document.getElementById('automationNotes'),
    },
  });
  assert.equal(tasks.length, 1);
  assert.equal(page.document.getElementById('automationCount').textContent, '1');
  assert.match(page.document.getElementById('automationList').textContent, /first task/);
  assert.equal(page.document.__innerHTMLWrites.length, 0, 'list is built with textContent only');
});

test('renderDraft offers Save only for a real draft; a "none" draft explains itself', () => {
  const page = install({ bridge: fakeBridge() });
  const host = page.document.createElement('div');
  page.document.body.append(host);

  saved.renderDraft(host, {
    name: 'mystery', steps: [], source: 'none',
    reason: 'no operator template matched this sentence, so no steps were written',
    proposal: { name: 'mystery', steps: [] },
  }, fakeBridge());
  assert.match(host.textContent, /no operator template/);
  assert.equal(host.querySelectorAll('button').length, 0, 'no save button on an empty draft (no dead control)');
  assert.equal(page.document.__innerHTMLWrites.length, 0);

  // and the real one does offer a working save button
  const host2 = page.document.createElement('div');
  page.document.body.append(host2);
  saved.renderDraft(host2, {
    name: 'disk', steps: [{ label: 'Search', action: 'search.web' }],
    source: 'template:disk-check', reason: '',
    proposal: { name: 'disk', steps: [{ label: 'Search', action: 'search.web' }] },
  }, fakeBridge());
  const buttons = host2.querySelectorAll('button');
  assert.equal(buttons.length, 1);
  assert.match(buttons[0].textContent, /Save/);
  // The card must say what saving does before it is pressed. The body it posts is
  // the planner's proposal, which carries enabled:false, so "stored disarmed" is a
  // fact about the request, not a reassurance.
  assert.match(host2.textContent, /stores the task disarmed/i);
  assert.equal(page.document.__innerHTMLWrites.length, 0);
});

test('a draft says it is saved disarmed — matching the proposal the button posts', () => {
  const page = install({ bridge: fakeBridge() });
  const host = page.document.createElement('div');
  page.document.body.append(host);
  const proposal = { name: 'disk', steps: [{ label: 'Search', action: 'search.web' }], enabled: false };
  saved.renderDraft(host, {
    name: 'disk', steps: proposal.steps, source: 'template:disk-check', reason: '', proposal,
  }, fakeBridge());
  assert.match(host.textContent, /no schedule or event starts it until you arm it/i);
  assert.equal(proposal.enabled, false);
});

test('renderSavedTaskDetail exposes rename, run, arm and delete — all wired', async () => {
  const bridge = fakeBridge();
  const created = await saved.saveTask(bridge, {
    name: 'detail task', steps: [{ label: 'Search', action: 'search.web', parameters: { query: 'x' } }],
  });
  const page = install({ bridge });
  const host = page.document.createElement('div');
  page.document.body.append(host);
  let changed = null;
  saved.renderSavedTaskDetail(host, created, bridge, { onChanged: (u) => { changed = u; } });
  assert.match(host.textContent, /detail task/);
  const labels = host.querySelectorAll('button').map((b) => b.textContent);
  for (const want of ['Save changes', 'Run now', 'Arm', 'Delete task']) {
    assert.ok(labels.includes(want), `detail offers "${want}"`);
  }
  // Click Run now -> the real run route, and the honest denial is shown.
  const runButton = host.querySelectorAll('button').find((b) => b.textContent === 'Run now');
  runButton.click();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.ok(bridge.calls.some((c) => c.method === 'POST' && c.path.endsWith('/run')));
  assert.match(host.textContent, /refused/);
  assert.equal(page.document.__innerHTMLWrites.length, 0);
});

// ---------------------------------------------------------------------------
// First-run flow (onboarding_flow.js)
// ---------------------------------------------------------------------------

test('first-run flow has exactly the six mandated steps', () => {
  assert.deepEqual(
    flow.STEPS.map((s) => s.id),
    ['welcome', 'first-task', 'see-plan', 'approve-run', 'history', 'done'],
  );
  assert.equal(flow.nextStepId('welcome'), 'first-task');
  assert.equal(flow.nextStepId('done'), null);
});

test('first-run isSaveable mirrors the saved-tasks rule', () => {
  assert.equal(flow.isSaveable(null), false);
  assert.equal(flow.isSaveable({ steps: [], proposal: { name: 'x' } }), false);
  assert.match(flow.saveableReason({ steps: [] }), /no steps/);
  assert.equal(flow.isSaveable({ steps: [{ action: 'message.send' }], proposal: { name: 'x', steps: [{ action: 'message.send' }] } }), true);
});

test('renderFirstRun mounts without bridge I/O and only writes on complete()', async () => {
  const calls = [];
  const bridge = {
    request: async (req) => { calls.push(req); return { ok: true, status: 200, body: {} }; },
    updateSettings: async (patch) => { calls.push(['updateSettings', patch]); return { ok: true }; },
  };
  const page = install({ bridge });
  const host = page.document.createElement('div');
  page.document.body.append(host);
  const controller = flow.renderFirstRun(host, bridge, { initiallyCompleted: false });
  assert.ok(controller, 'mount returns a controller');
  assert.equal(controller.currentStep(), 'welcome');
  assert.equal(calls.length, 0, 'mount performs no bridge I/O and no writes');
  assert.equal(host.querySelectorAll('.first-run-step').length, 6);
  assert.match(host.textContent, /proposes, policy decides/);

  // skip() walks the real steps, forward only, never past done
  assert.equal(controller.skip(), true);
  assert.equal(controller.currentStep(), 'first-task');
  assert.equal(controller.goTo('approve-run'), true);
  assert.equal(controller.currentStep(), 'approve-run');
  assert.equal(controller.goTo('nope'), false);

  // complete() is the only write, and it writes exactly the existing key
  const before = calls.length;
  await controller.complete();
  const writes = calls.slice(before).filter((c) => Array.isArray(c));
  assert.equal(writes.length, 1);
  assert.deepEqual(writes[0][1], { onboardingCompleted: true });
  assert.equal(host.querySelector('#firstRunRoot').hidden, true);
});

test('first-run drafts a real plan and drives save+run through the bridge', async () => {
  const bridge = fakeBridge();
  const page = install({ bridge });
  const host = page.document.createElement('div');
  page.document.body.append(host);
  const navigated = [];
  const controller = flow.renderFirstRun(host, bridge, {
    navigate: (view) => navigated.push(view),
  });

  // Step 2: Draft my task (prefilled with the safe suggestion)
  assert.equal(controller.goTo('first-task'), true);
  const draftButton = host.querySelectorAll('button').find((b) => b.textContent === 'Draft my task');
  assert.ok(draftButton, 'draft button exists on the first-task step');
  draftButton.click();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.ok(bridge.calls.some((c) => c.path === '/automations/draft'), 'a real draft request was made');
  assert.ok(controller.draftState, 'draft state is held');
  assert.equal(controller.currentStep(), 'see-plan');

  // Step 4 (approve-run): Save & run is enabled for a real draft
  assert.equal(controller.goTo('approve-run'), true);
  const saveRun = host.querySelectorAll('button').find((b) => b.textContent === 'Save & run');
  assert.ok(saveRun, 'save & run exists');
  assert.equal(saveRun.disabled, false);
  saveRun.click();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.ok(bridge.calls.some((c) => c.method === 'POST' && c.path === '/automations'), 'saved via the real route');
  assert.ok(bridge.calls.some((c) => c.path.endsWith('/run')), 'ran via the real route');
  assert.ok(controller.savedTask, 'saved task is held');
  assert.equal(controller.lastRun.state, 'denied', 'run was denied (no consent) — the honest outcome');
  assert.match(host.textContent, /refused/);
  assert.match(host.textContent, /Open approvals/);

  // Step 5: history navigates for real
  assert.equal(controller.goTo('history'), true);
  const openHistory = host.querySelectorAll('button').find((b) => b.textContent === 'Open history');
  openHistory.click();
  assert.deepEqual(navigated, ['history']);
  assert.equal(page.document.__innerHTMLWrites.length, 0);
});

test('first-run with an un-draftable sentence refuses to offer Save & run', async () => {
  const bridge = fakeBridge();
  const page = install({ bridge });
  const host = page.document.createElement('div');
  page.document.body.append(host);
  const controller = flow.renderFirstRun(host, bridge, {});
  assert.equal(controller.goTo('approve-run'), true);
  const saveRun = host.querySelectorAll('button').find((b) => b.textContent === 'Save & run');
  assert.equal(saveRun.disabled, true, 'save & run is disabled until a real plan exists');
  assert.match(host.textContent, /no draft yet/);
});
