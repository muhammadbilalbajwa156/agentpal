/* Test Lab is an owner UI for the ordinary governed Lab agent. No agent key reaches JS. */
(()=>{
 const clockTime=v=>{const d=new Date(v);return Number.isNaN(d.getTime())?String(v??''):d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'});};
 'use strict';
 const api=window.AgentPalAPI,safe=v=>App.escape(String(v??''));
 let current=null,scenario=null,events=null,refs=[],refunds=[],refundsKnown=false,notice='',agentReady=null,epoch=0,revision=0;
 const busy=new Set();
 const sandbox='<p class="f-sandbox-note">PayPal sandbox · no real money</p>';
 const positive=value=>/^\d{1,7}(\.\d{1,2})?$/.test(value)&&/[1-9]/.test(value);
 const pending=['submitted_to_provider','provider_pending'];
 const cents=value=>{const m=/^(\d{1,7})(?:\.(\d{1,2}))?$/.exec(String(value??''));return m?BigInt(m[1])*100n+BigInt((m[2]||'').padEnd(2,'0')):null;};
 const decimal=n=>(n/100n)+'.'+(n%100n).toString().padStart(2,'0');
 function remaining(){const total=cents(current?.terms?.amount);if(total===null||!refundsKnown)return null;const used=refunds.filter(r=>!['FAILED','CANCELLED'].includes(r.status)).reduce((sum,r)=>sum+(cents(r.amount)||0n),0n);return total>used?total-used:0n;}
 function servicesHTML(){
  const ready=id=>(api.data.integrations||[]).some(i=>i.provider===id&&(i.status==='verified'||i.source==='server'&&i.status!=='failed'));
  return (ready('channel3')&&(ready('groq')||ready('openrouter'))?'Services ready. Try a shopping task using the provided integrations.':'Connect Channel3 and Groq or OpenRouter in Integrations to run a shopping task.')+' AI output is advisory; the agent cannot approve its own payment.';
 }
 function sandboxURL(value){try{const url=new URL(value);return url.protocol==='https:'&&['www.sandbox.paypal.com','sandbox.paypal.com'].includes(url.hostname)&&!url.username&&!url.password&&!url.port?url.href:null;}catch(_){return null;}}
 function errorMessage(err){
  if(err.code==='integration_required'){const name={paypal_sandbox:'PayPal sandbox',channel3:'Channel3',kernel:'Kernel',groq:'Groq',openrouter:'OpenRouter'}[err.detail?.provider]||'the required service';return 'Connect '+name+' in Integrations, then try this task again.';}
  if(err.status===403||['policy_block','policy_blocked','hard_limit_exceeded','insufficient_allowance'].includes(err.code))return 'A spending or permission rule blocked this action. Review the request, wallet limits and agent permissions. No approval or payment was assumed.';
  return api.message(err);
 }
 function render(){
  if(!api?.live)return '<section class="app-fallback"><h1>Test Lab</h1><p>Open the dashboard at /app/ on your AgentPal server and sign in to use Test Lab.</p></section>';
  return `<div class="lab-screen finance-screen f-live"><section class="f-heading"><div><h1>Test Lab</h1><p>Try an AI shopping errand or walk through a governed test payment.</p></div><button class="i-secondary" data-route="integrations">View integrations</button></section>${sandbox}<section class="i-panel"><h2>Test money. Your approval.</h2><p>The Test Lab agent has a virtual USD 200 monthly allowance and a USD 25 per-payment limit. These are spending limits, not money held. Try a request above USD 25 to watch a spending rule block it before any human sees it. Requests use the same policies and exact-term owner approval as any external agent.</p><p>To approve a PayPal checkout, use a <strong>sandbox buyer (personal) account</strong>, separate from your sandbox business seller. Find it in <a href="https://developer.paypal.com/dashboard/accounts" target="_blank" rel="noopener noreferrer">PayPal Developer · Testing Tools · Sandbox Accounts</a>. Only PayPal-verified outcomes count as paid. Without PayPal credentials the local simulator is labelled simulated and cannot confirm a real provider capture.</p></section><section class="i-panel lab-attacks"><h2>Try an attack</h2><p>Run a governed example. These demonstrations never approve a payment or call PayPal. Any demonstration request left awaiting a decision is declined automatically, so nothing waits in Approvals.</p><div class="ws-actions"><button class="i-secondary" data-lab-scenario="over_limit">Over the $25 limit</button><button class="i-secondary" data-lab-scenario="split_purchase">Split a purchase</button><button class="i-secondary" data-lab-scenario="changed_terms">Change the terms</button><button class="i-secondary" data-lab-scenario="payee_swap">Swap the payee</button></div><p id="lab-scenario-status" role="status"></p></section><div class="lab-grid"><section class="i-panel"><h2>Ask the AI agent to shop</h2><p id="lab-services-status">${servicesHTML()}</p><form id="lab-errand-form"><label for="lab-task">Shopping task<textarea id="lab-task" name="task" required maxlength="4000" placeholder="Find a USB mouse for the office"></textarea></label><label for="lab-budget">Budget (USD)<input id="lab-budget" name="budget" inputmode="decimal" value="30.00" required></label><button class="i-primary" type="submit">Ask the AI agent to shop</button></form><p id="lab-errand-status" role="status"></p><p id="lab-errand-task" class="lab-original-task"></p><ol id="lab-errand-events" class="lab-events"></ol></section><section class="i-panel"><h2>Create a test payment request</h2><form id="lab-payment-form"><label for="lab-amount">Amount (USD)<input id="lab-amount" name="amount" inputmode="decimal" required placeholder="10.00"></label><label for="lab-merchant">Merchant name<input id="lab-merchant" name="merchant_name" required maxlength="200"></label><label for="lab-purpose">Purpose<input id="lab-purpose" name="purpose" required maxlength="1000"></label><details><summary>Optional merchant and product details</summary><label>Merchant domain<input name="merchant_domain" maxlength="253"></label><label>Sandbox merchant PayPal email<input name="merchant_paypal_email" type="email" maxlength="127"></label><label>Product query<input name="product_query" maxlength="500"></label><label>Business justification<textarea name="business_justification" maxlength="2000"></textarea></label></details><p>The local simulator is used when no PayPal sandbox key is configured. Never enter real payment details.</p><button class="i-primary" type="submit">Create test payment request</button></form><p id="lab-payment-status" role="alert"></p></section></div><section id="lab-result" class="i-panel lab-result" aria-live="polite">${resultHTML()}</section></div>`;
 }
 function resultHTML(){
  if(!current)return '<h2>Request walkthrough</h2><p>Create a request or run a shopping task to begin.</p>';
  const value=current,status=value.status,blocked=!!value.policy_block,executed=[...pending,'provider_confirmed','reconciliation_required','reversed'].includes(status),captured=['provider_confirmed','reversed'].includes(status);
  const rawLink=value.payer_action_url||refs.find(r=>r.payer_action_url)?.payer_action_url,link=sandboxURL(rawLink);
  const simulated=value.provider_mode==='simulated'||refs.some(r=>r.provider==='local_simulation')||value.simulated===true||value.provider==='local_simulation';
  const wait=busy.size?'disabled':'',unavailable='Not applicable: blocked by rule',left=remaining();
  const timelineHTML=window.FinanceScreens?.timelineHTML?FinanceScreens.timelineHTML(events):'';
  let shield='';
  if(blocked||scenario){
   const refused=scenario?.outcome==='refused',title=refused?(scenario.scenario==='changed_terms'?'Approval refused: the terms changed after the fingerprint was taken':'Approval refused: the payee changed'):'Blocked before any human was asked';
   const codes=scenario?.rule_codes||[value.policy_block.rule],reason=scenario?.plain_reason||value.policy_block.label;
   shield=`<div class="lab-shield-card" role="status"><span class="lab-shield-icon" aria-hidden="true">${App.icon('shield')}</span><div><h2>${safe(title)}</h2><p>${refused?'Exact-term approval refused':'Blocked by rule'} · <strong>${codes.map(safe).join(', ')}</strong></p><p>${safe(reason)}</p><p><strong>${safe(api.money(value.terms?.amount,value.terms?.currency))}</strong> · ${safe(value.terms?.merchant_name)}</p><p>${safe(value.request_id)}</p>${refused?'<p>The fingerprint belongs to the original request. This replacement still awaits an owner decision; the scenario submitted no matching approval.</p>':'<p>No owner approval or payment was made for this blocked request.</p>'}<button class="i-secondary" data-lab-action="timeline">View timeline</button></div></div>`;
   if(scenario)return shield+sandbox+`<p role="status">${safe(notice)}</p>`+timelineHTML;
  }
  return `${blocked?shield+sandbox:`<h2>Request walkthrough</h2><p><strong>${safe(value.request_id)}</strong> · ${blocked?'Blocked by rule':safe(String(status??'status unavailable').replace(/_/g,' '))}</p>${sandbox}${simulated?'<p class="f-live-warning">Local simulator (no PayPal). No verified provider capture.</p>':''}<p>${safe(api.money(value.terms?.amount,value.terms?.currency))} · ${safe(value.terms?.merchant_name)} · ${safe(value.terms?.purpose)}</p><p role="status">${safe(notice)}</p>${value.countered_by?`<div class="lab-countered" role="status"><p><strong>The owner countered.</strong> This request was declined and replaced by ${safe(value.countered_by)}. The Lab agent can only execute the replacement exactly as you approve it.</p><button class="i-primary" data-lab-action="follow-counter" ${wait}>Continue with the replacement</button></div>`:''}${value.counter_of?`<p class="lab-countered">Owner counter-offer replacing ${safe(value.counter_of)}. Approve its own fingerprint, then execute it unchanged.</p>`:''}`}
  <ol class="lab-steps" ${blocked?'hidden':''}><li>Create request — ${blocked?'Blocked by rule':'Recorded'}</li>
  <li>Approve exact terms — ${blocked?unavailable:['approved',...pending,'provider_confirmed','reconciliation_required','reversed'].includes(status)?'Approval recorded':'Awaiting owner review'}<div><button class="i-primary" data-lab-action="approve" ${status!=='awaiting_approval'||blocked||wait?'disabled':''}>Approve exact terms</button></div></li>
  <li>Execute as Lab agent — ${blocked?unavailable:executed?'Submitted':status==='approved'?'Ready':'Waiting for approval'}<div><button class="i-primary" data-lab-action="execute" ${status!=='approved'||blocked||wait?'disabled':''}>Execute as lab agent</button></div></li>
  <li>PayPal sandbox buyer approval — ${blocked?unavailable:simulated?'Unavailable in simulator':captured?'PayPal outcome verified':link?'Open checkout and approve as the sandbox buyer':'Waiting for provider link'}${link&&pending.includes(status)&&!blocked?`<div><a class="lab-provider-link" href="${safe(link)}" target="_blank" rel="noopener noreferrer" referrerpolicy="no-referrer">Open PayPal sandbox approval</a><p>Log in with the sandbox buyer, click Continue/Complete Purchase. PayPal sandbox may stay on the review page; come back here and click Capture. Capture only succeeds if PayPal confirms your approval.</p><button class="i-secondary" data-lab-action="check-approval" ${wait}>Check PayPal approval</button></div>`:''}${rawLink&&!link?'<p role="alert">The approval link was not a PayPal sandbox URL. It was blocked; refresh status or ask the operator for help.</p>':''}</li>
  <li>Capture — ${blocked?unavailable:captured?'Verified provider outcome':simulated?'Unavailable in simulator':'Waiting for verified PayPal buyer approval'}<div><button class="i-primary" data-lab-action="capture" ${!pending.includes(status)||simulated||blocked||wait?'disabled':''}>Capture after buyer approval</button></div></li>
  <li>Refund — ${blocked?unavailable:status==='reversed'?'Reversed':refunds.length?'Refund recorded; review status below':'Available after confirmed capture'}<div class="lab-refund-controls"><label for="lab-refund-amount">Refund amount (USD)<input id="lab-refund-amount" inputmode="decimal" autocomplete="off" value="${left===null?'':decimal(left)}" ${status!=='provider_confirmed'||left===null||blocked||wait?'disabled':''}></label><button class="i-secondary" data-lab-action="refund" ${status!=='provider_confirmed'||left===null||left===0n||blocked||wait?'disabled':''}>Refund</button></div></li></ol>
  ${refunds.map(r=>`<p>Refund ${safe(r.refund_id)} · ${safe(r.amount)} ${safe(r.currency||'USD')} · ${safe(r.status)}</p>`).join('')}${status==='reconciliation_required'?'<p role="alert">Provider outcome is uncertain. Open Transactions to reconcile; do not execute again.</p><button class="i-secondary" data-route="transactions">Open Transactions</button>':''}<div class="ws-actions"><button class="i-secondary" data-lab-action="refresh" ${wait}>Refresh request status</button></div>${timelineHTML}`;
 }
 function paint(){const root=document.getElementById('lab-result');if(root)root.innerHTML=resultHTML();}
 async function ensureAgent(){if(!agentReady)agentReady=api.post('lab/agent').catch(err=>{agentReady=null;throw err;});return agentReady;}
 async function load(id=current?.request_id){
  if(!id)return;const version=epoch,sequence=++revision;
  const value=await api.get('payment-requests/'+encodeURIComponent(id));
  const executed=[...pending,'provider_confirmed','reconciliation_required','reversed'].includes(value.status);
  const [timeline,reconciliation,refundResult]=await Promise.allSettled([api.get('payment-requests/'+encodeURIComponent(id)+'/timeline'),executed?api.get('payment-requests/'+encodeURIComponent(id)+'/reconciliation'):Promise.resolve(null),['provider_confirmed','reversed'].includes(value.status)?api.get('payment-requests/'+encodeURIComponent(id)+'/refunds'):Promise.resolve(null)]);
  if(version!==epoch||sequence!==revision||current?.request_id!==id)return;
  current={...value,payer_action_url:value.payer_action_url||current.payer_action_url};
  events=timeline.status==='fulfilled'?timeline.value:timeline.reason?.status===404?null:{unavailable:true};
  refundsKnown=refundResult.status==='fulfilled'&&Array.isArray(refundResult.value?.refunds);
  if(refundsKnown)refunds=refundResult.value.refunds;
  if(refundResult.status==='rejected')notice='Refund amounts could not be loaded. Refresh before refunding.';
  refs=reconciliation.status==='fulfilled'?reconciliation.value?.provider_references||[]:refs;paint();
 }
 async function submit(form,type){
  const button=form.querySelector('button[type=submit]'),output=document.getElementById(type==='errand'?'lab-errand-status':'lab-payment-status');
  if(busy.size)return;
  const fields=new FormData(form),amount=String(fields.get(type==='errand'?'budget':'amount')).trim();
  if(!positive(amount)){output.textContent='Enter a positive USD amount with up to two decimal places.';return;}
  busy.add('create');button.disabled=true;output.textContent=type==='errand'?'The AI agent is shopping. Spending rules and owner approval still apply.':'Creating the request through the Lab agent…';paint();const version=epoch;
  try{
   await ensureAgent();
   const body=type==='errand'?{task:String(fields.get('task')).trim(),budget:amount}:{amount,currency:'USD',merchant_name:String(fields.get('merchant_name')).trim(),purpose:String(fields.get('purpose')).trim()};
   if(type!=='errand')for(const name of ['merchant_domain','merchant_paypal_email','product_query','business_justification']){const value=String(fields.get(name)||'').trim();if(value)body[name]=value;}
   const result=await api.post(type==='errand'?'lab/errands':'lab/payment-requests',body);
   if(version!==epoch)return;
   scenario=null;
   if(type==='errand'){
    const taskRoot=document.getElementById('lab-errand-task');if(taskRoot)taskRoot.textContent='Original task: '+(result.task||body.task);
    output.textContent='Agent status: '+String(result.status||'unavailable').replace(/_/g,' ');
    const errandEvents=result.events||[];
    const candidateNote=errandEvents.some(e=>e.kind==='candidate')&&errandEvents.some(e=>e.kind==='selection')?'<li class="f-demo-note">Showing up to 3 candidates; the agent may choose any returned offer.</li>':'';
    const root=document.getElementById('lab-errand-events');if(root)root.innerHTML=candidateNote+errandEvents.map(e=>`<li><time datetime="${safe(e.at)}">${safe(clockTime(e.at))}</time> ${safe(e.message)}${e.kind==='candidate'?`<p>${[e.title,e.amount!=null?api.money(e.amount,e.currency):null,e.merchant,e.in_stock===true?'In stock':e.in_stock===false?'Out of stock':null].filter(v=>v!=null).map(safe).join(' · ')}</p>`:''}${e.kind==='selection'?`<p><strong>Chosen: ${[e.title,e.amount!=null?api.money(e.amount,e.currency):null,e.merchant].filter(v=>v!=null).map(safe).join(' · ')}</strong></p>${e.justification?`<p>Why this offer: ${safe(e.justification)}</p>`:''}`:''}</li>`).join('');
    if(result.request_id){current={request_id:result.request_id};events=null;refs=[];refunds=[];notice='';await load();}
   }else{revision++;current=result;events=null;refs=[];refunds=[];notice='';output.textContent=result.policy_block?'A spending rule blocked this request. Review the walkthrough.':'Request created. Review and approve its exact terms below.';await load();}
   await api.refresh();
  }catch(err){if(version===epoch&&output.isConnected)output.textContent=errorMessage(err);}
  finally{busy.delete('create');button.disabled=false;paint();}
 }
 async function action(button){
  if(!current||busy.size||button.disabled)return;const kind=button.dataset.labAction,id=current.request_id,path='payment-requests/'+encodeURIComponent(id);
  if(kind==='timeline'){await FinanceScreens.openRequest(id,'payment');return;}
  if(kind==='approve'){await FinanceScreens.openRequest(id,'payment',true);return;}
  if(kind==='follow-counter'){
   // Hand the walkthrough over to the owner's replacement; the original stays declined.
   const next=current.countered_by;if(!next)return;
   busy.add(kind);paint();const version=epoch;
   try{revision++;current={request_id:next};events=null;refs=[];refunds=[];refundsKnown=false;notice='Now following the owner\'s counter-offer. Approve its exact terms, then execute.';await load(next);}
   catch(err){if(version===epoch)notice=errorMessage(err);}
   finally{busy.delete(kind);paint();}
   return;
  }
  let amount;
  if(kind==='refund'){
   amount=document.getElementById('lab-refund-amount')?.value||'';
   const units=cents(amount),left=remaining();
   if(units===null||units<=0n||left===null||units>left){notice='Enter a positive decimal amount with up to two decimal places, no more than the remaining refundable amount.';paint();return;}
  }
  busy.add(kind);notice='';paint();const version=epoch;
  try{
   if(kind==='execute'){const value=await api.post('lab/'+path+'/execute');if(version!==epoch)return;current=value;}
   if(kind==='capture')await api.post(path+'/capture');
   if(kind==='check-approval')await api.post(path+'/reconciliation',{});
   if(kind==='refund'){const value=await api.post(path+'/refunds',{amount}, {idempotency:api.newKey('lab-refund')});if(version!==epoch)return;refunds.push(value);}
   await load(id);await api.refresh();
   if(version===epoch)notice=kind==='check-approval'?(current.provider_error_code==='PAYER_NOT_APPROVED'?'PayPal has not confirmed buyer approval yet. Complete the sandbox buyer step, then check again.':'PayPal approval checked by the server. Click Capture to verify buyer approval and exact terms.'):kind==='capture'?'Capture checked. Only a verified provider outcome counts as paid.':kind==='refund'?'Refund request recorded. Check its status and timeline.':'';
  }catch(err){
   if(version!==epoch)return;
   notice=kind==='capture'&&err.status===409?'PayPal has not confirmed buyer approval yet. Complete the sandbox checkout, then refresh and try Capture again.':errorMessage(err);
   // A write response may be lost: read current status before offering another action.
   try{await load(id);}catch(_){notice+=' Current status could not be loaded. Refresh before taking another action.';}
  }finally{busy.delete(kind);paint();}
 }
 async function runScenario(button){
  if(busy.size||button.disabled)return;const version=epoch;busy.add('scenario');notice='';paint();
  const output=document.getElementById('lab-scenario-status');output.textContent='Running the example through the real payment controls…';
  document.querySelectorAll('[data-lab-scenario]').forEach(b=>{b.disabled=true;});
  try{
   const result=await api.post('lab/scenarios/'+encodeURIComponent(button.dataset.labScenario));
   if(version!==epoch)return;
   scenario=result;revision++;current={request_id:result.request_ids.at(-1)};events=null;refs=[];refunds=[];refundsKnown=false;
   await load();await api.refresh();output.textContent='Scenario finished: '+result.outcome+'. No payment was approved or executed.';
  }catch(err){if(version===epoch&&output.isConnected)output.textContent=errorMessage(err);}
  finally{busy.delete('scenario');document.querySelectorAll('[data-lab-scenario]').forEach(b=>{b.disabled=false;});paint();}
 }
 function mount(){const root=document.querySelector('.lab-screen');if(!root||!api?.live)return;root.querySelector('#lab-errand-form').onsubmit=e=>{e.preventDefault();submit(e.currentTarget,'errand');};root.querySelector('#lab-payment-form').onsubmit=e=>{e.preventDefault();submit(e.currentTarget,'payment');};root.onclick=e=>{const attempt=e.target.closest('[data-lab-scenario]');if(attempt){runScenario(attempt);return;}const button=e.target.closest('[data-lab-action]');if(button)action(button);};const version=epoch;api.get('integrations').then(value=>{if(version!==epoch)return;api.data.integrations=value.integrations||[];const status=document.getElementById('lab-services-status');if(status)status.textContent=servicesHTML();}).catch(()=>{});if(current)load().catch(err=>{notice=errorMessage(err);paint();});}
 window.addEventListener('agentpal-live',e=>{if(e.detail?.type==='refreshed'&&current&&document.querySelector('.lab-screen')&&!busy.size)load().catch(err=>{notice=errorMessage(err);paint();});if(e.detail?.type==='signed-out'){epoch++;revision++;current=null;scenario=null;events=null;refs=[];refunds=[];refundsKnown=false;notice='';agentReady=null;busy.clear();}});
 window.LabScreen={render,mount,sandboxURL};
})();
