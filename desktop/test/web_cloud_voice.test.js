'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const python=fs.readFileSync(path.resolve(__dirname,'../../actionmesh/voice_audio.py'),'utf8');
const source=python.split('SCRIPT = r"""')[1].split('"""')[0];
const flush=async()=>{for(let i=0;i<8;i++)await new Promise(setImmediate);};
function setup(options={}){
 const recorders=[],tracks=[{stops:0,stop(){this.stops++;}}],timers=new Map(),uploads=[],states=[],texts=[];
 let captures=0,timerId=0;
 class Recorder{
  static isTypeSupported(){return true;}
  constructor(){this.state='inactive';recorders.push(this);}
  start(){this.state='recording';}
  stop(){this.state='inactive';this.ondataavailable({data:new Blob([options.audio||'synthetic recording'])});this.onstop();}
 }

 const levels = options.levels && [...options.levels];
 class AudioContext {
  createAnalyser(){return {fftSize:1024,getFloatTimeDomainData(samples){samples.fill(levels.length?levels.shift():0);}};}
  createMediaStreamSource(){return {connect(){},disconnect(){}};}
  resume(){return Promise.resolve();}
  close(){return Promise.resolve();}
 }
 const window={Blob,AbortController,MediaRecorder:Recorder,btoa:(v)=>Buffer.from(v,'binary').toString('base64'),
  navigator:{mediaDevices:{getUserMedia:async()=>{captures++;return options.permission?options.permission():{getTracks:()=>tracks};}}}};
 if (options.levels) window.AudioContext = AudioContext;
 vm.runInNewContext(source,{window,Uint8Array,setTimeout:(fn,ms)=>{timers.set(++timerId,{fn,ms});return timerId;},clearTimeout:id=>timers.delete(id)});
 const api=window.ActionMeshCloudVoice.create({
  getStatus:async()=>({configured:options.configured!==false}),
  transcribe:async(audio,signal)=>{assert.equal(tracks[0].stops,1);uploads.push({audio,signal});return options.transcribe?options.transcribe():{text:'list files'};},
  onTranscript:text=>texts.push(text),onState:(state,detail)=>states.push({state,detail}),
 });
 return{api,recorders,tracks,timers,uploads,states,texts,captures:()=>captures};
}

test('cloud web capture opens only on start, stops tracks before upload and returns editable text',async()=>{
 const s=setup();assert.equal(s.captures(),0);await s.api.start();assert.equal(s.captures(),1);
 assert.equal(s.timers.get(1).ms,30000);s.api.stop();await flush();
 assert.deepEqual(s.texts,['list files']);assert.equal(s.uploads.length,1);assert.equal(s.api.active,false);assert.equal(s.timers.size,0);
});
test('missing provider configuration never opens the microphone',async()=>{
 const s=setup({configured:false});await s.api.start();assert.equal(s.captures(),0);assert.equal(s.states.at(-1).state,'error');
});
test('cancellation while permission is pending releases a late stream',async()=>{
 let grant;const s=setup({permission:()=>new Promise(r=>grant=r)});s.api.start();await flush();s.api.cancel();grant({getTracks:()=>s.tracks});await flush();
 assert.equal(s.tracks[0].stops,1);assert.equal(s.recorders.length,0);assert.equal(s.uploads.length,0);
});
test('cancelling an upload aborts it and discards a late transcript',async()=>{
 let finish;const s=setup({transcribe:()=>new Promise(r=>finish=r)});await s.api.start();s.api.stop();await flush();
 s.api.cancel();assert.equal(s.uploads[0].signal.aborted,true);finish({text:'late answer'});await flush();assert.deepEqual(s.texts,[]);
});
test('oversize recordings are released without sending partial audio',async()=>{
 const s=setup({audio:'x'.repeat(128*1024+1)});await s.api.start();s.api.stop();await flush();
 assert.equal(s.tracks[0].stops,1);assert.equal(s.uploads.length,0);assert.equal(s.states.at(-1).state,'error');
});
test('hitting the timer stops one recording and never restarts capture',async()=>{
 const s=setup();await s.api.start();s.timers.get(1).fn();await flush();assert.equal(s.captures(),1);assert.equal(s.uploads.length,1);
});

test('web dictation also stops after speech followed by a short pause',async()=>{
 const s=setup({levels:[...Array(4).fill(.1),...Array(12).fill(0)]});await s.api.start();await flush();
 for(let i=0;i<16;i++){
  const entry=[...s.timers].find(([,t])=>t.ms===80);assert.ok(entry);
  s.timers.delete(entry[0]);entry[1].fn();await flush();
 }
 assert.equal(s.uploads.length,1);assert.equal(s.api.active,false);assert.equal(s.tracks[0].stops,1);
 assert.deepEqual(s.texts,['list files']);assert.equal(s.timers.size,0);
});
