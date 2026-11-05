'use strict';
/**
 * Live GUI-path acceptance driver (DOM harness + real backend).
 *
 * NOT a graphical inspection. It runs the page's own nine scripts (index.html
 * order) against the harness document, with a bridge whose `request` forwards to
 * a REAL ActionMesh backend over HTTP. It then drives the five journeys through
 * the same functions the page's controls call, and ASSERTS on what the page
 * rendered. What it proves: script wiring, navigation, and that each module's own
 * code path works against the live engine, and what it puts on screen.
 *
 * What it does not prove: pixels, layout, or that the Electron window shows this.
 * That is BLOCKED here (no display tooling); it is never reported as live.
 *
 * Safety: harmless registry operations only. No payment, no financial action.
 *
 *   node test/live_gui_path.js            # all journeys
 *   node test/live_gui_path.js --json     # machine-readable result
 */
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const { install } = require('./dom_harness.js');

const UI_DIR = path.resolve(__dirname, '..', 'ui');
const HOST = '127.0.0.1';
const PORT = Number(process.env.AM_PORT || 8000);
const INTENT = process.env.AM_INTENT || 'read this file note.txt';
const JSON_OUT = process.argv.includes('--json');

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok: Boolean(ok), detail: detail === undefined ? '' : String(detail) });
  if (!JSON_OUT) console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? ` — ${detail}` : ''}`);
}

function httpRequest({ method, path: reqPath, body, long }) {
  return new Promise((resolve) => {
    const payload = body === undefined || body === null ? null : JSON.stringify(body);
    const req = http.request(
      {
        host: HOST,
        port: PORT,
        path: reqPath,
        method: method || 'GET',
        headers: Object.assign(
          { Accept: 'application/json' },
          payload ? { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload) } : {},
        ),
        timeout: long ? 60000 : 20000,
      },
      (res) => {
        const chunks = [];
        res.on('data', (c) => chunks.push(c));
        res.on('end', () => {
          const text = Buffer.concat(chunks).toString('utf8');
          let parsed = null;
          try { parsed = text ? JSON.parse(text) : null; } catch { parsed = null; }
          resolve({ ok: res.statusCode >= 200 && res.statusCode < 300, status: res.statusCode, body: parsed, text });
        });
      },
    );
    req.on('error', (error) => resolve({ ok: false, status: 0, error: error.message }));
    req.on('timeout', () => { req.destroy(new Error('request timed out')); });
    if (payload) req.write(payload);
    req.end();
  });
}

/**
 * The app state snapshot the page's own preload bridge reports. `request` is
 * the only field that touches the network, so it is the only one overridden to
 * be live; everything else is answered from a fixed, honest snapshot.
 */
function liveBridge(api) {
  return {
    state: async () => ({
      backend: { status: 'ready', port: PORT },
      version: 'phase6-acceptance',
      settings: { shortcut: 'CommandOrControl+Shift+Space' },
    }),
    providerStatus: async () => ({ lanes: [] }),
    purposeStatus: async () => ({ lanes: [] }),
    onStreamEvent: () => () => {},
    onStreamStatus: () => () => {},
    onBackendChanged: () => () => {},
    onNavigate: () => () => {},
    onBrowserEvent: () => () => {},
    request: api,
  };
}

function loadPage() {
  const page = install({ bridge: liveBridge(httpRequest) });
  const context = vm.createContext(page.window);
  vm.runInContext('globalThis.window = globalThis; globalThis.self = globalThis;', context);
  const html = fs.readFileSync(path.join(UI_DIR, 'index.html'), 'utf8');
  const order = [...html.matchAll(/<script\s+src="([^"]+)"><\/script>/g)].map((m) => m[1]);
  for (const file of order) {
    vm.runInContext(fs.readFileSync(path.join(UI_DIR, file), 'utf8'), context, { filename: file });
  }
  return { page, context, order };
}

/** Flush pending microtasks and the harness's recorded timers a bounded number of times. */
async function settle(runTimers, rounds = 5) {
  for (let i = 0; i < rounds; i += 1) {
    await new Promise((r) => setImmediate(r));
    try { runTimers(); } catch { /* a timer that throws is the page's, not ours */ }
  }
}

function textOf(node) {
  if (!node) return '';
  if (typeof node.textContent === 'string' && node.textContent) return node.textContent;
  return (node.childNodes || []).map(textOf).join('');
}

const flat = (node) => textOf(node).replace(/\s+/g, ' ').trim();

/**
 * Wait for a predicate to hold, flushing the page's recorded timers between polls.
 * Network round trips are real here, so a fixed number of `settle` rounds is a guess;
 * this polls on the driver's own clock instead until the page says it is done.
 */
async function waitUntil(runTimers, predicate, timeoutMs = 6000) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    await settle(runTimers, 1);
    let ok = false;
    try { ok = Boolean(predicate()); } catch { ok = false; }
    if (ok) return true;
    if (Date.now() > deadline) return false;
    await new Promise((r) => setTimeout(r, 40));
  }
}

/**
 * Depth-first search for the first element whose own text is exactly `label`.
 * A wrapper (a `.btn-row` around one button) has the same flattened text as the
 * button inside it, so a tag filter is offered to pick out the control itself.
 */
function findByText(root, label, tag) {
  for (const child of root.childNodes || []) {
    if (child.tagName && flat(child) === label && (!tag || child.tagName === tag)) return child;
    const found = findByText(child, label, tag);
    if (found) return found;
  }
  return null;
}

/**
 * Use the page's own "Standing permissions" control — the card in the Approvals view
 * — to grant one scope to the page's own actor. This is deliberately not a curl: the
 * question being answered is "can a user fix a refusal from the interface", and a
 * consent written behind the page would answer a different one.
 */
async function grantStanding(win, doc, runTimers, scope, risk) {
  const app = win.ActionMeshApp;
  app.showView('approvals');
  await waitUntil(runTimers, () => flat(doc.getElementById('consentCount')) !== 'Load…', 4000);
  const field = doc.getElementById('consentScope');
  const select = doc.getElementById('consentRisk');
  const button = doc.getElementById('consentGrant');
  if (!field || !button) return { ok: false, reason: 'the consent controls are not on the page' };
  field.value = scope;
  if (select) select.value = risk;
  const handlers = button.listeners.click || [];
  if (handlers.length !== 1) return { ok: false, reason: `${handlers.length} handler(s) on Grant` };
  await handlers[0]({ type: 'click', target: button, currentTarget: button, preventDefault() {} });
  const listed = await waitUntil(runTimers, () => flat(doc.getElementById('consentList')).includes(scope), 6000);
  return { ok: listed, reason: listed ? '' : flat(doc.getElementById('consentList')).slice(0, 160) };
}

/**
 * Remove a standing permission through the page's own Remove control. Returns true
 * when the scope is no longer listed — matched against the SERVER's list, because
 * the page's list is re-rendered asynchronously and a stale row would let the next
 * step inherit a grant this driver was supposed to have cleared.
 */
async function revokeStanding(win, doc, runTimers, scope) {
  const serverHasScope = async () => {
    const live = await httpRequest({
      method: 'GET',
      path: `/consents?actor_id=${encodeURIComponent('voiceos-local')}`,
    });
    return (live.body || []).some((c) => c.scope === scope);
  };
  /** Has the list finished a render? An empty list is not the same as a pending one. */
  const rendered = () => {
    const text = flat(doc.getElementById('consentList'));
    return text.includes(scope)
      || /No standing permissions/i.test(text)
      || /Could not read permissions/i.test(text)
      || /Backend offline/i.test(text);
  };
  win.ActionMeshApp.showView('approvals');
  // `showView` kicks off an async refresh that first CLEARS the host, so a list that
  // is merely empty for a moment is not a list that finished loading. Wait for a
  // render that says something definite.
  const sawRender = await waitUntil(runTimers, rendered, 8000);
  const row = (doc.getElementById('consentList').childNodes || [])
    .find((node) => node.dataset && flat(node).startsWith(scope));
  if (!row) return sawRender && !(await serverHasScope());
  const remove = findByText(row, 'Remove', 'BUTTON');
  if (!remove) return false;
  await (remove.listeners.click || [])[0]({ type: 'click', target: remove, currentTarget: remove, preventDefault() {} });
  for (let i = 0; i < 80; i += 1) {
    await settle(runTimers, 1);
    if (!(await serverHasScope())) return true;
    await new Promise((r) => setTimeout(r, 50));
  }
  return false;
}

async function main() {
  const { page, context, order } = loadPage();
  const doc = page.document;
  const win = context.window || context;
  const tick = () => settle(page.runTimers);

  // --- M1: the page loads and boots ---------------------------------------
  check('index.html declares all nine scripts, app.js last',
    order.length === 9 && order[order.length - 1] === 'app.js', order.join(' → '));

  const boot = await vm.runInContext('boot()', context);
  await tick();
  check('boot() reaches the shell (no refusal artefact)',
    !(boot && boot.ok === false) && doc.querySelector('.boot-refusal') === null,
    boot && boot.missing ? JSON.stringify(boot.missing) : 'no refusal');

  // Backend snapshot must be applied before anything calls requireBackend().
  const app = win.ActionMeshApp;
  check('ActionMeshApp navigation surface is exported', app && typeof app.showView === 'function');

  // --- Standing permissions: the page cannot manufacture authority --------
  // The Approvals view hosts the user's own control for standing permissions. It is
  // checked here, before the workbench runs, because it is the control the refusal
  // below will be resolved with — and because a page that could grant itself
  // authority silently would make every later "success" meaningless.
  app.showView('approvals');
  check('the Approvals view offers the standing-permissions control',
    Boolean(doc.getElementById('consentGrant')), flat(doc.getElementById('consentList')).slice(0, 120));

  // This driver must be re-runnable: a repeat run that inherited last run's grants
  // would skip the refusal it exists to prove. Nothing is left behind either way.
  const cleanStart = await revokeStanding(win, doc, page.runTimers, 'file.read');
  check('the driver starts from no standing permission for the scope it tests', cleanStart);
  app.showView('workbench');

  // --- M2 / Journey B: Workbench ------------------------------------------
  app.showView('workbench');
  check('Workbench is reachable from navigation',
    doc.querySelector('section.view[data-view="workbench"]').hidden === false);

  const wb = win.ActionMeshWorkbench;
  check('Workbench published its surface',
    wb && typeof wb.submitInstruction === 'function' && typeof wb.runExecution === 'function');

  // Type into the field and press the button a person presses. Calling
  // `wb.submitInstruction` would prove the module and skip the binding, and the
  // binding is exactly what §9 asks about.
  const askText = doc.getElementById('wbAskText');
  const askSend = doc.getElementById('wbAskSend');
  check('the Workbench offers a text field and a send control',
    Boolean(askText) && Boolean(askSend), `${Boolean(askText)}/${Boolean(askSend)}`);
  check('the send control has exactly one handler bound (no duplicate wiring)',
    (askSend.listeners.click || []).length === 1,
    `${(askSend.listeners.click || []).length} handler(s)`);
  askText.value = INTENT;
  await (askSend.listeners.click || [])[0]({
    type: 'click', target: askSend, currentTarget: askSend, preventDefault() {},
  });
  // The click starts an async plan request; wait for the card, not a timer count.
  const planShown = await waitUntil(
    page.runTimers, () => doc.getElementById('wbPlan') && doc.getElementById('wbPlan').hidden === false, 8000);
  await tick();
  const planText = flat(doc.getElementById('wbPlanList'));
  const planHost = doc.getElementById('wbPlan');
  check('pressing Send produces a real plan',
    planShown && /device\.file\.read/.test(planText), planText.slice(0, 160));
  check('the plan card is shown, not hidden', Boolean(planHost) && planHost.hidden === false);
  check('the rendered plan names the real action type',
    /device\.file\.read/.test(planText), planText.slice(0, 120));
  check('the rendered plan lists the permission it will need',
    /file\.read/.test(planText), planText.slice(0, 200));
  check('the rendered plan is honest that permission is decided at execute',
    /decided at execute/.test(planText));

  // Press the Execute control a person presses. `runExecution` returns the record
  // and the click does not, so the click's outcome is read back from the rendered
  // card — which is the thing on screen, and therefore the stronger assertion.
  const execBtn = doc.getElementById('wbExecute');
  check('the superuser\'s own Execute control is present and enabled for a planned task',
    Boolean(execBtn) && execBtn.disabled === false, execBtn ? `disabled=${execBtn.disabled}` : 'missing');
  check('the Execute control has exactly one handler bound (no duplicate wiring)',
    (execBtn.listeners.click || []).length === 1,
    `${(execBtn.listeners.click || []).length} handler(s)`);
  await (execBtn.listeners.click || [])[0]({
    type: 'click', target: execBtn, currentTarget: execBtn, preventDefault() {},
  });
  const refusedRendered = await waitUntil(
    page.runTimers, () => /denied/i.test(flat(doc.getElementById('wbResult'))), 10000);
  await tick(2);
  // Two runs are compared, and the difference between them is the point. The first
  // asks the engine to do work nobody has granted; the second asks the page's own
  // consent control for the permission and runs again. If the first had succeeded,
  // the consent store would not be deciding anything — so the refusal is asserted,
  // not tolerated, and the success below is only meaningful because of it.
  const refusedState = /button|denied/i.test(flat(doc.getElementById('wbStatus'))) ? 'denied' : '';
  const refusedText = flat(doc.getElementById('wbResult'));
  check('an ungranted action is refused, not run', refusedRendered, refusedText.slice(0, 200));
  check('the refusal names the missing permission rather than blaming the machine',
    /file\.read/.test(refusedText) || /not authorized|missing permission/i.test(refusedText),
    refusedText.slice(0, 200));
  check('the refused run is not rendered as verified success',
    !/\bSucceeded and verified\b/.test(flat(doc.getElementById('wbStatus'))), refusedState);
  // A refusal must not carry a verified badge anywhere: if the card says the run
  // was verified, invariant 3 has been broken even though the state is "denied".
  check('the refused run is not reported as verified',
    !/\bverified\b/i.test(flat(doc.getElementById('wbResult')).replace(/not recorded|not verified/gi, '')),
    refusedText.slice(0, 200));

  // Now resolve it through the user's own control, then run the same plan again.
  const grantFile = await grantStanding(win, doc, page.runTimers, 'file.read', 'high');
  check('the refusal is resolved by granting the permission from the Approvals view',
    grantFile.ok, grantFile.reason);
  const consentListed = flat(doc.getElementById('consentList'));
  check('the granted scope is listed with the risk it covers',
    consentListed.includes('file.read') && /up to high-risk/i.test(consentListed),
    consentListed.slice(0, 160));

  app.showView('workbench');
  // Same control, same path as the refused run above — only the consent changed.
  const execBtn2 = doc.getElementById('wbExecute');
  await (execBtn2.listeners.click || [])[0]({
    type: 'click', target: execBtn2, currentTarget: execBtn2, preventDefault() {},
  });
  const succeeded = await waitUntil(
    page.runTimers, () => /\bsuccess\b/i.test(flat(doc.getElementById('wbResult')))
      || /\bSucceeded and verified\b/.test(flat(doc.getElementById('wbStatus'))), 15000);
  await tick(2);
  // The click returns nothing; the module's own record does.
  const exec = (wb.workbench && wb.workbench.result) || {};
  const execState = String(exec.state || '');
  const resultText = flat(doc.getElementById('wbResult'));
  check('pressing Execute on a granted plan reaches a terminal state',
    succeeded && ['success', 'failed', 'denied', 'partial'].includes(execState), `${execState} / ${succeeded}`);

  if (execState === 'success') {
    check('the run reports simulated=false (real, not a mock)',
      exec.simulated === false, `simulated=${JSON.stringify(exec.simulated)}`);
    // The card prints a verification badge and, separately, an "unverified" one for
    // runs that were not. Match the badge that stands alone, so this cannot pass on
    // the word "unverified" or on "not verified".
    const statusText = flat(doc.getElementById('wbStatus'));
    check('the rendered result is marked verified, not merely un-flagged',
      /\bverified\b/i.test(resultText) && !/\bunverified\b/i.test(resultText) && !/\bnot verified\b/i.test(resultText),
      `${resultText.slice(0, 140)} | status: ${statusText}`);
    check('the rendered result does NOT claim a mock provider', !/mock provider/i.test(resultText));
    check('the result card is shown', doc.getElementById('wbResult').hidden === false);
    check('file CONTENT never crosses the result boundary (invariant 4)',
      !/[A-Za-z0-9]{20,}/.test(JSON.stringify(exec.result || {}).replace(/"(path|idem_[^"]*)":"[^"]*"/g, '')),
      JSON.stringify(exec.result));
  } else {
    check('a non-success state is not rendered as success',
      !/\bSucceeded and verified\b/.test(flat(doc.getElementById('wbStatus'))), execState);
  }

  // --- Journey A: Dashboard / Activity ------------------------------------
  app.showView('home');
  const activity = win.ActionMeshActivity;
  const dash = await activity.loadDashboard();
  // Home is painted by app.js's own refreshHome (the code path the nav triggers),
  // so drive that rather than the module's standalone renderer.
  await vm.runInContext('refreshHome()', context);
  await tick(2);
  check('Activity reads the live dashboard without going offline', dash.offline === false);
  check('Activity sees a healthy audit chain', dash.audit && dash.audit.chain && dash.audit.chain.ok);
  const auditChipText = flat(doc.getElementById('homeAuditChain'));
  const auditHeadText = flat(doc.getElementById('homeAuditHead'));
  const auditLogText = flat(doc.getElementById('homeAuditLog'));
  check('Activity reports the audit chain as verified', /chain verified/i.test(auditChipText), auditChipText);
  check('Activity shows how many audit records the ledger holds',
    /\d+ record/.test(auditHeadText), auditHeadText);
  check('Activity lists audit records on screen', auditLogText.length > 0, auditLogText.slice(0, 200));
  check('the audit list names the execution that just ran',
    auditLogText.includes(String(exec.execution_id || '')), String(exec.execution_id || ''));

  // --- Journey D: Connections --------------------------------------------
  app.showView('connections');
  const conn = win.actionmeshConnections;
  await conn.loadConnections();
  await tick();
  const connText = flat(doc.getElementById('connectionsCenter'));
  check('Connections reports the live backend as running', /Backend:\s*running/i.test(connText),
    connText.slice(0, 100));
  check('Connections does not claim a browser or media it does not have',
    !/Browser:\s*(Connected|Running|Ready)/i.test(connText),
    connText.slice(0, 200));

  // --- Standing permissions, granted from the page's own control ----------
  // The saved task below needs `device.file.list` — a different scope from the one
  // the workbench used, because the action is the operation and the scope is what the
  // user consented to. Granting it here also proves the control works more than once
  // and that two grants coexist rather than replacing one another.
  const grantSecond = await grantStanding(win, doc, page.runTimers, 'device.file.list', 'high');
  check('a second standing permission can be granted from the same control',
    grantSecond.ok, grantSecond.reason);
  const consentListed2 = flat(doc.getElementById('consentList'));
  check('both grants are listed, each naming its own scope',
    consentListed2.includes('device.file.list') && consentListed2.includes('file.read'),
    consentListed2.slice(0, 200));

  // --- Journey E: Saved Tasks — draft, save, reopen, run ----------------
  // The backend must be started with an operator-authored template
  // (ACTIONMESH_VOICEOS_TASKS) whose match phrase is "list the workspace files";
  // without one the draft correctly comes back with zero steps, and this journey
  // cannot reach "save". The tag makes each run its own automation, so "reopen"
  // really means "read it back from the store" rather than "the row was already
  // there".
  app.showView('automations');
  await vm.runInContext('refreshAutomations()', context);
  await tick();
  const listText = flat(doc.getElementById('automationList'));
  check('the Saved Tasks / Automations journey renders its list',
    listText.length > 0 && !/Loading…/.test(listText), listText.slice(0, 120));

  const tag = `acceptance ${Date.now().toString(36)}`;
  const sentence = `list the workspace files every day at 9am ${tag}`;
  const automationText = doc.getElementById('automationText');
  automationText.value = sentence;
  await vm.runInContext('draftAutomation()', context);
  await tick(3);
  const draftHost = doc.getElementById('automationDraft');
  const draftText = flat(draftHost);
  check('drafting a saved task reaches the real planner and renders',
    draftHost && draftHost.hidden === false, draftText.slice(0, 160));
  check('the draft is marked as not yet saved', /not saved/i.test(draftText), draftText.slice(0, 80));
  check('the draft carries a real step from an operator template',
    /device\.files\.list/.test(draftText), draftText.slice(0, 200));

  const saveDraftButton = findByText(draftHost, 'Save this plan', 'BUTTON');
  check('the draft offers a Save control', Boolean(saveDraftButton));
  if (saveDraftButton) {
    await saveDraftButton.listeners.click[0]({ type: 'click', target: saveDraftButton });
    await tick(4);
  }

  const savedListText = flat(doc.getElementById('automationList'));
  check('saving stores the plan and it appears in the library',
    savedListText.includes(tag), savedListText.slice(0, 220));

  // Reopen: read the library again from the server, then select the row the way a
  // click does, so the detail card is built by the page's own renderer.
  await vm.runInContext('refreshAutomations()', context);
  await tick(2);
  const rows = doc.getElementById('automationList').childNodes.filter((n) => n.dataset && n.dataset.id);
  check('the saved task is a selectable row after a fresh read',
    rows.some((row) => flat(row).includes(tag)), `${rows.length} row(s)`);
  let detailText = '';
  const mine = rows.find((row) => flat(row).includes(tag));
  if (mine) {
    await mine.listeners.click[0]({ type: 'click', target: mine });
    await tick();
    detailText = flat(doc.getElementById('automationSelected'));
  }
  check('reopening a saved task shows the stored plan', /List the workspace directory/.test(detailText),
    detailText.slice(0, 200));
  check('the saved task is stored disarmed (saving starts nothing)', /disarmed/.test(detailText),
    detailText.slice(0, 120));
  // "Disarmed" is not decoration: the stored plan carries a schedule, and the only
  // thing keeping it from firing at 09:00 unasked is that the trigger is off. The
  // page must say so in the same breath, and no due time may exist while it is off.
  check('a disarmed scheduled plan has no due time and says why',
    !/Next run:/.test(detailText) && /no schedule or event will start it/i.test(detailText),
    detailText.slice(0, 240));

  const runNow = findByText(doc.getElementById('automationSelected'), 'Run now', 'BUTTON');
  check('the opened task offers Run now', Boolean(runNow));
  if (runNow) {
    await runNow.listeners.click[0]({ type: 'click', target: runNow });
    await tick(5);
    const rerunDetail = flat(doc.getElementById('automationSelected'));
    const toastText = flat(doc.getElementById('toast'));
    check('running a saved task reports an honest terminal state',
      /Last run .*succeeded/i.test(rerunDetail) || /Run finished: .*succeeded/i.test(toastText),
      `${rerunDetail.slice(0, 160)} | toast: ${toastText}`);
    check('a finished run is not reported as still awaiting approval',
      !/awaiting approval/i.test(`${rerunDetail} ${toastText}`), rerunDetail.slice(0, 200));
  }

  // --- Journey C: VoiceOS (typed text, the primary interface) -------------
  app.showView('voice');
  const transcript = doc.getElementById('voiceTranscript');
  const send = doc.getElementById('voiceSend');
  check('Voice is reachable and has a typed-text path',
    transcript && send && doc.querySelector('section.view[data-view="voice"]').hidden === false);

  transcript.value = 'read this file note.txt';
  // Fire the exact handler the page bound to the Send button and await the promise
  // it returns. The harness's dispatch() drops return values, and the real request
  // is genuine network I/O, so a fixed setImmediate budget is not a reliable wait.
  const sendHandlers = send.listeners.click || [];
  check('the Send button has exactly one handler bound (no duplicate wiring)',
    sendHandlers.length === 1, `${sendHandlers.length} handler(s)`);
  const sendResult = await sendHandlers[0]({ type: 'click', target: send, preventDefault() {} });
  await tick(4);
  check('the Send handler reports the turn was sent', sendResult === true, String(sendResult));
  const reply = doc.getElementById('voiceReply');
  const replyText = flat(reply);
  check('typed VoiceOS text reaches /voiceos/messages and renders a reply',
    reply && reply.hidden === false && replyText.length > 0, replyText.slice(0, 200));
  check('the VoiceOS reply reports its real kind', /VoiceOS · \S+/.test(replyText), replyText.slice(0, 80));

  // --- The guided tour (onboarding_flow.js) ------------------------------
  // It was loaded by index.html and never called: the module and its markup both
  // existed, but nothing opened it. Pressing the Settings control is the whole
  // proof that the module is wired, and mounting performs no I/O, so this cannot
  // disturb the journeys above.
  app.showView('settings');
  const tourMount = doc.getElementById('firstRunMount');
  const tourOpen = doc.getElementById('firstRunOpen');
  check('Settings offers a control that opens the guided tour', Boolean(tourOpen));
  if (tourOpen) {
    await tourOpen.listeners.click[0]({ type: 'click', target: tourOpen, preventDefault() {} });
    await tick();
    const tourText = flat(tourMount);
    check('the guided tour opens on its own host',
      tourMount.hidden === false && /Welcome/.test(tourText), tourText.slice(0, 160));
    check('the tour grants nothing and can be closed at any step',
      /grants nothing/i.test(tourText) && Boolean(doc.getElementById('firstRunRoot')));
  }

  // --- M4: every journey is reachable from the main navigation -----------
  for (const view of ['home', 'workbench', 'voice', 'connections', 'automations']) {
    const nav = doc.querySelector(`.nav-item[data-view="${view}"]`);
    check(`nav entry exists for "${view}"`, Boolean(nav));
  }

  // --- Report -------------------------------------------------------------
  // Clean up after the journey: a withdrawal exercised through the page's own Remove
  // control, so a second run of the Saved Tasks journey (which needs this scope) is
  // not silently riding on a grant the first run left behind.
  win.ActionMeshApp.showView('approvals');
  const cleaned = await revokeStanding(win, doc, page.runTimers, 'device.file.list');
  check('a standing permission can be withdrawn from the page, and the scope is gone',
    cleaned, flat(doc.getElementById('consentList')).slice(0, 160));

  const failed = results.filter((r) => !r.ok);
  if (JSON_OUT) {
    console.log(JSON.stringify({ passed: results.length - failed.length, failed: failed.length, results }, null, 2));
  } else {
    console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
  }
  process.exit(failed.length === 0 ? 0 : 1);
}

main().catch((error) => {
  console.error('DRIVER FAILED:', error && error.stack ? error.stack : error);
  process.exit(2);
});