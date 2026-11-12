/* TOUR: synthetic owner API, production CSP, real dashboard and Chromium. No writes. */
const fs=require('node:fs'),path=require('node:path'),http=require('node:http');
const assert=require('node:assert/strict'),{chromium}=require('playwright');
const today=new Date().toISOString(),old='2020-01-01T00:00:00Z',future='2099-01-01T00:00:00Z';
const hostile='<img src=x onerror=alert(1)>',digest='a'.repeat(64),calls=[];
const payment=(id,status='awaiting_approval',extra={})=>({
  request_id:id,agent_id:'agent',wallet_id:'wallet',status,created_at:today,
  provider_mode:'paypal_sandbox',terms_sha256:digest,decision_requested:true,
  policy_block:null,owner_decision:null,verification:null,counter_of:null,
  counter_saving:null,counter_saving_verified:false,
  terms:{amount:'24.99',currency:'USD',merchant_name:hostile,purpose:'USB hub'},...extra,
});
const block=(id,amount,currency='USD',extra={})=>payment(id,'rejected',{
  policy_block:{rule:'AMOUNT_ABOVE_HARD_LIMIT',label:hostile},
  terms:{amount,currency,merchant_name:hostile},...extra,
});
const counter=(id,saving,verified,currency='USD',extra={})=>payment(id,verified?'provider_confirmed':'approved',{
  counter_of:'original-'+hostile,counter_saving:saving,counter_saving_verified:verified,
  terms:{amount:'10.00',currency,merchant_name:hostile},...extra,
});
const fixture=()=>[
  payment('older-pending','awaiting_approval',{created_at:new Date(Date.now()-60000).toISOString()}),
  payment('newest-pending'),
  block('blocked-usd','30.10'),block('blocked-cents','0.20'),block('blocked-eur','12.00','EUR'),
  block('old-block','100.00','USD',{created_at:old}),block('future-block','900.00','USD',{created_at:future}),
  payment('declined','rejected'),
  payment('exact-approval','approved',{owner_decision:{decision:'approved',at:today}}),
  counter('verified-counter','8.50',true),counter('unverified-counter','1.20',false),
  counter('higher-counter','-0.10',false),counter('euro-counter','2.25',true,'EUR'),
  counter('old-counter','100.00',true,'USD',{created_at:old}),
  counter('future-counter','999.00',true,'USD',{created_at:future}),
  payment('not-a-counter','provider_confirmed',{counter_saving:'999.00',counter_saving_verified:true}),
];
let payments=fixture(),agents=[{agent_id:'agent',name:hostile,role:'external',status:'active',metadata:{},capabilities:[],keys:[],created_at:today},
  {agent_id:'saver',name:'Saver',role:'finance',status:'active',metadata:{kind:'saver'},capabilities:[],keys:[],created_at:today}];
const wallets=[{wallet_id:'wallet',agent_id:'agent',currency:'USD',status:'active',
  limits:{monthly_budget:'200.00',per_transaction_limit:'25.00'},
  allowance:{available:'100.00',reserved:'50.00',pending:'0.00',spent:'50.00'}}];
let server,browser,origin;
const json=(res,value,status=200)=>{res.writeHead(status,{'Content-Type':'application/json'});res.end(JSON.stringify(value));};
function serve(req,res){
  const url=new URL(req.url,origin||'http://localhost');
  if(url.pathname.startsWith('/v1/')){
    const p=url.pathname.slice(4);calls.push({method:req.method,path:p});
    assert.equal(req.method,'GET','Tour navigation must never write or approve');
    const lists={
      'setup/status':{mode:'hosted',needs_setup:false,signup:'disabled'},
      health:{status:'ok',payment_environment:'sandbox',is_real_money:false},
      agents:{agents},wallets:{wallets},'payment-requests':{payment_requests:payments},
      'payout-requests':{payout_requests:[]},approvals:{payment_requests:payments.filter(r=>r.status==='awaiting_approval')},
      integrations:{integrations:[]},disputes:{disputes:[]},savers:{savers:[]},
    };
    if(p in lists)return json(res,lists[p]);
    const match=/^payment-requests\/([^/]+)(?:\/(timeline|refunds))?$/.exec(p);
    if(match){
      const record=payments.find(r=>r.request_id===decodeURIComponent(match[1]));
      if(!record)return json(res,{error:'not_found'},404);
      return json(res,match[2]==='timeline'?{request_id:record.request_id,events:[]}:match[2]==='refunds'?{refunds:[]}:record);
    }
    if(p.endsWith('/transactions'))return json(res,{transactions:[]});
    return json(res,{error:'not_found'},404);
  }
  const name=url.pathname.replace(/^\/app\//,'')||'index.html';
  if(name.includes('..'))return json(res,{},404);
  const file=path.join(__dirname,'site',name);
  if(!fs.existsSync(file))return json(res,{},404);
  const ext=path.extname(file),mime={'.js':'text/javascript','.css':'text/css','.html':'text/html','.svg':'image/svg+xml'}[ext];
  res.writeHead(200,{'Content-Type':mime||'text/plain','Content-Security-Policy':
    "default-src 'none'; script-src 'self'; style-src 'self' 'nonce-tour-test'; style-src-attr 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self'; manifest-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"});
  res.end(ext==='.html'?fs.readFileSync(file,'utf8').replace('__CSP_NONCE__','tour-test'):fs.readFileSync(file));
}
(async()=>{
  server=http.createServer(serve);await new Promise(r=>server.listen(0,'127.0.0.1',r));
  origin='http://127.0.0.1:'+server.address().port;
  browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE||undefined});
  const shots=process.env.AGENTPAL_TOUR_SHOTS||'/tmp/agentpal-shots/tour-shots';fs.mkdirSync(shots,{recursive:true});
  for(const width of [1440,393]){
    payments=fixture();
    const page=await browser.newPage({viewport:{width,height:width===1440?1000:852},timezoneId:'America/Los_Angeles',reducedMotion:'reduce'});
    const errors=[],external=[];
    page.on('pageerror',e=>errors.push(e.message));
    page.on('request',r=>{if(!r.url().startsWith(origin))external.push(r.url());});
    page.on('dialog',d=>{errors.push(d.message());d.dismiss();});
    await page.addInitScript(()=>{window.__csp=[];document.addEventListener('securitypolicyviolation',e=>__csp.push(e.violatedDirective));});
    await page.goto(origin+'/app/');await page.locator('#signin-token').fill('apo_'+'t'.repeat(32));
    await page.locator('.auth-submit').click();await page.waitForFunction(()=>window.App?.ready);
    await page.getByRole('button',{name:'Skip setup',exact:true}).click();
    const overview=()=>page.evaluate(()=>App.navigate('overview'));
    const steps=page.locator('[data-tour-step]');
    const marks=()=>steps.evaluateAll(list=>list.map(a=>a.dataset.evidence==='true'));
    const readTile=key=>page.locator(`[data-tour-impact="${key}"]`).innerText();
    await overview();
    assert.equal(await steps.count(),4);
    assert.deepEqual(await steps.evaluateAll(list=>list.map(a=>a.getAttribute('href'))),['#lab?tour=attack','#approvals','#approvals','#agents']);
    assert.deepEqual(await steps.locator('.tour-step-number').allTextContents(),['1','2','3','4']);
    assert.deepEqual(await marks(),[true,true,true,true]);
    assert.equal(await steps.locator('.tour-evidence-check').count(),4);
    assert.equal(await page.locator('.overview-audience>p').innerText(),
      'For teams and people who let AI agents buy things: an allowance, hard rules, and your approval of the exact terms.');
    assert.deepEqual(await page.locator('.tour-payment-flow li').allTextContents(),[
      'Agent asks','Rules check','You approve exact terms','PayPal buyer approves (sandbox)','Capture','Verified by PayPal']);
    assert.match(await page.locator('.tour-payment-note').innerText(),/In this sandbox demo the payee is AgentPal's PayPal sandbox account; no retailer order is placed\./);
    assert.match(await readTile('blocked'),/\$30\.30 USD.*€12\.00 EUR/s);
    assert.match(await readTile('counter'),/\$9\.60 USD.*€2\.25 EUR/s);
    assert.match(await readTile('counter'),/PayPal-verified: \$8\.50 USD · €2\.25 EUR/);
    assert.match(await readTile('counter'),/Not yet verified: \$1\.10 USD/);
    assert.equal(await page.locator('.overview-tour [style],.protection-impact [style]').count(),0);
    assert.equal(await page.locator('#screen img').count(),0,'API markup is never an element');
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),width);
    assert.equal(await page.locator('.overview-tour').evaluate(el=>el.scrollWidth<=el.clientWidth),true);
    await page.screenshot({path:path.join(shots,`tour-evidence-${width}.png`),fullPage:true});
    // The first step scrolls and focuses the existing attack section; links survive reload/back.
    await steps.nth(0).click();assert.equal(await page.evaluate(()=>location.hash),'#lab?tour=attack');
    assert.equal(await page.locator('.lab-attacks').evaluate(el=>document.activeElement===el),true);
    assert.ok(await page.locator('.lab-attacks').evaluate(el=>el.getBoundingClientRect().top>=0&&el.getBoundingClientRect().top<innerHeight));
    await page.reload();await page.waitForFunction(()=>window.App?.ready);
    assert.equal(await page.locator('.lab-attacks').evaluate(el=>document.activeElement===el),true);
    await overview();
    const before=calls.length;await steps.nth(1).click();await page.locator('dialog[open] #f-live-confirm').waitFor();
    assert.equal(await page.evaluate(()=>location.hash),'#approvals');
    assert.ok(calls.slice(before).some(c=>c.path==='payment-requests/newest-pending'));
    assert.equal(await page.locator('dialog img').count(),0);assert.ok((await page.locator('dialog').innerText()).includes(hostile));
    await overview();await steps.nth(2).click();await page.locator('dialog[open] #f-live-counter').waitFor();
    assert.match(await page.locator('dialog').innerText(),/counter.*replacement.*own.*approval/is);
    await overview();await steps.nth(3).click();assert.equal(await page.locator('#screen h1').innerText(),'AI Agents');
    // A visit or a click never creates evidence. A name alone is not a Saver identity.
    payments=[payment('only-pending')];await page.evaluate(()=>{AgentPalAPI.data.agents=AgentPalAPI.data.agents.map(a=>({...a,metadata:{}}));});
    await page.evaluate(rows=>{AgentPalAPI.data.payments=rows;AgentPalAPI.data.approvals=rows;App.navigate('overview');},payments);
    assert.deepEqual(await marks(),[false,false,false,false]);
    await steps.nth(0).click();await overview();assert.deepEqual(await marks(),[false,false,false,false]);
    // Empty queue: both review and counter routes point at the shopping form with a hint.
    payments=[];await page.evaluate(()=>{AgentPalAPI.data.payments=[];AgentPalAPI.data.approvals=[];App.navigate('overview');});
    assert.equal(await steps.nth(1).getAttribute('href'),'#lab?tour=shop');
    assert.equal(await steps.nth(2).getAttribute('href'),'#lab?tour=shop');
    for(const index of [1,2]){
      await steps.nth(index).click();assert.equal(await page.evaluate(()=>location.hash),'#lab?tour=shop');
      assert.match(await page.locator('.tour-lab-hint').innerText(),/Ask the AI agent to shop/);
      assert.equal(await page.locator('#lab-task').evaluate(el=>document.activeElement===el),true);
      await overview();
    }
    // Dismissal persists across renders and reloads; it can always be reversed.
    await page.locator('[data-action=tour-dismiss]').click();assert.equal(await page.locator('.overview-tour').isVisible(),false);
    await page.reload();await page.waitForFunction(()=>window.App?.ready);
    assert.equal(await page.locator('.overview-tour').isVisible(),false);
    await page.locator('[data-action=tour-show]').click();assert.equal(await page.locator('.overview-tour').isVisible(),true);
    await page.reload();await page.waitForFunction(()=>window.App?.ready);assert.equal(await page.locator('.overview-tour').isVisible(),true);
    // Missing fields/dates cannot silently become zero or partial totals.
    await page.evaluate(rows=>{AgentPalAPI.data.payments=rows;App.navigate('overview');},fixture());
    for(const mutate of [
      ()=>{delete AgentPalAPI.data.payments[2].terms.amount;},
      ()=>{delete AgentPalAPI.data.payments[2].policy_block;},
      ()=>{AgentPalAPI.data.payments[2].created_at='invalid';},
    ]){
      await page.evaluate(mutate);await overview();assert.equal(await page.locator('[data-tour-impact=blocked]').count(),0);
      await page.evaluate(rows=>{AgentPalAPI.data.payments=rows;},fixture());
    }
    for(const mutate of [
      ()=>{delete AgentPalAPI.data.payments[9].counter_saving;},
      ()=>{delete AgentPalAPI.data.payments[9].counter_saving_verified;},
      ()=>{delete AgentPalAPI.data.payments[0].counter_of;},
      ()=>{AgentPalAPI.data.payments[9].terms.currency='<img src=x>';},
    ]){
      await page.evaluate(mutate);await overview();assert.equal(await page.locator('[data-tour-impact=counter]').count(),0);
      await page.evaluate(rows=>{AgentPalAPI.data.payments=rows;},fixture());
    }
    await page.evaluate(()=>{AgentPalAPI.data.errors=['payment-requests','agents'];App.navigate('overview');});
    assert.equal(await page.locator('.protection-strip').count(),0);assert.deepEqual(await marks(),[false,false,false,false]);
    await page.evaluate(()=>{AgentPalAPI.data.errors=[];AgentPalAPI.data.payments=[];App.navigate('overview');});
    assert.equal(await page.locator('[data-tour-impact]').count(),0,'An empty list has no monetary evidence');
    const utcStart=Date.UTC(new Date().getUTCFullYear(),new Date().getUTCMonth(),1);
    await page.evaluate(rows=>{AgentPalAPI.data.payments=rows;App.navigate('overview');},[
      block('utc-first','0.10','USD',{created_at:new Date(utcStart).toISOString()}),
      block('utc-second','0.20','USD',{created_at:new Date(utcStart).toISOString()}),
      block('previous-utc-month','900.00','USD',{created_at:new Date(utcStart-1).toISOString()}),
      counter('negative-total','-0.25',false),
    ]);
    assert.match(await readTile('blocked'),/\$0\.30 USD/,'UTC midnight belongs to this month even in Los Angeles');
    assert.match(await readTile('counter'),/-\$0\.25 USD/,'Higher counters cannot inflate the total');
    assert.match(await readTile('counter'),/Not yet verified: -\$0\.25 USD/);
    assert.doesNotMatch(await readTile('counter'),/PayPal-verified:/);
    // Review screenshots use a small, readable synthetic workspace; security fixtures stay above.
    payments=[payment('newest-pending'),block('limit-demo','30.00'),
      counter('verified-counter','8.50',true,'USD',{verification:{verified_by:'paypal_read',at:today}}),
      counter('unverified-counter','1.20',false),
      payment('exact-approval','approved',{owner_decision:{decision:'approved',at:today}}),
    ].map(r=>({...r,counter_of:r.counter_of?'original-request':null,
      policy_block:r.policy_block?{...r.policy_block,label:'Amount exceeds the per-payment limit.'}:null,
      terms:{...r.terms,merchant_name:'Office Supply Demo'}}));
    await page.evaluate(rows=>{
      AgentPalAPI.data.payments=rows;AgentPalAPI.data.approvals=rows.filter(r=>r.status==='awaiting_approval');
      AgentPalAPI.data.agents=AgentPalAPI.data.agents.map(a=>({...a,name:a.agent_id==='agent'?'Office shopper':'Saver',metadata:a.agent_id==='saver'?{kind:'saver'}:{}}));
      App.navigate('overview');
    },payments);
    await page.screenshot({path:path.join(shots,`tour-${width}.png`)});
    await page.screenshot({path:path.join(shots,`tour-${width}-full.png`),fullPage:true});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),width);
    assert.deepEqual(await page.evaluate(()=>__csp),[]);assert.deepEqual(errors,[]);assert.deepEqual(external,[]);
    await page.close();
  }
  // Browsers can deny storage reads, writes or the localStorage accessor itself.
  for(const mode of ['methods','accessor']){
    const page=await browser.newPage({viewport:{width:393,height:852}}),errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    await page.addInitScript(mode=>{
      sessionStorage.setItem('agentpal-owner-session','apo_'+'t'.repeat(32));
      const denied=()=>{throw new DOMException('Storage disabled','SecurityError');};
      if(mode==='accessor')Object.defineProperty(window,'localStorage',{get:denied});
      else for(const name of ['getItem','setItem','removeItem'])Object.defineProperty(localStorage,name,{value:denied});
    },mode);
    await page.goto(origin+'/app/');await page.waitForFunction(()=>window.App?.ready);
    assert.equal(await page.locator('.overview-tour').isVisible(),true);
    await page.locator('[data-action=tour-dismiss]').click();assert.equal(await page.locator('.overview-tour').isVisible(),false);
    await page.evaluate(()=>App.navigate('overview'));assert.equal(await page.locator('.overview-tour').isVisible(),false);
    await page.locator('[data-action=tour-show]').click();assert.equal(await page.locator('.overview-tour').isVisible(),true);
    await page.reload();await page.waitForFunction(()=>window.App?.ready);assert.equal(await page.locator('.overview-tour').isVisible(),true);
    assert.deepEqual(errors,[]);await page.close();
  }
  console.log('PASS: tour routes/newest approval/empty-queue hint, live evidence only, persistent and unavailable storage, escaping, exact per-currency monthly impact and verification split, missing-data omission, production CSP, 1440/393px without overflow. Synthetic screenshots: '+shots);
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(async()=>{await browser?.close();await new Promise(r=>server?server.close(r):r());});
