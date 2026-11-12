/* FX-J: judge journey with real dashboard libraries and synthetic same-origin APIs. */
const fs=require('node:fs'),path=require('node:path'),http=require('node:http'),assert=require('node:assert/strict');
const {chromium}=require('playwright');
const site=process.env.AGENTPAL_SCREEN_DIR||path.join(__dirname,'site'),nonce='fxj-browser-nonce',today=new Date().toISOString(),calls=[];
const terms={amount:'10.00',currency:'USD',merchant_name:'Catalogue store',purpose:'Office mouse',merchant_paypal_email:null};
let scenario='pending',record=null,refunded='0.00',server,browser,origin;
const integrations=['paypal_sandbox','channel3','kernel','groq'].map(provider=>({provider,label:provider,fields:[],status:'verified',source:'server',verified_at:today,...(provider==='paypal_sandbox'?{hosted_limits:{max_amount:'50.00',max_monthly_budget:'500.00'}}:{})}));
const wallet={wallet_id:'wallet',agent_id:'agent',currency:'USD',status:'active',limits:{monthly_budget:'200.00',per_transaction_limit:'50.00',per_transaction_review_limit:'50.00',rolling_window_limit:'200.00'},allowance:{available:'200.00',spent:'0.00',reserved:'0.00',pending:'0.00'}};
const agent={agent_id:'agent',name:'Judge Lab',status:'active',capabilities:['payment.prepare'],keys:[]};
const payment=()=>({request_id:'judge-'+scenario,agent_id:'agent',wallet_id:'wallet',status:scenario==='captured'?'provider_confirmed':scenario==='blocked'?'rejected':'provider_pending',provider_mode:scenario==='simulator'?'simulated':'paypal_sandbox',terms,terms_sha256:'a'.repeat(64),created_at:today,decision_requested:true,market_check:{market:{claimed_vs_median:'1.0'},raw_marker:'private-evidence-marker'},...(scenario==='blocked'?{policy_block:{rule:'AMOUNT_ABOVE_HARD_LIMIT',label:'Limit exceeded'}}:{})});
const json=(res,body,status=200)=>{res.writeHead(status,{'Content-Type':'application/json'});res.end(JSON.stringify(body));};
async function serve(req,res){
 const url=new URL(req.url,origin||'http://localhost'),p=url.pathname.slice(4);
 if(url.pathname.startsWith('/v1/')){
  let raw='';for await(const chunk of req)raw+=chunk;const body=raw?JSON.parse(raw):null;calls.push({method:req.method,path:p,body,key:req.headers['idempotency-key']});
  if(p==='setup/status')return json(res,{mode:'hosted',needs_setup:false,signup:'disabled'});
  assert.equal(req.headers.authorization,'Bearer apo_'+'f'.repeat(32));
  if(p==='integrations')return json(res,{integrations});
  if(p==='lab/agent')return json(res,{agent_id:'agent',wallet_id:'wallet'});
  if(p==='lab/payment-requests'){record=payment();refunded='0.00';return json(res,record,201);}
  const lists={agents:{agents:[agent]},wallets:{wallets:[wallet]},'payment-requests':{payment_requests:record?[record]:[]},'payout-requests':{payout_requests:[]},approvals:{approvals:[]},health:{status:'ok',payment_environment:'sandbox',is_real_money:false},disputes:{disputes:[]}};
  if(lists[p])return json(res,lists[p]);
  if(p.endsWith('/transactions'))return json(res,{transactions:[]});
  if(p.endsWith('/timeline'))return json(res,{events:[{at:today,actor:'paypal',label:'Authenticated provider read',verified_by:'paypal_read'}]});
  if(p.endsWith('/reconciliation')){
   if(req.method==='POST')record.provider_error_code='PAYER_NOT_APPROVED';
   return json(res,{provider_references:[{provider:scenario==='simulator'?'local_simulation':'paypal',payer_action_url:scenario==='simulator'?null:'https://www.sandbox.paypal.com/checkoutnow?token=synthetic',status:'pending'}]});
  }
  if(p.endsWith('/refunds')){
   if(req.method==='POST'){assert.equal(body.amount,'0.50');assert.ok(req.headers['idempotency-key']);refunded='0.50';return json(res,{refund_id:'refund',amount:refunded,currency:'USD',status:'COMPLETED'},201);}
   return json(res,{refunds:refunded==='0.00'?[]:[{refund_id:'refund',amount:refunded,currency:'USD',status:'COMPLETED'}],remaining_refundable:refunded==='0.00'?'10.00':'9.50'});
  }
  if(record&&p==='payment-requests/'+record.request_id)return json(res,record);
  if(p==='wallets/wallet')return json(res,wallet);
  return json(res,{error:'not_found'},404);
 }
 const name=url.pathname.replace(/^\/app\//,'')||'index.html',file=path.join(site,name);
 if(name.includes('..')||!fs.existsSync(file)){res.writeHead(404);return res.end();}
 const ext=path.extname(file),type={'.js':'text/javascript','.css':'text/css','.html':'text/html','.svg':'image/svg+xml'}[ext]||'text/plain';
 res.writeHead(200,{'Content-Type':type,'Content-Security-Policy':`default-src 'none'; script-src 'self'; style-src 'self' 'nonce-${nonce}'; style-src-attr 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`});
 res.end(ext==='.html'?fs.readFileSync(file,'utf8').replace('__CSP_NONCE__',nonce):fs.readFileSync(file));
}
(async()=>{
 server=http.createServer((req,res)=>serve(req,res).catch(e=>{console.error(e);json(res,{error:'fixture_failure'},500);}));await new Promise(r=>server.listen(0,'127.0.0.1',r));origin='http://127.0.0.1:'+server.address().port;
 browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE||undefined});
 const page=await browser.newPage({viewport:{width:393,height:852}}),errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.addInitScript(()=>{window.__csp=[];document.addEventListener('securitypolicyviolation',e=>__csp.push(e.violatedDirective));});
 await page.goto(origin+'/app/');await page.locator('#signin-token').fill('apo_'+'f'.repeat(32));await page.locator('.auth-submit').click();await page.waitForFunction(()=>window.App?.ready);
 assert.equal(await page.getByRole('button',{name:'Try Test Lab',exact:true}).count(),1,'Overview primary journey');
 for(const route of ['overview','agents','wallets','transactions','approvals','integrations','lab','analytics','api','security','settings']){
  const primary=['overview','approvals','lab','transactions'].includes(route);
  if(!primary)await page.locator('#phone-more').click();
  const box=await page.locator(`${primary?'#phone-nav':'.phone-more-menu'} [data-route="${route}"]`).boundingBox();assert.ok(box&&box.x>=0&&box.x+box.width<=393,route+' is visible without swiping');
  if(!primary)await page.locator('#close-modal').click();
 }
 await page.evaluate(()=>App.navigate('lab'));await page.getByText('Services ready',{exact:false}).waitFor();assert.doesNotMatch(await page.locator('.lab-screen').innerText(),/Connect Channel3 and Groq or OpenRouter first/);
 const create=async mode=>{scenario=mode;await page.locator('#lab-amount').fill('10.00');await page.locator('#lab-merchant').fill('Catalogue store');await page.locator('#lab-purpose').fill('Office mouse');await page.locator('#lab-payment-form button').click();await page.waitForFunction(id=>document.querySelector('#lab-result')?.textContent.includes(id),'judge-'+mode);await page.waitForTimeout(100);};
 await create('pending');
 assert.match(await page.locator('#lab-result').innerText(),/Log in with the sandbox buyer, click Continue\/Complete Purchase\. PayPal sandbox may stay on the review page; come back here and click Capture\. Capture only succeeds if PayPal confirms your approval\./);
 const start=calls.length;await page.getByRole('button',{name:'Check PayPal approval',exact:true}).click();await page.waitForFunction(()=>document.querySelector('#lab-result')?.textContent.includes('PayPal has not confirmed buyer approval'));
 assert.ok(calls.slice(start).some(c=>c.method==='POST'&&c.path.endsWith('/reconciliation')),'Check performs authenticated server read');assert.ok(!calls.slice(start).some(c=>c.path.endsWith('/capture')));
 await create('blocked');const steps=await page.locator('.lab-steps>li').allTextContents();assert.equal(steps.slice(1).filter(t=>t.includes('Not applicable: blocked by rule')).length,5);assert.doesNotMatch(steps.join(' '),/Awaiting owner|Waiting for|Available after/);
 await create('captured');await page.locator('#lab-refund-amount').waitFor();assert.equal(await page.locator('#lab-refund-amount').inputValue(),'10.00');
 for(const invalid of ['0','-1','0.001','1e1','10.01','NaN','']){const before=calls.length;await page.locator('#lab-refund-amount').fill(invalid);await page.locator('[data-lab-action=refund]').click();assert.ok(!calls.slice(before).some(c=>c.method==='POST'&&c.path.endsWith('/refunds')),invalid);assert.match(await page.locator('#lab-result').innerText(),/positive decimal.*remaining/i);}
 await page.locator('#lab-refund-amount').fill('0.50');await page.locator('[data-lab-action=refund]').click();await page.waitForFunction(()=>document.querySelector('#lab-refund-amount')?.value==='9.50');
 await page.evaluate(()=>App.navigate('transactions'));await page.locator('#transaction-ledger .ag-row').first().waitFor();
 const beforeScroll=await page.locator('#transaction-ledger .ag-body-horizontal-scroll-viewport').evaluate(el=>el.scrollLeft);assert.equal(beforeScroll,0);
 await page.locator('#transaction-ledger .ag-row [col-id=amount]').first().click();await page.locator('dialog[open]').waitFor();assert.match(await page.locator('dialog').innerText(),/AgentPal's PayPal sandbox business account/);await page.locator('#close-modal').click();
 await page.evaluate(()=>App.navigate('agents'));await page.locator('#connected-agents .ag-row [col-id=spent]').first().click();await page.locator('dialog[open]').waitFor();assert.match(await page.locator('dialog').innerText(),/Judge Lab/);await page.locator('#close-modal').click();
 // Open the same real approval renderer with exact terms and raw evidence.
 record.status='awaiting_approval';await page.evaluate(id=>FinanceScreens.openRequest(id,'payment',true),record.request_id);await page.locator('#f-live-confirm').waitFor();
 const evidence=page.locator('dialog details.f-advisory');assert.equal(await evidence.count(),1);assert.equal(await evidence.getAttribute('open'),null);assert.match(await evidence.locator('summary').innerText(),/Evidence \(advisory\)/);
 assert.equal(await page.getByText('private-evidence-marker',{exact:true}).isVisible(),false);
 const order=await page.evaluate(()=>({button:document.querySelector('#f-live-confirm').getBoundingClientRect().top,evidence:document.querySelector('details.f-advisory').getBoundingClientRect().top}));assert.ok(order.button<order.evidence,'Decision precedes evidence');assert.match(await page.locator('dialog').innerText(),/terms fingerprint/i);
 assert.ok((await page.locator('#f-live-confirm').boundingBox()).y<852,'Approve reachable on phone without evidence scrolling');await page.locator('#close-modal').click();
 record.provider_mode='simulated';await page.evaluate(id=>FinanceScreens.openRequest(id,'payment',true),record.request_id);assert.match(await page.locator('dialog').innerText(),/Local simulator \(no PayPal\)/);await page.locator('#close-modal').click();
 await page.evaluate(()=>App.navigate('wallets'));await page.locator('[data-ws-action=limits]').first().click();await page.locator('#ws-form').waitFor();assert.match(await page.locator('dialog').innerText(),/50.00.*per transaction.*500.00.*monthly/i);await page.locator('#close-modal').click();
 await page.evaluate(()=>App.navigate('api'));assert.match(await page.locator('#screen').innerText(),/Requests \(payments \+ payouts\)/);
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),393);assert.deepEqual(await page.evaluate(()=>__csp),[]);assert.deepEqual(errors,[]);
 console.log('PASS: FX-J judge guidance, provider payees, receipt-driven timeline, PayPal approval read, partial refund validation, blocked walkthrough, phone row action/nav, compact exact-term decision, portal count and hosted limit notice.');
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(async()=>{await browser?.close();await new Promise(r=>server?server.close(r):r());});
