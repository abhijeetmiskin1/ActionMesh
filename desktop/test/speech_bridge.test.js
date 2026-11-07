'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),net=require('node:net'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const{createSpeechHandler}=require('../app/speech_bridge');
const{startBridge}=require('../app/provider_bridge');
const KEY='sk-test-audio-do-not-expose';
function setup(transcribe){
 const values={purpose_audio_provider:'router_audio',purpose_audio_model:'fixture-model',purpose_audio_endpoint:'http://127.0.0.1:20128/api/v1'};
 const settings={get:key=>values[key],secret:key=>key==='audioApiKey'?KEY:''};
 const calls=[];
 const providers={transcribe:async(config,audio)=>{calls.push({config,audio});return transcribe?transcribe():{ok:true,text:'list files'};}};
 return{settings,providers,calls,handler:createSpeechHandler({settings,providers})};
}
const frame={op:'transcribe',audio:Buffer.from('audio fixture').toString('base64')};
test('bridge spends only the Audio lane key and returns no credential/configuration',async()=>{
 const s=setup();const result=await s.handler(frame);assert.deepEqual(result,{ok:true,text:'list files'});
 assert.equal(s.calls[0].config.apiKey,KEY);assert.equal(JSON.stringify(result).includes(KEY),false);
 assert.deepEqual(await s.handler({op:'audio_status'}),{ok:true,configured:true,max_bytes:131072});
});
test('invalid, oversized, or caller-supplied credentials are refused before a provider call',async()=>{
 const s=setup();for(const value of [{...frame,audio:'%'},{...frame,apiKey:'caller'},
  {...frame,audio:Buffer.alloc(131073).toString('base64')},{...frame,audio:''}])assert.equal((await s.handler(value)).ok,false);
 assert.equal(s.calls.length,0);
});
test('provider errors and key echoes never reach a web client',async()=>{
 for(const outcome of [()=>{throw Error(KEY);},()=>({ok:false,detail:KEY}),()=>({ok:true,text:KEY})]){
  const answer=await setup(outcome).handler(frame);assert.equal(answer.ok,false);assert.equal(JSON.stringify(answer).includes(KEY),false);
 }
});
test('concurrent recordings fail fast and a completed call releases the slot',async()=>{
 let resolve;const s=setup(()=>new Promise(r=>resolve=r));const first=s.handler(frame);
 assert.equal((await s.handler(frame)).state,'rate_limited');resolve({ok:true,text:'okay'});assert.equal((await first).ok,true);
});
test('real private socket authenticates audio and serves only one request per connection',async(t)=>{
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'actionmesh-audio-bridge-')),socketPath=path.join(dir,'bridge.sock');
 const s=setup();let finish;
 s.providers.transcribe=async()=>{s.calls.push({});return new Promise(r=>finish=r);};
 const bridge=await startBridge({socketPath,token:'test-token',settings:s.settings,providers:s.providers});
 t.after(async()=>{await bridge.close();fs.rmSync(dir,{recursive:true,force:true});});
 const result=await new Promise((resolve,reject)=>{
  const socket=net.connect(socketPath);let data='';const timer=setTimeout(()=>{socket.destroy();reject(Error('timeout'));},3000);
  socket.on('error',reject);socket.on('connect',()=>socket.write(JSON.stringify({...frame,token:'test-token'})+'\n'));
  const poll=setInterval(()=>{if(!finish)return;clearInterval(poll);
   socket.write(JSON.stringify({...frame,token:'test-token'})+'\n');setTimeout(()=>finish({ok:true,text:'fixture'}),30);
  },5);
  socket.on('data',chunk=>data+=chunk);socket.on('end',()=>{clearTimeout(timer);clearInterval(poll);resolve(JSON.parse(data));});
 });
 assert.equal(result.text,'fixture');assert.equal(s.calls.length,1);
});
