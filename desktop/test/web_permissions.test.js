'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const {configureVoicePermissions,sameBackendOrigin}=require('../app/web_permissions');
const base='http://127.0.0.1:54321';
function handlers(){const pages={setPermissionRequestHandler(fn){this.request=fn;},setPermissionCheckHandler(fn){this.check=fn;}};
 configureVoicePermissions(pages,()=>base);return pages;}
const wc={getURL:()=>base+'/voice'};
test('only microphone audio on the current backend Voice page is granted',()=>{
 const pages=handlers();let granted;
 pages.request(wc,'media',value=>granted=value,{requestingUrl:base+'/voice',mediaTypes:['audio'],isMainFrame:true});
 assert.equal(granted,true);
 assert.equal(pages.check(wc,'media',base,{mediaType:'audio'}),true);
 for(const details of [{mediaTypes:['video']},{mediaTypes:['audio','video']},{mediaTypes:[]},{},
   {mediaTypes:['audio'],requestingUrl:'https://elsewhere.test/voice'},
   {mediaTypes:['audio'],isMainFrame:false}]){
  pages.request(wc,'media',value=>granted=value,{requestingUrl:base+'/voice',...details});assert.equal(granted,false);
 }
 pages.request({getURL:()=>base+'/dashboard'},'media',value=>granted=value,{requestingUrl:base,mediaTypes:['audio']});
 assert.equal(granted,false);
 assert.equal(pages.check(wc,'media',base,{mediaType:'video'}),false);
});
test('API headers stay on the exact backend origin, including the port',()=>{
 assert.equal(sameBackendOrigin(base+'/voice',base),true);
 for(const target of ['http://127.0.0.1:20128/v1','http://127.0.0.1:543210/voice','https://elsewhere.test','data:text/html,test'])
  assert.equal(sameBackendOrigin(target,base),false);
});
