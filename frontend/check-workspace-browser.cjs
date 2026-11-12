/* Shared-contract browser checks. Every API/provider response is local and synthetic. */
const fs=require('node:fs'),path=require('node:path'),http=require('node:http'),assert=require('node:assert/strict'),crypto=require('node:crypto');
const {chromium}=require('playwright');
const shots=process.env.AGENTPAL_TEST_SHOTS;if(shots)fs.mkdirSync(shots,{recursive:true});
const site=process.env.AGENTPAL_SCREEN_DIR||path.join(__dirname,'site'),nonce='shb-browser-nonce',calls=[],secrets=[],today=new Date().toISOString();
const token='apo_'+crypto.randomBytes(24).toString('hex'),setupCode=crypto.randomUUID();secrets.push(setupCode);
const providerIds=['paypal_sandbox','channel3','kernel','groq','openrouter'];
let setup={mode:'self_hosted',needs_setup:true,signup:'setup_code'},integrationFailure=false,labMode='happy',server,browser,origin;
let items=providerIds.map(provider=>({provider,label:provider,purpose:'Fixture',required:false,fields:[],status:'not_configured',source:null,hint:null,verified_at:null,error_code:null}));
const payments=new Map(),timeline=new Map(),hash='a'.repeat(64);
const json=(res,body,status=200)=>{res.writeHead(status,{'Content-Type':'application/json'});res.end(JSON.stringify(body));};
async function serve(req,res){
 const url=new URL(req.url,origin||'http://localhost');
 if(url.pathname.startsWith('/v1/')){
  let text='';for await(const chunk of req)text+=chunk;const body=text?JSON.parse(text):null,p=url.pathname.slice(4);calls.push({method:req.method,path:p,body,auth:req.headers.authorization});
  if(p==='setup/status')return json(res,setup);
  if(p==='setup/owner'){assert.equal(req.headers.authorization,undefined);if(body.code!==setupCode)return json(res,{error:'invalid_setup_code'},403);setup={...setup,needs_setup:false};return json(res,{owner_id:body.owner_id,token},201);}
  assert.equal(req.headers.authorization,'Bearer '+token,'Owner bearer required for '+p);
  if(p==='integrations')return json(res,{integrations:items});
  if(p.startsWith('integrations/')){
   const id=p.split('/')[1],old=items.find(i=>i.provider===id);
   if(req.method==='DELETE'){items=items.map(i=>i.provider===id?{...i,status:'not_configured',source:null,hint:null,verified_at:null}:i);res.writeHead(204);return res.end();}
   if(integrationFailure){integrationFailure=false;return json(res,{error:'verification_failed',error_code:'invalid_credentials'},422);}
   if(req.method==='PUT'){assert.ok(Object.values(body.fields).every(v=>secrets.includes(v)));await new Promise(r=>setTimeout(r,100));}
   const next={...old,status:'verified',source:req.method==='PUT'?'workspace':old.source,hint:'…test',verified_at:today};items=items.filter(i=>i.provider!==id).concat(next);return json(res,next);
  }
  if(p==='lab/agent')return json(res,{agent_id:'lab-agent',wallet_id:'lab-wallet'});
  if(p==='lab/errands'){
   if(labMode==='missing')return json(res,{error:'integration_required',provider:'groq'},409);
   if(labMode==='rate')return json(res,{error:'rate_limited'},429);
   return json(res,{task:body.task,status:'awaiting_approval',request_id:null,events:[{at:today,message:'Found a sandbox item <img src=x onerror=alert(1)>'},{at:today,kind:'plan',message:'Plan: search for office supplies within USD 30.00.'},{at:today,kind:'candidate',message:'Candidate offer from Channel3.',title:'Pencil <img src=x onerror=alert(1)>',amount:'2.00',currency:'USD',merchant:'supplies.example',in_stock:true},{at:today,kind:'selection',message:'Selected one catalogue offer for owner review.',justification:'Lowest price for the requested supplies.'},{at:today,kind:'boundary',message:'AI proposes · Rules decide · You approve'}]});
  }
  if(p.startsWith('lab/scenarios/')){
   const scenario=p.split('/').at(-1),refused=['changed_terms','payee_swap'].includes(scenario),id='scenario-'+scenario;
   const value={request_id:id,terms:{amount:scenario==='over_limit'?'30.00':'15.00',currency:'USD',merchant_name:'Scenario <img src=x onerror=alert(1)>'},status:refused?'awaiting_approval':'rejected',policy_block:refused?null:{rule:'AMOUNT_ABOVE_HARD_LIMIT',label:'Amount above limit'}};
   payments.set(id,value);timeline.set(id,[{at:today,actor:'policy',label:'Scenario control recorded'}]);
   return json(res,{scenario,outcome:refused?'refused':'blocked',rule_codes:[refused?'terms_mismatch':'AMOUNT_ABOVE_HARD_LIMIT'],plain_reason:scenario==='changed_terms'?'Approval refused: the terms changed after the fingerprint was taken':refused?'Approval refused: the payee changed':'Amount exceeds the per-payment limit.',request_ids:[id],timeline_url_hint:'/v1/payment-requests/'+id+'/timeline'});
  }
  if(p==='lab/payment-requests'){
   const id='lab-'+(payments.size+1),blocked=labMode==='blocked';
   const value={request_id:id,agent_id:'lab-agent',wallet_id:'lab-wallet',terms:{...body},status:blocked?'rejected':'awaiting_approval',created_at:today,terms_sha256:hash,decision_requested:true,...(blocked?{policy_block:{rule:'PER_TRANSACTION_LIMIT',label:'Amount exceeds the sandbox agent limit'}}:{})};
   payments.set(id,value);timeline.set(id,[{at:today,actor:'agent',label:'Request created',detail:blocked?'Blocked by rule':'Awaiting exact-term owner approval'}]);return json(res,value,201);
  }
  const lists={savers:{savers:[]},agents:{agents:[]},wallets:{wallets:[]},'payment-requests':{payment_requests:[...payments.values()]},'payout-requests':{payout_requests:[]},approvals:{approvals:[...payments.values()].filter(p=>p.status==='awaiting_approval')},health:{status:'ok',payment_environment:'sandbox',is_real_money:false},disputes:{disputes:[]}};
  if(lists[p])return json(res,lists[p]);
  const id=p.split('/')[p.startsWith('lab/')?2:1],value=payments.get(id);
  if(value){
   if(p.endsWith('/timeline'))return json(res,{request_id:id,events:timeline.get(id)});
   if(req.method==='GET'&&p.endsWith('/refunds'))return json(res,{refunds:[]});
   if(req.method==='GET')return json(res,value);
   let label;
   if(p.endsWith('/approve')){assert.equal(body.terms_sha256,hash);value.status='approved';label='Owner approved exact terms';}
   if(p.endsWith('/execute')){assert.equal(value.status,'approved');value.status='provider_pending';value.payer_action_url=labMode==='unsafe'?'https://www.paypal.com/checkoutnow?token=test':'https://www.sandbox.paypal.com/checkoutnow?token=test';label='Executed as Lab agent';}
   if(p.endsWith('/capture')){assert.equal(value.status,'provider_pending');value.status='provider_confirmed';label='PayPal sandbox capture verified';}
   if(p.endsWith('/refunds')){assert.equal(value.status,'provider_confirmed');value.status='reversed';label='PayPal sandbox refund verified';timeline.get(id).push({at:today,actor:'paypal',label});return json(res,{refund_id:'refund-test',status:'COMPLETED'});}
   if(label)timeline.get(id).push({at:today,actor:p.endsWith('/approve')?'owner':'paypal',label});return json(res,value);
  }
  return json(res,{error:'not_found'},404);
 }
 const name=url.pathname.replace(/^\/app\//,'')||'index.html',file=path.join(site,name);if(name.includes('..')||!fs.existsSync(file)){res.writeHead(404);return res.end();}
 const ext=path.extname(file),mime={'.js':'text/javascript','.css':'text/css','.html':'text/html','.svg':'image/svg+xml'}[ext]||'text/plain';
 res.writeHead(200,{'Content-Type':mime,'Content-Security-Policy':`default-src 'none'; script-src 'self'; style-src 'self' 'nonce-${nonce}'; style-src-attr 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`});res.end(ext==='.html'?fs.readFileSync(file,'utf8').replace('__CSP_NONCE__',nonce):fs.readFileSync(file));
}
const secret=()=>{const value=crypto.randomUUID();secrets.push(value);return value;};
(async()=>{
 server=http.createServer((req,res)=>serve(req,res).catch(e=>{console.error(e.message);json(res,{error:'fixture_failure'},500);}));await new Promise(r=>server.listen(0,'127.0.0.1',r));origin='http://127.0.0.1:'+server.address().port;
 browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE||undefined});
 const context=await browser.newContext({viewport:{width:393,height:844}});const page=await context.newPage(),errors=[],external=[],logs=[],consoleProblems=[];
 page.on('pageerror',e=>errors.push(e.message));page.on('console',m=>{logs.push(m.text());if(['error','warning'].includes(m.type()))consoleProblems.push(m.text());});page.on('request',r=>{if(!r.url().startsWith(origin))external.push(r.url());});
 await page.addInitScript(()=>{window.__violations=[];document.addEventListener('securitypolicyviolation',e=>__violations.push(e.violatedDirective));window.__copied='';Object.defineProperty(navigator,'clipboard',{value:{writeText:async v=>{window.__copied=v;}}});});
 const bounds=async()=>assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),393,'No horizontal overflow');
 const noSecrets=async()=>{const state=await page.evaluate(()=>({html:document.documentElement.outerHTML,values:[...document.querySelectorAll('input,textarea')].map(i=>i.value),local:JSON.stringify({...localStorage}),session:JSON.stringify({...sessionStorage})}));for(const value of secrets){assert.ok(!JSON.stringify(state).includes(value),'Credential must be absent from DOM and storage');assert.ok(!logs.join('').includes(value),'Credential must be absent from logs');assert.ok(!calls.some(c=>c.path.includes(value)),'Credential must be absent from URLs');}assert.ok(!state.local.includes(token));};
 await page.goto(origin+'/app/');await page.getByRole('heading',{name:'Create your workspace',exact:true}).waitFor();await bounds();if(shots)await page.screenshot({path:path.join(shots,'setup-393.png'),fullPage:true});
 await page.locator('#setup-owner').fill('judge');await page.locator('#setup-name').fill('Judge');await page.locator('#setup-code').fill('incorrect');await page.locator('#setup-form button').click();await page.getByText('That setup or invite code was not accepted.',{exact:false}).waitFor();assert.equal(await page.locator('#setup-code').inputValue(),'');
 await page.locator('#setup-code').fill(setupCode);await page.locator('#setup-form button').click();await page.locator('#owner-token-once').waitFor();assert.equal(await page.locator('#owner-token-once').inputValue(),token);assert.equal(await page.evaluate(()=>JSON.stringify({...localStorage})), '{}');assert.equal(await page.evaluate(()=>sessionStorage.getItem('agentpal-owner-session')),null);
 await page.locator('#copy-owner-token').click();assert.equal(await page.evaluate(()=>__copied),token);await page.locator('#owner-token-saved').click();await page.waitForFunction(()=>window.App?.ready);await page.locator('#modal [data-workspace-services]').waitFor();assert.equal(await page.locator('#owner-token-once').count(),0);items=items.map(i=>i.provider==='paypal_sandbox'?{...i,status:'verified',source:'server',hint:'…test',verified_at:today}:i);await page.locator('#modal [data-service-action=refresh]').click();await bounds();
 await page.locator('#modal .ws-progress').getByText('1 of 5 ready').waitFor();
 const groq=secret();await page.locator('#ws-groq-api_key').fill(groq);await page.evaluate(()=>{window.__secretInput=document.getElementById('ws-groq-api_key');});await page.locator('[data-provider-form=groq] button[type=submit]').click();await page.getByText('Verified & saved. Credentials cleared.').waitFor();assert.equal(await page.evaluate(()=>__secretInput.value),'');assert.equal(calls.filter(c=>c.path==='integrations/groq'&&c.method==='PUT').length,1);await page.locator('#modal .ws-progress').getByText('2 of 5 ready').waitFor();assert.equal(await page.evaluate(()=>AgentPalAPI.data.integrations.find(i=>i.provider==='groq')?.source),'workspace','Service writes refresh guidance/limit metadata immediately');await noSecrets();
 integrationFailure=true;await page.locator('#ws-kernel-api_key').fill(secret());await page.locator('[data-provider-form=kernel] button[type=submit]').click();await page.getByText(/error code: invalid_credentials/).waitFor();assert.equal(await page.locator('#ws-kernel-api_key').inputValue(),'');await noSecrets();
 await page.locator('#ws-channel3-api_key').fill(secret());await page.locator('[data-service-action=skip][data-id=channel3]').click();assert.equal(await page.locator('#ws-channel3-api_key').count(),0);await page.getByRole('button',{name:'Skip setup',exact:true}).click();
 await page.reload();await page.waitForFunction(()=>window.App?.ready);assert.equal(await page.locator('#owner-token-once').count(),0);assert.equal(await page.locator('dialog[open]').count(),0);
 await page.evaluate(()=>App.navigate('integrations'));await page.locator('.ws-progress').getByText('2 of 5 ready').waitFor();await page.locator('[data-service-action=wizard]').click();assert.equal(await page.locator('#modal #ws-channel3-api_key').count(),0,'Skip remembered in same tab');await page.getByRole('button',{name:'Skip setup',exact:true}).click();
 await page.locator('[data-service-action=verify][data-id=groq]').click();await page.getByText('Verified again.',{exact:true}).waitFor();await page.locator('[data-service-action=replace][data-id=groq]').click();await page.locator('#ws-groq-api_key').fill(secret());await page.locator('[data-provider-form=groq] button[type=submit]').click();await page.getByText('Verified & saved. Credentials cleared.').waitFor();await page.locator('[data-service-action=remove][data-id=groq]').click();await page.locator('.ws-progress').getByText('1 of 5 ready').waitFor();await noSecrets();await bounds();if(shots)await page.screenshot({path:path.join(shots,'integrations-393.png'),fullPage:true});
 // Other signup modes and per-tab skip isolation.
 setup={mode:'hosted',needs_setup:false,signup:'invite_code'};
 const other=await page.context().newPage();await other.goto(origin+'/app/');await other.getByRole('button',{name:'Create a workspace',exact:true}).click();await other.getByText('Ask your operator for the workspace invite code.').waitFor();await other.locator('#setup-back').click();await other.locator('#signin-token').fill(token);await other.locator('.auth-submit').click();await other.waitForFunction(()=>window.App?.ready);await other.waitForTimeout(800);assert.equal(await other.locator('#modal [data-workspace-services]').count(),0,'The wizard is not re-offered once a service is connected');await other.close();
 setup={mode:'hosted',needs_setup:false,signup:'disabled'};const disabled=await page.context().newPage();await disabled.goto(origin+'/app/');await disabled.locator('#signin-token').waitFor();assert.equal(await disabled.getByRole('button',{name:'Create a workspace',exact:true}).count(),0);await disabled.close();
 // Test Lab uses the exact owner approval dialog and normal owner endpoints.
 {
  await page.evaluate(()=>App.navigate('lab'));await page.getByRole('heading',{name:'Test Lab',exact:true}).waitFor();await bounds();
  labMode='missing';await page.locator('#lab-task').fill('Find sandbox office supplies');await page.locator('#lab-budget').fill('30.00');await page.locator('#lab-errand-form button').click();await page.locator('#lab-errand-status').getByText(/Connect Groq/).waitFor();
  labMode='rate';await page.locator('#lab-errand-form button').click();await page.locator('#lab-errand-status').getByText(/Too many requests/).waitFor();
  labMode='happy';await page.locator('#lab-errand-form button').click();await page.locator('#lab-errand-events').getByText(/Found a sandbox item/).waitFor();assert.equal(await page.locator('#lab-errand-events img').count(),0);await page.locator('#lab-errand-task').getByText('Original task: Find sandbox office supplies',{exact:true}).waitFor();await page.locator('#lab-errand-events').getByText(/Plan: search for office supplies/).waitFor();assert.match(await page.locator('#lab-errand-events').innerText(),/Pencil.*2.00.*supplies.example.*In stock/);await page.locator('#lab-errand-events').getByText('Why this offer: Lowest price for the requested supplies.',{exact:true}).waitFor();await page.locator('#lab-errand-events').getByText('AI proposes · Rules decide · You approve',{exact:false}).waitFor();
  const create=async()=>{await page.locator('#lab-amount').fill('10.00');await page.locator('#lab-merchant').fill('Sandbox supplies');await page.locator('#lab-purpose').fill('Judge demo');await page.locator('#lab-payment-form button').click();await page.locator('#lab-result .lab-steps:not([hidden]), #lab-result .lab-shield-card').waitFor();};
  await create();await page.locator('[data-lab-action=approve]').click();await page.locator('#f-live-confirm').waitFor();assert.match(await page.locator('dialog').innerText(),/Any change to amount, payee or purpose voids this approval/);await page.locator('#f-live-confirm').click();await page.locator('[data-lab-action=execute]:enabled').waitFor();
  await page.locator('[data-lab-action=execute]').click();const link=page.getByRole('link',{name:'Open PayPal sandbox approval',exact:true});await link.waitFor();assert.equal(await link.getAttribute('rel'),'noopener noreferrer');assert.equal(new URL(await link.getAttribute('href')).hostname,'www.sandbox.paypal.com');
  await page.locator('[data-lab-action=capture]').click();await page.locator('[data-lab-action=refund]:enabled').waitFor();await page.locator('[data-lab-action=refund]').click();await page.locator('#lab-result').getByText('reversed',{exact:false}).first().waitFor();assert.match(await page.locator('#lab-result').innerText(),/PayPal sandbox refund verified/);await bounds();if(shots)await page.screenshot({path:path.join(shots,'lab-happy-393.png'),fullPage:true});
  labMode='blocked';await create();await page.locator('#lab-result').getByText(/Blocked by rule/).first().waitFor();assert.equal(await page.locator('[data-lab-action=execute]:enabled').count(),0);assert.equal(await page.locator('[data-lab-action=approve]:enabled').count(),0);if(shots)await page.screenshot({path:path.join(shots,'lab-blocked-393.png'),fullPage:true});const blockedId=[...payments.keys()].at(-1);assert.ok(!calls.some(c=>c.path===`lab/payment-requests/${blockedId}/execute`));
  assert.equal(await page.locator('#lab-result .lab-steps:visible').count(),0,'Blocked requests hide all walkthrough steps');
  await page.getByRole('heading',{name:'Blocked before any human was asked',exact:true}).waitFor();
  for(const name of ['over_limit','split_purchase','changed_terms','payee_swap']){
   await page.locator('[data-lab-scenario='+name+']').click();
   await page.locator('#lab-scenario-status').getByText(/Scenario finished/).waitFor();
   assert.equal(await page.locator('#lab-result .lab-steps:visible').count(),0);
   assert.equal(await page.locator('#lab-result [data-lab-action=approve]').count(),0);
   assert.equal(await page.locator('#lab-result img').count(),0,'Merchant data is escaped');
   assert.match(await page.locator('#lab-result').innerText(),/terms_mismatch|AMOUNT_ABOVE_HARD_LIMIT/);
   if(name==='changed_terms')await page.getByRole('heading',{name:'Approval refused: the terms changed after the fingerprint was taken',exact:true}).waitFor();
   await page.getByRole('button',{name:'View timeline',exact:true}).click();
   await page.locator('dialog').getByText('Scenario control recorded').waitFor();
   await page.locator('#close-modal').click();await bounds();
  }
  assert.ok(!calls.some(c=>c.method==='POST'&&/scenario-.*\/(approve|execute|capture)$/.test(c.path)));
  labMode='unsafe';await create();await page.locator('[data-lab-action=approve]').click();await page.locator('#f-live-confirm').click();await page.locator('[data-lab-action=execute]:enabled').waitFor();await page.locator('[data-lab-action=execute]').click();await page.locator('#lab-result').getByText(/approval link was not a PayPal sandbox URL/).waitFor();assert.equal(await page.getByRole('link',{name:'Open PayPal sandbox approval',exact:true}).count(),0);
  await page.evaluate(()=>App.navigate('overview'));await page.evaluate(()=>App.navigate('lab'));await page.locator('#lab-result .lab-steps').waitFor();
 }
 await page.setViewportSize({width:1600,height:1000});if(shots)await page.screenshot({path:path.join(shots,'lab-1600.png'),fullPage:true});await noSecrets();assert.equal(await page.locator('script:not([src])').count(),0);assert.deepEqual(await page.evaluate(()=>__violations),[]);assert.deepEqual(errors,[]);assert.deepEqual(external,[]);assert.deepEqual(consoleProblems.filter(m=>!/Failed to load resource: the server responded with a status of (403|409|422|429) /.test(m)),[],'Only deliberate fixture HTTP errors may appear in console');
 console.log('PASS: setup modes, one-time owner token/copy/tab storage, wizard verify/skip/tab isolation, secret clearing, integration status/re-verify/replace/remove; Test Lab errands/errors, exact-term approval/execute/capture/refund, policy block and sandbox URL boundary; 393px and strict CSP.');
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(async()=>{await browser?.close();await new Promise(r=>server?server.close(r):r());});
