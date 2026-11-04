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
.stats { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); }
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
.output-head { align-items: center; display: flex; justify-content: space-between; margin-top: 16px; }
.follow { align-items: center; color: var(--muted); display: flex; font-size: .75rem; font-weight: 700; gap: 6px; margin: 0; }
.follow input { width: auto; }
.output { background: #0f1a18; border-radius: 6px; color: #d7efe6; font-family: "SFMono-Regular", Consolas, monospace; font-size: .74rem; line-height: 1.45; margin: 8px 0 0; max-height: 240px; overflow: auto; padding: 10px 12px; white-space: pre-wrap; word-break: break-word; }
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
<nav class="nav" aria-label="Primary navigation"><button class="nav-item active" data-target="devices">My devices</button><button class="nav-item" data-target="approvals">Approvals</button><button class="nav-item" data-target="live">Live</button><button class="nav-item" data-target="workspace">Control room</button><button class="nav-item" data-target="capability-list">Capabilities</button><button class="nav-item" data-target="history">Activity</button></nav>
<div class="rail-footer"><div class="rail-status"><span id="railDot" class="dot"></span><span id="railStatus">Service online</span></div><div style="margin-top:8px">AMP/1.0<br>Simulated environment</div></div>
</aside>
<main class="main">
<div class="topbar"><div><div class="eyebrow">Control plane</div><h1>Your devices, one intent.</h1><p class="subtle">AI proposes. Policy decides. You approve. The laptop acts, and verification confirms.</p></div><div class="sync"><span id="serviceDot" class="dot" aria-label="Service online"></span><span id="updated">Connecting...</span><button id="refresh" class="toolbar-button" type="button">Refresh</button></div></div>
<div class="dashboard">
<div class="stats">
<section class="stat"><div class="stat-label">Service</div><div id="health" class="metric" aria-live="polite">Loading</div></section>
<section class="stat"><div class="stat-label">Devices online</div><div id="devicesOnline" class="metric">Loading</div></section>
<section class="stat"><div class="stat-label">Activity total</div><div id="metrics" class="metric">Loading</div></section>
<section class="stat"><div class="stat-label">Available actions</div><div id="capabilities" class="metric">Loading</div></section>
</div>
<div id="devices" class="lower">
<section class="panel"><div class="panel-head"><div><h2 class="panel-title">My devices</h2><p class="panel-note">Presence is asserted by the agent on the laptop, never by a caller.</p></div><span class="chip" id="deviceCount">0</span></div>
<ul id="deviceList" aria-live="polite"><li data-empty>No devices are enrolled yet.</li></ul>
</section>
<section class="panel"><div class="panel-head"><div><h2 class="panel-title">Pair a phone</h2><p class="panel-note">Mint a code here, on the laptop. A pairing secret is shown once and never logged.</p></div><span class="chip">Local only</span></div>
<label for="pairDevice">Laptop device id</label><input id="pairDevice" value="laptop-1" autocomplete="off">
<label for="pairRisk">Grant this pairing up to</label><select id="pairRisk"><option value="low" selected>low</option><option value="medium">medium</option><option value="high">high</option><option value="critical">critical — run programs</option></select>
<p class="panel-note">critical lets this phone ask the laptop to run a program the laptop allows. It buys reach, not permission: each run still needs the consent on record and your approval at the moment it asks. Anything less than critical leaves the command panel unable to run at all.</p>
<button id="issueCode" type="button" data-idle="Mint pairing code">Mint pairing code</button>
<div id="codeStatus" class="status" role="status">No code issued.</div>
<label for="redeemCode">Or paste a code received on this phone</label><input id="redeemCode" placeholder="000 000" autocomplete="off" inputmode="numeric">
<label for="phoneId">This phone's device id</label><input id="phoneId" value="phone-1" autocomplete="off">
<button id="redeem" type="button" data-idle="Redeem and store token">Redeem and store token</button>
<div id="tokenStatus" class="status" role="status">No device token stored in this browser.</div>
</section>
</div>
<div id="approvals" class="lower">
<section class="panel"><div class="panel-head"><div><h2 class="panel-title">Awaiting your approval</h2><p class="panel-note">Nothing here has run. Approving names the exact action the server asked about.</p></div><span class="chip" id="approvalCount">0 waiting</span></div>
<div id="approvalList"><p class="panel-note" data-empty>Nothing is waiting for a decision.</p></div>
</section>
</div>
<div id="live" class="lower">
<section class="panel"><div class="panel-head"><div><h2 class="panel-title">Live execution</h2><p class="panel-note">Driven by the server's event stream. Planned, approved, executed and verified are shown apart.</p></div><span class="chip" id="liveCount">idle</span></div>
<ul id="liveExecs" aria-live="polite"><li data-empty>Watching for executions&hellip;</li></ul>
<div class="output-head"><label class="follow"><input type="checkbox" id="followOutput" checked> Follow output</label><span class="panel-note" id="outputMeta">No output yet.</span></div>
<pre id="liveOutput" class="output" aria-live="off"></pre>
</section>
<section class="panel"><div class="panel-head"><div><h2 class="panel-title">Run a command on the laptop</h2><p class="panel-note">One program the laptop allows, with its arguments. The command policy and the approval path decide whether it runs — not this form.</p></div><span class="chip">Laptop</span></div>
<label for="cmdCommand">Program</label><input id="cmdCommand" value="python3" autocomplete="off" spellcheck="false">
<label for="cmdArguments">Arguments, one per line</label><textarea id="cmdArguments" rows="3" spellcheck="false" autocomplete="off"></textarea>
<label for="cmdWorkingDir">Working directory, inside the workspace</label><input id="cmdWorkingDir" value="." autocomplete="off" spellcheck="false">
<label for="cmdTimeout">Timeout in seconds</label><input id="cmdTimeout" type="number" min="1" max="600" value="60">
<button id="runCommand" type="button" data-idle="Run on laptop">Run on laptop</button>
<div id="runStatus" class="status" role="status">Nothing run from this panel yet.</div>
</section>
<section class="panel"><div class="panel-head"><div><h2 class="panel-title">What the laptop is running</h2><p class="panel-note">Only the processes this agent started, addressed by the id it issued — never by pid.</p></div><span class="chip" id="procCount">not loaded</span></div>
<button id="refreshProcs" type="button" data-idle="Refresh processes">Refresh processes</button>
<ul id="procList" aria-live="polite"><li data-empty>Nothing loaded yet.</li></ul>
<div id="procStatus" class="status" role="status">No inventory loaded from the laptop yet.</div>
</section>
<section class="panel"><div class="panel-head"><div><h2 class="panel-title">Files on the laptop</h2><p class="panel-note">One directory inside the laptop's workspace. Opening a file streams its body to this screen — never into the audit trail.</p></div><span class="chip" id="browseCount">not loaded</span></div>
<label for="browsePath">Directory, relative to the workspace</label><input id="browsePath" value="." autocomplete="off" spellcheck="false">
<button id="browseGo" type="button" data-idle="List files">List files</button>
<ul id="browseList" aria-live="polite"><li data-empty>No directory listed yet.</li></ul>
<div id="browseStatus" class="status" role="status">Nothing browsed on the laptop yet.</div>
</section>
</div>
<div id="workspace" class="workspace">
<section class="panel"><div class="panel-head"><div><h2 class="panel-title">Authorize a scope</h2><p class="panel-note">Consent stays attached to the actor who grants it.</p></div><span class="chip">Protected</span></div>
<label for="actor">Actor ID</label><input id="actor" value="dashboard-user" autocomplete="off">
<label for="scope">Permission scope</label><select id="scope"><option value="message.send">message.send (mock)</option><option value="device.control">device.control (laptop)</option><option value="device.file.list">device.file.list (laptop)</option><option value="device.ide.open">device.ide.open (laptop)</option><option value="device.process.control">device.process.control (laptop)</option><option value="device.process.read">device.process.read (laptop)</option><option value="device.project.read">device.project.read (laptop)</option><option value="device.server.control">device.server.control (laptop)</option><option value="device.server.read">device.server.read (laptop)</option><option value="device.screen.read">device.screen.read (laptop)</option><option value="device.screen.control">device.screen.control (laptop)</option><option value="file.delete">file.delete (laptop)</option><option value="file.read">file.read (laptop)</option><option value="file.write">file.write (laptop)</option><option value="system.app.launch">system.app.launch (laptop)</option><option value="system.shell">system.shell (laptop)</option><option value="payment.execute">payment.execute (laptop)</option><option value="browser.control">browser.control (laptop)</option><option value="media.control">media.control (laptop)</option><option value="search.web">search.web (laptop)</option></select>
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
<section id="capability-list" class="panel capabilities"><div class="panel-head"><div><h2 class="panel-title">Registered actions</h2><p class="panel-note">Capabilities currently available to the workspace.</p></div><span class="chip" id="capabilityChip">loading</span></div><ul id="actions"><li>Loading</li></ul></section>
</div>
</main>
</div>
<script>
// The device token from POST /pair is held only in this browser's sessionStorage.
// It is what says *which* device sent a request; the ceiling behind it was chosen at
// issuance, on the laptop, and no request body can raise it.
const tokenKey = 'actionmesh.device.token';
const getToken = () => { try { return sessionStorage.getItem(tokenKey) || ''; } catch (e) { return ''; } };
const setToken = value => { try { value ? sessionStorage.setItem(tokenKey, value) : sessionStorage.removeItem(tokenKey); } catch (e) {} };
const authHeaders = extra => {
  const headers = Object.assign({'content-type': 'application/json'}, extra || {});
  const token = getToken();
  if (token) headers.authorization = 'Bearer ' + token;
  return headers;
};
const identityBody = () => (getToken() ? {} : {actor: {id: document.querySelector('#actor').value}});
const consentsPath = () => (getToken() ? '/consents' : '/consents?actor_id=' + encodeURIComponent(document.querySelector('#actor').value));
const consentBody = () => (getToken() ? {} : {actor_id: document.querySelector('#actor').value});
const get = async (path, timeoutMs = 8000) => {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  let response;
  try {
    response = await fetch(path, {headers: authHeaders({accept: 'application/json'}), signal: controller.signal});
  } finally { window.clearTimeout(timeout); }
  if (!response.ok) {
    // A 401 means the stored token is no longer good -- the server restarted on an
    // ephemeral key, or the device was revoked. Drop it so the dashboard stops
    // presenting a dead credential and falls back to the local view, rather than
    // hammering every endpoint with something that will never be accepted again.
    if (response.status === 401) setToken('');
    throw new Error((await response.json()).detail || `Request failed: ${response.status}`);
  }
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
// Element.replaceChildren arrived in Chrome 86. The Android 11 system
// WebView here is Chrome 83 and genuinely lacks it — this threw a TypeError on
// every load, so the console showed "Offline" while the API was answering 200.
// textContent + appendChild works on every engine that can run this page.
const replaceAll = (element, nodes) => {
  element.textContent = '';
  nodes.forEach(node => element.appendChild(node));
};
const renderList = (selector, values, formatter) => {
  const list = document.querySelector(selector);
  replaceAll(list, (values.length ? values : ['None']).map(value => {
    const row = document.createElement('li');
    row.textContent = typeof value === 'string' ? value : formatter(value);
    return row;
  }));
};
  let refreshing = false;
  let capabilitiesCache = null;
  // The poll re-runs every ten seconds. Rebuilding a panel that did not change wipes
  // and recreates its buttons, which steals a tap that landed a moment before -- on a
  // phone mid-approval. So each panel remembers what it last rendered and only rebuilds
  // when the server's data actually differs.
  let lastDeviceSig = null;
  let lastApprovalSig = null;
  const button = (label, className, onClick) => {
    const element = document.createElement('button');
    element.type = 'button';
    element.textContent = label;
    element.className = className || 'toolbar-button';
    element.addEventListener('click', onClick);
    return element;
  };
  const renderDevices = devices => {
    const signature = JSON.stringify(devices);
    if (signature === lastDeviceSig) return;
    lastDeviceSig = signature;
    const list = document.querySelector('#deviceList');
    const online = devices.filter(device => device.online).length;
    document.querySelector('#devicesOnline').textContent = String(online);
    document.querySelector('#deviceCount').textContent = String(devices.length);
    replaceAll(list, (devices.length ? devices : ['No devices are enrolled yet.']).map(device => {
      if (typeof device === 'string') {
        const empty = document.createElement('li');
        empty.textContent = device;
        empty.dataset.empty = '';
        return empty;
      }
      const row = document.createElement('li');
      const dot = document.createElement('span');
      dot.className = 'dot' + (device.online ? '' : ' offline');
      const label = document.createElement('span');
      label.textContent = `${device.name || device.device_id} · ${device.kind} · ${device.online ? 'online' : 'offline'}`;
      row.appendChild(dot);
      row.appendChild(document.createTextNode(' '));
      row.appendChild(label);
      if (device.capabilities.length) {
        const note = document.createElement('code');
        note.textContent = ` ${device.capabilities.length} action(s)`;
        row.appendChild(note);
      }
      if (device.enrolled) {
        row.appendChild(document.createTextNode(' '));
        row.appendChild(button('Revoke', 'toolbar-button', () => revokeDevice(device.device_id)));
      }
      return row;
    }));
  };
  const renderApprovals = waiting => {
    const signature = JSON.stringify(waiting);
    if (signature === lastApprovalSig) return;
    lastApprovalSig = signature;
    const box = document.querySelector('#approvalList');
    document.querySelector('#approvalCount').textContent = `${waiting.length} waiting`;
    if (!waiting.length) {
      const note = document.createElement('p');
      note.className = 'panel-note';
      note.dataset.empty = '';
      note.textContent = 'Nothing is waiting for a decision.';
      replaceAll(box, [note]);
      return;
    }
    replaceAll(box, waiting.map(item => {
      const panel = document.createElement('div');
      panel.className = 'status';
      panel.textContent = `Execution ${item.execution_id} · actor ${item.actor}`;
      (item.actions || []).forEach(action => {
        const line = document.createElement('div');
        const summary = document.createElement('code');
        summary.textContent = `${action.action_type} (${action.risk_level})`;
        line.appendChild(summary);
        line.appendChild(document.createTextNode(' '));
        line.appendChild(button('Approve', 'toolbar-button', () => decideApproval(item.execution_id, action, 'approved')));
        line.appendChild(button('Reject', 'toolbar-button', () => decideApproval(item.execution_id, action, 'rejected')));
        panel.appendChild(line);
      });
      return panel;
    }));
  };
  const load = async () => {
    if (refreshing) return;
    refreshing = true;
    try {
      const capabilityRequest = capabilitiesCache ? Promise.resolve(capabilitiesCache) : get('/capabilities').then(value => {
        capabilitiesCache = value;
        return value;
      });
      const [health, metrics, capabilities, executions, consents, devices, waiting] = await Promise.all([
        get('/health'), get('/metrics'), capabilityRequest,
        get('/executions'), get(consentsPath()),
        get('/devices'), get('/approvals')
      ]);
  document.querySelector('#health').textContent = health.status;
  document.querySelector('#metrics').textContent = Object.values(metrics).reduce((a, b) => a + b, 0);
  document.querySelector('#capabilities').textContent = capabilities.length;
    document.querySelector('#serviceDot').classList.remove('offline');
  document.querySelector('#railDot').classList.remove('offline');
  document.querySelector('#railStatus').textContent = 'Service online';
    document.querySelector('#updated').textContent = `Updated ${new Date().toLocaleTimeString()}`;
  const actions = document.querySelector('#actions');
  document.querySelector('#capabilityChip').textContent = `${capabilities.length} registered`;
  replaceAll(actions, capabilities.map(item => {
    const row = document.createElement('li');
    row.textContent = `${item.action} (${item.risk_level})`;
    return row;
  }));
  renderList('#executions', executions, item => `${item.execution_id}: ${item.state}`);
  setStatus('#consentStatus', consents.length ? `${consents.length} consent(s) recorded.` : 'No consent recorded in this session.');
  renderDevices(devices);
  renderApprovals(waiting);
  renderLive();
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
// Minting a code is loopback-only: it is the act of standing at the laptop and
// choosing a ceiling. Redeeming is not, because the phone is remote by definition.
document.querySelector('#issueCode').addEventListener('click', async () => {
  setBusy('#issueCode', true);
  try {
    const response = await fetch('/pairing/code', {method: 'POST', headers: authHeaders(), body: JSON.stringify({
      device_id: document.querySelector('#pairDevice').value,
      max_risk: document.querySelector('#pairRisk').value
    })});
    const body = await response.json();
    if (!response.ok) { setStatus('#codeStatus', body.detail, true); return; }
    const minutes = Math.round(body.expires_in_s / 60);
    setStatus('#codeStatus', `${body.code} · up to ${body.max_risk} risk · valid ~${minutes} min. Read it to the phone; it is shown once.`);
    document.querySelector('#redeemCode').value = body.code;
  } catch (error) {
    setStatus('#codeStatus', errorText(error), true);
  } finally { setBusy('#issueCode', false); }
});
document.querySelector('#redeem').addEventListener('click', async () => {
  setBusy('#redeem', true);
  try {
    const response = await fetch('/pair', {method: 'POST', headers: authHeaders(), body: JSON.stringify({
      code: document.querySelector('#redeemCode').value,
      device_id: document.querySelector('#phoneId').value,
      actor_id: document.querySelector('#actor').value,
      name: 'This phone'
    })});
    const body = await response.json();
    if (!response.ok) { setStatus('#tokenStatus', body.detail, true); return; }
    setToken(body.token);
    setStatus('#tokenStatus', `Paired as ${body.device_id}, capped at ${body.max_risk}. The token lives only in this browser.`);
    // The stream opened before pairing was the operator's view of everything. Keep it
    // and the page goes on receiving other actors' events, then failing to read the
    // records behind them. Close it and re-open with a watch ticket bound to this
    // credential.
    if (eventStream) eventStream.close();
    openStream();
    load();
  } catch (error) {
    setStatus('#tokenStatus', errorText(error), true);
  } finally { setBusy('#redeem', false); }
});
const revokeDevice = async deviceId => {
  try {
    const response = await fetch(`/devices/${encodeURIComponent(deviceId)}/revoke`, {method: 'POST', headers: authHeaders(), body: JSON.stringify({reason: 'revoked from dashboard'})});
    setStatus('#railStatus', response.ok ? `Device ${deviceId} revoked.` : (await response.json()).detail, !response.ok);
  } catch (error) {
    setStatus('#railStatus', errorText(error), true);
  }
  load();
};
const decideApproval = async (executionId, action, status) => {
  // The id is the one the server minted for this run and echoed back through
  // GET /approvals; approving anything else matches nothing.
  try {
    const response = await fetch('/approvals', {method: 'POST', headers: authHeaders(), body: JSON.stringify({
      execution_id: executionId,
      action_id: action.action_id,
      action_type: action.action_type,
      status
    })});
    const body = await response.json();
    if (!response.ok) { setStatus('#railStatus', body.detail, true); return; }
    const run = body.execution;
    setStatus('#railStatus', run ? `Execution ${run.state}.` : `Approval ${status}.`);
  } catch (error) {
    setStatus('#railStatus', errorText(error), true);
  }
  load();
};
document.querySelector('#consent').addEventListener('click', async () => {
  setBusy('#consent', true);
  try {
    const response = await fetch('/consents', {method: 'POST', headers: authHeaders(), body: JSON.stringify({
      ...consentBody(),
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
    const response = await fetch('/execute', {method: 'POST', headers: authHeaders(), body: JSON.stringify({
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
    const response = await fetch('/plan', {method: 'POST', headers: authHeaders(), body: JSON.stringify({intent: document.querySelector('#intent').value})});
    const body = await response.json();
    if (!response.ok) { setStatus('#planStatus', body.detail, true); return; }
    plannedActions = body.actions || [];
    const placeholder = planFeed.querySelector('li[data-empty]');
    if (placeholder) placeholder.remove();
    replaceAll(planFeed, (plannedActions.length ? plannedActions.map(describeAction) : ['No actions proposed.']).map(text => {
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
    const response = await fetch('/execute', {method: 'POST', headers: authHeaders(), body: JSON.stringify({
      ...identityBody(),
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
const runLaptopCommand = async () => {
  const command = document.querySelector('#cmdCommand').value.trim();
  // One argument per line, kept as a list all the way to the request. A single string
  // is the shape only a shell could make sense of, and the laptop's command policy
  // refuses that shape on purpose: nothing here can smuggle `;`, `&&` or a pipe.
  const argv = document.querySelector('#cmdArguments').value.split('\\n')
    .map(line => line.trim()).filter(line => line);
  const workingDirectory = document.querySelector('#cmdWorkingDir').value.trim() || '.';
  const timeoutS = Number(document.querySelector('#cmdTimeout').value);
  if (!command) { setStatus('#runStatus', 'Name the one program to run.', true); return; }
  if (!Number.isFinite(timeoutS) || timeoutS < 1) {
    setStatus('#runStatus', 'Timeout must be a number of seconds of its own.', true);
    return;
  }
  setBusy('#runCommand', true);
  try {
    const response = await fetch('/execute', {method: 'POST', headers: authHeaders(), body: JSON.stringify({
      ...identityBody(),
      graph: {nodes: [
        {node_id: 'a1', action: {type: 'device.terminal.execute', provider: 'laptop',
          parameters: {command, arguments: argv, working_directory: workingDirectory, timeout_s: timeoutS}}},
        {node_id: 'end', node_type: 'end', depends_on: ['a1']}
      ]},
      // One rule, for the one action type this panel can ask about. It names what the
      // operator is willing to consider; it authorises nothing by itself. Consent stays
      // in the server-side store and a CRITICAL command still parks for a decision.
      policy: {rules: [{action: 'device.terminal.execute', decision: 'allow'}]}
    })});
    const body = await response.json();
    if (!response.ok) { setStatus('#runStatus', body.detail || 'The laptop refused the request.', true); return; }
    setStatus('#runStatus', body.state === 'waiting_permission'
      ? 'Submitted. It is waiting on your approval — decide in "Awaiting your approval" above; the output appears below as it runs.'
      : `Execution ${body.state}. Its node result is in the live panel.`);
    load();
  } catch (error) {
    setStatus('#runStatus', errorText(error), true);
  } finally { setBusy('#runCommand', false); }
};
document.querySelector('#runCommand').addEventListener('click', runLaptopCommand);
const runCapability = async (actionType, parameters) => {
  const response = await fetch('/execute', {method: 'POST', headers: authHeaders(), body: JSON.stringify({
    ...identityBody(),
    graph: {nodes: [
      {node_id: 'a1', action: {type: actionType, provider: 'laptop', parameters}},
      {node_id: 'end', node_type: 'end', depends_on: ['a1']}
    ]},
    // Names what the operator is willing to consider; authorises nothing by itself.
    policy: {rules: [{action: actionType, decision: 'allow'}]}
  })});
  const body = await response.json();
  if (!response.ok) throw new Error(body.detail || 'The laptop refused the request.');
  return body;
};
const procFeed = document.querySelector('#procList');
const procRows = new Map();
const loadProcesses = async () => {
  setBusy('#refreshProcs', true);
  try {
    const result = await runCapability('device.process.list', {});
    const node = (result.nodes && result.nodes.a1) || {};
    if (result.state !== 'success') {
      setStatus('#procStatus', `The laptop answered ${result.state}.`, true);
      return;
    }
    const rows = (node.result && node.result.processes) || [];
    document.querySelector('#procCount').textContent = `${rows.length}`;
    setStatus('#procStatus', `${rows.length} process(es) started by this agent.`, false);
    const placeholder = procFeed.querySelector('li[data-empty]');
    if (placeholder) placeholder.remove();
    const seen = new Set();
    rows.forEach(row => {
      const processId = row.process_id || row.label || 'row';
      seen.add(processId);
      const text = `${row.label || processId} · ${row.kind || 'process'} · ${row.state || ''}`
        + (row.port ? ` · :${row.port}` : '')
        + (row.exit_code ? ` · exit ${row.exit_code}` : '');
      let li = procRows.get(processId);
      if (!li) {
        li = document.createElement('li');
        li.appendChild(document.createElement('span'));
        const stop = document.createElement('button');
        stop.type = 'button';
        stop.className = 'toolbar-button';
        stop.textContent = 'Stop';
        stop.addEventListener('click', () => stopProcess(processId));
        li.appendChild(stop);
        const logs = document.createElement('button');
        logs.type = 'button';
        logs.className = 'toolbar-button';
        logs.textContent = 'Logs';
        logs.addEventListener('click', () => readLogs(processId));
        // A child launched with its output discarded has no log to read, so the button
        // that would only be refused is not offered.
        logs.hidden = row.output_captured !== true;
        li.appendChild(logs);
        procRows.set(processId, li);
        procFeed.prepend(li);
      }
      const label = li.querySelector('span');
      if (label && label.textContent !== text) label.textContent = text;
      // A finished child has nothing to stop, so the control disappears rather than
      // sitting there promising an action that cannot happen.
      const stop = li.querySelector('button');
      if (stop) stop.hidden = row.state !== 'running';
      li.dataset.state = row.state || '';
    });
    procRows.forEach((li, key) => {
      if (seen.has(key)) return;
      if (li.parentNode) li.parentNode.removeChild(li);
      procRows.delete(key);
    });
  } catch (error) {
    setStatus('#procStatus', errorText(error), true);
  } finally { setBusy('#refreshProcs', false); }
};
const stopProcess = async processId => {
  try {
    // No message before the request: the answer is what the reader needs, and a
    // placeholder here just gets overwritten by the inventory refresh below.
    const result = await runCapability('device.process.stop', {process_id: processId});
    if (result.state === 'waiting_permission') {
      setStatus('#procStatus', `Stopping ${processId} needs your approval — decide in "Awaiting your approval".`, false);
      return;
    }
    // Refresh first, then speak: the inventory write would otherwise overwrite this
    // message and leave the reader with a count where the answer should be.
    await loadProcesses();
    setStatus('#procStatus', `Stop ${processId}: ${result.state}.`, false);
  } catch (error) {
    setStatus('#procStatus', errorText(error), true);
  }
};
document.querySelector('#refreshProcs').addEventListener('click', loadProcesses);
const readLogs = async processId => {
  try {
    await runCapability('device.process.logs', {process_id: processId});
    // The bytes arrive as transient output, so the live panel is where they land.
    setStatus('#procStatus', `Read ${processId}'s log — its contents are in the live output panel.`, false);
  } catch (error) {
    setStatus('#procStatus', errorText(error), true);
  }
};
const browseFeed = document.querySelector('#browseList');
const browseRows = new Map();
const loadFiles = async () => {
  const directory = document.querySelector('#browsePath').value.trim() || '.';
  setBusy('#browseGo', true);
  try {
    const result = await runCapability('device.file.list', {path: directory});
    const node = (result.nodes && result.nodes.a1) || {};
    if (result.state !== 'success') {
      setStatus('#browseStatus', `The laptop answered ${result.state}.`, true);
      return;
    }
    const entries = (node.result && node.result.entries) || [];
    document.querySelector('#browseCount').textContent = `${entries.length}`;
    setStatus('#browseStatus', `${entries.length} entry(ies) in ${node.result.path || directory}.`, false);
    const placeholder = browseFeed.querySelector('li[data-empty]');
    if (placeholder) placeholder.remove();
    const seen = new Set();
    entries.forEach(entry => {
      const filePath = entry.path || entry.name || 'entry';
      seen.add(filePath);
      const size = entry.type === 'dir' ? '' : ` · ${entry.size || 0} B`;
      const text = `${filePath} · ${entry.type || '?'}${size}${entry.confined === false ? ' · outside root' : ''}`;
      let li = browseRows.get(filePath);
      if (!li) {
        li = document.createElement('li');
        li.appendChild(document.createElement('span'));
        const open = document.createElement('button');
        open.type = 'button';
        open.className = 'toolbar-button';
        open.textContent = 'Open';
        open.addEventListener('click', () => readFile(filePath));
        li.appendChild(open);
        browseRows.set(filePath, li);
        browseFeed.prepend(li);
      }
      const label = li.querySelector('span');
      if (label && label.textContent !== text) label.textContent = text;
      // A directory is not a body to open; the path field is how you move into one.
      const open = li.querySelector('button');
      if (open) open.hidden = entry.type !== 'file';
    });
    browseRows.forEach((li, key) => {
      if (seen.has(key)) return;
      if (li.parentNode) li.parentNode.removeChild(li);
      browseRows.delete(key);
    });
  } catch (error) {
    setStatus('#browseStatus', errorText(error), true);
  } finally { setBusy('#browseGo', false); }
};
const readFile = async filePath => {
  try {
    await runCapability('device.file.read', {path: filePath});
    // The body arrives as transient output, so the live panel is where it lands; this
    // panel only says that it was asked for.
    setStatus('#browseStatus', `Opened ${filePath} — its body is in the live output panel.`, false);
  } catch (error) {
    setStatus('#browseStatus', errorText(error), true);
  }
};
document.querySelector('#browseGo').addEventListener('click', loadFiles);
const eventFeed = document.querySelector('#events');
// Authoritative per-execution state, assembled from the event stream and reconciled
// against GET /executions on (re)connect. A phone that sleeps through events recovers
// the current truth rather than the last frame it happened to receive.
const execStates = new Map();
const seenEvents = new Set();
const liveFeed = document.querySelector('#liveExecs');
const liveRows = new Map();
let focusedOutput = null;
const outputBox = document.querySelector('#liveOutput');
const outputMeta = document.querySelector('#outputMeta');
const STAGE = {
  created: 'Planned', planned: 'Planned', authorized: 'Approved',
  waiting_permission: 'Needs approval', requires_user: 'Unverified',
  executing: 'Executing', verifying: 'Verifying',
  success: 'Succeeded', failed: 'Failed', denied: 'Denied',
  cancelled: 'Cancelled', timeout: 'Timed out', partially_completed: 'Partial',
  interrupted: 'Interrupted'
};
const stageOf = state => STAGE[state] || state || 'idle';
// Which outcomes are over. `waiting_permission` is deliberately absent: a parked run has
// finished its request, not its execution, and treating it as done hides the Cancel
// control from a run that resumes and keeps going.
const TERMINAL_STATES = new Set([
  'success', 'failed', 'denied', 'cancelled', 'timeout', 'partially_completed', 'interrupted'
]);
const bumpFeed = (text, title) => {
  const placeholder = eventFeed.querySelector('li[data-placeholder]');
  if (placeholder) placeholder.remove();
  const row = document.createElement('li');
  row.textContent = text;
  if (title) row.title = title;
  eventFeed.prepend(row);
  while (eventFeed.children.length > 12) eventFeed.lastElementChild.remove();
};
const applyEvent = (type, data) => {
  // De-duplicate by event_id: a reconnect replays the retained window, and a
  // duplicate applied twice must not double-count progress or corrupt state.
  if (data.event_id) {
    if (seenEvents.has(data.event_id)) return;
    seenEvents.add(data.event_id);
    while (seenEvents.size > 2000) seenEvents.delete(seenEvents.values().next().value);
  }
  const id = data.execution_id;
  if (!id) return;
  const detail = data.payload || {};
  let state = execStates.get(id);
  if (!state) {
    state = {id, actor: data.actor || 'system', stage: 'Planned', progress: null, nodes: {}, approval: false, verification: 'pending', startedAt: Date.now(), updatedAt: Date.now()};
    execStates.set(id, state);
  }
  state.updatedAt = Date.now();
  if (type === 'execution.started') {
    state.stage = 'Executing';
    bumpFeed(`Execution started · ${id}`, `${id}`);
  } else if (type === 'node.started') {
    state.nodes[detail.node_id] = {state: 'executing', action_type: detail.action_type};
    state.current = detail.node_id;
    state.stage = 'Executing';
    // A resumed run is live again, whatever an earlier completion said.
    state.final = null;
  } else if (type === 'node.completed') {
    state.nodes[detail.node_id] = {state: detail.state, action_type: detail.action_type, verified: detail.verified};
    if (detail.verified === true) state.verification = 'verified';
    else if (detail.verified === false) state.verification = 'unverified';
    bumpFeed(`${detail.action_type || detail.node_id || 'Node'} · ${detail.state}`, `${id}`);
  } else if (type === 'execution.progress') {
    state.progress = {completed: detail.completed, total: detail.total};
  } else if (type === 'execution.output') {
    // Live process output for the authorized observer only. Bounded to a tail so a
    // chatty command cannot grow the page; rendered as text, never markup.
    state.output = (state.output || '') + (detail.chunk || '');
    if (state.output.length > 8000) state.output = state.output.slice(-8000);
    state.current = detail.node_id || state.current;
    state.truncated = !!detail.truncated;
    focusedOutput = id;
    renderLive();
    renderOutput();
    return;
  } else if (type === 'approval.required') {
    state.stage = 'Needs approval';
    state.approval = true;
    load();
  } else if (type === 'execution.completed') {
    state.stage = stageOf(detail.state);
    state.final = TERMINAL_STATES.has(detail.state) ? detail.state : null;
    bumpFeed(`Execution ${detail.state} · ${id}`, `${id}`);
    noteLimits(id);
    // A stop approved from the approval panel finishes somewhere else, and the row must
    // not go on claiming the process is running. Read once now and once after the
    // registry has had a moment to reap the child -- the completion event can arrive
    // while the exit status is still in flight.
    if (procRows.size) { setTimeout(loadProcesses, 250); setTimeout(loadProcesses, 1500); }
  } else if (type === 'execution.reconciled') {
    // A restart happened underneath this run and this process adopted it. The stage says
    // what the record says -- still executing, now watched here -- rather than pretending
    // the run began again.
    state.stage = 'Reconciled';
    state.final = null;
    bumpFeed(`Execution recovered from the record · ${id}`, `${id}`);
  } else if (type === 'execution.cancelled') {
    state.stage = 'Cancelled';
    state.final = 'cancelled';
    bumpFeed(
      (detail.unconfirmed || []).length
        ? `Cancelled · ${(detail.unconfirmed || []).length} effect(s) unconfirmed · ${id}`
        : `Cancelled · ${id}`,
      `${id}`
    );
    if (procRows.size) setTimeout(loadProcesses, 250);
  } else if (type === 'execution.denied') {
    state.stage = 'Denied';
    state.final = 'denied';
    load();
  } else if (type === 'compensation.unconfirmed' || type === 'compensation.refused') {
    state.verification = 'unverified';
    state.compensation = true;
  }
  renderLive();
};
const renderLive = () => {
  const empty = liveFeed.querySelector('li[data-empty]');
  if (empty && execStates.size) empty.remove();
  // Update rows in place, keyed by execution id; never rebuild a row that did not
  // change, so the list neither flickers nor loses scroll position.
  execStates.forEach(state => {
    const text = `${state.id} · ${state.stage}`
      + (state.progress ? ` · ${state.progress.completed}/${state.progress.total}` : '')
      + (state.current ? ` · node ${state.current}` : '')
      + ` · ${state.verification}`;
    let row = liveRows.get(state.id);
    if (!row) {
      row = document.createElement('li');
      row.appendChild(document.createElement('span'));
      const cancel = document.createElement('button');
      cancel.type = 'button';
      cancel.className = 'toolbar-button';
      cancel.textContent = 'Cancel';
      cancel.addEventListener('click', () => cancelExecution(state.id));
      row.appendChild(cancel);
      liveRows.set(state.id, row);
      liveFeed.prepend(row);
      while (liveRows.size > 12) {
        const oldest = [...liveRows.keys()][liveRows.size - 1];
        const node = liveRows.get(oldest);
        if (node && node.parentNode) node.parentNode.removeChild(node);
        liveRows.delete(oldest);
      }
    }
    const label = row.querySelector('span');
    if (label && label.textContent !== text) label.textContent = text;
    // Only a run that has not finished can be cancelled, so the control goes away with
    // the outcome instead of staying to be refused.
    const cancel = row.querySelector('button');
    if (cancel) cancel.hidden = Boolean(state.final);
    row.dataset.state = state.stage;
  });
  const active = [...execStates.values()].filter(s => !s.final).length;
  document.querySelector('#liveCount').textContent = active ? `${active} running` : 'idle';
};
const renderOutput = () => {
  const state = focusedOutput ? execStates.get(focusedOutput) : null;
  const text = state && state.output ? state.output : '';
  // Preserve the reader's scroll: only jump to the newest line if they were already
  // at the bottom and asked to follow. Rebuilding scroll on every chunk would make
  // older output impossible to read while a command runs.
  const atBottom = outputBox.scrollHeight - outputBox.scrollTop - outputBox.clientHeight < 24;
  const follow = document.querySelector('#followOutput').checked;
  outputBox.textContent = text;
  if (atBottom && follow) outputBox.scrollTop = outputBox.scrollHeight;
  if (state && state.output) {
    outputMeta.textContent = [
      `${focusedOutput} · ${state.current || ''}`,
      state.truncated ? 'output truncated' : '',
      ...(state.limits || [])
    ].filter(part => part).join(' · ');
  } else if (state && state.limits && state.limits.length) {
    outputMeta.textContent = state.limits.join(' · ');
  } else {
    outputMeta.textContent = 'No output yet.';
  }
};
const cancelExecution = async id => {
  try {
    const response = await fetch(`/executions/${encodeURIComponent(id)}/cancel`, {method: 'POST', headers: authHeaders()});
    const body = await response.json();
    if (!response.ok) { bumpFeed(`Cancel refused: ${body.detail}`, id); return; }
    // C31: a provider call already running is interrupted rather than left to finish, and
    // the executor waits for that interruption to land before it reports anything. The
    // row's own terminal event is what the reader waits for next.
    bumpFeed(`Cancellation requested · ${id}`, id);
  } catch (error) {
    bumpFeed(errorText(error));
  }
};
const limitNote = (nodeId, result) => {
  // The event stream says a node finished; only the record says which limit it
  // finished against. Without this an output panel that is empty or short leaves the
  // reader to blame the program for the laptop's own ceiling.
  if (result.timeout_clipped) {
    return `${nodeId}: stopped by the laptop's own ${result.timeout_applied_s}s ceiling, not the ${result.timeout_requested_s}s that was asked for`;
  }
  if (result.output_captured === false) {
    return `${nodeId}: launched detached, so nothing captures its output — run it as a command to watch it`;
  }
  return '';
};
const limitsOf = record => Object.entries(record.results || {})
  .map(([nodeId, node]) => limitNote(nodeId, node.result || {}))
  .filter(Boolean);
const noteLimits = async id => {
  try {
    const record = await get(`/executions/${encodeURIComponent(id)}`);
    const state = execStates.get(id);
    if (!state) return;
    state.limits = limitsOf(record);
    // A run that produced no output still owns the panel, because the note below is
    // the explanation for that silence.
    if (state.limits.length) focusedOutput = id;
    renderLive();
    renderOutput();
  } catch (error) { /* the record may have aged out; the panel stays as rendered */ }
};
const reconcile = async () => {
  // After (re)connecting, pull the authoritative record for every execution we know
  // about so a phone that missed events while asleep catches up exactly.
  const ids = [...execStates.keys()];
  for (const id of ids) {
    try {
      const record = await get(`/executions/${encodeURIComponent(id)}`);
      const state = execStates.get(id);
      if (state) {
        state.stage = stageOf(record.state);
        if (record.state === 'success' || record.state === 'failed' || record.state === 'denied') state.final = record.state;
        state.limits = limitsOf(record);
        if (state.limits.length) focusedOutput = id;
      }
    } catch (error) { /* the record may have aged out; the stream is still live */ }
  }
  renderLive();
  renderOutput();
};
let eventStream = null;
const bindStream = source => {
  ['execution.started', 'node.started', 'node.completed', 'execution.progress',
    'execution.output', 'approval.required', 'authorization.decided', 'execution.completed',
    'execution.denied', 'execution.reconciled', 'execution.cancelled',
    'compensation.unconfirmed', 'compensation.completed',
    'compensation.refused'].forEach(type => {
    source.addEventListener(type, event => {
      let data;
      try { data = JSON.parse(event.data); } catch (error) { return; }
      applyEvent(type, data);
    });
  });
  // EventSource reconnects on its own and replays with Last-Event-ID; reconcile the
  // authoritative state each time the pipe (re)opens so a gap can never mislead.
  source.addEventListener('open', () => { reconcile(); });
};
const openStream = async () => {
  if (getToken()) {
    // A paired phone authenticates its stream with a short-lived watch ticket,
    // because EventSource cannot carry an Authorization header.
    try {
      const response = await fetch('/events/stream-ticket', {headers: authHeaders({accept: 'application/json'})});
      if (response.ok) {
        const body = await response.json();
        const source = new EventSource(`/events?ticket=${encodeURIComponent(body.ticket)}`);
        eventStream = source;
        bindStream(source);
        return;
      }
    } catch (error) { /* fall back to the unauthenticated local view below */ }
  }
  const source = new EventSource('/events');
  eventStream = source;
  bindStream(source);
};
openStream();
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
SHELL_VERSION = "actionmesh-shell-v2"

_SW = """// C16 — offline shell only.
//
// Deliberately narrow: this caches the two control surfaces and the icon so the
// app opens instantly and can show *something* offline. `/voice` is in the list
// because the offline fallback is the dashboard, and a handset that asked for the
// microphone page and received the control room instead would be reading the wrong
// surface. It never caches /api responses,
// /audit, /executions, or any other JSON — a stale capability list, execution
// record or audit chain would be actively misleading, and this system's whole
// purpose is that what it shows you is what actually happened.
const CACHE = '%(version)s';
const SHELL = ['/dashboard', '/voice', '/icon.svg'];

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
