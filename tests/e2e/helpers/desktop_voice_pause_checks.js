(async()=>{
 const wait=ms=>new Promise(r=>setTimeout(r,ms));
 document.querySelector('.nav-item[data-view=voice]').click();await wait(100);
 const language=document.getElementById('voiceLanguage');
 if(language.value!=='en-US')throw Error('Voice input did not default to English');
 language.value='hi-IN';
 language.dispatchEvent(new Event('change'));
 if(window.VoiceOSVoice.getLanguage()!=='hi-IN')throw Error('The language picker did not update voice input');
 language.value='en-US';
 language.dispatchEvent(new Event('change'));
 const started=performance.now();
 document.getElementById('voiceMic').click();
 let sawListening=false,recordingMs=0,text='';
 for(let i=0;i<200;i++){
  await wait(100);
  const phase=window.VoiceOSVoice.state;
  if(phase==='listening')sawListening=true;
  if(sawListening&&!recordingMs&&phase==='transcribing')recordingMs=Math.round(performance.now()-started);
  text=document.getElementById('voiceTranscript').value;
  if(text)break;
  if(phase==='error')throw Error('Speech did not produce a transcript');
 }
 if(!sawListening||!recordingMs||recordingMs>=8000)throw Error('Recording did not auto-finish promptly');
 if(!/files?/i.test(text))throw Error('Synthetic file-list request was not recognized');
 if(document.getElementById('voiceTranscript').readOnly)throw Error('Transcript is not editable');
 for(let i=0;i<120;i++){
  const reply=document.getElementById('voiceReply').textContent;
  // The isolated profile has no standing file consent. Reaching this denial
  // proves the transcript submitted the task to the normal runner and policy.
  if(reply.includes('Task status: failed')&&reply.includes('missing permission(s)')){
   const records=await window.actionmesh.request({path:'/executions'});
   return {recordingMs,totalMs:Math.round(performance.now()-started),editable:true,
     executionCount:records.body.length,policyDenied:true};
  }
  await wait(100);
 }
 throw Error(`Expected a policy decision for "${text}". ${document.getElementById('voiceReply').textContent}`);
})()
