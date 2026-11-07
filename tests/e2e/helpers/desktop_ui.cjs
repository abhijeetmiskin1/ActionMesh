const {app}=require('electron'),fs=require('fs'),os=require('os'),path=require('path');
if(process.env.UI_AUDIO_WAV){app.commandLine.appendSwitch('use-fake-device-for-media-stream');app.commandLine.appendSwitch('use-file-for-fake-audio-capture',process.env.UI_AUDIO_WAV);}
const dir=fs.mkdtempSync(path.join(os.tmpdir(),'actionmesh-ui-'));app.setPath('userData',dir);
const s=JSON.parse(fs.readFileSync(path.join(os.homedir(),'.config/ActionMesh/settings.json')));s.workspaceRoot=path.join(dir,'workspace');s.allowDesktop=false;s.allowBrowser=false;s.allowMedia=false;s.allowShell=false;fs.writeFileSync(path.join(dir,'settings.json'),JSON.stringify(s),{mode:0o600});
app.on('browser-window-created',(_e,w)=>{w.webContents.on('console-message',(_e,...args)=>{if(args[0]===3)console.log('UI_ERROR',JSON.stringify(args))});w.webContents.once('did-finish-load',()=>setTimeout(async()=>{try{
const prefix=process.env.UI_PREFIX||'/tmp/actionmesh-ui-before';
w.setSize(Number(process.env.UI_WIDTH||1280),Number(process.env.UI_HEIGHT||900));await new Promise(r=>setTimeout(r,150));
fs.writeFileSync(prefix+'-home.png',(await w.capturePage()).toPNG());
await w.webContents.executeJavaScript("document.querySelector('.nav-item[data-view=settings]').click()");await new Promise(r=>setTimeout(r,250));
fs.writeFileSync(prefix+'-settings.png',(await w.capturePage()).toPNG());
await w.webContents.executeJavaScript("document.querySelector('.nav-item[data-view=providers]').click()");await new Promise(r=>setTimeout(r,100));fs.writeFileSync(prefix+'-providers.png',(await w.capturePage()).toPNG());
console.log('UI_REVIEW',await w.webContents.executeJavaScript("JSON.stringify({rail:document.getElementById('railStatus').textContent,width:innerWidth,overflow:document.documentElement.scrollWidth>innerWidth})"));
if(process.env.UI_QA){
const qa=fs.readFileSync(process.env.UI_QA,'utf8');console.log('UI_QA',JSON.stringify(await w.webContents.executeJavaScript(qa,true)));
await new Promise(r=>setTimeout(r,150));fs.writeFileSync(prefix+'-provider.png',(await w.capturePage()).toPNG());
}
}catch(e){console.log('UI_FAILURE',e.message);process.exitCode=1}finally{app.quit()}},1600))});
if(process.env.UI_PACKAGED==='1'){
 const resources=path.join(process.env.UI_ROOT,'desktop/release/router-update/linux-unpacked/resources');
 Object.defineProperty(app,'isPackaged',{value:true});
 Object.defineProperty(process,'resourcesPath',{value:resources});
 require(path.join(resources,'app.asar/app/main'));
}else require(path.join(process.env.UI_ROOT, 'desktop/app/main'));
app.on('quit',()=>fs.rmSync(dir,{recursive:true,force:true}));
