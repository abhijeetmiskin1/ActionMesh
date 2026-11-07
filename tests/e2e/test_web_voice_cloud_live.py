"""Opt-in real web capture → API → private bridge → configured speech provider.

Chromium receives a synthetic WAV through its fake device, never a physical mic.
Secrets are decrypted only inside Electron main using the existing SettingsStore.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(os.environ.get('ACTIONMESH_LIVE_AUDIO') != '1', reason='live cloud audio acceptance is opt-in')
def test_web_cloud_dictation_with_synthetic_input_and_real_provider(tmp_path) -> None:
    wav = os.environ.get('ACTIONMESH_LIVE_AUDIO_WAV', '')
    if not wav or not Path(wav).is_file():
        pytest.skip('provide a generated synthetic speech WAV')
    electron = ROOT / 'desktop/node_modules/electron/dist/electron'
    if not electron.is_file() or not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        pytest.skip('Electron and a graphical session are required')
    helper = tmp_path / 'cloud-web-acceptance.cjs'
    helper.write_text(r'''
const {app,BrowserWindow,safeStorage}=require('electron');
const path=require('path');
app.setName('ActionMesh');
app.setPath('userData',process.env.AUDIO_PROFILE);
app.commandLine.appendSwitch('use-fake-ui-for-media-stream');
app.commandLine.appendSwitch('use-fake-device-for-media-stream');
app.commandLine.appendSwitch('use-file-for-fake-audio-capture',process.env.AUDIO_WAV);
let backend,bridge,window;
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
app.whenReady().then(async()=>{
 try {
  const root=process.env.AUDIO_REPO;
  const packaged=process.env.AUDIO_PACKAGED==='1';
  const resources=path.join(root,'desktop/release/router-update/linux-unpacked/resources');
  const shellRoot=packaged?path.join(resources,'app.asar'):path.join(root,'desktop');
  const {SettingsStore}=require(shellRoot+'/app/settings');
  const settings=new SettingsStore({userDataDir:process.env.AUDIO_SETTINGS,safeStorage});
  if(!settings.hasSecret('audioApiKey'))throw Error('Audio credential is unavailable to the OS keyring');
  const {startBridge}=require(shellRoot+'/app/provider_bridge');
  const {Backend,newLoopbackKey}=require(shellRoot+'/app/backend');
  const token=newLoopbackKey(), socket=path.join(process.env.AUDIO_PROFILE,'provider.sock');
  bridge=await startBridge({socketPath:socket,token,settings,providers:require(shellRoot+'/app/providers')});
  backend=new Backend({dataDir:process.env.AUDIO_PROFILE,appRoot:root,isPackaged:packaged,
   resourcesPath:packaged?resources:process.resourcesPath,settings:{agentEnabled:false},key:newLoopbackKey(),bridge:{socketPath:socket,token}});
  backend.prepare();await backend.start();
  window=new BrowserWindow({show:false,webPreferences:{sandbox:true,contextIsolation:true,nodeIntegration:false,backgroundThrottling:false}});
  require(shellRoot+'/app/web_permissions').configureVoicePermissions(window.webContents.session,()=>backend.baseUrl);
  window.webContents.session.webRequest.onBeforeSendHeaders({urls:['http://127.0.0.1/*']},(details,done)=>{
   const headers={...details.requestHeaders};
   if(new URL(details.url).origin===backend.baseUrl)headers['x-api-key']=backend.key;
   done({requestHeaders:headers});
  });
  await window.loadURL(backend.baseUrl+'/voice');
  const read=()=>window.webContents.executeJavaScript(`({phase:document.getElementById('micButton').dataset.state,
   transcript:document.getElementById('transcript').value,message:document.getElementById('messageStatus').textContent,
   editable:!document.getElementById('transcript').readOnly})`);
  await window.webContents.executeJavaScript("document.getElementById('micButton').click()",true);
  let state;
  for(let i=0;i<100;i++){state=await read();if(state.phase==='listening'||state.phase==='error')break;await sleep(100);}
  if(state.phase!=='listening')throw Error('Web capture did not enter listening state');
  // Pause detection may already have stopped capture. Never turn the mic back on.
  await sleep(4500);
  if((await read()).phase==='listening')
    await window.webContents.executeJavaScript("document.getElementById('micButton').click()",true);
  for(let i=0;i<600;i++){state=await read();if(state.transcript||state.phase==='error')break;await sleep(100);}
  if(!state.transcript||!state.editable)throw Error('No editable cloud transcript was received: '+state.message);
  const query=async(route)=>await (await fetch(backend.baseUrl+route,{headers:{'x-api-key':backend.key}})).json();
  if((await query('/executions')).length||(await query('/voiceos/sessions')).length)throw Error('Dictation must not create tasks or execute actions');
  await window.webContents.executeJavaScript(`window.__busyCalls=0;window.__originalFetch=window.fetch;
    window.fetch=(url,options)=>{if(url==='/voiceos/messages'){window.__busyCalls++;
      return Promise.resolve(new Response(JSON.stringify({detail:'Fixture service busy'}),{status:503,headers:{'content-type':'application/json'}}));}
      return window.__originalFetch(url,options);};document.getElementById('sendMessage').click();`);
  await sleep(150);
  const recovery=await window.webContents.executeJavaScript(`({calls:window.__busyCalls,
    text:document.getElementById('transcript').value,message:document.getElementById('messageStatus').textContent})`);
  if(recovery.calls!==1||recovery.text!==state.transcript||!recovery.message.includes('kept'))throw Error('Busy recovery did not preserve text without retry');
  await window.webContents.executeJavaScript(`window.fetch=(url,options)=>{
    if(url==='/voiceos/audio/transcribe')return new Promise(resolve=>window.__heldAudioReply=resolve);
    return window.__originalFetch(url,options);};document.getElementById('micButton').click();`);
  for(let i=0;i<100;i++){const observed=await read();if(observed.phase==='listening')break;await sleep(50);}
  await sleep(400);
  await window.webContents.executeJavaScript("document.getElementById('micButton').click()");
  for(let i=0;i<100;i++){if(await window.webContents.executeJavaScript('Boolean(window.__heldAudioReply)'))break;await sleep(50);}
  await window.webContents.executeJavaScript(`const field=document.getElementById('transcript');field.value='my corrected request';
    field.dispatchEvent(new Event('input',{bubbles:true}));
    window.__heldAudioReply(new Response(JSON.stringify({text:'stale dictated words'}),{status:200,headers:{'content-type':'application/json'}}));`);
  await sleep(100);
  const corrected=await read();
  if(corrected.transcript!=='my corrected request'||!corrected.message.includes('manual edit'))throw Error('Late dictation overwrote a manual edit');
  process.stdout.write('ACCEPTANCE_JSON '+JSON.stringify({transcript:state.transcript,editable:state.editable,
    executions:0,sessions:0,busyPreservesText:true,manualEditPreserved:true,packaged})+'\n');
 }catch(error){process.stdout.write('ACCEPTANCE_FAILURE '+error.message+'\n');process.exitCode=1;}
 finally{if(window)window.destroy();if(backend)await backend.stop();if(bridge)await bridge.close();app.exit(process.exitCode||0);}
});
''')
    env = {**os.environ, 'AUDIO_REPO': str(ROOT), 'AUDIO_PROFILE': str(tmp_path), 'AUDIO_WAV': wav,
           'AUDIO_SETTINGS': str(Path.home() / '.config/ActionMesh'),
           'AUDIO_PACKAGED': os.environ.get('ACTIONMESH_LIVE_PACKAGED', '0')}
    env.pop('ELECTRON_RUN_AS_NODE', None)
    result = subprocess.run([str(electron), str(helper)], env=env, capture_output=True, text=True, timeout=90)
    records = [json.loads(line.split(' ', 1)[1]) for line in result.stdout.splitlines() if line.startswith('ACCEPTANCE_JSON ')]
    failures = [line for line in result.stdout.splitlines() if line.startswith('ACCEPTANCE_FAILURE ')]
    assert result.returncode == 0 and records, failures or 'web cloud acceptance did not finish'
    assert 'workspace' in records[-1]['transcript'].lower().replace('work space', 'workspace')
    assert records[-1]['executions'] == records[-1]['sessions'] == 0
