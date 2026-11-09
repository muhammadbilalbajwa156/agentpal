/* Workspace credentials travel only in authenticated same-origin request bodies. */
(()=>{
 'use strict';
 if(!window.AgentPalAPI?.live)return;
 const api=window.AgentPalAPI,safe=v=>App.escape(String(v??''));
 const providers=[
  {id:'paypal_sandbox',label:'PayPal sandbox',purpose:'Create sandbox orders, capture approved test payments and refund them.',url:'https://developer.paypal.com/dashboard/applications/sandbox',where:'PayPal Developer · Apps & Credentials · Sandbox',fields:[['client_id','Client ID',true],['client_secret','Client secret',true],['webhook_id','Webhook ID (optional)',false]]},
  {id:'channel3',label:'Channel3',purpose:'Find products and compare market prices for the Shopper AI agent.',url:'https://trychannel3.com/developers',where:'Channel3 · developer dashboard',fields:[['api_key','API key',true]]},
  {id:'kernel',label:'Kernel',purpose:'Read product pages for advisory checks. Browser evidence cannot authorize payments.',url:'https://dashboard.onkernel.com',where:'Kernel · dashboard API keys',fields:[['api_key','API key',true]]},
  {id:'groq',label:'Groq',purpose:'Run the Shopper AI agent’s model. Connect Groq or OpenRouter for AI errands.',url:'https://console.groq.com/keys',where:'GroqCloud · API Keys',fields:[['api_key','API key',true]]},
  {id:'openrouter',label:'OpenRouter',purpose:'An alternative model provider for the Shopper AI agent. Spending rules still apply.',url:'https://openrouter.ai/settings/keys',where:'OpenRouter · API Keys',fields:[['api_key','API key',true]]},
 ];
 const ids=new Set(providers.map(p=>p.id)),tabKey='agentpal-service-setup-v1';
 let items=[],loaded=false,loadError='',skipped=new Set(),seen=false,epoch=0;
 const editing=new Set(),busy=new Set(),notices=new Map();
 try{const saved=JSON.parse(sessionStorage.getItem(tabKey));seen=saved?.seen===true;skipped=new Set((Array.isArray(saved?.skipped)?saved.skipped:[]).filter(id=>ids.has(id)));}catch(_){}
 function remember(){try{sessionStorage.setItem(tabKey,JSON.stringify({seen,skipped:[...skipped]}));}catch(_){} }
 const item=id=>items.find(i=>i.provider===id);
 function verificationMessage(code){
  const messages={invalid_credentials:'The provider did not accept this key.',invalid_api_key:'The provider did not accept this API key.',invalid_key:'The provider did not accept this key.',unauthorized:'The provider refused these credentials.',authentication_failed:'The provider did not accept these credentials.',provider_unavailable:'The provider is unavailable. Try again later.',provider_error:'The provider could not complete verification.',network_error:'The provider could not be reached.',timeout:'The provider took too long to respond.',request_timeout:'The provider took too long to respond.',rate_limited:'The provider is busy. Wait before trying again.',verification_failed:'Check the credentials and try again.'};
  const key=typeof code==='string'&&/^[a-z][a-z0-9_]{1,63}$/i.test(code)?code:'verification_failed';
  return (Object.hasOwn(messages,key)?messages[key]:'The provider could not verify these credentials.')+' (error code: '+key+')';
 }
 function card(p,wizard){
  const value=item(p.id),connected=value?.status==='verified',edit=editing.has(p.id)||(wizard&&!connected&&!skipped.has(p.id));
  const status=!loaded?'Status unavailable':connected?'Verified':value?.status==='failed'?'Verification failed':value?.source==='server'?'Provided by server':'Not connected';
  return `<article class="i-provider ws-provider" data-provider="${p.id}"><div class="i-provider-head"><span class="i-provider-logo blue">${p.label[0]}</span><span class="i-badge ${connected?'connected':''}">${status}</span></div><h3>${p.label}</h3><p>${p.purpose}</p><p>Get credentials: <a href="${p.url}" target="_blank" rel="noopener noreferrer" referrerpolicy="no-referrer">${p.where}</a>.</p>${p.id==='paypal_sandbox'?'<p>Choose a Sandbox app (free). Without PayPal sandbox credentials, payments run in the local simulator, clearly marked simulated, and stop after execution: only a PayPal-verified outcome can count as paid, so capture and refunds need these keys.</p>':''}<dl class="ws-meta"><dt>Source</dt><dd>${value?.source==='workspace'?'Workspace key':value?.source==='server'?'AgentPal server key (encrypted, never shown) · add your own to override':'None'}</dd><dt>Key hint</dt><dd>${safe(value?.hint||'—')}</dd><dt>Verified at</dt><dd>${safe(value?.verified_at?new Date(value.verified_at).toLocaleString():'Not verified')}</dd></dl>${value?.error_code?`<p role="status">${safe(verificationMessage(value.error_code))}</p>`:''}<p class="ws-card-status" role="status">${safe(notices.get(p.id)|| (skipped.has(p.id)?'Skipped for this tab. Connect any time.':''))}</p>${edit?`<form data-provider-form="${p.id}" autocomplete="off"><fieldset ${busy.has(p.id)?'disabled':''}><legend>${connected?'Replace credentials':'Connect '+p.label}</legend>${p.fields.map(([name,label,required])=>`<label for="ws-${p.id}-${name}">${label}<input id="ws-${p.id}-${name}" name="${name}" type="password" ${required?'required':''} autocomplete="new-password" spellcheck="false" autocapitalize="off" maxlength="4096"></label>`).join('')}<div class="ws-actions"><button class="i-primary" type="submit">Verify &amp; save</button><button class="i-secondary" type="button" data-service-action="cancel" data-id="${p.id}">Cancel</button></div></fieldset></form>`:`<div class="ws-actions"><button class="i-primary" data-service-action="replace" data-id="${p.id}" ${busy.has(p.id)?'disabled':''}>${connected?'Replace':'Connect'}</button>${value?.source?`<button class="i-secondary" data-service-action="verify" data-id="${p.id}" ${busy.has(p.id)?'disabled':''}>Re-verify</button><button class="i-secondary" data-service-action="remove" data-id="${p.id}" ${busy.has(p.id)||value.source==='server'?'disabled':''}>Remove</button>`:''}</div>`}${value?.source==='server'?'<small>The operator manages the server key. A workspace key overrides it; removing a workspace key restores the server key.</small>':''}${wizard?`<button class="link ws-skip" data-service-action="skip" data-id="${p.id}" ${busy.has(p.id)?'disabled':''}>Skip for now</button>`:''}</article>`;
 }
 function progress(){return `${providers.filter(p=>item(p.id)?.status==='verified').length} of 5 connected`;}
 function render(wizard=false){return `<section class="ws-services" data-workspace-services="${wizard?'wizard':'page'}"><div class="i-heading"><div><h2>${wizard?'Add the keys you have, or skip':'Workspace services'}</h2><p>Keys are verified by the server and never saved in browser storage.</p></div><div class="ws-actions">${wizard?'<button class="i-secondary" data-service-action="finish">Skip setup</button>':'<button class="i-secondary" data-service-action="wizard">Connect your services</button>'}<button class="i-secondary" data-service-action="refresh">Refresh status</button></div></div><p class="ws-progress" role="status">${progress()}</p><p class="f-sandbox-note">PayPal sandbox · no real money</p><p class="ws-load-status" role="alert">${safe(loadError||(!loaded?'Loading service status…':''))}</p><div class="i-provider-grid">${providers.map(p=>card(p,wizard)).join('')}</div>${wizard?'<div class="ws-actions"><button class="i-primary" data-service-action="finish">Done — open dashboard</button></div>':''}</section>`;}
 function roots(){return document.querySelectorAll('[data-workspace-services]');}
 function paint(id){if(loaded)api.data.integrations=items;for(const root of roots()){const old=root.querySelector(`[data-provider="${id}"]`);if(old)old.outerHTML=card(providers.find(p=>p.id===id),root.dataset.workspaceServices==='wizard');root.querySelector('.ws-progress').textContent=progress();}}
 async function load(root){
  const version=epoch;root.querySelector('.ws-load-status').textContent='Loading service status…';
  try{const result=await api.get('integrations');if(version!==epoch)return;items=(result.integrations||[]).filter(i=>ids.has(i.provider));loaded=true;loadError='';for(const p of providers)if(!editing.has(p.id)&&!busy.has(p.id))paint(p.id);}
  catch(err){if(version!==epoch)return;loadError=api.message(err);}
  if(root.isConnected)root.querySelector('.ws-load-status').textContent=loadError;
 }
 async function save(form){
  const id=form.dataset.providerForm;if(!ids.has(id)||busy.has(id))return;
  busy.add(id);const fields={};for(const input of form.querySelectorAll('input')){if(input.value)fields[input.name]=input.value;input.value='';}
  form.querySelector('fieldset').disabled=true;const version=epoch;
  try{const value=await api.put('integrations/'+id,{fields});if(version!==epoch)return;items=items.filter(i=>i.provider!==id).concat(value);loaded=true;editing.delete(id);skipped.delete(id);remember();notices.set(id,'Verified & saved. Credentials cleared.');}
  catch(err){if(version!==epoch)return;notices.set(id,err.code==='verification_failed'?verificationMessage(err.detail?.error_code):api.message(err));}
  finally{for(const name of Object.keys(fields))delete fields[name];busy.delete(id);if(version===epoch)paint(id);}
 }
 async function action(button,root){
  const {serviceAction:kind,id}=button.dataset;if(button.disabled)return;
  if(kind==='finish'){seen=true;remember();for(const input of root.querySelectorAll('input'))input.value='';root.remove();document.getElementById('modal').close();return;}
  if(kind==='wizard'){openWizard();return;}
  if(kind==='refresh'){button.disabled=true;await load(root);button.disabled=false;return;}
  if(!ids.has(id)||busy.has(id))return;
  if(kind==='replace'){skipped.delete(id);editing.add(id);remember();paint(id);return;}
  if(kind==='cancel'||kind==='skip'){editing.delete(id);skipped.add(id);remember();paint(id);return;}
  busy.add(id);button.disabled=true;const version=epoch;
  try{
   if(kind==='verify'){const value=await api.post('integrations/'+id+'/verify');if(version!==epoch)return;items=items.filter(i=>i.provider!==id).concat(value);notices.set(id,value.status==='verified'?'Verified again.':verificationMessage(value.error_code));}
   if(kind==='remove'){await api.delete('integrations/'+id);if(version!==epoch)return;notices.set(id,'Workspace key removed. Refreshing effective server status.');await load(root);}
  }catch(err){if(version===epoch){if(kind==='verify')items=items.map(i=>i.provider===id?{...i,status:'failed'}:i);notices.set(id,err.code==='verification_failed'?verificationMessage(err.detail?.error_code):api.message(err));}}
  finally{busy.delete(id);if(version===epoch)paint(id);}
 }
 function mount(root=document.querySelector('[data-workspace-services]')){if(!root)return;root.onsubmit=e=>{const form=e.target.closest('[data-provider-form]');if(form){e.preventDefault();save(form);}};root.onclick=e=>{const button=e.target.closest('[data-service-action]');if(button)action(button,root);};load(root);}
 function openWizard(){seen=true;remember();App.modal('Connect your services',render(true));const root=document.querySelector('#modal [data-workspace-services]');mount(root);document.getElementById('modal').addEventListener('close',()=>{for(const input of root.querySelectorAll('input'))input.value='';root.remove();},{once:true});}
 // Offer the wizard only when nothing usable is connected yet (judges on a server with operator keys skip it).
 async function offer(){if(seen)return;try{const result=await api.get('integrations');if((result.integrations||[]).some(i=>i.status==='verified'||i.source==='server'))return;}catch(_){}openWizard();}
 window.addEventListener('agentpal-live',e=>{if(e.detail?.type==='signed-out'){epoch++;items=[];loaded=false;skipped.clear();editing.clear();busy.clear();notices.clear();seen=false;try{sessionStorage.removeItem(tabKey);}catch(_){} }});
 window.WorkspaceServices={render,mount,offer,openWizard};
})();
