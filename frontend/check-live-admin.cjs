/* UI-F2 live contract/security regression checks. No network, packages or credentials. */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const sourceDir = process.env.AGENTPAL_SCREEN_DIR || path.join(__dirname, 'site');
const attack = '<img src=x onerror=alert(1)>';
const escaped = '&lt;img src=x onerror=alert(1)&gt;';
const attribute = '\"><img src=x onerror=alert(1)>';
const names = ['agents-screen','wallets-screen','insights-screens','admin-screens'];
const clone = v => JSON.parse(JSON.stringify(v));
const capEnum = JSON.parse(fs.readFileSync(path.join(__dirname,'../docs/api/openapi.json'))).components.schemas.Capability.enum;
function harness(withDelete = false) {
  const nodes = new Map(), calls = [], toasts = [], events = [], listeners = new Map();
  let modal = '', title = '', storage = 0, signedOut = 0, copied = '', failure = null, pending = null;
  const node = selector => {
    if (!nodes.has(selector)) nodes.set(selector, {
      innerHTML:'', outerHTML:'', textContent:'', disabled:false, dataset:{},
      classList:{toggle(){}}, querySelector:node,removeEventListener(){},
      querySelectorAll:s=>s==='[data-a-revoke]'?[node('revoke-button')]:[],
      addEventListener(type,fn){this[type]=fn;},
      close(){this.open=false;this.closeEventCount=(this.closeEventCount||0)+1;if(this.eventsClose)this.eventsClose();},
    });
    return nodes.get(selector);
  };
  // Model dialog close listeners separately from its close() method.
  node('dialog[open]').addEventListener=(type,fn)=>{if(type==='close')node('dialog[open]').eventsClose=fn;};
  const agent={agent_id:'agt_1',name:attack,description:attack,role:attack,status:'active',created_at:attack,capabilities:[attack],keys:[{key_id:attribute,status:'live',created_at:attack,expires_at:attack,revoked_at:null}]};
  const limits={monthly_budget:'120.00',per_transaction_limit:'25.00',per_transaction_review_limit:'20.00',rolling_window_limit:'100.00'};
  const wallet={wallet_id:'wal_1',agent_id:'agt_1',label:attack,currency:'USD',status:'active',limits,allowance:{available:'110.00',spent:'10.00',reserved:'0.00',pending:'0.00',committed:'10.00',period:attack}};
  const data={health:{status:'ok',payment_environment:'sandbox',is_real_money:false},agents:[agent,{...clone(agent),agent_id:'agt_2',name:'Unassigned external agent',keys:[]}],wallets:[wallet],payments:[{recommendation:attack,status:'approved'},{recommendation:'require_review',status:'rejected'}],payouts:[{status:'provider_pending'}],disputes:[{dispute_id:attribute,request_id:attack,status:attack,reason:attack}],transactions:{}};
  const record = async (method,p,body) => {
    calls.push({method,path:p,...(body===undefined?{}:{body:clone(body)})});
    if (pending) await pending;
    if (failure) throw Error('raw backend error must not be rendered');
    if(method==='GET')return {capabilities:['wallet.read'],ceiling:capEnum};
    return {api_key:'one-time-test-value'};
  };
  const api={live:true,data,get:p=>record('GET',p),post:(p,b)=>record('POST',p,b),put:(p,b)=>record('PUT',p,b),patch:(p,b)=>record('PATCH',p,b),
    refresh:async()=>{calls.push({method:'REFRESH'});},
    walletTransactions:async id=>{calls.push({method:'TRANSACTIONS',path:id});if(failure)throw Error();data.transactions[id]=[{request_id:attribute,merchant_name:attack,purpose:attack,created_at:attack,ledger_state:attack,amount:'10.00',currency:'USD'}];},
    money:(a,c)=>{assert.equal(typeof a,'string','Money stays a decimal string');return `${c} ${a}`;},message:()=>attack,newKey:p=>p+'-test',signOut:()=>{signedOut++;}};
  if(withDelete)api.delete=p=>record('DELETE',p);
  const c={Intl,Date,Math,JSON,Number,String,Array,Set,Map,Object,Promise,Uint8Array,structuredClone,console,
    location:{origin:'https://agentpal.test'},navigator:{clipboard:{writeText:async value=>{copied=value;}}},
    CustomEvent:class {constructor(type,options){this.type=type;this.detail=options?.detail;}},
    FormData:class {constructor(form){this.values=form.values;}get(k){return this.values[k]??null;}getAll(k){return this.values[k]||[];}has(k){return !!this.values[k];}},
    localStorage:{getItem(){storage++;throw Error('live storage read');},setItem(){storage++;throw Error('live storage write');}},
    sessionStorage:{getItem(){storage++;throw Error('live storage read');},setItem(){storage++;throw Error('live storage write');}},
    document:{querySelector:node,addEventListener(){}},AgentPalAPI:api,
    App:{escape:v=>String(v).replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch])),icon:()=>'<svg/>',spark:()=>'<svg/>',toast:v=>toasts.push(v),navigate:r=>events.push(r),modal:(t,html)=>{node('dialog[open]').close();title=t;modal=html;node('dialog[open]').eventsClose=null;node('dialog[open]').open=true;}},
    addEventListener(type,fn){if(!listeners.has(type))listeners.set(type,[]);listeners.get(type).push(fn);},
    removeEventListener(type,fn){listeners.set(type,(listeners.get(type)||[]).filter(f=>f!==fn));},
    dispatchEvent(e){events.push(e.type);for(const fn of listeners.get(e.type)||[])fn(e);},
  };
  c.window=c;vm.createContext(c);
  require('./visual-harness.cjs')(c,node);
  for(const name of names)vm.runInContext(fs.readFileSync(path.join(sourceDir,name+'.js'),'utf8'),c,{filename:name+'.js'});
  return {c,api,data,node,calls,toasts,events,get modal(){return modal;},get title(){return title;},get storage(){return storage;},get signedOut(){return signedOut;},get copied(){return copied;},setFailure(v){failure=v;},setPending(v){pending=v;},
    submit(selector,values){const form=node(selector);form.values=values;return form.onsubmit({preventDefault(){},target:form});},
    click(root,dataset={},id=''){const b={dataset,id,disabled:false};return node(root).onclick({target:{closest:()=>b},currentTarget:b});},
  };
}
let groups=0;
async function main(){
  const h=harness(),{c,node,api,data}=h;
  const renders=[()=>c.AgentScreen.render(),()=>c.WalletScreen.render(),()=>c.InsightsScreens.integrations(),()=>c.InsightsScreens.analytics(),()=>c.AdminScreens.api(),()=>c.AdminScreens.security(),()=>c.AdminScreens.settings()];
  assert.equal(h.storage,0,'Live initialization must never read demo persistence');
  for(const render of renders){const html=render();assert.ok(!html.includes(attack));assert.ok(!html.includes(attribute));assert.doesNotMatch(html,/ChatGPT Agent|Claude Agent|Gemini Agent|Midjourney Agent|Meta Agent|Tanveer|18230|24,840|12\.5%|Illustrative analytics/);assert.ok(!html.includes('NaN'));}
  for(const render of [renders[1],renders[5]])assert.ok(render().includes(escaped),'API strings must be escaped');
  c.AgentScreen.mount();const gridRows=c.__testGrids.at(-1).rowData;assert.equal(gridRows[0].budget,'120.00');assert.equal(gridRows[0].spent,'10.00');assert.equal(gridRows[0].name,attack);
  assert.doesNotMatch(c.WalletScreen.render(),/Add Funds|Transfer|Business Bank|Crypto Wallet/);
  assert.match(c.InsightsScreens.integrations(),/Connected · sandbox/);
  data.health=null;assert.doesNotMatch(c.InsightsScreens.integrations(),/Connected · sandbox/);data.health={status:'ok',payment_environment:'sandbox',is_real_money:false};
  assert.match(c.AdminScreens.api(),/https:\/\/agentpal.test\/v1/);assert.match(c.AdminScreens.api(),/\$AGENT_KEY/);assert.doesNotMatch(c.AdminScreens.api(),/apo_|ad-simulator/);
  const portal=c.AdminScreens.api(),security=c.AdminScreens.security(),integrations=c.InsightsScreens.integrations();
  for(const href of ['./openapi.json','./agentpal.postman_collection.json'])assert.ok(portal.includes('href="'+href+'"'));
  assert.match(portal,/sdk\/python/);assert.match(portal,/Python SDK origin/);
  assert.equal((security.match(/<li>/g)||[]).length,12,'All product principles are visible');assert.doesNotMatch(security,/ · live/);
  for(const name of ['Built with','PayPal sandbox','Channel3','Kernel','Render + Render Workflows','Astropods','AG Grid / AG Charts','Groq','Postman'])assert.ok(integrations.includes(name),name);
  assert.match(c.InsightsScreens.analytics(),/spend-by-company/);
  groups++;

  c.AgentScreen.create();
  for(const cap of capEnum)assert.ok(h.modal.includes(`value="${cap}"`),'Capabilities must match OpenAPI');
  assert.doesNotMatch(h.modal,/<input[^>]+ checked/,'No grants selected by default');
  await h.submit('#a-create-form',{name:'New external agent',description:'Owner workflow',capabilities:['wallet.read','payment.prepare']});
  assert.deepEqual(h.calls.filter(x=>x.method==='POST').at(-1),{method:'POST',path:'agents',body:{name:'New external agent',description:'Owner workflow',role:'external',capabilities:['wallet.read','payment.prepare']}});
  assert.match(h.modal,/Shown once\. Give it to the agent; AgentPal stores only a hash\./);
  assert.ok(!h.modal.includes('one-time-test-value'),'Secret assigned through textContent');
  assert.equal(node('#a-once-key').textContent,'one-time-test-value');node('#a-once-key').isConnected=true;node('dialog[open]').eventsClose();assert.equal(node('#a-once-key').textContent,'one-time-test-value','Previous modal close event must not clear the newly issued key');await node('#a-copy-key').onclick();assert.equal(h.copied,'one-time-test-value');
  node('dialog[open]').close();assert.equal(node('#a-once-key').textContent,'','Closing the modal clears the key');assert.equal(node('#a-copy-key').onclick,null);
  assert.ok(!c.AgentScreen.render().includes('one-time-test-value'));assert.equal(h.storage,0);groups++;

  c.AgentScreen.manage('agt_1');assert.ok(!h.modal.includes(attack));assert.match(h.modal,/revocation is unavailable/);
  let before=h.calls.length;node('#a-status').onclick();assert.equal(h.calls.length,before,'Status change requires confirmation');await node('#a-confirm').onclick();
  assert.deepEqual(h.calls.filter(x=>x.method==='POST').at(-1),{method:'POST',path:'agents/agt_1/suspend',body:{}});
  data.agents[0].status='suspended';c.AgentScreen.manage('agt_1');node('#a-status').onclick();await node('#a-confirm').onclick();assert.equal(h.calls.filter(x=>x.method==='POST').at(-1).path,'agents/agt_1/reactivate');
  c.AgentScreen.manage('agt_1');await node('#a-permissions').onclick({currentTarget:node('#a-permissions')});assert.deepEqual(h.calls.at(-1),{method:'GET',path:'agents/agt_1/permissions'});
  await h.submit('#a-permissions-form',{capabilities:['wallet.read','payment.execute_sandbox','unsupported']});
  assert.deepEqual(h.calls.filter(x=>x.method==='PUT').at(-1),{method:'PUT',path:'agents/agt_1/permissions',body:{capabilities:['wallet.read','payment.execute_sandbox']}});
  c.AgentScreen.manage('agt_1');node('#a-rotate').onclick();await node('#a-confirm').onclick();assert.deepEqual(h.calls.filter(x=>x.method==='POST').at(-1),{method:'POST',path:'agents/agt_1/credentials',body:{}});assert.equal(node('#a-once-key').textContent,'one-time-test-value');node('dialog[open]').close();groups++;

  // The current lead client lacks DELETE. Also prove the already-wired action if it is added.
  const d=harness(true);d.c.AgentScreen.manage('agt_1');d.node('revoke-button').dataset={aRevoke:attribute};before=d.calls.length;d.node('revoke-button').onclick();assert.equal(d.calls.length,before);await d.node('#a-confirm').onclick();assert.deepEqual(d.calls.find(x=>x.method==='DELETE'),{method:'DELETE',path:'agents/agt_1/credentials/'+encodeURIComponent(attribute)});groups++;

  c.WalletScreen.create();assert.ok(!h.modal.includes('value="agt_1"'),'Agents already assigned a wallet excluded');
  const values={agent_id:'agt_2',monthly_budget:'000120.00',per_transaction_limit:'25.0'};
  await h.submit('#ws-form',values);assert.deepEqual(h.calls.filter(x=>x.path==='wallets').at(-1),{method:'POST',path:'wallets',body:{...values,currency:'USD'}});
  c.WalletScreen.create();before=h.calls.length;
  for(const invalid of ['1e2',' 2','2 ','-1','1.001','10000000','1.','NaN','<img>'])await h.submit('#ws-form',{...values,monthly_budget:invalid});
  assert.equal(h.calls.length,before,'Invalid amounts never reach API');
  c.WalletScreen.action('limits');await h.submit('#ws-form',{...data.wallets[0].limits,monthly_budget:'150.00'});assert.deepEqual(h.calls.filter(x=>x.method==='PATCH').at(-1),{method:'PATCH',path:'wallets/wal_1',body:{monthly_budget:'150.00'}});
  c.WalletScreen.action('limits');before=h.calls.length;await h.submit('#ws-form',data.wallets[0].limits);assert.equal(h.calls.length,before,'Unchanged limits never sent');
  for(const [state,op] of [['active','freeze'],['frozen','unfreeze']]){data.wallets[0].status=state;before=h.calls.length;c.WalletScreen.action('freeze');assert.equal(h.calls.length,before);await node('#ws-confirm').onclick();assert.deepEqual(h.calls.filter(x=>x.method==='POST').at(-1),{method:'POST',path:'wallets/wal_1/'+op,body:{}});}
  await c.WalletScreen.action('transactions');assert.ok(h.calls.some(x=>x.method==='TRANSACTIONS'&&x.path==='wal_1'));assert.ok(c.WalletScreen.render().includes(escaped));assert.ok(!c.WalletScreen.render().includes(attack));groups++;

  // In-flight writes disable their button and ignore a duplicate submission.
  let release;h.setPending(new Promise(resolve=>{release=resolve;}));c.AgentScreen.create();before=h.calls.filter(x=>x.method==='POST').length;
  const running=h.submit('#a-create-form',{name:'In flight',capabilities:[]});assert.equal(node('[type="submit"]').disabled,true);const duplicate=h.submit('#a-create-form',{name:'Duplicate',capabilities:[]});assert.equal(h.calls.filter(x=>x.method==='POST').length,before+1);release();await Promise.all([running,duplicate]);h.setPending(null);assert.equal(node('[type="submit"]').disabled,false);
  h.setFailure(true);c.AgentScreen.create();before=node('#a-once-key').textContent;await h.submit('#a-create-form',{name:'Fails',capabilities:[]});assert.equal(node('#a-once-key').textContent,before);assert.match(h.modal,/a-create-form/);assert.equal(h.toasts.at(-1),attack);assert.equal(h.calls.at(-1).method,'REFRESH');
  c.WalletScreen.action('limits');await h.submit('#ws-form',{...data.wallets[0].limits,monthly_budget:'500.00'});assert.equal(h.calls.at(-1).method,'REFRESH');assert.equal(node('[type="submit"]').disabled,false);h.setFailure(false);
  // The wallet writer also ignores duplicate submissions while waiting on the API.
  let releaseWallet;h.setPending(new Promise(resolve=>{releaseWallet=resolve;}));c.WalletScreen.action('limits');before=h.calls.filter(x=>x.method==='PATCH').length;
  const walletWrite=h.submit('#ws-form',{...data.wallets[0].limits,monthly_budget:'600.00'});assert.equal(node('[type="submit"]').disabled,true);
  const duplicateWallet=h.submit('#ws-form',{...data.wallets[0].limits,monthly_budget:'600.00'});assert.equal(h.calls.filter(x=>x.method==='PATCH').length,before+1);releaseWallet();await Promise.all([walletWrite,duplicateWallet]);h.setPending(null);
  for(const log of [h.calls,d.calls])for(let i=0;i<log.length;i++)if(['POST','PUT','PATCH','DELETE'].includes(log[i].method))assert.equal(log[i+1]?.method,'REFRESH','Every write must refresh before rendering');
  assert.ok(h.events.includes('agents-updated'));assert.ok(h.events.includes('wallets-updated'));assert.equal(h.storage,0);groups++;

  node('[data-admin]').dataset.admin='security';c.AdminScreens.mount();h.click('[data-admin]',{adLive:'sign-out'});assert.equal(h.signedOut,1);
  data.errors=['wallets'];assert.match(c.AgentScreen.render(),/Wallet data unavailable/);data.errors=['agents'];c.WalletScreen.create();assert.match(h.modal,/could not be loaded/);
  data.errors=['agents','wallets','payment-requests','disputes'];assert.match(c.AgentScreen.render(),/could not be loaded/);assert.match(c.WalletScreen.render(),/could not be loaded/);assert.match(c.InsightsScreens.analytics(),/report is incomplete/);assert.match(c.AdminScreens.security(),/Disputes could not be loaded/);data.errors=[];
  for(const key of ['agents','wallets','payments','payouts','disputes'])data[key]=[];
  for(const render of renders){const html=render();assert.doesNotMatch(html,/ChatGPT Agent|Claude Agent|Gemini Agent|Tanveer|NaN|one-time-test-value/);}
  assert.match(c.AgentScreen.render(),/No agents yet/);assert.match(c.WalletScreen.render(),/No wallets yet/);assert.match(c.InsightsScreens.analytics(),/No payment or payout requests yet/);assert.match(c.AdminScreens.security(),/No disputes reported/);assert.equal(h.storage,0);groups++;
  console.log(`PASS: ${groups} live check groups — escaping, fixture/storage isolation, one-time keys, all available writes, DELETE dependency guard, decimal validation, changed fields, confirmation, in-flight protection, refresh/errors, ledger reads, docs, integrations, analytics, sign-out and empty states.`);
}
main().catch(err=>{console.error(err);process.exitCode=1;});
