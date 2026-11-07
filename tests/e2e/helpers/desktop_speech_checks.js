(async()=>{
 const voice=window.VoiceOSVoice;
 const wait=ms=>new Promise(r=>setTimeout(r,ms));
 for(let i=0;i<30&&!speechSynthesis.getVoices().length;i++)await wait(100);
 const checks=[];
 for(const [language,text] of [['English','Speech playback is ready.'],['Hindi','नमस्ते']]){
  let error='';voice.onError(e=>{error=e;});
  if(!voice.speak(text))throw Error('Speech playback was refused');
  for(let i=0;i<120&&voice.state==='speaking';i++)await wait(100);
  if(error||voice.state!=='idle')throw Error(language+' speech did not complete: '+error);
  checks.push(language);
 }
 voice.speak('This is a longer sentence that will be interrupted by the stop button.');
 await wait(200);voice.stopSpeaking();await wait(200);
 if(voice.state!=='idle')throw Error('Stop speaking did not settle');
 return {completed:checks,stopWorks:true,localVoices:speechSynthesis.getVoices().filter(v=>v.localService).length};
})()
