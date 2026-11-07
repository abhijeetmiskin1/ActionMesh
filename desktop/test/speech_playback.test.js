'use strict';
const assert=require('node:assert/strict'),test=require('node:test'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../ui/voice.js'),'utf8');
function setup(){
 const spoken=[],errors=[];let cancelled=0;
 class Utterance{constructor(text){this.text=text;}}
 const voices=[{lang:'en-US',localService:false,name:'remote'},{lang:'en-US',localService:true,name:'English'},{lang:'hi',localService:true,name:'Hindi'}];
 const window={navigator:{language:'en-US'},SpeechSynthesisUtterance:Utterance,
  speechSynthesis:{speak:u=>spoken.push(u),cancel:()=>cancelled++,getVoices:()=>voices}};
 vm.runInNewContext(source,{window,setTimeout,clearTimeout});
 window.VoiceOSVoice.onError(e=>errors.push(e));
 return {api:window.VoiceOSVoice,spoken,errors,cancels:()=>cancelled};
}
test('spoken answers use a matching local English voice and settle on completion',()=>{
 const s=setup();assert.equal(s.api.speak('A helpful answer.'),true);
 assert.equal(s.spoken[0].voice.name,'English');assert.equal(s.api.state,'speaking');
 s.spoken[0].onend();assert.equal(s.api.state,'idle');assert.deepEqual(s.errors,[]);
});
test('Hindi answers select the Hindi voice instead of forcing English',()=>{
 const s=setup();s.api.speak('नमस्ते');assert.equal(s.spoken[0].lang,'hi-IN');assert.equal(s.spoken[0].voice.name,'Hindi');s.api.stopSpeaking();
});
test('stop cancels playback and ignores late cancellation errors',()=>{
 const s=setup();s.api.speak('A sentence.');const speech=s.spoken[0];s.api.stopSpeaking();
 speech.onerror({error:'interrupted'});assert.equal(s.api.state,'idle');assert.deepEqual(s.errors,[]);assert.ok(s.cancels()>=2);
});
test('a service failure reports unavailable playback without claiming success',()=>{
 const s=setup();s.api.speak('An answer.');s.spoken[0].onerror({error:'synthesis-failed'});
 assert.equal(s.api.state,'error');assert.match(s.errors[0],/local speech service/);assert.match(s.errors[0],/text answer is still available/);
});
