'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const { install } = require('./dom_harness');

test('all classic renderer scripts share a browser global and reach backend ready', async () => {
  const page = install({ bridge: {
    state: async () => ({backend: {status: 'ready'}, settings: {onboardingCompleted: true}}),
    request: async () => ({ok: true, body: []}),
  }});
  const ui = path.resolve(__dirname, '../ui');
  page.window.window = page.window;
  const context = vm.createContext(page.window);
  const html = fs.readFileSync(path.join(ui, 'index.html'), 'utf8');
  const scripts = [...html.matchAll(/<script src="([^"]+)"/g)].map(match => match[1]);
  assert.ok(scripts.includes('history.js'));
  for (const filename of scripts) {
    vm.runInContext(fs.readFileSync(path.join(ui, filename), 'utf8'), context, {filename});
  }
  const history = page.window.ActionMeshHistory;
  assert.equal(history.isTerminal('success'), true);
  assert.equal(history.isTerminal('executing'), false);
  assert.notEqual(history.handleStreamEvent, vm.runInContext('handleStreamEvent', context));
  await vm.runInContext('boot()', context);
  assert.equal(page.document.getElementById('railStatus').textContent, 'Backend ready');
  assert.ok(page.calls.some(call => call.name === 'state'));
  await vm.runInContext('refreshSettings()', context);
  const firstLane = page.document.getElementById('settingsProviderKv').children[0];
  await vm.runInContext('refreshSettings()', context);
  assert.equal(page.document.getElementById('settingsProviderKv').children[0], firstLane);
  await vm.runInContext('Promise.all([...viewRefreshes.values()])', context);
  let refreshCalls = 0;
  page.window.actionmesh.state = async () => {
    refreshCalls++;
    return {backend:{status:'ready'},settings:{onboardingCompleted:true}};
  };
  vm.runInContext("state.view='settings'", context);
  const firstRefresh = vm.runInContext('refreshCurrent()', context);
  const secondRefresh = vm.runInContext('refreshCurrent()', context);
  assert.equal(firstRefresh, secondRefresh);
  await firstRefresh;
  assert.equal(refreshCalls, 1);
  const tabs = page.document.querySelectorAll('[data-settings-tab]');
  assert.equal(tabs.length, 3);
  for (const tab of tabs) {
    tab.click();
    assert.equal(tab.getAttribute('aria-selected'), 'true');
    const panels = page.document.querySelectorAll('[data-settings-panel]');
    assert.equal([...panels].filter(panel => !panel.hidden).length, 1);
    assert.equal(page.document.getElementById(tab.getAttribute('aria-controls')).hidden, false);
  }
  tabs[2].dispatch('keydown', {key: 'Home'});
  assert.equal(tabs[0].getAttribute('aria-selected'), 'true');
  tabs[0].dispatch('keydown', {key: 'ArrowLeft'});
  assert.equal(tabs[2].getAttribute('aria-selected'), 'true');
  assert.equal(vm.runInContext('providerDisplayState({state:"not_configured",provider:"openai_compatible",hasKey:true})', context), 'Not tested');
  assert.equal(vm.runInContext('providerDisplayState({state:"not_configured"})', context), 'Not configured');

  const reply = page.document.getElementById('voiceReply');
  vm.runInContext(`
    let requestedSession;
    runSession = (id) => { requestedSession = id; };
    renderReply(byId('voiceReply'), { kind: 'task', session_id: 'screen-task', target_online: true });
  `, context);
  assert.equal(vm.runInContext('requestedSession', context), undefined);
  reply.querySelector('button').click();
  assert.equal(vm.runInContext('requestedSession', context), 'screen-task');

  const requests = [];
  let requiresReview = false;
  let listFiles = false;
  page.window.actionmesh.request = async (request) => {
    if (request.method !== 'POST') return {ok: true, body: []};
    requests.push(request);
    if (request.path === '/voiceos/messages') {
      return {ok: true, body: {kind: 'task', session_id: 'screen-task', target_online: true}};
    }
    return {ok: true, body: requiresReview
      ? {review_required: true}
      : {status: 'completed', steps: [
        listFiles
          ? {action_type: 'device.file.list', observed: {count: 3}, verified: true}
          : {action_type: 'device.screen.capture', observed: {status: 'captured'}},
      ]}};
  };
  const spokenReplies = [];
  let transcript;
  let onVoiceState;
  let recordings = 0;
  page.window.VoiceOSVoice = {
    setState() {},
    getLanguage: () => 'en-US',
    setLanguage() {},
    textToSpeechSupported: true,
    speak: (value) => spokenReplies.push(value),
    onTranscript: (callback) => { transcript = callback; },
    onStateChange: (callback) => { onVoiceState = callback; },
    startListening: () => { recordings++; return true; },
    onError() {},
  };
  vm.runInContext('voiceWired = false; wireVoice();', context);
  transcript('टेक', {final: false});
  assert.equal(requests.length, 0);
  transcript('टेक क्रिंशोट', {final: true});
  transcript('टेक क्रिंशोट', {final: true});
  await new Promise(setImmediate);
  assert.deepEqual(requests.map(({path}) => path), [
    '/voiceos/messages', '/voiceos/sessions/screen-task/run',
  ]);
  assert.equal(requests[1].body.hands_free, true);
  assert.match(spokenReplies[0], /reported a screenshot capture/);

  requiresReview = true;
  transcript('Delete a file', {final: true});
  await new Promise(setImmediate);
  assert.match(spokenReplies[1], /needs review/);
  assert.equal(reply.querySelector('button').textContent, 'Run task');

  requiresReview = false;
  listFiles = true;
  const followUp = page.document.getElementById('voiceFollowUp');
  followUp.checked = true;
  vm.runInContext("state.view = 'voice'; startVoiceCapture(voiceController());", context);
  for (const [before, after] of [[1, 2], [2, 3], [3, 3]]) {
    transcript('Show me my files', {final: true});
    await new Promise(setImmediate);
    assert.equal(spokenReplies.at(-1), 'I found 3 entries.');
    assert.equal(recordings, before, 'the microphone stays closed while speaking');
    onVoiceState('speaking');
    onVoiceState('idle');
    assert.equal(recordings, after);
  }
  vm.runInContext('startVoiceCapture(voiceController());', context);
  requiresReview = true;
  transcript('Search the web', {final: true});
  await new Promise(setImmediate);
  onVoiceState('speaking');
  onVoiceState('idle');
  assert.equal(recordings, 4, 'review stops automatic follow-ups');

  const reviewTranscript = page.document.getElementById('voiceReviewTranscript');
  const beforeReview = requests.length;
  reviewTranscript.checked = true;
  transcript('Find files named report', {final: true});
  await new Promise(setImmediate);
  assert.equal(requests.length, beforeReview, 'review sends no request before confirmation');
  assert.equal(page.document.getElementById('voiceTranscript').value, 'Find files named report');

  const queries = [];
  page.window.actionmesh.searchPublic = async (query) => {
    queries.push(query);
    return {ok: true, results: [{title: 'Flight source', url: 'https://example.com/flights'}]};
  };
  const flight = await vm.runInContext(
    "discoveryReply('I said give a list of planes Mumbai to Chennai')", context,
  );
  assert.deepEqual(queries, ['Flights from Mumbai to Chennai']);
  assert.equal(flight.kind, 'flight lookup');
  assert.equal(flight.sources[0].url,
    'https://www.google.com/travel/flights/flights-from-mumbai-to-chennai.html');
  assert.equal(flight.sources.at(-1).title, 'Flight source');
  const maps = await vm.runInContext("discoveryReply('Directions from Mumbai to Chennai')", context);
  assert.equal(new URL(maps.sources[0].url).searchParams.get('destination'), 'Chennai');
  assert.equal(await vm.runInContext("discoveryReply('Find my tax files')", context), null);
  assert.equal(queries.length, 1);

  vm.runInContext(`
    const rankedAnswer = {kind:'task', session_id:'ranked'};
    applyVoiceRun(rankedAnswer, {status:'completed', steps:[{
      action_type:'device.files.search', verified:true, parameters:{sort_by:'size'},
      observed:{count:2,path:'Documents',truncated:true,entries:[
        {path:'Documents/large.bin',size:1048576}, {path:'Documents/small.txt',size:32},
      ]},
    }]});
    renderReply(byId('voiceReply'), rankedAnswer);
  `, context);
  const resultTable = reply.querySelector('table');
  assert.ok(resultTable);
  assert.equal(resultTable.querySelector('caption').textContent, 'Largest files');
  assert.equal(resultTable.querySelectorAll('tbody tr').length, 2);
  assert.match(resultTable.textContent, /1 MiB/);
  assert.match(reply.textContent, /Scan limit reached/);
  vm.runInContext(`
    const storageAnswer = {kind:'task', session_id:'storage'};
    applyVoiceRun(storageAnswer, {status:'completed', steps:[{
      action_type:'device.files.storage', verified:true,
      observed:{total_bytes:1073741824,used_bytes:536870912,available_bytes:536870912},
    }]});
    renderReply(byId('voiceReply'), storageAnswer);
  `, context);
  assert.equal(reply.querySelector('caption').textContent, 'Workspace filesystem');
  assert.match(reply.textContent, /512 MiB available/);
  assert.match(reply.textContent, /Total capacity/);

  let backendStartedAt = 'first';
  const voiceReads = [];
  page.window.actionmesh.state = async () => ({
    backend: {status:'ready', startedAt:backendStartedAt}, settings:{onboardingCompleted:true},
  });
  page.window.actionmesh.request = async ({path}) => {
    voiceReads.push(path);
    return {ok:true, body:path === '/voiceos/guide' ? {phrases:[]} : []};
  };
  await vm.runInContext("state.view = 'voice'; refreshCurrent()", context);
  const guide = page.document.getElementById('quickGrid').children[0];
  await vm.runInContext('refreshCurrent()', context);
  assert.equal(voiceReads.filter(path => path === '/voiceos/guide').length, 1);
  assert.equal(page.document.getElementById('quickGrid').children[0], guide);
  backendStartedAt = 'second';
  await vm.runInContext('refreshCurrent()', context);
  assert.equal(voiceReads.filter(path => path === '/voiceos/guide').length, 2);
});
