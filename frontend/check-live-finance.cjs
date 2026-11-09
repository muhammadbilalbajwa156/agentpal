const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const payload='<img src=x onerror=alert(1)>',hostileId='req-"'+payload;
const payment=(id,status='awaiting_approval')=>({request_id:id,agent_id:'agent-live',wallet_id:'wallet-live',created_at:'2026-11-06T12:00:00Z',status,terms_sha256:'hash-original',recommendation:payload,risk_score:72,risk_signals:[{code:payload,message:payload}],terms:{amount:'12.34',currency:'USD',merchant_name:payload,merchant_domain:payload,merchant_paypal_email:'merchant@example.test',purpose:payload},market_check:{advisory_level:'review_recommended',better_deals:[{price:'10.00',currency:'USD',buy_url:payload}],disclosure:payload},page_check:{advisory_level:'high_risk',signals:[{code:payload,message:payload}],observed:{product_name:payload}}});
const payout={request_id:'payout-live',agent_id:'agent-live',wallet_id:'wallet-live',created_at:'2026-11-05T12:00:00Z',amount:'2.50',currency:'EUR',receiver_email:payload,purpose:payload,note:payload,terms_sha256:'payout-hash',status:'awaiting_approval'};
const records=new Map([['payment-requests/pay-live',payment('pay-live')],['payment-requests/'+hostileId,payment(hostileId)],['payment-requests/confirmed-live',payment('confirmed-live','provider_confirmed')],['payment-requests/unknown-live',payment('unknown-live','reconciliation_required')],['payout-requests/payout-live',payout]]);
const data={agents:[{agent_id:'agent-live',name:payload,status:'active'}],wallets:[{wallet_id:'wallet-live',currency:'USD',allowance:{available:'0.10',spent:'12.34',reserved:'1.00',pending:'2.00',committed:'15.34'}},{wallet_id:'wallet-two',currency:'USD',allowance:{available:'0.20',spent:'0.00',reserved:'0.00',pending:'0.00',committed:'0.00'}},{wallet_id:'wallet-eur',currency:'EUR',allowance:{available:'9.00',spent:'2.50',reserved:'0.00',pending:'0.00',committed:'2.50'}}],payments:[...records.values()].filter(r=>r.terms),approvals:[records.get('payment-requests/pay-live'),records.get('payment-requests/'+hostileId)],payouts:[payout],errors:[]};
records.get('payment-requests/confirmed-live').owner_decision={decision:'approved',at:'2026-11-06T12:00:00Z'};
records.get('payment-requests/confirmed-live').verification={verified_by:'paypal_read',at:'2026-11-06T12:01:00Z'};
records.get('payment-requests/confirmed-live').terms.merchant_paypal_email=null;
const blocked=payment('blocked-live','rejected');blocked.policy_block={rule:payload,label:payload};data.payments.push(blocked);records.set('payment-requests/blocked-live',blocked);
const elements=new Map(),listeners={},clicks=[],calls=[],toasts=[],events=[];
let storageAccess=0,keyCount=0,timelineStatus=200;
let html='',title='',csvBlob,downloaded=false,failPost=null,holdPost=null,holdRefresh=null;
function element(selector){if(!elements.has(selector))elements.set(selector,{innerHTML:'',outerHTML:'',textContent:'',value:'',hidden:false,disabled:false,dataset:{liveFinance:'approvals'},classList:{toggle(){},add(){},remove(){}},setAttribute(){},removeAttribute(){},addEventListener(){},querySelector:element,querySelectorAll:()=>[],getBoundingClientRect:()=>({left:0,top:0,right:500,bottom:500}),focus(){},close(){this.open=false},showModal(){this.open=true}});return elements.get(selector);}
const api={live:true,data,newKey:prefix=>prefix+'-random-key-'+(++keyCount),money:(amount,currency)=>currency+' '+Number(amount).toFixed(2),message:err=>'API failure: '+(err.code||'network'),
 async get(path){calls.push(['GET',path]);if(path.endsWith('/timeline')){if(timelineStatus!==200)throw {status:timelineStatus};return {events:[{at:'2026-11-06T12:00:00Z',actor:'agent',label:payload,detail:payload,verified_by:null},{at:'2026-11-06T12:01:00Z',actor:'paypal',label:'Outcome verified',detail:null,verified_by:'paypal_webhook'}]};}if(path.endsWith('/reconciliation'))return {reconciliation_required:path.includes('unknown-live'),provider_references:[{provider:payload,provider_order_id:payload,status:payload}]};const value=records.get(decodeURIComponent(path));assert.ok(value,'Unknown fake GET '+path);return structuredClone(value);},
 async post(path,body,options){calls.push(['POST',path,body,options]);if(holdPost)await holdPost;if(failPost){const err=failPost;failPost=null;throw err;}return {};},
 async refunds(id){calls.push(['REFUNDS',id]);return [{refund_id:payload,amount:'1.00',currency:'USD',status:'FAILED',failure_reason:'merchant_permission_required'},{refund_id:'refund-unknown',amount:'0.50',currency:'USD',status:'reconciliation_required',failure_reason:null}];},
 async refresh(){calls.push(['REFRESH']);if(holdRefresh)await holdRefresh;return data;}};
const context={Intl,Date,Math,JSON,Number,String,Array,Set,Map,BigInt,Blob,structuredClone,URL:{createObjectURL:b=>(csvBlob=b,'blob:live'),revokeObjectURL(){}},setTimeout:()=>0,clearTimeout(){},console,location:{hash:'#overview'},history:{pushState(_a,_b,url){context.location.hash=url}},navigator:{onLine:true},scrollTo(){},CustomEvent:class{constructor(type,options){this.type=type;this.detail=options?.detail}},FormData:class{constructor(form){this.form=form;}get(name){return this.form.values[name];}},localStorage:{getItem(){storageAccess++;throw Error('Live screen read localStorage');},setItem(){storageAccess++;throw Error('Live screen wrote localStorage');}},sessionStorage:{getItem(){storageAccess++;throw Error('Live screen read sessionStorage');},setItem(){storageAccess++;throw Error('Live screen wrote sessionStorage');}},document:{body:{dataset:{},classList:{toggle(){}}},querySelector:element,querySelectorAll:()=>[],addEventListener(name,fn){if(name==='click')clicks.push(fn);},createElement:()=>({click(){downloaded=true;}})},addEventListener(name,fn){(listeners[name]??=[]).push(fn);},dispatchEvent(event){events.push(event.type);for(const fn of listeners[event.type]||[])fn(event);},AgentPalAPI:api};
context.window=context;
const escape=value=>String(value).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
context.App={escape,icon:()=>'<svg/>',spark:()=>'<svg/>',toast:message=>toasts.push(message),modal:(t,h)=>{title=t;html=h;}};
vm.createContext(context);
require('./visual-harness.cjs')(context,element);
const source=process.env.AGENTPAL_FINANCE_SOURCE||'site/finance-screens.js';
vm.runInContext(fs.readFileSync(source,'utf8'),context,{filename:source});
const f=context.FinanceScreens;
function clean(value){assert.ok(!value.includes(payload),'API markup must be escaped');assert.doesNotMatch(value,/Tanveer|Agent Nova|Gemini Agent|ChatGPT Agent|Business Bank|Card Wallet|REQ-1008|TX-1048|\+12\.5%|Approval Settings|Demo data|Illustrative/);}
function escaped(value){clean(value);assert.ok(value.includes('&lt;img src=x onerror=alert(1)&gt;'));}
const button=(dataset={})=>({dataset,disabled:false,hasAttribute(name){return name==='data-live-export'&&dataset.export;}});
async function rootClick(dataset){element('[data-live-finance]').dataset.liveFinance=dataset.export?'transactions':'approvals';f.mount();const b=button(dataset);await element('[data-live-finance]').onclick({target:{closest:()=>b}});return b;}
const modalClick=selector=>{const b=element(selector);return b.onclick({currentTarget:b});};
const posts=()=>calls.filter(c=>c[0]==='POST');
function expectPost(path,body){const last=posts().at(-1);assert.equal(last[1],path);assert.deepEqual(JSON.parse(JSON.stringify(last[2])),body);}
async function refund(amount){const b=element('button[type="submit"]'),form={values:{amount},querySelector:()=>b};await element('#f-live-refund').onsubmit({preventDefault(){},target:form});return b;}
async function run(){
  escaped(f.approvals());clean(f.transactions());assert.ok(f.approvals().includes(escape(hostileId)));assert.equal(f.pendingCount(),3);assert.equal(calls.length,0,'Provider details must load only on demand');assert.equal(storageAccess,0);
  data.errors=['approvals'];assert.match(f.approvals(),/Some live data could not be loaded/);assert.match(f.transactions(),/Some live data could not be loaded/);data.errors=[];
  assert.match(f.approvals(),/Approved requests<[^>]*>.*?1/s);assert.match(f.approvals(),/Blocked by rules/);assert.equal(f.summary().transactions.find(r=>r.id==='blocked-live').status,'blocked_by_rule');
  const snapshot=JSON.stringify(data);
  await rootClick({liveDetails:'pay-live',kind:'payment'});escaped(html);assert.equal(title,'Request details');assert.match(html,/merchant@example.test/);assert.match(html,/Risk signals/);assert.match(html,/Timeline/);assert.match(html,/Verified by PayPal webhook/);assert.doesNotMatch(html,/<img/);assert.match(html,/Market check/);assert.match(html,/Page check/);assert.match(html,/10.00/);assert.doesNotMatch(html,/<a\b/);assert.equal(posts().length,0);assert.equal(calls[0][1],'payment-requests/pay-live');
  await modalClick('#f-live-confirm');assert.equal(title,'Confirm sandbox approval');assert.match(html,/merchant@example.test/);assert.match(html,/12.34/);
  assert.match(html,/hash-origina/);assert.match(html,/Any change to amount, payee or purpose voids this approval\./);
  const start=calls.length;
  await modalClick('#f-live-confirm');expectPost('payment-requests/pay-live/approve',{terms_sha256:'hash-original'});
  assert.deepEqual(calls.slice(start).map(c=>c[0]),['GET','POST','REFRESH']);assert.ok(events.includes('finance-updated'));assert.ok(events.includes('wallets-updated'));
  // Changed terms must be reviewed again; the newly fetched hash cannot silently authorize them.
  await rootClick({liveApprove:'pay-live',kind:'payment'});
  records.get('payment-requests/pay-live').terms_sha256='hash-changed';records.get('payment-requests/pay-live').terms.amount='99.00';
  let count=posts().length;await modalClick('#f-live-confirm');assert.equal(posts().length,count);assert.match(html,/terms changed/);assert.match(html,/99.00/);
  await modalClick('#f-live-confirm');expectPost('payment-requests/pay-live/approve',{terms_sha256:'hash-changed'});
  await rootClick({liveApprove:'pay-live',kind:'payment'});failPost={status:409,code:'approval_terms_mismatch'};
  await modalClick('#f-live-confirm');assert.match(html,/terms changed/);assert.equal(calls.at(-1)[0],'REFRESH');assert.ok(toasts.some(t=>t.includes('approval_terms_mismatch')));
  // The same fresh-read and exact-body protocol applies to payouts.
  await rootClick({liveApprove:'payout-live',kind:'payout'});escaped(html);await modalClick('#f-live-confirm');expectPost('payout-requests/payout-live/approve',{terms_sha256:'payout-hash'});
  await rootClick({liveReject:'payout-live',kind:'payout'});expectPost('payout-requests/payout-live/reject',{});
  await rootClick({liveDetails:'pay-live',kind:'payment'});await modalClick('#f-live-reject');expectPost('payment-requests/pay-live/reject',{});
  await rootClick({liveTransaction:'confirmed-live',kind:'payment'});escaped(html);assert.match(html,/merchant&#39;s permission/);assert.match(html,/AgentPal sandbox merchant \(simulated checkout\)/);assert.match(html,/Verified by PayPal read/);assert.match(html,/needs reconciliation: no money assumed/);assert.ok(calls.some(c=>c[0]==='REFUNDS'&&c[1]==='confirmed-live'));
  for(const invalid of ['1e2','-1','1.234',' 1.00','1.00 ','10000000','0.00','<img>']){count=posts().length;await refund(invalid);assert.equal(posts().length,count,invalid);assert.match(element('#f-live-refund-error').textContent,/positive amount/);}
  await refund('001.20');expectPost('payment-requests/confirmed-live/refunds',{amount:'001.20'});assert.equal(posts().at(-1)[3].idempotency,'rf-random-key-1');
  await refund('');expectPost('payment-requests/confirmed-live/refunds',{});assert.equal(posts().at(-1)[3].idempotency,'rf-random-key-2');
  await rootClick({liveTransaction:'unknown-live',kind:'payment'});assert.match(html,/Needs reconciliation: no money assumed/);assert.ok(!html.includes('id="f-live-refund"'));await modalClick('#f-live-reconcile');expectPost('payment-requests/unknown-live/reconciliation',{});
  await rootClick({liveTransaction:'payout-live',kind:'payout'});clean(html);assert.ok(!html.includes('id="f-live-refund"'));
  payout.status='reconciliation_required';await rootClick({liveTransaction:'payout-live',kind:'payout'});await modalClick('#f-live-reconcile');expectPost('payout-requests/payout-live/reconciliation',{});payout.status='awaiting_approval';
  // Optional endpoints disappear on older servers; never-executed requests issue no reconciliation read.
  timelineStatus=404;const before=calls.length;await rootClick({liveTransaction:'blocked-live',kind:'payment'});assert.doesNotMatch(html,/>Timeline</);assert.ok(!calls.slice(before).some(c=>c[1]?.endsWith('/reconciliation')));assert.match(html,/Blocked by rule/);
  timelineStatus=503;await rootClick({liveDetails:'pay-live',kind:'payment'});assert.match(html,/Timeline could not be loaded/);timelineStatus=200;
  // Failed writes refresh, show only the API's safe message, and never claim success.
  await rootClick({liveTransaction:'confirmed-live',kind:'payment'});failPost={status:503,code:payload};let toastStart=toasts.length;
  await refund('1.00');assert.deepEqual(toasts.slice(toastStart),['API failure: '+payload]);assert.equal(calls.at(-1)[0],'REFRESH');
  // Duplicate submissions are blocked through both provider POST and the following refresh.
  await rootClick({liveApprove:'pay-live',kind:'payment'});
  let releasePost,releaseRefresh;holdPost=new Promise(r=>releasePost=r);holdRefresh=new Promise(r=>releaseRefresh=r);
  count=posts().length;const first=modalClick('#f-live-confirm');await new Promise(r=>setImmediate(r));assert.equal(element('#f-live-confirm').disabled,true);
  const duplicate=modalClick('#f-live-confirm');await new Promise(r=>setImmediate(r));assert.equal(posts().length,count+1);releasePost();await new Promise(r=>setImmediate(r));assert.equal(element('#f-live-confirm').disabled,true);
  releaseRefresh();await Promise.all([first,duplicate]);holdPost=null;holdRefresh=null;assert.equal(element('#f-live-confirm').disabled,false);
  await rootClick({export:true});assert.ok(downloaded);const csv=await csvBlob.text();assert.match(csv,/confirmed-live/);assert.match(csv,/EUR/);assert.match(csv,/99.00/);data.agents[0].name='=2+2';context.dispatchEvent(new context.CustomEvent('agentpal-live',{detail:{type:'refreshed'}}));await rootClick({export:true});assert.ok((await csvBlob.text()).includes("\"'=2+2\""),'CSV cells cannot become spreadsheet formulas');data.agents[0].name=payload;
  // Live reads/actions must not mutate API snapshots to pretend a payment completed.
  assert.equal(data.payments[0].status,'awaiting_approval');assert.equal(JSON.parse(snapshot).payments[0].status,'awaiting_approval');
  // Load the actual shell with live summaries; demo modules must not supply its data.
  context.AgentScreen={summary(){throw Error('Demo agent summary used');},render:()=>'',mount(){}};
  context.WalletScreen={summary(){throw Error('Demo wallet summary used');},render:()=>'',mount(){}};
  context.AdminScreens={profile(){throw Error('Demo profile used');}};
  // Core owns App after boot. Keep toast evidence but use its real modal rendering below.
  vm.runInContext(fs.readFileSync('site/icons.js','utf8'),context);
  vm.runInContext(fs.readFileSync('site/core.js','utf8'),context);
  assert.equal(context.App.ready,true);escaped(element('#screen').innerHTML);assert.match(element('#screen').innerHTML,/USD 0.30/);assert.match(element('#screen').innerHTML,/EUR 9.00/);assert.match(element('#screen').innerHTML,/USD 12.34/);assert.match(element('#screen').innerHTML,/Daily confirmed spend/);
  assert.equal(element('.profile strong').textContent,'Owner');assert.equal(element('#connection-status').textContent,'System status unavailable');assert.equal(element('#connection-detail').textContent,'Live · PayPal sandbox');
  for(const fn of clicks)fn({target:{closest:selector=>selector==='[data-action]'?{dataset:{action:'notifications'}}:null}});
  escaped(element('#modal-content').innerHTML);assert.match(element('#modal-content').innerHTML,/Awaiting owner approval/);
  for(const route of ['approvals','transactions']){context.App.navigate(route);clean(element('#screen').innerHTML);}
  const saved={...data};Object.assign(data,{agents:[],wallets:[],payments:[],approvals:[],payouts:[]});
  clean(f.approvals());clean(f.transactions());assert.match(f.approvals(),/No requests are awaiting/);assert.match(f.transactions(),/No payment requests or payouts/);assert.equal(f.pendingCount(),0);
  context.App.navigate('overview');clean(element('#screen').innerHTML);assert.match(element('#screen').innerHTML,/No agents connected/);assert.match(element('.overview-right .activity').innerHTML,/No payment activity/);
  for(const fn of clicks)fn({target:{closest:selector=>selector==='[data-action]'?{dataset:{action:'notifications'}}:null}});assert.match(element('#modal-content').innerHTML,/No requests need your review/);
  Object.assign(data,saved);assert.equal(storageAccess,0);
  console.log('PASS: live escaping and no fixtures/storage; exact fresh-term payment/payout approvals and re-review; rejects, refund validation/idempotency/failures, reconciliation, duplicate lock, CSV, real overview/shell/notifications and empty states.');
}
const watchdog=setTimeout(()=>{console.error('FAIL: live-finance check did not finish');process.exitCode=1;},10000);
run().catch(err=>{console.error(err);process.exitCode=1;}).finally(()=>clearTimeout(watchdog));
