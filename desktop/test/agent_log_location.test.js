'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {chooseAgentLogDir}=require('../app/backend');
test('a home-style workspace gets private logs outside the controllable root',t=>{
 const home=fs.mkdtempSync(path.join(os.tmpdir(),'actionmesh-home-test-'));t.after(()=>fs.rmSync(home,{recursive:true,force:true}));
 const logs=path.join(home,'.config/ActionMesh/logs');fs.mkdirSync(logs,{recursive:true});
 const selected=chooseAgentLogDir(home,path.join(logs,'agent'));t.after(()=>fs.rmSync(selected,{recursive:true,force:true}));
 const relative=path.relative(fs.realpathSync(home),selected);
 assert.ok(relative.startsWith('..'+path.sep));assert.equal(fs.statSync(selected).mode&0o077,0);
});
test('ordinary workspaces retain the existing persistent log directory',t=>{
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'actionmesh-logs-test-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
 const workspace=path.join(root,'workspace'),logs=path.join(root,'logs');fs.mkdirSync(workspace);fs.mkdirSync(logs);
 assert.equal(chooseAgentLogDir(workspace,path.join(logs,'agent')),path.join(logs,'agent'));
});
