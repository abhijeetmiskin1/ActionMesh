"""Small dependency-free C15 dashboard page."""

from __future__ import annotations

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#153b37">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="ActionMesh">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="apple-touch-icon" href="/icon.svg">
<link rel="icon" href="/icon.svg" type="image/svg+xml">
<title>ActionMesh Control Room</title>
<style>
:root { color-scheme: light; font-family: "Trebuchet MS", "Segoe UI", sans-serif; --ink: #17302d; --muted: #6d7f7c; --paper: #f4f7f5; --panel: #ffffff; --line: #dce7e3; --teal: #147d70; --teal-soft: #e1f3ee; --coral: #d66b58; --nav: #153b37; }
* { box-sizing: border-box; }
body { background: var(--paper); color: var(--ink); margin: 0; }
.shell { display: grid; grid-template-columns: 232px minmax(0, 1fr); min-height: 100vh; }
.rail { background: var(--nav); color: #e4f2ed; display: flex; flex-direction: column; padding: 26px 18px; }
.brand { align-items: center; display: flex; gap: 10px; font-size: 1.05rem; font-weight: 700; letter-spacing: .02em; }
.brand-mark { align-items: center; background: #a8e4d5; border-radius: 8px; color: var(--nav); display: flex; font-size: .8rem; height: 30px; justify-content: center; width: 30px; }
.rail-label { color: #8db9ad; font-size: .7rem; letter-spacing: .12em; margin: 52px 10px 12px; text-transform: uppercase; }
.nav-item { background: transparent; border: 0; border-radius: 7px; color: #c0dcd4; cursor: pointer; display: block; font-size: .9rem; margin: 3px 0; padding: 11px 12px; text-align: left; }
.nav-item:hover, .nav-item.active { background: rgba(168, 228, 213, .14); color: #fff; }
.rail-footer { border-top: 1px solid rgba(255,255,255,.14); color: #9dc3b9; font-size: .78rem; line-height: 1.5; margin-top: auto; padding: 16px 8px 0; }
.rail-status { align-items: center; display: flex; gap: 7px; }
.dot { background: #8ce0ba; border-radius: 50%; height: 8px; width: 8px; }
.dot.offline { background: var(--coral); }
.main { min-width: 0; padding: 30px clamp(20px, 4vw, 58px) 52px; }
.topbar { align-items: center; display: flex; justify-content: space-between; margin: 0 auto; max-width: 1160px; }
.eyebrow { color: var(--teal); font-size: .75rem; font-weight: 700; letter-spacing: .14em; text-transform: uppercase; }
h1 { font-size: clamp(2rem, 4vw, 3.4rem); letter-spacing: -.03em; line-height: 1; margin: 8px 0 10px; }
.subtle { color: var(--muted); margin: 0; }
.sync { align-items: center; color: var(--muted); display: flex; font-size: .78rem; gap: 8px; }
.sync .dot { animation: pulse 2.2s infinite; }
.toolbar-button { background: transparent; border: 1px solid var(--line); color: var(--ink); margin: 0 0 0 8px; padding: 8px 11px; width: auto; }
.toolbar-button:hover { background: var(--teal-soft); color: var(--teal); }
.dashboard { margin: 28px auto 0; max-width: 1160px; }
.stats { display: grid; gap: 12px; grid-template-columns: repeat(3, 1fr); }
.stat { animation: rise .45s ease both; background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 18px 20px; }
.stat:nth-child(2) { animation-delay: .06s; }.stat:nth-child(3) { animation-delay: .12s; }
.stat-label { color: var(--muted); font-size: .78rem; font-weight: 700; text-transform: uppercase; }
.metric { color: var(--ink); font-size: 2rem; font-weight: 700; margin-top: 10px; transition: color .2s ease, transform .2s ease; }
.metric.updated { color: var(--teal); transform: translateY(-2px); }
.workspace { display: grid; gap: 16px; grid-template-columns: minmax(0, 1.1fr) minmax(290px, .9fr); margin-top: 16px; }
.panel { animation: rise .45s ease both; background: var(--panel); border: 1px solid var(--line); border-radius: 10px; min-width: 0; padding: 22px; }
.panel-head { align-items: flex-start; display: flex; justify-content: space-between; gap: 12px; }
.panel-title { font-size: 1rem; font-weight: 700; margin: 0; }.panel-note { color: var(--muted); font-size: .78rem; margin: 5px 0 0; }
.chip { background: var(--teal-soft); border-radius: 999px; color: var(--teal); font-size: .7rem; font-weight: 700; padding: 6px 9px; white-space: nowrap; }
label { color: var(--muted); display: block; font-size: .78rem; font-weight: 700; margin: 18px 0 7px; }
input, select, textarea, button { box-sizing: border-box; font: inherit; width: 100%; }
input, select, textarea { background: #fbfdfc; border: 1px solid var(--line); border-radius: 7px; color: var(--ink); outline: none; padding: 11px 12px; transition: border-color .2s ease, box-shadow .2s ease; }
input:focus, select:focus, textarea:focus { border-color: var(--teal); box-shadow: 0 0 0 3px var(--teal-soft); }
textarea { min-height: 92px; resize: vertical; }
button { background: var(--teal); border: 0; border-radius: 7px; color: #fff; cursor: pointer; font-weight: 700; margin-top: 18px; padding: 12px 14px; transition: background .2s ease, transform .2s ease, opacity .2s ease; }
button:hover { background: #0f685e; } button:active { transform: translateY(1px); } button:disabled { cursor: wait; opacity: .55; }
.status { background: #f5faf8; border-left: 3px solid var(--teal); color: #496460; font-size: .82rem; margin-top: 14px; padding: 10px 12px; }
.status.error { background: #fff6f4; border-color: var(--coral); color: #a64d3e; }
.lower { display: grid; gap: 16px; grid-template-columns: 1fr 1fr; margin-top: 16px; }
ul { list-style: none; margin: 14px 0 0; max-height: 250px; overflow: auto; padding: 0; }
li { animation: rise .25s ease both; border-bottom: 1px solid #edf2f0; color: #496460; font-size: .82rem; padding: 10px 0; }
li:last-child { border-bottom: 0; } code { color: var(--teal); }
.capabilities { margin-top: 16px; }.capabilities ul { column-gap: 26px; columns: 2; max-height: 320px; }
@media (max-width: 820px) { .shell { display: block; }.rail { padding: 16px 20px; }.rail-label, .rail-footer { display: none; }.nav { display: flex; gap: 5px; margin-top: 14px; }.nav-item { padding: 8px 10px; width: auto; }.main { padding: 25px 18px 42px; } }
@media (max-width: 620px) { .stats, .workspace, .lower { grid-template-columns: 1fr; }.topbar { align-items: flex-start; flex-direction: column; gap: 12px; }.capabilities ul { columns: 1; } }
@keyframes rise { from { opacity: 0; transform: translateY(7px); } to { opacity: 1; transform: translateY(0); } }
@keyframes pulse { 0%, 100% { box-shadow: 0 0 0 0 rgba(140,224,186,.6); } 50% { box-shadow: 0 0 0 5px transparent; } }
@media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation-duration: .01ms !important; transition-duration: .01ms !important; } }
</style>
</head>
<body>
<div class="shell">
<aside class="rail">
<div class="brand"><span class="brand-mark">AM</span><span>ActionMesh</span></div>
<div class="rail-label">Workspace</div>
<nav class="nav" aria-label="Primary navigation"><button class="nav-item active" data-target="workspace">Control room</button><button class="nav-item" data-target="capability-list">Capabilities</button><button class="nav-item" data-target="history">Activity</button></nav>
<div class="rail-footer"><div class="rail-status"><span id="railDot" class="dot"></span><span id="railStatus">Service online</span></div><div style="margin-top:8px">AMP/1.0<br>Simulated environment</div></div>
</aside>
<main class="main">
<div class="topbar"><div><div class="eyebrow">Control room</div><h1>Good to go.</h1><p class="subtle">Permission-aware execution, verification, and simulated providers.</p></div><div class="sync"><span id="serviceDot" class="dot" aria-label="Service online"></span><span id="updated">Connecting...</span><button id="refresh" class="toolbar-button" type="button">Refresh</button></div></div>
<div class="dashboard">
<div class="stats">
<section class="stat"><div class="stat-label">Service</div><div id="health" class="metric" aria-live="polite">Loading</div></section>
<section class="stat"><div class="stat-label">Activity total</div><div id="metrics" class="metric">Loading</div></section>
<section class="stat"><div class="stat-label">Available actions</div><div id="capabilities" class="metric">Loading</div></section>
</div>
<div id="workspace" class="workspace">
<section class="panel"><div class="panel-head"><div><h2 class="panel-title">Authorize a scope</h2><p class="panel-note">Consent stays attached to the actor who grants it.</p></div><span class="chip">Protected</span></div>
<label for="actor">Actor ID</label><input id="actor" value="dashboard-user" autocomplete="off">
<label for="scope">Permission scope</label><select id="scope"><option value="message.send">message.send</option></select>
<label for="risk">Maximum risk</label><select id="risk"><option>low</option><option selected>high</option><option>critical</option></select>
<button id="consent" type="button" data-idle="Record consent">Record consent</button><div id="consentStatus" class="status" role="status">No consent recorded in this session.</div>
</section>
<section class="panel"><div class="panel-head"><div><h2 class="panel-title">Plan from natural language</h2><p class="panel-note">The planner proposes; policy decides. Nothing runs until you execute.</p></div><span class="chip">Propose only</span></div>
<label for="intent">Intent</label><input id="intent" value="tell mom I'll be late" autocomplete="off">
<button id="plan" type="button" data-idle="Propose plan">Propose plan</button>
<div id="planStatus" class="status" role="status">No plan proposed yet.</div>
<ul id="planActions" aria-live="polite"><li data-empty>Proposed actions appear here.</li></ul>
<button id="executePlan" type="button" data-idle="Execute planned actions" disabled>Execute planned actions</button>
<div id="planExecutionStatus" class="status" role="status">Nothing executed from a plan yet.</div>
</section>
<section class="panel"><div class="panel-head"><div><h2 class="panel-title">Send a simulated message</h2><p class="panel-note">A real graph, policy check, provider call, and verification.</p></div><span class="chip">Mock</span></div>
<label for="recipient">Recipient</label><input id="recipient" value="mom" autocomplete="off">
<label for="message">Message</label><textarea id="message">Hello from ActionMesh.</textarea>
<button id="execute" type="button" data-idle="Execute guarded action">Execute guarded action</button><div id="executionStatus" class="status" role="status">Execution results appear here.</div>
</section>
</div>
<div id="history" class="lower"><section class="panel"><div class="panel-head"><h2 class="panel-title">Recent executions</h2><span class="chip">Live</span></div><ul id="executions" aria-live="polite"><li>Loading</li></ul></section><section class="panel"><div class="panel-head"><h2 class="panel-title">Live activity</h2><span class="chip">SSE</span></div><ul id="events" aria-live="polite"><li data-placeholder>Waiting for events</li></ul></section></div>
<section id="capability-list" class="panel capabilities"><div class="panel-head"><div><h2 class="panel-title">Registered actions</h2><p class="panel-note">Capabilities currently available to the workspace.</p></div><span class="chip">16 seeded</span></div><ul id="actions"><li>Loading</li></ul></section>
</div>
</main>
</div>
<script>
const get = async (path, timeoutMs = 8000) => {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  let response;
  try {
    response = await fetch(path, {headers: {'accept': 'application/json'}, signal: controller.signal});
  } finally { window.clearTimeout(timeout); }
  if (!response.ok) throw new Error((await response.json()).detail || `Request failed: ${response.status}`);
  return response.json();
};
const setStatus = (id, message, error = false) => {
  const element = document.querySelector(id);
  element.textContent = message;
  element.classList.toggle('error', error);
};
const setBusy = (id, busy) => {
  const button = document.querySelector(id);
  button.disabled = busy;
  button.textContent = busy ? 'Working...' : (button.dataset.idle || 'Done');
};
const errorText = error => error.name === 'AbortError' ? 'The service took too long to respond.' : error.message;
const renderList = (selector, values, formatter) => {
  const list = document.querySelector(selector);
  list.replaceChildren(...(values.length ? values : ['None']).map(value => {
    const row = document.createElement('li');
    row.textContent = typeof value === 'string' ? value : formatter(value);
    return row;
  }));
};
  let refreshing = false;
  let capabilitiesCache = null;
  const load = async () => {
    if (refreshing) return;
    refreshing = true;
    try {
      const capabilityRequest = capabilitiesCache ? Promise.resolve(capabilitiesCache) : get('/capabilities').then(value => {
        capabilitiesCache = value;
        return value;
      });
      const [health, metrics, capabilities, executions, consents] = await Promise.all([
        get('/health'), get('/metrics'), capabilityRequest,
        get('/executions'), get(`/consents?actor_id=${encodeURIComponent(document.querySelector('#actor').value)}`)
      ]);
  document.querySelector('#health').textContent = health.status;
  document.querySelector('#metrics').textContent = Object.values(metrics).reduce((a, b) => a + b, 0);
  document.querySelector('#capabilities').textContent = capabilities.length;
    document.querySelector('#serviceDot').classList.remove('offline');
  document.querySelector('#railDot').classList.remove('offline');
  document.querySelector('#railStatus').textContent = 'Service online';
    document.querySelector('#updated').textContent = `Updated ${new Date().toLocaleTimeString()}`;
  const actions = document.querySelector('#actions');
  actions.replaceChildren(...capabilities.map(item => {
    const row = document.createElement('li');
    row.textContent = `${item.action} (${item.risk_level})`;
    return row;
  }));
  renderList('#executions', executions, item => `${item.execution_id}: ${item.state}`);
  setStatus('#consentStatus', consents.length ? `${consents.length} consent(s) recorded.` : 'No consent recorded in this session.');
  } catch (error) {
    document.querySelector('#serviceDot').classList.add('offline');
    document.querySelector('#railDot').classList.add('offline');
    document.querySelector('#railStatus').textContent = 'Service offline';
    document.querySelector('#health').textContent = 'Offline';
    document.querySelector('#updated').textContent = 'Refresh failed';
    setStatus('#executionStatus', errorText(error), true);
  } finally {
    refreshing = false;
    window.setTimeout(load, 10000);
  }
};
document.querySelector('#consent').addEventListener('click', async () => {
  setBusy('#consent', true);
  try {
    const response = await fetch('/consents', {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify({
      actor_id: document.querySelector('#actor').value,
      scope: document.querySelector('#scope').value,
      max_risk: document.querySelector('#risk').value,
      reason: 'dashboard consent'
    })});
    const body = await response.json();
    setStatus('#consentStatus', response.ok ? `Consent recorded for ${body.scope}.` : body.detail, !response.ok);
    load();
  } catch (error) {
    setStatus('#consentStatus', errorText(error), true);
  } finally { setBusy('#consent', false); }
});
document.querySelector('#execute').addEventListener('click', async () => {
  setBusy('#execute', true);
  const graph = {nodes: [
    {node_id: 'message', action: {type: 'message.send', parameters: {
      to: document.querySelector('#recipient').value, message: document.querySelector('#message').value
    }}},
    {node_id: 'end', node_type: 'end', depends_on: ['message']}
  ]};
  try {
    const response = await fetch('/execute', {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify({
      actor: {id: document.querySelector('#actor').value}, graph,
      policy: {rules: [{action: 'message.send', decision: 'allow'}]}
    })});
    const body = await response.json();
    setStatus('#executionStatus', response.ok ? `Execution ${body.state}.` : body.detail, !response.ok);
    load();
  } catch (error) {
    setStatus('#executionStatus', errorText(error), true);
  } finally { setBusy('#execute', false); }
});
// Planning is proposing. The graph below is built from whatever the planner
// returned and is sent WITHOUT an inline policy, so the server-side policy
// engine decides it — the same way it decides every other execution.
let plannedActions = [];
const planFeed = document.querySelector('#planActions');
const describeAction = action => {
  const params = Object.entries(action.parameters || {}).map(([key, value]) => `${key}=${value}`).join(', ');
  return params ? `${action.type} — ${params}` : action.type;
};
document.querySelector('#plan').addEventListener('click', async () => {
  setBusy('#plan', true);
  try {
    const response = await fetch('/plan', {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify({intent: document.querySelector('#intent').value})});
    const body = await response.json();
    if (!response.ok) { setStatus('#planStatus', body.detail, true); return; }
    plannedActions = body.actions || [];
    const placeholder = planFeed.querySelector('li[data-empty]');
    if (placeholder) placeholder.remove();
    planFeed.replaceChildren(...(plannedActions.length ? plannedActions.map(describeAction) : ['No actions proposed.']).map(text => {
      const row = document.createElement('li');
      row.textContent = text;
      return row;
    }));
    document.querySelector('#executePlan').disabled = plannedActions.length === 0;
    const notes = (body.notes || []).length ? ` ${body.notes.length} planner note(s) withheld from the plan.` : '';
    setStatus('#planStatus', `Proposed ${plannedActions.length} action(s).${notes} Policy decides next.`);
  } catch (error) {
    setStatus('#planStatus', errorText(error), true);
  } finally { setBusy('#plan', false); }
});
document.querySelector('#executePlan').addEventListener('click', async () => {
  setBusy('#executePlan', true);
  const nodes = plannedActions.map((action, index) => ({
    node_id: `p${index + 1}`,
    action: {type: action.type, parameters: action.parameters},
    depends_on: index ? [`p${index}`] : []
  }));
  if (nodes.length) {
    const last = nodes[nodes.length - 1].node_id;
    nodes.push({node_id: 'end', node_type: 'end', depends_on: [last]});
  }
  try {
    const response = await fetch('/execute', {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify({
      actor: {id: document.querySelector('#actor').value},
      graph: {nodes},
      // Policy rules for exactly the proposed action types. This says what the
      // workspace is willing to consider — it authorises nothing by itself. Consent
      // still comes from the server-side store, and CRITICAL risk still asks.
      policy: {rules: plannedActions.map(action => ({action: action.type, decision: 'allow'}))}
    })});
    const body = await response.json();
    setStatus('#planExecutionStatus', response.ok ? `Execution ${body.state}.` : body.detail, !response.ok);
    load();
  } catch (error) {
    setStatus('#planExecutionStatus', errorText(error), true);
  } finally { setBusy('#executePlan', false); }
});
const eventFeed = document.querySelector('#events');
const eventStream = new EventSource('/events');
['execution.started', 'node.completed', 'execution.completed'].forEach(type => {
  eventStream.addEventListener(type, event => {
    const data = JSON.parse(event.data);
    const detail = data.payload || {};
    const summary = type === 'execution.started'
      ? `Execution started · ${data.execution_id}`
      : type === 'execution.completed'
        ? `Execution ${detail.state || 'updated'} · ${data.execution_id}`
        : `${detail.action_type || detail.node_id || 'Node'} · ${detail.state || 'updated'}`;
    const placeholder = eventFeed.querySelector('li[data-placeholder]');
    if (placeholder) placeholder.remove();
    const row = document.createElement('li');
    row.textContent = summary;
    row.title = `${type} · ${data.actor || 'system'}`;
    eventFeed.prepend(row);
    while (eventFeed.children.length > 12) eventFeed.lastElementChild.remove();
  });
});
document.querySelector('#refresh').addEventListener('click', load);
document.querySelectorAll('.nav-item').forEach(button => {
  button.addEventListener('click', () => {
    document.querySelector(`#${button.dataset.target}`).scrollIntoView({behavior: 'smooth', block: 'start'});
    document.querySelectorAll('.nav-item').forEach(item => item.classList.remove('active'));
    button.classList.add('active');
  });
});
load();
// Installable as a home-screen app. The worker caches only the shell, never
// API responses, so a stale chain or capability list can never be shown.
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  });
}
</script>
</body>
</html>"""


def manifest() -> dict[str, object]:
    """Web-app manifest, so a phone can install the dashboard to its home screen.

    This is the zero-toolchain path to "an app on my phone" — no APK, no signing
    key: Chrome on Android offers *Add to home screen* and launches it
    full-screen with no browser chrome. (C17 also builds a native APK shell that
    loads the same ``/dashboard``.) ``start_url`` is the dashboard because that is
    the only page; ``display: standalone`` is what makes it feel installed.
    """
    return {
        "name": "ActionMesh Control Room",
        "short_name": "ActionMesh",
        "description": "Permission-aware execution, verification, and audit.",
        "start_url": "/dashboard",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait-primary",
        "background_color": "#f4f7f5",
        "theme_color": "#153b37",
        "icons": [
            {
                "src": "/icon.svg",
                "sizes": "any",
                "type": "image/svg+xml",
                "purpose": "any maskable",
            }
        ],
    }


#: Cache-busting version. Bump when the shell changes so phones pick it up.
SHELL_VERSION = "actionmesh-shell-v1"

_SW = """// C16 — offline shell only.
//
// Deliberately narrow: this caches the dashboard HTML and icon so the app opens
// instantly and can show *something* offline. It never caches /api responses,
// /audit, /executions, or any other JSON — a stale capability list, execution
// record or audit chain would be actively misleading, and this system's whole
// purpose is that what it shows you is what actually happened.
const CACHE = '%(version)s';
const SHELL = ['/dashboard', '/icon.svg'];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== self.location.origin) return;
  // Network-first for the shell: fresh when online, cached when not.
  event.respondWith(
    fetch(event.request)
      .then(response => {
        const copy = response.clone();
        caches.open(CACHE).then(c => c.put(event.request, copy)).catch(() => {});
        return response;
      })
      .catch(() => caches.match(event.request).then(hit => hit || caches.match('/dashboard')))
  );
});
"""


def service_worker() -> str:
    """The service worker source, with the cache name interpolated."""
    return _SW % {"version": SHELL_VERSION}


#: Dependency-free SVG icon. A maskable-safe full-bleed background with the
#: brand mark centred, so Android's adaptive-icon mask cannot clip it.
_ICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" role="img" aria-label="ActionMesh">
<rect width="512" height="512" fill="#153b37"/>
<g fill="none" stroke="#a8e4d5" stroke-width="26" stroke-linecap="round" stroke-linejoin="round">
<path d="M256 122 L378 190 L378 322 L256 390 L134 322 L134 190 Z"/>
<path d="M256 190 L256 322"/>
<path d="M134 190 L256 256 L378 190"/>
</g>
<circle cx="256" cy="256" r="21" fill="#a8e4d5"/>
</svg>"""


def icon() -> str:
    """The application icon as SVG."""
    return _ICON


def page() -> str:
    return _PAGE


__all__ = ["page"]
