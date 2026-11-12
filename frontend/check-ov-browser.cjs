/* OV dashboard audit regressions: local HTTP API fixtures, real scripts and CSP. */
const fs=require('node:fs'),path=require('node:path'),http=require('node:http');
const assert=require('node:assert/strict');
const {chromium}=require('playwright');
const today=new Date().toISOString(),old='2020-01-01T00:00:00Z',digest='a'.repeat(64);
const hostile='<img src=x onerror=alert(1)>',calls=[];
const payment=(id,status,extra={})=>({request_id:id,agent_id:'agent',wallet_id:'wallet',status,
  provider_mode:'paypal_sandbox',created_at:today,terms_sha256:digest,decision_requested:true,
  owner_decision:null,policy_block:null,verification:null,
  terms:{amount:'0.10',currency:'USD',merchant_name:id,purpose:'Local test fixture'},...extra});
const approved={decision:'approved',at:today},verification={verified_by:'paypal_read',at:today};
const payments=[
  payment('Blocked request','rejected',{policy_block:{rule:'AMOUNT_ABOVE_HARD_LIMIT',label:hostile}}),
  payment('Verified payment','provider_confirmed',{owner_decision:approved,verification}),
  payment('Another verified payment','provider_confirmed',{owner_decision:approved,verification,
    terms:{amount:'0.20',currency:'USD',merchant_name:'Second verified payment'}}),
  payment('Needs reconciliation','reconciliation_required'),
  payment('Pending approval','awaiting_approval'),
  payment('No recorded verification','provider_confirmed',{terms:{amount:'7.00',currency:'USD'}}),
  payment('Old request approved this month','approved',{created_at:old,owner_decision:approved}),
  payment('No terms digest','approved',{owner_decision:approved,terms_sha256:null}),
  payment('Old block','rejected',{created_at:old,policy_block:{rule:'AMOUNT_ABOVE_HARD_LIMIT'}}),
  payment('Old reconciliation','reconciliation_required',{created_at:old}),
];
const payouts=[{request_id:'payout',agent_id:'agent',wallet_id:'wallet',created_at:today,
  amount:'3.00',currency:'USD',status:'provider_confirmed',owner_decision:approved,
  terms_sha256:digest,verification:{verified_by:'paypal_webhook',at:today}}];
const wallets=[{wallet_id:'wallet',agent_id:'agent',currency:'USD',status:'active',
  limits:{monthly_budget:'100.00',per_transaction_limit:'20.00'},
  allowance:{available:'80.00',spent:'8.00',reserved:'10.00',pending:'2.00'}}];
const agents=[{agent_id:'agent',name:'Audit test agent',status:'active',role:'external',
  capabilities:[],created_at:today,keys:[{key_id:'test-key-id',status:'live',created_at:today,
    expires_at:'2030-01-01T12:30:00Z',revoked_at:null}]}];
let server,browser,origin,failFreeze=false;
function json(res,value,status=200){res.writeHead(status,{'Content-Type':'application/json'});res.end(JSON.stringify(value));}
function serve(req,res){
  const url=new URL(req.url,origin||'http://localhost');
  if(url.pathname.startsWith('/v1/')){
    calls.push({method:req.method,path:url.pathname});
    if(req.method==='POST'&&url.pathname==='/v1/wallets/freeze-all'){
      return setTimeout(()=>{
        if(failFreeze)return json(res,{error:'not_configured'},503);
        wallets.forEach(w=>w.status='frozen');json(res,{wallets});
      },180);
    }
    assert.equal(req.method,'GET','Only the explicitly confirmed freeze may write');
    const p=url.pathname.slice(4),lists={
      'setup/status':{mode:'hosted',needs_setup:false,signup:'disabled'},
      health:{status:'ok',payment_environment:'sandbox',is_real_money:false},
      agents:{agents},wallets:{wallets},'payment-requests':{payment_requests:payments},
      'payout-requests':{payout_requests:payouts},integrations:{integrations:[]},disputes:{disputes:[]},
    };
    return json(res,lists[p]||(p.endsWith('/transactions')?{transactions:[]}:{}));
  }
  const name=url.pathname.replace(/^\/app\//,'')||'index.html';
  if(name.includes('..'))return json(res,{},404);
  const file=path.join(__dirname,'site',name);
  if(!fs.existsSync(file))return json(res,{},404);
  const ext=path.extname(file),type={'.js':'text/javascript','.css':'text/css','.html':'text/html','.svg':'image/svg+xml'}[ext];
  res.writeHead(200,{'Content-Type':type||'text/plain','Content-Security-Policy':
    "default-src 'none'; script-src 'self'; style-src 'self' 'nonce-ov-test'; img-src 'self' data:; connect-src 'self'; font-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"});
  res.end(ext==='.html'?fs.readFileSync(file,'utf8').replace('__CSP_NONCE__','ov-test'):fs.readFileSync(file));
}
(async()=>{
  server=http.createServer(serve);await new Promise(r=>server.listen(0,'127.0.0.1',r));
  origin='http://127.0.0.1:'+server.address().port;
  browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE||undefined});
  const page=await browser.newPage({viewport:{width:1440,height:1000},timezoneId:'America/Los_Angeles'});
  const errors=[],external=[];
  page.on('pageerror',e=>errors.push(e.message));
  page.on('request',r=>{if(!r.url().startsWith(origin))external.push(r.url());});
  await page.addInitScript(()=>{window.__csp=[];document.addEventListener('securitypolicyviolation',e=>__csp.push(e.violatedDirective));});
  await page.goto(origin+'/app/');await page.locator('#signin-token').fill('apo_'+'t'.repeat(32));
  await page.locator('.auth-submit').click();await page.waitForFunction(()=>window.App?.ready);
  await page.getByRole('button',{name:'Skip setup',exact:true}).click();
  const navigate=async route=>{await page.evaluate(r=>App.navigate(r),route);await page.waitForTimeout(120);assert.equal(await page.getByText('This page could not load',{exact:true}).count(),0);};
  await navigate('overview');
  assert.equal(await page.locator('#screen h1').innerText(),'Overview');
  assert.equal(await page.locator('.promo,.carousel-dots').count(),0);
  assert.match(await page.locator('.overview-audience').innerText(),/every payment needs a human's approval of the exact terms/);
  const tiles=await page.locator('.protection-tiles article').allTextContents();
  assert.equal(tiles.length,4);assert.match(tiles[0],/^1Requests blocked by rule$/);
  assert.match(tiles[1],/^4Approvals bound to exact terms$/);assert.match(tiles[2],/^\$3\.30Verified by PayPal \(gross\)$/);
  assert.match(tiles[3],/^1Requests needing reconciliation$/);
  assert.match(await page.locator('.activity').innerText(),/AMOUNT_ABOVE_HARD_LIMIT/);
  assert.equal(await page.locator('.activity img').count(),0);
  assert.match(await page.locator('.stats').innerText(),/Confirmed payments \+ payouts/);
  assert.equal(await page.locator('#phone-nav').isVisible(),false);
  assert.equal(await page.locator('body>aside').isVisible(),true);
  const shots=process.env.AGENTPAL_TEST_SHOTS||'/tmp/win/ov-shots';fs.mkdirSync(shots,{recursive:true});
  payments[0].policy_block.label="The request exceeds this wallet's per-transaction limit.";
  await page.evaluate(()=>AgentPalAPI.refresh());
  const activityFits=async()=>{
    const panel=await page.locator('.overview-right .activity').boundingBox();
    const last=await page.locator('.overview-right .activity-row').last().boundingBox();
    assert.ok(last.y+last.height<=panel.y+panel.height,'All activity rows and rule reasons fit inside the panel');
  };
  await activityFits();
  await page.screenshot({path:path.join(shots,'overview-1440.png')});
  // A failed list read cannot become an apparently complete zero count.
  await page.evaluate(()=>{AgentPalAPI.data.errors=['payment-requests'];App.navigate('overview');});
  assert.equal(await page.locator('.protection-strip').count(),0);
  await page.evaluate(()=>{AgentPalAPI.data.errors=[];for(const r of [...AgentPalAPI.data.payments,...AgentPalAPI.data.payouts])r.verification=null;App.navigate('overview');});
  assert.equal(await page.getByText('Verified by PayPal (gross)',{exact:true}).count(),0);
  await page.evaluate(()=>AgentPalAPI.refresh());
  await page.setViewportSize({width:393,height:852});await navigate('overview');
  assert.equal(await page.locator('body>aside').isVisible(),false);
  assert.equal(await page.locator('#phone-nav button').count(),5);
  assert.equal(await page.locator('#phone-nav .approval-count').innerText(),'1');
  const tabBox=await page.locator('#phone-nav').boundingBox();assert.ok(tabBox.height<85);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),393);
  assert.ok(await page.evaluate(()=>parseFloat(getComputedStyle(document.querySelector('main')).paddingBottom)>document.querySelector('#phone-nav').getBoundingClientRect().height));
  await activityFits();
  await page.screenshot({path:path.join(shots,'overview-393.png')});
  await page.getByRole('button',{name:'More',exact:true}).click();
  assert.equal(await page.locator('.phone-more-menu button').count(),7);
  await page.screenshot({path:path.join(shots,'phone-menu-393.png')});
  await page.locator('.phone-more-menu [data-route=security]').click();
  assert.equal(await page.locator('dialog[open]').count(),0);
  assert.equal(await page.locator('#phone-more').getAttribute('class'),'active');
  const expectedDate=await page.evaluate(at=>AgentPalVisuals.date(at),today);
  assert.match(await page.locator('.ad-live-credential').first().innerText(),new RegExp(expectedDate.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')));
  assert.ok(!(await page.locator('.ad-live-credential').first().innerText()).includes(today));
  assert.match(await page.locator('.ad-live-credential').first().innerText(),/No expiry|Expires/);
  const freezes=()=>calls.filter(c=>c.method==='POST').length;
  await page.getByRole('button',{name:'Freeze all agent spending',exact:true}).click();assert.equal(freezes(),0);
  await page.getByRole('button',{name:'Cancel',exact:true}).click();assert.equal(freezes(),0);
  await navigate('overview');await page.getByRole('button',{name:'Freeze all agent spending',exact:true}).click();
  await page.getByRole('button',{name:'Confirm freeze all',exact:true}).dblclick();
  await page.waitForFunction(()=>AgentPalAPI.data.wallets.every(w=>w.status==='frozen'));
  assert.equal(freezes(),1);assert.equal(await page.locator('dialog[open]').count(),0);
  // A failed write keeps the confirmation open and leaves displayed wallet state alone.
  await page.evaluate(()=>AgentPalAPI.refresh());
  failFreeze=true;wallets.forEach(w=>w.status='active');await page.evaluate(()=>AgentPalAPI.refresh());await navigate('security');await page.getByRole('button',{name:'Freeze all agent spending',exact:true}).click();
  assert.equal(await page.evaluate(()=>AgentPalAPI.data.wallets.every(w=>w.status==='active')),true,'Failure scenario starts with active wallets');
  await page.getByRole('button',{name:'Confirm freeze all',exact:true}).click();
  await page.locator('.freeze-error:not([hidden])').waitFor();assert.equal(await page.locator('dialog[open]').count(),1);
  assert.equal(await page.evaluate(()=>AgentPalAPI.data.wallets.every(w=>w.status==='active')),true,'A failed freeze must not simulate frozen wallets');
  await page.getByRole('button',{name:'Cancel',exact:true}).click();
  for(const route of ['overview','approvals','lab','transactions','agents','wallets','integrations','analytics','api','security','settings']){
    await navigate(route);assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),393,route+' phone overflow');
  }
  await navigate('wallets');assert.match(await page.locator('.ws-stats').innerText(),/Spent, net of refunds/);
  await page.setViewportSize({width:760,height:852});assert.equal(await page.locator('#phone-nav').isVisible(),true);
  await page.setViewportSize({width:761,height:852});assert.equal(await page.locator('#phone-nav').isVisible(),false);
  assert.deepEqual(await page.evaluate(()=>__csp),[]);assert.deepEqual(external,[]);assert.deepEqual(errors,[]);
  console.log('PASS: OV evidence totals/dates/missing-data/escaping; five phone tabs and seven More pages; all 11 routes at 393px without overflow; local Security dates; confirmed freeze/cancel/double-click/failure; 760px breakpoint; strict CSP. Three local fixture screenshots saved to '+shots);
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(async()=>{await browser?.close();await new Promise(r=>server?server.close(r):r());});
