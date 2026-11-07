(async()=>{
 const assert=(value,message)=>{if(!value)throw Error(message)};
 const checks=[];
 for(const button of document.querySelectorAll('.nav-item')){
  button.click();await new Promise(r=>setTimeout(r,20));
  assert([...document.querySelectorAll('section.view')].filter(x=>!x.hidden).length===1,'one main page');
  assert(button.getAttribute('aria-current')==='true','active navigation');
 }
 checks.push('all 12 main pages');
 for(const button of document.querySelectorAll('[data-settings-tab]')){
  button.click();assert(button.getAttribute('aria-selected')==='true','tab selected');
  assert([...document.querySelectorAll('[data-settings-panel]')].filter(x=>!x.hidden).length===1,'one settings panel');
 }
 const tab=document.getElementById('settings-tab-about');
 tab.dispatchEvent(new KeyboardEvent('keydown',{key:'Home',bubbles:true}));
 assert(document.activeElement.id==='settings-tab-app','keyboard Home');
 document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true}));
 assert(document.activeElement.id==='settings-tab-backend','keyboard ArrowRight');checks.push('tab clicks and keyboard navigation');
 document.getElementById('settings-tab-app').click();
 const input=document.getElementById('setDeviceName');const original=input.value;
 input.value='Draft name';input.dispatchEvent(new Event('input',{bubbles:true}));
 const checkbox=document.getElementById('setNotifications');const oldChecked=checkbox.checked;checkbox.checked=!oldChecked;checkbox.dispatchEvent(new Event('change',{bubbles:true}));
 await refreshSettings();assert(input.value==='Draft name'&&checkbox.checked===!oldChecked,'draft survives refresh');
 document.getElementById('settingsReset').click();await new Promise(r=>setTimeout(r,100));
 assert(input.value===original&&checkbox.checked===oldChecked,'discard restores saved settings');checks.push('draft preservation and discard');
 document.getElementById('providerConfigure').click();await new Promise(r=>setTimeout(r,100));
 const opener=document.querySelector('#settingsProviderKv [data-lane-action=configure][data-purpose=chat]');opener.focus();opener.click();
 assert(document.getElementById('app').inert,'background inert');
 const modal=document.getElementById('providerModal');const controls=[...modal.querySelectorAll('button,input,select')].filter(x=>!x.disabled&&x.getClientRects().length);
 controls.at(-1).focus();document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:'Tab',bubbles:true,cancelable:true}));
 assert(document.activeElement===controls[0],'focus trap forward');
 document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:'Tab',shiftKey:true,bubbles:true,cancelable:true}));
 assert(document.activeElement===controls.at(-1),'focus trap reverse');
 document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true}));
 assert(modal.hidden&&!document.getElementById('app').inert&&document.activeElement===opener,'Escape and restored focus');checks.push('modal focus, keyboard trap and Escape');
 for(const purpose of ['chat','reasoning','audio','image']){
  document.querySelector('#settingsProviderKv [data-lane-action=configure][data-purpose='+purpose+']').click();
  assert(modal.dataset.purpose===purpose,'purpose-specific form');
  assert(document.getElementById('cfgApiKey').value==='','stored credential never returned');
  document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true}));
 }
 for(const purpose of ['reasoning','chat']){
  const select=document.querySelector('#settingsProviderKv [data-lane-action=default][data-purpose='+purpose+']');
  if(!select.disabled){select.click();await new Promise(r=>setTimeout(r,150));}
  assert(document.querySelector('#settingsProviderKv [data-lane-action=default][data-purpose='+purpose+']').disabled,'planner preference updated');
 }
 checks.push('four independent provider forms and planner preference');
 for(const view of ['home','voice','providers','settings']){showView(view);assert(document.documentElement.scrollWidth<=innerWidth,'horizontal overflow '+view)}
 assert(!document.getElementById('toast').textContent.includes('backend is not running'),'no false startup warning');
 showView('providers');await new Promise(r=>setTimeout(r,100));document.querySelector('#settingsProviderKv [data-lane-action=configure][data-purpose=chat]').click();
 return {checks,width:innerWidth,overflow:false};
})()
