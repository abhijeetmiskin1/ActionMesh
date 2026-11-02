"""VoiceOS — the conversational task surface over ActionMesh.

VoiceOS is a **client**, not an authority. It has no policy engine, no consent store, no
executor and no OS access. Every action it takes is an ordinary ActionMesh call made with
the caller's own credential, and every privileged step in this page is a press a human
made: *send* proposes, *run* advances one authorised step, *approve* decides one named
action. Nothing chains automatically, and no sentence — heard or typed — can skip a step.

What it owns:

* the microphone (one tap, one utterance, never continuous, never self-restarting) and the
  text fallback, which is always available;
* the conversation: what was said, what was answered, and the observed items a later turn
  may refer to ("the second one" resolves to an id the user was actually shown);
* the task view: the steps proposed for a request, each with the registry's own risk and
  permission label, its execution id, and whether it was independently verified;
* the live timeline, driven only by real events, with a parked approval drawn as parked
  rather than as an outcome;
* spoken feedback: short phrases, never command output, and success claimed only from the
  authoritative record.

Recognition and synthesis each sit behind one adapter seam. `docs/voiceos.md` is the
contract and the privacy evaluation.
"""

from __future__ import annotations

_PAGE = r"""<!doctype html>
<html lang="en" data-surface="voiceos">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark">
<title>VoiceOS · ActionMesh</title>
<style>
:root {
  --ink: #edf1ff;
  --ink-soft: #97a3c8;
  --ink-faint: #6b7799;
  --card: rgba(255, 255, 255, 0.045);
  --card-edge: rgba(255, 255, 255, 0.09);
  --accent: #6be3c8;
  --warn: #ffbf6b;
  --bad: #ff7a8a;
  --glow: 0 24px 70px -30px rgba(57, 220, 180, 0.55);
}
* { box-sizing: border-box; }
body {
  margin: 0;
  min-height: 100vh;
  font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Ubuntu, sans-serif;
  color: var(--ink);
  background: radial-gradient(1100px 620px at 18% -8%, #16303a 0%, #0b1220 45%, #070a12 100%);
  padding: env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left);
}
main { max-width: 680px; margin: 0 auto; padding: 22px 18px 48px; }
header { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
.brand { font-size: 13px; letter-spacing: 0.28em; text-transform: uppercase; color: var(--accent); }
.brand small { display: block; letter-spacing: 0.06em; text-transform: none; color: var(--ink-faint); font-size: 11px; }
.nav { color: var(--ink-soft); font-size: 13px; text-decoration: none; border-bottom: 1px solid transparent; }
.nav:hover { color: var(--ink); border-bottom-color: var(--accent); }
p.status-line { margin: 6px 0 0; font-size: 12px; color: var(--ink-faint); }
.stage {
  margin-top: 22px;
  padding: 26px 22px 20px;
  border: 1px solid var(--card-edge);
  border-radius: 22px;
  background: linear-gradient(180deg, rgba(107, 227, 200, 0.07), rgba(255, 255, 255, 0.02));
  box-shadow: var(--glow);
  text-align: center;
}
button.mic {
  position: relative;
  width: 112px; height: 112px; border-radius: 50%;
  border: 1px solid rgba(107, 227, 200, 0.5);
  background: radial-gradient(circle at 35% 30%, #1e5f55, #0d2b2c 70%);
  color: var(--ink); cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center;
  transition: transform 120ms ease, border-color 120ms ease;
}
button.mic:focus-visible { outline: 2px solid var(--accent); outline-offset: 4px; }
button.mic svg { width: 40px; height: 40px; fill: none; stroke: var(--ink); stroke-width: 1.7; }
button.mic::after {
  content: ""; position: absolute; inset: -14px; border-radius: 50%;
  border: 1px solid rgba(107, 227, 200, 0.34); opacity: 0;
}
button.mic[data-state="listening"] { border-color: var(--accent); transform: scale(1.04); }
button.mic[data-state="listening"]::after { opacity: 1; animation: pulse 1.9s ease-out infinite; }
button.mic[data-state="running"] { border-color: var(--warn); }
button.mic[data-state="running"]::after { opacity: 0.7; animation: pulse 2.6s linear infinite; }
button.mic[data-state="approval"] { border-color: var(--bad); }
button.mic[data-state="error"] { border-color: var(--bad); }
@keyframes pulse {
  0% { transform: scale(0.86); opacity: 0.85; }
  100% { transform: scale(1.22); opacity: 0; }
}
@media (prefers-reduced-motion: reduce) {
  button.mic::after { animation: none !important; opacity: 0.4; }
  button.mic { transition: none; }
}
#micState { margin: 14px 0 2px; font-size: 14px; color: var(--ink); min-height: 1.4em; }
#micHint { margin: 0; font-size: 12px; color: var(--ink-faint); }
section.card {
  margin-top: 15px; padding: 16px 19px;
  border: 1px solid var(--card-edge); border-radius: 18px;
  background: var(--card);
}
label { display: block; font-size: 11px; letter-spacing: 0.16em; text-transform: uppercase; color: var(--ink-faint); margin-bottom: 8px; }
textarea {
  width: 100%; min-height: 74px; resize: vertical;
  padding: 12px 13px; border-radius: 12px;
  border: 1px solid var(--card-edge);
  background: rgba(0, 0, 0, 0.28); color: var(--ink);
  font: 15px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
}
textarea:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.row { display: flex; gap: 9px; align-items: center; margin-top: 11px; flex-wrap: wrap; }
button.action {
  border: 1px solid rgba(107, 227, 200, 0.4); border-radius: 11px;
  background: rgba(107, 227, 200, 0.12); color: var(--ink);
  padding: 9px 14px; font-size: 14px; cursor: pointer;
}
button.action:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
button.action:disabled { opacity: 0.45; cursor: not-allowed; }
button.ghost { border-color: var(--card-edge); background: transparent; color: var(--ink-soft); }
button.danger { border-color: rgba(255, 122, 138, 0.5); background: rgba(255, 122, 138, 0.12); }
.note { margin: 10px 0 0; font-size: 12.5px; color: var(--ink-soft); }
.note.error { color: var(--bad); }
h2 { margin: 0 0 11px; font-size: 12px; letter-spacing: 0.16em; text-transform: uppercase; color: var(--ink-faint); }
ul { list-style: none; margin: 0; padding: 0; }
#taskSteps li, #timeline li, #approvalList > div, #resultNodes li, #convoList li {
  padding: 11px 13px; margin-bottom: 9px;
  border: 1px solid var(--card-edge); border-radius: 13px;
  background: rgba(0, 0, 0, 0.22);
}
#convoList li { font-size: 13px; }
#convoList li[data-role="you"] { color: var(--ink-soft); }
#convoList li[data-role="voiceos"] { border-color: rgba(107, 227, 200, 0.28); white-space: pre-wrap; }
#taskSteps li div:first-child { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13.5px; }
.step-state { font-size: 12.5px; margin-top: 5px; color: var(--ink-faint); }
.step-state[data-done="yes"] { color: var(--accent); }
.step-state[data-done="no"] { color: var(--bad); }
.step-state[data-done="wait"] { color: var(--warn); }
.plan-target, .plan-detail { font-size: 12.5px; color: var(--ink-soft); margin-top: 5px; word-break: break-word; }
.plan-detail { color: var(--warn); }
#taskNotes li { font-size: 12.5px; color: var(--ink-faint); margin-bottom: 6px; border: 0; padding: 2px 0; background: none; }
#timeline li { display: flex; gap: 9px; align-items: baseline; font-size: 13px; color: var(--ink-faint); }
#timeline li[data-done="true"] { color: var(--ink); border-color: rgba(107, 227, 200, 0.35); }
#timeline li[data-done="true"]::before { content: "\2713"; color: var(--accent); }
#timeline li[data-done="false"]::before { content: "\00b7"; color: var(--ink-faint); }
#timeline li[data-done="pending"] { color: var(--warn); border-color: rgba(255, 191, 107, 0.4); }
#timeline li[data-done="pending"]::before { content: "!"; color: var(--warn); }
pre {
  margin: 10px 0 0; padding: 11px 12px; max-height: 220px; overflow: auto;
  border-radius: 12px; border: 1px solid var(--card-edge);
  background: rgba(0, 0, 0, 0.38); color: #cfe6dd;
  font: 12.5px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
  white-space: pre-wrap; word-break: break-word;
}
.privacy { font-size: 12px; color: var(--ink-faint); }
.privacy strong { color: var(--ink-soft); font-weight: 600; }
footer { margin-top: 20px; font-size: 11.5px; color: var(--ink-faint); }
.dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: var(--bad); margin-right: 6px; }
.dot.live { background: var(--accent); }
.chip { font-size: 12px; color: var(--ink-soft); border: 1px solid var(--card-edge); border-radius: 999px; padding: 3px 10px; }
.result-line { font-size: 13.5px; margin: 0 0 6px; }
#resultLine, #execHead { font-size: 14px; }
.result-line.verified { color: var(--accent); }
.result-line.unverified { color: var(--warn); }
.result-line.failed { color: var(--bad); }
#liveOutput:empty { display: none; }
[hidden] { display: none !important; }
</style>
</head>
<body>
<main>
<header>
  <div>
    <div class="brand">VoiceOS<small>conversational task control over the ActionMesh pipeline</small></div>
    <p class="status-line"><span id="serviceDot" class="dot"></span><span id="service">Checking the service…</span></p>
    <p class="status-line" id="identity">Checking this session…</p>
    <p class="status-line"><span class="chip" id="deviceChip">Checking the laptop…</span></p>
  </div>
  <a class="nav" href="/dashboard">Control room</a>
</header>

<section class="stage">
  <button id="micButton" class="mic" type="button" data-state="idle" aria-pressed="false" aria-label="Tap to speak">
    <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3a3 3 0 0 1 3 3v5a3 3 0 0 1-6 0V6a3 3 0 0 1 3-3z"/><path d="M6 11a6 6 0 0 0 12 0"/><path d="M12 17v4"/></svg>
  </button>
  <p id="micState">The microphone is off.</p>
  <p id="micHint">One tap opens it for one utterance. It never listens on its own.</p>
  <div class="row" style="justify-content:center">
    <button id="speakToggle" class="action ghost" type="button" aria-pressed="true">Voice feedback: on</button>
    <button id="stopSpeaking" class="action ghost" type="button">Stop speaking</button>
  </div>
</section>

<section class="card">
  <label for="transcript">Say it or type it — the transcript stays editable</label>
  <textarea id="transcript" spellcheck="false" placeholder="What is my laptop running? · run my tests · stop the second one · what's the status?"></textarea>
  <div class="row">
    <button id="sendMessage" class="action" type="button">Ask VoiceOS</button>
    <button id="askStatus" class="action ghost" type="button">Status</button>
    <button id="askStop" class="action ghost" type="button">Stop it</button>
    <button id="askClear" class="action ghost" type="button">Clear conversation</button>
  </div>
  <p class="note" id="messageStatus">Nothing has been asked yet.</p>
</section>

<section class="card">
  <h2>Conversation</h2>
  <ul id="convoList"><li>You have not asked anything yet.</li></ul>
  <ul id="taskNotes"></ul>
</section>

<section class="card" id="taskCard" hidden>
  <h2>Task plan</h2>
  <p class="result-line" id="taskHead">No task.</p>
  <ul id="taskSteps"></ul>
  <div class="row">
    <button id="runTask" class="action" type="button" disabled>Run next step</button>
    <button id="cancelTask" class="action danger" type="button" disabled>Stop the task</button>
  </div>
  <p class="note" id="taskDisclaimer">Each step is its own ActionMesh execution: permission check, then
  approval if the risk asks for one, then the verifier. Nothing runs from the transcript alone.</p>
</section>

<section class="card" id="execCard" hidden>
  <h2>Live execution</h2>
  <p class="result-line" id="execHead">Not started.</p>
  <ul id="timeline"></ul>
  <pre id="liveOutput" aria-live="off"></pre>
  <div class="row">
    <button id="cancelRun" class="action danger" type="button" hidden>Cancel execution</button>
    <span class="note" id="cancelStatus"></span>
  </div>
</section>

<section class="card" id="approvalCard" hidden>
  <h2>Waiting for your decision</h2>
  <div id="approvalList"></div>
  <p class="note">This is the same approval mechanism the control room uses. Nothing here decides for you.</p>
</section>

<section class="card">
  <h2>Result</h2>
  <p class="result-line" id="resultLine">No execution yet.</p>
  <ul id="resultNodes"></ul>
</section>

<section class="card">
  <h2>Where your voice goes</h2>
  <p class="privacy">Recognition is performed by <strong>your browser's own speech service</strong>. On desktop Chrome
  that audio goes to the browser vendor, not to this laptop and not to us; on Android it is handled by Google's
  speech service, and in a WebView or a browser without recognition there is no microphone path at all — you type
  instead. <strong>ActionMesh receives text</strong>: the transcript, and nothing else. This page has no audio
  capture, stores nothing on your device, and keeps no recording — the conversation lives on the laptop as task
  state, never as audio.</p>
</section>

<footer id="capabilityCount">Loading the laptop's capability list…</footer>
</main>

<script>
const tokenKey = 'actionmesh.device.token';
const getToken = () => { try { return sessionStorage.getItem(tokenKey) || ''; } catch (error) { return ''; } };
const setToken = value => { try { value ? sessionStorage.setItem(tokenKey, value) : sessionStorage.removeItem(tokenKey); } catch (error) {} };
const authHeaders = extra => {
  const headers = Object.assign({'content-type': 'application/json'}, extra || {});
  const token = getToken();
  if (token) headers.authorization = 'Bearer ' + token;
  return headers;
};
const el = id => document.querySelector('#' + id);
// The session id is kept so a reload can find the task it was looking at. It buys no
// authority: every read is still scoped by the server to this actor, and resuming never
// runs a step.
const sessionKey = 'actionmesh.voice.session';
const storedSession = () => { try { return sessionStorage.getItem(sessionKey) || ''; } catch (error) { return ''; } };
const rememberSession = id => {
  state.sessionId = id || '';
  try { id ? sessionStorage.setItem(sessionKey, id) : sessionStorage.removeItem(sessionKey); } catch (error) {}
};
const micButton = el('micButton');
// A paired phone's principal comes from its credential, so the body must not name an
// actor at all; only the local operator view may, and the server still decides what that
// means. Same rule as the control room, no second version of it.
const identityBody = () => (getToken() ? {} : {actor: {id: 'voice-user'}});
// A GET carries no body, so the local operator view names its actor in the query, the
// way the consent-listing route already does. A paired phone never does: its credential
// is the identity.
const conversationPath = () => (getToken()
  ? '/voiceos/conversation'
  : '/voiceos/conversation?actor_id=' + encodeURIComponent('voice-user'));
const readJson = async response => {
  let body = null;
  try { body = await response.json(); } catch (error) { body = null; }
  if (!response.ok) {
    if (response.status === 401) setToken('');
    throw new Error((body && body.detail) || 'The laptop refused this request (' + response.status + ').');
  }
  return body;
};
const get = async path => readJson(await fetch(path, {headers: authHeaders({accept: 'application/json'})}));
const post = async (path, payload) => readJson(await fetch(path, {
  method: 'POST', headers: authHeaders(), body: JSON.stringify(payload)
}));
const setPhase = (phase, message) => {
  micButton.dataset.state = phase;
  if (message) el('micState').textContent = message;
};
const setMessage = (message, isError) => {
  const box = el('messageStatus');
  box.textContent = message;
  box.classList.toggle('error', Boolean(isError));
};
const say = message => { el('micState').textContent = message; };

// Which step states are over. `interrupted` belongs here for the same reason it is
// terminal on the execution: nothing is running, and the next step is not offered just
// because a page came back.
const STEP_SETTLED = ['succeeded', 'succeeded_unverified', 'failed', 'denied', 'cancelled', 'skipped', 'interrupted'];
const state = {
  sessionId: '', task: null, executionId: null, running: false, cancelRequested: false,
  output: '', nodes: {}, recognizer: null, listening: false,
  transcriptDirty: false, writing: false, speakEnabled: true,
  reported: null, reattachedTo: ''
};
// Which outcomes are actually over. `waiting_permission` is deliberately absent: a run
// that parks publishes `execution.completed` because the *request* ended, not the
// execution — reading it as a result would announce an outcome that has not happened.
// `interrupted` is terminal too: the server lost the thread and says so. Reading it as
// "still running" would keep a Cancel button live on a run nothing is watching.
const TERMINAL = ['success', 'failed', 'denied', 'cancelled', 'timeout', 'partially_completed', 'interrupted'];
const isTerminal = outcome => TERMINAL.includes(outcome);

// -- transcript --
const text = () => el('transcript').value.trim();
const setTranscript = value => {
  state.writing = true;
  el('transcript').value = value;
  state.transcriptDirty = false;
  state.writing = false;
};
el('transcript').addEventListener('input', () => {
  // A late recognition result must not erase a word the user just fixed by hand, and an
  // edited request is a new request: the task on screen was built from the old words, so
  // its steps must not stay runnable.
  if (!state.writing) state.transcriptDirty = true;
  syncTranscript();
});
const syncTranscript = () => {
  if (!state.task) return;
  if (text() === state.task.request) return;
  state.task = null;
  el('runTask').disabled = true;
  el('cancelTask').disabled = true;
  setMessage('The request was edited — the task on screen no longer matches it. Ask again first.');
};

// -- conversation --
const renderConversation = conversation => {
  const list = el('convoList');
  list.replaceChildren();
  const history = (conversation && conversation.history) || [];
  if (!history.length) {
    const empty = document.createElement('li');
    empty.textContent = 'You have not asked anything yet.';
    list.appendChild(empty);
    return;
  }
  history.slice(-12).forEach(entry => {
    const li = document.createElement('li');
    li.dataset.role = entry.role === 'user' ? 'you' : 'voiceos';
    li.textContent = (entry.role === 'user' ? 'You: ' : 'VoiceOS: ') + entry.text;
    list.appendChild(li);
  });
};
const renderNotes = notes => {
  const list = el('taskNotes');
  list.replaceChildren();
  // A provider that could not be reached, a template that was refused, a clause the
  // planner dropped: all of it is shown rather than quietly worked around.
  (notes || []).forEach(note => {
    const li = document.createElement('li');
    li.textContent = 'note · ' + note;
    list.appendChild(li);
  });
};
const sendMessage = async body => {
  const button = el('sendMessage');
  button.disabled = true;
  setPhase('running', 'Thinking…');
  try {
    const answer = await post('/voiceos/messages', body);
    setMessage(answer.reply || 'Answered.');
    renderNotes(answer.notes);
    renderConversation(await get(conversationPath()));
    if (answer.session_id) {
      rememberSession(answer.session_id);
      await loadTask();
    } else {
      el('taskCard').hidden = false;
      el('runTask').disabled = true;
      el('cancelTask').disabled = true;
    }
    // The state line must not stay on "Thinking…" once the answer has landed.
    setPhase('idle', 'Ready — tap to speak.');
    if (answer.kind !== 'answer') speak('Here is the plan. Nothing has run yet.');
    return answer;
  } catch (error) {
    setMessage(error.message, true);
    setPhase('error', 'The laptop could not answer.');
    speak('I could not reach the laptop.');
    return null;
  } finally {
    button.disabled = false;
  }
};
const askMessage = async preset => {
  setTranscript(preset);
  await sendMessage({...identityBody(), text: preset});
};
el('sendMessage').addEventListener('click', () => {
  const intent = text();
  if (!intent) { setMessage('Say or type something first.', true); return; }
  sendMessage({...identityBody(), text: intent});
});
el('askStatus').addEventListener('click', () => askMessage("what's the status?"));
el('askStop').addEventListener('click', () => askMessage('stop it'));
el('askClear').addEventListener('click', () => askMessage('clear the conversation'));

// -- task plan --
const capabilitiesByType = new Map();
const describe = type => {
  // Registry metadata, never the proposal's own words about itself.
  const cap = capabilitiesByType.get(type);
  if (!cap) return 'not registered on this laptop — policy will refuse it';
  const needs = cap.permissions.length ? 'needs ' + cap.permissions.join(', ') : 'no permission recorded';
  return cap.risk_level + ' risk · ' + needs;
};
const STEP_DONE = {succeeded: 'yes', succeeded_unverified: 'wait', failed: 'no', denied: 'no', cancelled: 'no', skipped: 'wait', awaiting_approval: 'wait', running: 'wait'};
const renderTask = task => {
  state.task = task;
  el('taskCard').hidden = false;
  el('taskHead').textContent = task.session_id + ' · ' + task.status + ' · ' + task.request;
  const list = el('taskSteps');
  list.replaceChildren();
  (task.steps || []).forEach(step => {
    const li = document.createElement('li');
    const line = document.createElement('div');
    line.textContent = (step.index + 1) + '. ' + step.label;
    const target = document.createElement('div');
    target.className = 'plan-target';
    const parameters = step.parameters || {};
    const shown = Object.keys(parameters).map(key => key + ' = ' + parameters[key]).join(' · ');
    target.textContent = shown ? 'target: ' + shown : 'no parameters';
    const detail = document.createElement('div');
    detail.className = 'plan-detail';
    detail.textContent = describe(step.action_type);
    const stateLine = document.createElement('div');
    stateLine.className = 'step-state';
    stateLine.dataset.done = STEP_DONE[step.state] || '';
    stateLine.textContent = step.state
      + (step.verified === true ? ' · verified' : (step.verified === false ? ' · NOT verified' : ''))
      + (step.execution_id ? ' · ' + step.execution_id : '')
      + (step.error ? ' · ' + step.error : '');
    li.appendChild(line);
    li.appendChild(target);
    li.appendChild(detail);
    li.appendChild(stateLine);
    list.appendChild(li);
  });
  const open = (task.steps || []).some(step => !STEP_SETTLED.includes(step.state));
  el('runTask').disabled = !open;
  el('cancelTask').disabled = !(task.steps || []).some(step => ['running', 'awaiting_approval'].includes(step.state));
  if (task.summary) setMessage(task.summary);
};
const loadTask = async () => {
  if (!state.sessionId) return;
  try {
    renderTask(await get('/voiceos/sessions/' + encodeURIComponent(state.sessionId)));
  } catch (error) {
    setMessage(error.message, true);
    return;
  }
  await reattach();
};
const reattach = async () => {
  // A page that comes back to a task whose step never finished has one job: find out
  // what is true now. It reads, it subscribes, and it stops. Running the next step just
  // because the tab reloaded would be a side effect nobody asked for.
  if (state.executionId || state.reattachedTo === state.sessionId) return;
  const live = ((state.task && state.task.steps) || []).filter(
    step => step.execution_id && !STEP_SETTLED.includes(step.state)
  );
  const last = live.slice(-1)[0];
  if (!last) return;
  state.reattachedTo = state.sessionId;
  state.executionId = last.execution_id;
  state.reported = null;
  el('execCard').hidden = false;
  el('execHead').textContent = last.execution_id + ' · reconnecting…';
  setPhase('running', 'Reconnecting to execution…');
  await openStream();
  await reconcile();
};
const resumeSession = async () => {
  const stored = storedSession();
  if (stored) {
    rememberSession(stored);
    await loadTask();
    return;
  }
  try {
    const rows = await get('/voiceos/sessions');
    const open = (rows || []).find(row => !row.settled || row.settled < row.steps);
    if (open) {
      rememberSession(open.session_id);
      await loadTask();
    }
  } catch (error) { /* nothing to resume on a fresh tab */ }
};

// -- execution --
const runTask = async () => {
  // The only path from a task to the executor is this press. The conversation, the
  // microphone and the page load all stop short of it.
  if (!state.sessionId) return;
  el('runTask').disabled = true;
  el('execCard').hidden = false;
  resetTimeline();
  markStep('understood', true);
  markStep('planned', true);
  state.output = '';
  state.nodes = {};
  state.cancelRequested = false;
  el('liveOutput').textContent = '';
  el('cancelStatus').textContent = '';
  el('execHead').textContent = 'Asking the executor to advance one step…';
  setPhase('running', 'Working…');
  // Subscribe before asking: output is transient and never replayed, so a page that
  // opens the stream after the response has already missed the run it reports.
  state.reported = null;
  state.executionId = null;
  await openStream();
  try {
    const task = await post('/voiceos/sessions/' + encodeURIComponent(state.sessionId) + '/run', {...identityBody()});
    await openStream();
    renderTask(task);
    const latest = (task.steps || []).slice(-1)[0] || {};
    if (latest.execution_id) {
      state.executionId = latest.execution_id;
      await reconcile();
    }
    if (task.status === 'awaiting_approval') {
      setPhase('approval', 'Waiting for your approval.');
      markStep('approval', 'pending');
      await refreshApprovals();
    }
  } catch (error) {
    el('execHead').textContent = 'The laptop refused the step.';
    setMessage(error.message, true);
    setPhase('error', 'Refused.');
    speak('The step was refused.');
  } finally {
    el('runTask').disabled = false;
  }
};
el('runTask').addEventListener('click', runTask);
const cancelTask = async () => {
  if (!state.sessionId) return;
  state.cancelRequested = true;
  el('cancelStatus').textContent = 'Cancellation requested — waiting for the laptop to confirm.';
  setPhase('running', 'Cancelling…');
  try {
    const answer = await post('/voiceos/sessions/' + encodeURIComponent(state.sessionId) + '/cancel', {});
    if (answer.status === 'nothing_running') {
      el('cancelStatus').textContent = answer.detail;
      setPhase('idle', null);
      return;
    }
    state.executionId = answer.execution_id;
    speak('Cancellation requested.');
    loadTask();
  } catch (error) {
    el('cancelStatus').textContent = 'The laptop refused the cancellation: ' + error.message;
  }
};
el('cancelTask').addEventListener('click', cancelTask);

// -- approval --
const refreshApprovals = async () => {
  el('approvalCard').hidden = false;
  const box = el('approvalList');
  box.replaceChildren();
  let waiting = [];
  try {
    waiting = await get('/approvals');
  } catch (error) {
    const row = document.createElement('div');
    row.textContent = 'The approval list could not be read: ' + error.message;
    box.appendChild(row);
    return;
  }
  if (!waiting.length) {
    const row = document.createElement('div');
    row.textContent = 'Nothing is waiting for a decision.';
    box.appendChild(row);
    return;
  }
  waiting.forEach(item => {
    (item.actions || []).forEach(action => {
      const panel = document.createElement('div');
      const head = document.createElement('div');
      head.textContent = item.execution_id + ' · ' + action.action_type + ' (' + action.risk_level + ')';
      panel.appendChild(head);
      const row = document.createElement('div');
      row.className = 'row';
      const approve = document.createElement('button');
      approve.className = 'action';
      approve.type = 'button';
      approve.textContent = 'Approve this action';
      approve.addEventListener('click', () => decideApproval(item.execution_id, action, 'approved'));
      const reject = document.createElement('button');
      reject.className = 'action ghost';
      reject.type = 'button';
      reject.textContent = 'Reject';
      reject.addEventListener('click', () => decideApproval(item.execution_id, action, 'rejected'));
      row.appendChild(approve);
      row.appendChild(reject);
      panel.appendChild(row);
      box.appendChild(panel);
    });
  });
};
const decideApproval = async (executionId, action, status) => {
  // One press per decision, on the action it names. Nothing in this file approves on a
  // timer, on load, or as a side effect of running.
  const buttons = el('approvalList').querySelectorAll('button');
  buttons.forEach(button => { button.disabled = true; });
  try {
    await post('/approvals', {
      ...identityBody(),
      execution_id: executionId,
      action_id: action.action_id,
      action_type: action.action_type,
      status
    });
    el('approvalCard').hidden = true;
    markStep('approval', true);
    if (status === 'approved') {
      setPhase('running', 'Approved — press Run next step to continue the task.');
      speak('Approved. Press run next step to continue.');
    } else {
      setPhase('idle', 'Rejected. Nothing ran.');
    }
    await loadTask();
  } catch (error) {
    setMessage(error.message, true);
    buttons.forEach(button => { button.disabled = false; });
  }
};

// -- live stream and timeline --
const STEPS = [
  ['understood', 'Request understood'],
  ['planned', 'Plan prepared'],
  ['permission', 'Permission check'],
  ['approval', 'Waiting for your approval'],
  ['running', 'Running on the laptop'],
  ['output', 'Streaming output'],
  ['verifying', 'Verifying the result'],
  ['result', 'Result reported']
];
const stepRows = new Map();
const resetTimeline = () => {
  const list = el('timeline');
  list.replaceChildren();
  stepRows.clear();
  STEPS.forEach(step => {
    const li = document.createElement('li');
    li.dataset.done = 'false';
    li.textContent = step[1];
    list.appendChild(li);
    stepRows.set(step[0], li);
  });
};
const markStep = (key, done) => {
  // A step is only ever marked from something that actually happened. Nothing here
  // animates ahead of the pipeline.
  const row = stepRows.get(key);
  if (!row) return;
  row.dataset.done = done === 'pending' ? 'pending' : (done ? 'true' : 'false');
};
const renderNodes = nodes => {
  Object.keys(nodes).forEach(id => { state.nodes[id] = nodes[id]; });
  const box = el('resultNodes');
  box.replaceChildren();
  Object.keys(state.nodes).sort().forEach(id => {
    const node = state.nodes[id] || {};
    if (!node.action_type) return;
    const li = document.createElement('li');
    li.className = node.verified === true ? 'result-line verified' : (node.verified === false ? 'result-line unverified' : 'result-line');
    li.textContent = id + ' · ' + node.action_type + ' · ' + node.state
      + (node.verified === true ? ' · verified' : (node.verified === false ? ' · not verified' : ''));
    box.appendChild(li);
  });
};
const appendOutput = chunk => {
  state.output = (state.output + chunk).slice(-8000);
  const box = el('liveOutput');
  box.textContent = state.output;
  box.scrollTop = box.scrollHeight;
  markStep('output', true);
};
let eventStream = null;
const bindStream = source => {
  const on = (type, handler) => source.addEventListener(type, event => {
    let data;
    try { data = JSON.parse(event.data); } catch (error) { return; }
    // Only ever the execution this page is watching -- and only when it knows which one
    // that is. The stream is actor-scoped server-side, but since C35 it also replays a
    // retained timeline, and the first event of a replay is whatever this actor's
    // earliest retained run published. A page that adopts that id reports an hours-old
    // outcome as the result of the step it just pressed; the run's own id comes from the
    // response, which is the only place it is honestly known.
    if (!state.executionId || data.execution_id !== state.executionId) return;
    handler(data.payload || {}, data);
  });
  on('node.started', () => { markStep('running', true); setPhase('running', 'Running on the laptop…'); });
  on('authorization.decided', () => markStep('permission', true));
  on('approval.required', () => {
    markStep('approval', 'pending');
    setPhase('approval', 'Waiting for your approval.');
    speak('Approval is required before continuing.');
    refreshApprovals();
  });
  on('execution.output', payload => appendOutput(payload.chunk || ''));
  on('execution.progress', payload => {
    el('execHead').textContent = state.executionId + ' · ' + payload.completed + '/' + payload.total + ' steps';
  });
  on('node.completed', payload => {
    state.nodes[payload.node_id] = payload;
    if (payload.verified === true || payload.verified === false) markStep('verifying', true);
    renderNodes({});
  });
  on('execution.reconciled', payload => {
    // The server restarted over a live run and adopted it: the work is still happening,
    // and this process is now the one watching it.
    say('Execution status recovered from the authoritative record.');
    el('execHead').textContent = state.executionId + ' · ' + (payload.observed || 'reconciled');
    markStep('running', true);
  });
  on('execution.cancelled', payload => {
    state.cancelRequested = true;
    el('cancelStatus').textContent = (payload.unconfirmed || []).length
      ? 'Cancelled. The dispatched effect is unconfirmed.'
      : 'Cancelled — the action stopped.';
  });
  on('compensation.unconfirmed', () => {
    el('cancelStatus').textContent = 'An effect may still be standing — the outcome is unconfirmed.';
  });
  on('execution.denied', () => {
    state.running = false;
    el('cancelRun').hidden = true;
    markStep('result', true);
    setPhase('error', 'Denied by policy.');
    el('resultLine').textContent = 'Denied — nothing ran.';
    el('resultLine').className = 'result-line failed';
    speak('Policy denied the request. Nothing ran.');
  });
  on('execution.completed', payload => {
    // A parked run publishes this event too, and it is not an outcome.
    if (!isTerminal(payload.state)) return;
    state.running = false;
    el('cancelRun').hidden = true;
    finishRun(payload);
  });
  // Reconnect replays the retained window; the record is the authority, so re-read it.
  source.addEventListener('open', () => { if (state.executionId) reconcile(); });
};
const openStream = async () => {
  if (eventStream) return eventStream;
  if (getToken()) {
    // EventSource cannot carry an Authorization header, so a paired phone watches
    // through a short-lived ticket minted from its credential.
    try {
      const body = await get('/events/stream-ticket');
      const source = new EventSource('/events?ticket=' + encodeURIComponent(body.ticket));
      eventStream = source;
      bindStream(source);
      return source;
    } catch (error) { /* fall through to the local view */ }
  }
  const source = new EventSource('/events');
  eventStream = source;
  bindStream(source);
  return source;
};
const reconcile = async () => {
  if (!state.executionId) return;
  try {
    const record = await get('/executions/' + encodeURIComponent(state.executionId));
    const nodes = record.results || {};
    renderNodes(nodes);
    state.running = !isTerminal(record.state);
    el('cancelRun').hidden = !state.running;
    if (state.running) {
      // The record, not a guess, is what makes this claim safe to show.
      el('execHead').textContent = state.executionId + ' · reattached';
      say(record.cancel_requested
        ? 'Reattached. Cancellation was requested — checking whether the action has stopped.'
        : 'Reattached to the active execution.');
      if (record.cancel_requested) {
        state.cancelRequested = true;
        el('cancelStatus').textContent = 'Cancellation was requested. Checking whether the action has stopped.';
      }
      return;
    }
    showResult({
      state: record.state,
      nodes: nodes,
      compensation_required: record.compensation_required
    });
    if (isTerminal(record.state) && state.reported !== state.executionId) {
      await finishRun({state: record.state});
    }
  } catch (error) { /* the stream still carries what happened */ }
};

// -- result and spoken feedback --
const finishRun = async payload => {
  // The response and the event stream can describe the same ending. Say it once, and
  // say it from the record: an event payload carries no per-node verification.
  if (state.reported === state.executionId) return;
  state.reported = state.executionId;
  const outcome = payload.state;
  markStep('result', true);
  state.running = false;
  el('cancelRun').hidden = true;
  setPhase(outcome === 'success' ? 'idle' : 'error', null);
  el('execHead').textContent = state.executionId + ' · ' + outcome;
  await reconcile();
  await loadTask();
  const nodes = state.nodes;
  const verified = Object.keys(nodes).filter(id => (nodes[id] || {}).verified === true).length;
  const unverified = Object.keys(nodes).filter(id => (nodes[id] || {}).verified === false).length;
  if (outcome === 'success') {
    const clean = verified > 0 && unverified === 0;
    speak(clean ? 'Done. Verified by an independent check.' : 'Done, but the result was not independently verified.');
    say(clean ? 'Completed and verified.' : 'Completed, not verified.');
  } else if (outcome === 'cancelled') {
    speak('Cancelled. The action had already been dispatched, so its effect could not be confirmed.');
    say('Cancelled — effect unconfirmed.');
  } else if (outcome === 'denied') {
    speak('Policy denied the request. Nothing ran.');
    say('Denied.');
  } else if (outcome === 'interrupted') {
    // No green light, and no claim about the effect: the server restarted and the
    // outcome was never established. The agent's ledger may still say more than this,
    // and the record's own text is what is shown.
    speak('The server restarted while this action was active. Its final effect could not be fully confirmed.');
    say('Interrupted by a restart — effect unconfirmed.');
  } else {
    speak('The step finished as ' + outcome + '. Check the details below.');
    say('Finished as ' + outcome + '.');
  }
  if (state.cancelRequested && (outcome === 'success' || outcome === 'failed')) {
    el('cancelStatus').textContent = 'It finished before the cancellation took effect.';
  } else if (state.cancelRequested && outcome === 'cancelled') {
    el('cancelStatus').textContent = 'Cancellation confirmed.';
  }
};
const showResult = result => {
  if (!result || !result.state) return;
  const line = el('resultLine');
  const nodes = result.nodes || {};
  const verified = Object.keys(nodes).filter(id => (nodes[id] || {}).verified === true);
  const unverified = Object.keys(nodes).filter(id => (nodes[id] || {}).verified === false);
  line.className = result.state === 'success' && verified.length ? 'result-line verified'
    : (result.state === 'success' ? 'result-line unverified' : 'result-line failed');
  line.textContent = result.state
    + (verified.length ? ' · ' + verified.length + ' node(s) verified by an independent check' : '')
    + (unverified.length ? ' · ' + unverified.length + ' node(s) NOT verified' : '')
    + (result.compensation_required ? ' · an effect may still be standing' : '');
};

// -- cancellation of the live step --
const cancelRun = async () => {
  if (!state.executionId) return;
  el('cancelRun').disabled = true;
  el('cancelStatus').textContent = 'Cancellation requested — waiting for the laptop to confirm.';
  state.cancelRequested = true;
  setPhase('running', 'Cancelling…');
  try {
    await post('/executions/' + encodeURIComponent(state.executionId) + '/cancel', {});
    speak('Cancellation requested.');
  } catch (error) {
    el('cancelStatus').textContent = 'The laptop refused the cancellation: ' + error.message;
    el('cancelRun').disabled = false;
  }
};
el('cancelRun').addEventListener('click', cancelRun);

// -- speech output --
const SPEAK_ADAPTERS = [
  {
    name: 'browser-native',
    create: () => {
      if (!('speechSynthesis' in window) || !('SpeechSynthesisUtterance' in window)) return null;
      return {
        speak: (line, done) => {
          const utterance = new window.SpeechSynthesisUtterance(line);
          utterance.rate = 1.05;
          // Interruptible: a new tap on the microphone cancels what is being said, so
          // the user is never waiting for a sentence to finish before speaking again.
          utterance.onend = done;
          utterance.onerror = done;
          window.speechSynthesis.cancel();
          window.speechSynthesis.speak(utterance);
        },
        cancel: () => window.speechSynthesis.cancel()
      };
    }
  }
];
const createSpeaker = () => {
  for (const adapter of SPEAK_ADAPTERS) {
    const speaker = adapter.create();
    if (speaker) return speaker;
  }
  return null;
};
const speaker = createSpeaker();
const speak = line => {
  // Short phrases only, and only about what the record already says. Command output is
  // never spoken, and nothing here announces success before the executor reports it.
  if (!state.speakEnabled || !speaker) return;
  try { speaker.speak(line, () => {}); } catch (error) {}
};
el('stopSpeaking').addEventListener('click', () => {
  if (speaker) speaker.cancel();
});
el('speakToggle').addEventListener('click', () => {
  state.speakEnabled = !state.speakEnabled;
  const button = el('speakToggle');
  button.setAttribute('aria-pressed', state.speakEnabled ? 'true' : 'false');
  button.textContent = 'Voice feedback: ' + (state.speakEnabled ? 'on' : 'off');
  if (speaker && !state.speakEnabled) speaker.cancel();
});
if (!speaker) {
  el('speakToggle').disabled = true;
  el('speakToggle').textContent = 'Voice feedback: unavailable here';
}

// -- push to talk --
const SPEECH_ADAPTERS = [
  {
    name: 'browser-native',
    create: () => {
      const Native = window.SpeechRecognition || window.webkitSpeechRecognition;
      if (!Native) return null;
      const recognizer = new Native();
      recognizer.lang = 'en-US';
      recognizer.interimResults = true;
      recognizer.maxAlternatives = 1;
      // Never continuous: one tap is one utterance. A recognizer left open is a
      // microphone left open.
      recognizer.continuous = false;
      return recognizer;
    }
  }
];
const createRecognizer = () => {
  for (const adapter of SPEECH_ADAPTERS) {
    const recognizer = adapter.create();
    if (recognizer) return recognizer;
  }
  return null;
};
const sayUnavailable = () => {
  say('Voice recognition is not available on this device. Type your request instead.');
  el('micHint').textContent = 'The text path works everywhere; nothing else on this page needs a microphone.';
};
const stopListening = () => {
  if (state.recognizer) {
    try { state.recognizer.stop(); } catch (error) {}
  }
};
const toggleListening = () => {
  if (state.listening) { stopListening(); return; }
  const recognizer = createRecognizer();
  if (!recognizer) { sayUnavailable(); return; }
  // Barge-in: talking over a spoken answer stops the answer. The microphone is the
  // user's turn, and it never waits politely for a sentence to end.
  if (speaker) { try { speaker.cancel(); } catch (error) {} }
  state.recognizer = recognizer;
  state.listening = true;
  micButton.setAttribute('aria-pressed', 'true');
  setPhase('listening', 'Listening…');
  wireRecognizer(recognizer);
  recognizer.start();
};
const finishListening = () => {
  state.listening = false;
  state.recognizer = null;
  micButton.setAttribute('aria-pressed', 'false');
};
const wireRecognizer = recognizer => {
  recognizer.onresult = event => {
    let finalText = '';
    for (let index = event.resultIndex; index < event.results.length; index += 1) {
      const result = event.results[index];
      const heard = result[0] && result[0].transcript ? result[0].transcript.trim() : '';
      if (!heard) continue;
      if (result.isFinal) finalText = heard;
      else say('Heard: ' + heard);
    }
    if (!finalText) return;
    if (state.transcriptDirty) {
      say('Recognition offered: ' + finalText + ' — your edit was kept.');
      return;
    }
    setTranscript(finalText);
    // Understanding is inert: it asks what the laptop would propose. Running is a press.
    setPhase('running', 'Processing…');
    sendMessage({...identityBody(), text: finalText});
  };
  recognizer.onerror = event => {
    say('Recognition stopped: ' + (event.error || 'the browser would not say why.'));
    finishListening();
    setPhase('error', 'The microphone is off.');
  };
  // Deliberately not a restart. An onend that re-opens the microphone turns one
  // deliberate tap into permanent listening.
  recognizer.onend = () => { finishListening(); setPhase('idle', 'The microphone is off.'); };
};
micButton.addEventListener('click', toggleListening);
document.addEventListener('visibilitychange', () => {
  if (document.hidden) stopListening();
});

// -- session and service --
const paintIdentity = () => {
  el('identity').textContent = getToken()
    ? 'Paired device session — the server decides what this request may do.'
    : 'Local operator session — no pairing token in this tab.';
};
const paintDevices = list => {
  const chip = el('deviceChip');
  const laptops = (list || []).filter(device => device.online);
  if (!laptops.length) {
    chip.textContent = 'No laptop online — actions will be refused';
    return;
  }
  // One agent serves this server, so the chip names it truthfully rather than offering
  // a picker that would change nothing about where the action runs.
  const first = laptops[0];
  chip.textContent = (first.name || first.device_id) + ' · online'
    + (laptops.length > 1 ? ' · ' + laptops.length + ' registered, one agent per server' : '');
};
const ping = async () => {
  const dot = el('serviceDot');
  try {
    const health = await get('/health');
    dot.classList.add('live');
    el('service').textContent = 'Service online · ' + health.protocol;
  } catch (error) {
    dot.classList.remove('live');
    el('service').textContent = 'Service unreachable — you can still read this page.';
  }
};
const loadCapabilities = async () => {
  try {
    const list = await get('/capabilities');
    list.forEach(item => capabilitiesByType.set(item.action, item));
    el('capabilityCount').textContent = list.length + ' action types registered on this laptop.';
  } catch (error) {
    el('capabilityCount').textContent = 'Capability list unavailable — risk labels will say "not registered".';
  }
};
resetTimeline();
paintIdentity();
if (!createRecognizer()) sayUnavailable();
loadCapabilities();
ping();
get('/devices').then(paintDevices).catch(() => { el('deviceChip').textContent = 'Device list unavailable'; });
get(conversationPath()).then(renderConversation).catch(() => {});
resumeSession();
window.setInterval(ping, 20000);
</script>
</body>
</html>
"""


def page() -> str:
    return _PAGE


__all__ = ["page"]
