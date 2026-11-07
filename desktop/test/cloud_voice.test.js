'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../ui/voice.js'), 'utf8');
const flush = async () => { for (let i = 0; i < 6; i++) await new Promise(setImmediate); };

function setup(overrides = {}) {
  const tracks = [{
    stops: 0,
    stop() { this.stops++; },
    addEventListener(name, callback) { if (name === 'ended') this.disconnect = callback; },
  }];
  const records = [];
  const uploads = [];
  const timers = new Map();
  let timerId = 0;
  let captures = 0;
  class Recorder {
    static isTypeSupported(type) { return type === 'audio/webm;codecs=opus'; }
    constructor() { this.state = 'inactive'; records.push(this); }
    start() { this.state = 'recording'; }
    stop() {
      this.state = 'inactive';
      this.ondataavailable({ data: new Blob(['synthetic audio']) });
      this.onstop();
    }
  }

 const levels = overrides.levels && [...overrides.levels];
 class AudioContext {
  createAnalyser(){return {fftSize:1024,getFloatTimeDomainData(samples){samples.fill(levels.length?levels.shift():0);}};}
  createMediaStreamSource(){return {connect(){},disconnect(){}};}
  resume(){return Promise.resolve();}
  close(){return Promise.resolve();}
 }
  const window = {
    Blob, MediaRecorder: Recorder,
    localStorage: overrides.storage,
    navigator: { mediaDevices: { getUserMedia: async () => {
      captures++;
      return overrides.permission ? overrides.permission() : { getTracks: () => tracks };
    } } },
    actionmesh: {
      speechStatus: async () => ({ configured: overrides.configured !== false }),
      speechTranscribe: async (payload) => {
        uploads.push(payload);
        return overrides.transcribe ? overrides.transcribe(payload) : { ok: true, text: 'list the files' };
      },
    },
  };
  if (overrides.levels) window.AudioContext = AudioContext;
  vm.runInNewContext(source, { window,
    setTimeout: (fn, delay) => { timers.set(++timerId, { fn, delay }); return timerId; },
    clearTimeout: (id) => timers.delete(id),
  });
  return { api: window.VoiceOSVoice, records, tracks, uploads, timers, captures: () => captures };
}

test('cloud recording stops the microphone before upload and only supplies editable text', async () => {
  const s = setup();
  const transcripts = [];
  s.api.onTranscript((text) => transcripts.push(text));
  assert.equal(s.api.cloudSupported, true);
  s.api.startListening();
  await flush();
  assert.equal(s.api.state, 'listening');
  s.api.startListening();
  assert.equal(s.records.length, 1);
  s.api.stopListening();
  await flush();
  assert.equal(s.tracks[0].stops, 1);
  assert.equal(s.uploads.length, 1);
  assert.equal(s.uploads[0].language, 'en');
  assert.deepEqual(transcripts, ['list the files']);
  assert.equal(s.api.state, 'idle');
  assert.equal(s.timers.size, 0);
});

test('a disconnected microphone releases capture and discards the partial recording', async () => {
  const s = setup();
  const errors = [];
  s.api.onError((message) => errors.push(message));
  s.api.startListening();
  await flush();
  s.tracks[0].disconnect();
  await flush();
  assert.equal(s.api.state, 'error');
  assert.match(errors[0], /microphone disconnected/);
  assert.equal(s.tracks[0].stops, 1);
  assert.equal(s.uploads.length, 0);
});

test('the saved language applies to the next recording and auto omits the hint', async () => {
  const values = new Map();
  const storage = { getItem: (key) => values.get(key), setItem: (key, value) => values.set(key, value) };
  const s = setup({ storage });
  s.api.setLanguage('hi-IN');
  s.api.startListening();
  await flush();
  s.api.setLanguage('auto');
  s.api.stopListening();
  await flush();
  assert.equal(s.uploads[0].language, 'hi');
  const reopened = setup({ storage });
  assert.equal(reopened.api.getLanguage(), 'auto');
  reopened.api.startListening();
  await flush();
  reopened.api.stopListening();
  await flush();
  assert.equal(reopened.uploads[0].language, '');
});

test('an unconfigured audio lane never opens the microphone', async () => {
  const s = setup({ configured: false });
  s.api.startListening();
  await flush();
  assert.equal(s.captures(), 0);
  assert.equal(s.api.state, 'error');
});

test('cancel drops recording without uploading', async () => {
  const s = setup();
  s.api.startListening();
  await flush();
  s.api.cancel();
  await flush();
  assert.equal(s.tracks[0].stops, 1);
  assert.equal(s.uploads.length, 0);
  assert.equal(s.api.state, 'idle');
});

test('cancel during microphone permission releases a late stream', async () => {
  let grant;
  const s = setup({ permission: () => new Promise((resolve) => { grant = resolve; }) });
  s.api.startListening();
  await flush();
  s.api.cancel();
  grant({ getTracks: () => s.tracks });
  await flush();
  assert.equal(s.tracks[0].stops, 1);
  assert.equal(s.records.length, 0);
  assert.equal(s.uploads.length, 0);
});

test('cancel during transcription discards a late transcript', async () => {
  let finish;
  const s = setup({ transcribe: () => new Promise((resolve) => { finish = resolve; }) });
  const transcripts = [];
  s.api.onTranscript((text) => transcripts.push(text));
  s.api.startListening();
  await flush();
  s.api.stopListening();
  await flush();
  s.api.cancel();
  finish({ ok: true, text: 'must not be delivered' });
  await flush();
  assert.deepEqual(transcripts, []);
  assert.equal(s.api.state, 'idle');
});

test('empty speech and provider refusals report errors without a transcript', async () => {
  for (const answer of [{ ok: true, text: '' }, { ok: false, detail: 'Provider unavailable.' }]) {
    const s = setup({ transcribe: async () => answer });
    const transcripts = [];
    s.api.onTranscript((text) => transcripts.push(text));
    s.api.startListening();
    await flush();
    s.api.stopListening();
    await flush();
    assert.equal(s.api.state, 'error');
    assert.deepEqual(transcripts, []);
    assert.equal(s.tracks[0].stops, 1);
  }
});

test('recording has a thirty-second automatic stop', async () => {
  const s = setup();
  s.api.startListening();
  await flush();
  const deadline = [...s.timers.values()].find((entry) => entry.delay === 30000);
  assert.ok(deadline);
  deadline.fn();
  await flush();
  assert.equal(s.tracks[0].stops, 1);
  assert.equal(s.uploads.length, 1);
});

async function tickEndpoint(s, count) {
 for(let i=0;i<count;i++){
  const entry=[...s.timers].find(([,t])=>t.delay===80);
  assert.ok(entry,'endpoint monitor is active');
  s.timers.delete(entry[0]);entry[1].fn();await flush();
 }
}

test('a short pause automatically finishes speech without waiting for the 30-second cap', async()=>{
 const s=setup({levels:[...Array(4).fill(.1),...Array(12).fill(0)]});
 s.api.startListening();await flush();
 await tickEndpoint(s,15);assert.equal(s.api.state,'listening');
 await tickEndpoint(s,1);assert.equal(s.uploads.length,1);assert.equal(s.tracks[0].stops,1);
 assert.equal(s.api.state,'idle');assert.equal(s.timers.size,0);
});
test('brief pauses do not cut a command in the middle',async()=>{
 const s=setup({levels:[...Array(3).fill(.1),...Array(6).fill(0),...Array(3).fill(.1),...Array(12).fill(0)]});
 s.api.startListening();await flush();await tickEndpoint(s,12);
 assert.equal(s.api.state,'listening');assert.equal(s.uploads.length,0);
 await tickEndpoint(s,12);assert.equal(s.uploads.length,1);
});
test('silence without speech cancels locally and never uploads an empty command',async()=>{
 const s=setup({levels:Array(100).fill(0)});s.api.startListening();await flush();
 await tickEndpoint(s,100);assert.equal(s.uploads.length,0);assert.equal(s.tracks[0].stops,1);
 assert.equal(s.api.state,'error');
});
test('cancellation releases the pause detector and cannot deliver a late transcript',async()=>{
 const s=setup({levels:Array(10).fill(.1)});s.api.startListening();await flush();
 await tickEndpoint(s,2);s.api.cancel();await flush();
 assert.equal(s.uploads.length,0);assert.equal(s.timers.size,0);assert.equal(s.tracks[0].stops,1);
});
