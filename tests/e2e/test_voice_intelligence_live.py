"""Opt-in real voice-message API → model bridge → 9router, with no executions."""
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(
    os.environ.get("ACTIONMESH_LIVE_VOICE_MODEL") != "1", reason="live model calls are opt-in"
)
def test_voice_understanding_through_real_api_and_model(tmp_path: Path) -> None:
    electron = ROOT / "desktop/node_modules/electron/dist/electron"
    if not electron.is_file() or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        pytest.skip("Electron and a graphical session are required")
    helper = tmp_path / "acceptance.cjs"
    helper.write_text(r'''
const {app,safeStorage}=require('electron');const path=require('path');
app.setName('ActionMesh');app.setPath('userData',process.env.VOICE_PROFILE);
let backend,bridge;
app.whenReady().then(async()=>{try{
 const root=process.env.VOICE_ROOT, packaged=process.env.VOICE_PACKAGED==='1';
 const resources=path.join(root,'desktop/release/router-update/linux-unpacked/resources');
 const shell=packaged?path.join(resources,'app.asar'):path.join(root,'desktop');
 const {SettingsStore}=require(shell+'/app/settings');
 const s=new SettingsStore({userDataDir:process.env.VOICE_SETTINGS,safeStorage});
 if(!s.hasSecret('chatApiKey'))throw Error('No usable saved Chat credential');
 for(const lane of ['chat','reasoning']){
  s.values['purpose_'+lane+'_provider']='openai_compatible';
  s.values['purpose_'+lane+'_model']='groq/openai/gpt-oss-120b';
  s.values['purpose_'+lane+'_endpoint']='http://127.0.0.1:20128/api/v1';
 }
 // In-memory selection only; this acceptance never rewrites the actual profile.
 if(!s.hasSecret('reasoningApiKey'))s.secrets.reasoningApiKey=s.secret('chatApiKey');
 const {Backend,newLoopbackKey}=require(shell+'/app/backend');
 const token=newLoopbackKey(),socket=path.join(process.env.VOICE_PROFILE,'model.sock');
 bridge=await require(shell+'/app/provider_bridge').startBridge({socketPath:socket,token,
  settings:s,providers:require(shell+'/app/providers')});
 backend=new Backend({dataDir:process.env.VOICE_PROFILE,appRoot:root,isPackaged:packaged,resourcesPath:resources,
  settings:{agentEnabled:true,allowShell:false,allowBrowser:true,allowMedia:true,allowDesktop:false},
  key:newLoopbackKey(),bridge:{socketPath:socket,token}});
 backend.prepare();await backend.start();
 const request=async(route,body)=>{
  const r=await fetch(backend.baseUrl+route,{method:body?'POST':'GET',
   headers:{'x-api-key':backend.key,'content-type':'application/json'},
   body:body?JSON.stringify(body):undefined});
  if(!r.ok)throw Error('Acceptance API HTTP '+r.status);
  return r.json();
 };
 const checks=[];
 for(const [id,text,task] of [
  ['explanation','फाइल और फोल्डर में क्या अंतर है?',false],
  ['clarification','Delete it.',false],
  ['hypothetical','How do I delete a file?',false],
  ['hinglish','Music pause kar do.',true],
  ['browser','Open https://example.com in the browser.',true],
  ['multi','Pause the music and list files in the workspace.',true],
 ]){
  const answer=await request('/voiceos/messages',{text});
  if(answer.ran!==false)throw Error(id+' attempted execution');
  if(task){
   if(answer.kind!=='task'||!answer.session_id)throw Error(id+' did not propose a task: '+JSON.stringify({kind:answer.kind,notes:answer.notes,unresolved:answer.unresolved,reply:answer.reply}));
   if(!answer.notes.some(n=>n==='planned by desktop'))throw Error(id+' did not use the model');
  }else{
   if(answer.kind!=='answer'||answer.session_id||!answer.notes.some(n=>n==='answered by desktop'))
    throw Error(id+' was not a model-only answer');
  }
  checks.push(id);
 }
 const records=await request('/executions');
 if(records.length)throw Error('Understanding created an execution');
 const status=await request('/voiceos/status');
 console.log('VOICE_ACCEPTANCE '+JSON.stringify({checks,executions:0,packaged,model:status.llm?.model||status.model}));
}catch(e){console.log('VOICE_FAILURE '+e.message);process.exitCode=1;}
finally{if(backend)await backend.stop();if(bridge)await bridge.close();app.exit(process.exitCode||0);}});
''')
    result = subprocess.run(
        [str(electron), str(helper)],
        env={**os.environ, "VOICE_ROOT": str(ROOT), "VOICE_PROFILE": str(tmp_path),
             "VOICE_SETTINGS": str(Path.home() / ".config/ActionMesh"),
             "VOICE_PACKAGED": os.environ.get("ACTIONMESH_LIVE_PACKAGED", "0")},
        capture_output=True, text=True, timeout=120,
    )
    records = [json.loads(line.split(" ", 1)[1]) for line in result.stdout.splitlines()
               if line.startswith("VOICE_ACCEPTANCE ")]
    failures = [line for line in result.stdout.splitlines() if line.startswith("VOICE_FAILURE ")]
    assert result.returncode == 0 and records, failures or "live acceptance did not finish"
    assert len(records[0]["checks"]) == 6
    assert records[0]["executions"] == 0
