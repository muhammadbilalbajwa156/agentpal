/* Synthetic Saver policy UI and owner request review. Owner counter-offer ("Buy this instead") in the real dashboard under the production CSP,
   with synthetic same-origin APIs. The fixture never approves on a counter call. */
const fs=require('node:fs'),path=require('node:path'),http=require('node:http'),assert=require('node:assert/strict');
const {chromium}=require('playwright');
const site=process.env.AGENTPAL_SCREEN_DIR||path.join(__dirname,'site'),nonce='counter-browser-nonce',today=new Date().toISOString(),calls=[];
let server,browser,origin,seq=0,sameTermsOnce=false;
const integrations=['paypal_sandbox','channel3','kernel','groq'].map(provider=>({provider,label:provider,fields:[],status:'verified',source:'server',verified_at:today}));
const wallet={wallet_id:'wallet',agent_id:'agent',currency:'USD',status:'active',limits:{monthly_budget:'200.00',per_transaction_limit:'25.00',per_transaction_review_limit:'25.00',rolling_window_limit:'200.00'},allowance:{available:'200.00',spent:'0.00',reserved:'0.00',pending:'0.00'}};
const agent={agent_id:'agent',name:'Shopper <b>bot</b>',status:'active',capabilities:['payment.prepare'],keys:[]};
const deal=(retailer,price,currency,save)=>({retailer,price,currency,condition:'new',availability:'in_stock',you_save:save,you_save_pct:'34.0',buy_url:null,on_sale:false});
const market=()=>({provider:'channel3',checked_at:today,advisory_level:'review_recommended',market:{claimed_vs_median:'1.5'},
 better_deals:[deal('eur-shop.example','9.00','EUR','15.99'),deal('known-retailer.example','16.49','USD','8.50'),deal('<img src=x onerror=alert(1)>','15.00','USD','9.99')],signals:[]});
const make=(id,amount,merchant,extra={})=>({request_id:id,agent_id:'agent',owner_id:'owner',wallet_id:'wallet',status:'awaiting_approval',provider_mode:'paypal_sandbox',
 terms:{request_id:id,agent_id:'agent',amount,currency:'USD',merchant_name:merchant,merchant_domain:merchant.toLowerCase().replace(/[^a-z]+/g,'-')+'.example',purpose:'USB-C hub',merchant_paypal_email:null},
 terms_sha256:(++seq).toString(16).repeat(64).slice(0,64),created_at:new Date(Date.now()+seq*1000).toISOString(),decision_requested:true,recommendation:'review',risk_score:10,
 risk_signals:[],owner_decision:null,policy_block:null,verification:null,market_check:null,page_check:null,countered_by:null,counter_of:null,counter_source:null,counter_saving:null,counter_saving_verified:false,...extra});
const records=new Map();
const add=r=>{records.set(r.request_id,r);return r;};
add(make('pay_orig','24.99','Unfamiliar Shop',{market_check:market()}));
const json=(res,body,status=200)=>{res.writeHead(status,{'Content-Type':'application/json'});res.end(JSON.stringify(body));};
function counter(original,body){
 if(original.status!=='awaiting_approval')return [409,{error:'not_awaiting_decision'}];
 let amount=body.amount,merchant=body.merchant_name,source='manual';
 if(body.deal_index!==undefined){
  assert.deepEqual(Object.keys(body),['deal_index'],'a deal counter sends only the index, never a price or merchant');
  const d=original.market_check.better_deals[body.deal_index];amount=d.price;merchant=d.retailer;source='channel3_deal';
 }else if(sameTermsOnce){sameTermsOnce=false;return [422,{error:'counter_same_terms'}];}
 original.status='rejected';original.owner_decision={decision:'rejected',at:today};
 const id='pay_new'+(records.size);
 const next=add(make(id,amount,merchant,{counter_of:original.request_id,counter_source:source,
  counter_saving:(Number(original.terms.amount)-Number(amount)).toFixed(2)}));
 original.countered_by=id;
 return [201,{original,replacement:next,approves_nothing:true}];
}
let savers=[],runRetireOnce=false;
const mockSaver=()=>({saver_id:'sav_'+savers.length,owner_id:'owner',agent_id:'saver-agent',status:'active',manifesto:'I exist to save you money. If I save you nothing this month, I retire myself.',created_at:today,retired_at:null,retirement_reason:null,note:'Read <img src=x onerror=alert(1)> offers carefully.',note_source:'ai',days_left:12,automatic_interval_seconds:900,periods:[],feed:[],current_period:{period:'2026-11',suggestions_made:0,suggestions_used:0,verified_savings:{},unverified_potential:{},report:'No PayPal-verified savings this month.'}});
async function serve(req,res){
 const url=new URL(req.url,origin||'http://localhost'),p=url.pathname.slice(4);
 if(url.pathname.startsWith('/v1/')){
  let raw='';for await(const chunk of req)raw+=chunk;const body=raw?JSON.parse(raw):null;calls.push({method:req.method,path:p,body});
  if(p==='setup/status')return json(res,{mode:'hosted',needs_setup:false,signup:'disabled'});
  assert.equal(req.headers.authorization,'Bearer apo_'+'f'.repeat(32));
  if(p==='savers'){
   if(req.method==='POST'){const s=mockSaver();savers.push(s);return json(res,s,201);}
   return json(res,{savers});
  }
  const saverRoute=/^savers\/([^/]+)\/(run|retire)$/.exec(p);
  if(saverRoute){
   const s=savers.find(s=>s.saver_id===saverRoute[1]);assert.ok(s);
   if(saverRoute[2]==='run'){
    if(runRetireOnce){runRetireOnce=false;s.status='retired';s.retirement_reason='Retired: saved $0 in 2026-11';return json(res,{error:'saver_retired'},409);}
    const suggestion={suggestion_id:'sgg_1',saver_id:s.saver_id,request_id:'pay_orig',deal_index:1,retailer:'known-retailer.example <img src=x onerror=alert(1)>',price:'16.49',currency:'USD',saving:'8.50',created_at:today};
    s.feed=[suggestion];s.current_period.suggestions_made=1;s.current_period.unverified_potential={USD:'8.50'};
    records.get('pay_orig').saver_suggestion=suggestion;
   }else{s.status='retired';s.retirement_reason='Retired: saved $0 in 2026-11';}
   return json(res,s);
  }
  const all=[...records.values()];
  const lists={agents:{agents:[agent]},wallets:{wallets:[wallet]},'payment-requests':{payment_requests:all},'payout-requests':{payout_requests:[]},
   approvals:{payment_requests:all.filter(r=>r.status==='awaiting_approval')},health:{status:'ok',payment_environment:'sandbox',is_real_money:false},disputes:{disputes:[]},integrations:{integrations}};
  if(req.method==='GET'&&lists[p])return json(res,lists[p]);
  if(p==='lab/agent')return json(res,{agent_id:'agent',wallet_id:'wallet'});
  if(p==='lab/payment-requests'&&req.method==='POST')return json(res,add(make('pay_lab',body.amount,body.merchant_name,{market_check:market()})),201);
  const m=/^(lab\/)?payment-requests\/([^/]+)(?:\/(.+))?$/.exec(p),record=m&&records.get(m[2]);
  if(!record)return json(res,{error:'not_found'},404);
  const action=m[3];
  if(!action)return json(res,record);
  if(action==='timeline')return json(res,{request_id:record.request_id,events:[{at:today,actor:'agent',kind:'created',label:'The agent requested a sandbox payment.',detail:null,verified_by:null},
   ...(record.counter_of?[{at:today,actor:'owner',kind:'counter_offer',label:"Created from the owner's counter-offer.",detail:'Replaces '+record.counter_of+'.',verified_by:null}]:[]),
   ...(record.countered_by?[{at:today,actor:'owner',kind:'counter_offer',label:'The owner countered with different terms.',detail:'Replaced by '+record.countered_by+'.',verified_by:null}]:[])]});
  if(action==='counter'&&req.method==='POST'){assert.ok(!m[1]);const [status,out]=counter(record,body);return json(res,out,status);}
  if(action==='approve'&&req.method==='POST'){
   if(body.terms_sha256!==record.terms_sha256||record.status!=='awaiting_approval')return json(res,{error:'terms_mismatch'},409);
   record.status='approved';record.owner_decision={decision:'approved',at:today};return json(res,{request_id:record.request_id,decision:'approved',status:'approved'});
  }
  if(action==='execute'&&req.method==='POST'){
   if(record.status!=='approved')return json(res,{error:'execution_refused'},409);
   record.status='provider_pending';return json(res,{...record,payer_action_url:'https://www.sandbox.paypal.com/checkoutnow?token=synthetic'});
  }
  if(action==='reconciliation')return json(res,{request_id:record.request_id,status:record.status,reconciliation_required:false,provider_references:[]});
  if(action==='refunds')return json(res,{refunds:[],remaining_refundable:record.terms.amount});
  return json(res,{error:'not_found'},404);
 }
 const name=url.pathname.replace(/^\/app\//,'')||'index.html',file=path.join(site,name);
 if(name.includes('..')||!fs.existsSync(file)){res.writeHead(404);return res.end();}
 const ext=path.extname(file),type={'.js':'text/javascript','.css':'text/css','.html':'text/html','.svg':'image/svg+xml'}[ext]||'text/plain';
 res.writeHead(200,{'Content-Type':type,'Content-Security-Policy':`default-src 'none'; script-src 'self'; style-src 'self' 'nonce-${nonce}'; style-src-attr 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`});
 res.end(ext==='.html'?fs.readFileSync(file,'utf8').replace('__CSP_NONCE__',nonce):fs.readFileSync(file));
}

(async()=>{
 fs.mkdirSync('/tmp/agentpal-shots/sv-shots',{recursive:true});
 server=http.createServer((req,res)=>serve(req,res).catch(e=>{console.error(e);json(res,{error:'fixture_failure'},500);}));
 await new Promise(r=>server.listen(0,'127.0.0.1',r));origin='http://127.0.0.1:'+server.address().port;
 browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE||undefined});
 for(const width of [1440,393]){
  savers=[];records.get('pay_orig').saver_suggestion=null;
  const page=await browser.newPage({viewport:{width,height:1000}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  page.on('dialog',d=>{errors.push(d.message());d.dismiss();});
  await page.addInitScript(()=>{window.__csp=[];document.addEventListener('securitypolicyviolation',e=>__csp.push(e.violatedDirective));});
  await page.goto(origin+'/app/');
  await page.locator('#signin-token').fill('apo_'+'f'.repeat(32));await page.locator('.auth-submit').click();
  await page.waitForFunction(()=>window.App?.ready);await page.evaluate(()=>App.navigate('agents'));
  const card=page.locator('.saver-agents');await card.locator('[data-saver-hire]').waitFor();
  assert.match(await card.innerText(),/I exist to save you money/);
  assert.match(await card.innerText(),/Runs automatically while the AgentPal server is running/);
  const start=calls.length;
  await card.locator('[data-saver-hire]').click();await card.locator('[data-saver-run]').waitFor();
  assert.match(await card.innerText(),/This month's verified savings[\s\S]*\$0/);
  assert.match(await card.innerText(),/AI-written note/);
  assert.equal(await card.locator('img').count(),0);
  assert.equal(await card.locator('[data-saver-hire]').isDisabled(),true);
  await card.locator('[data-saver-run]').click();await card.locator('[data-saver-review]').waitFor();
  assert.match(await card.innerText(),/Unverified potential: \$8.50 USD/);
  assert.match(await card.innerText(),/known-retailer/);
  assert.equal(await card.locator('img').count(),0,'feed and note are escaped');
  assert.deepEqual(calls.slice(start).filter(c=>c.method==='POST').map(c=>c.path),['savers','savers/sav_0/run']);
  await page.screenshot({path:'/tmp/agentpal-shots/sv-shots/saver-'+width+'.png',fullPage:true});
  await card.locator('[data-saver-review]').click();await page.locator('dialog[open] .saver-suggestion').waitFor();
  assert.match(await page.locator('.saver-suggestion').innerText(),/Saver suggests: Counter with known-retailer/);
  assert.equal(await page.locator('.saver-suggestion img').count(),0);
  assert.equal(await page.locator('[data-counter-deal="1"]').count(),1,'existing owner-only deal counter stays available');
  await page.screenshot({path:'/tmp/agentpal-shots/sv-shots/request-'+width+'.png',fullPage:true});
  await page.locator('#close-modal').click();
  // A server-verified capture is the only supplied savings total; currencies stay separate.
  savers[0].current_period.verified_savings={USD:'8.50',EUR:'2.00'};
  savers[0].current_period.unverified_potential={};savers[0].current_period.suggestions_used=1;
  await page.evaluate(()=>AgentPalAPI.refresh());
  await page.waitForFunction(()=>document.querySelector('.saver-metrics')?.textContent.includes('$8.50'));
  assert.match(await card.innerText(),/\$8.50 USD · €2.00 EUR/);
  await card.locator('[data-saver-retire]').click();await page.waitForFunction(()=>document.querySelector('.saver-card')?.textContent.includes('Retired: saved $0'));
  assert.equal(await card.locator('[data-saver-run]').count(),0);
  assert.equal(await card.locator('[data-saver-review]').count(),1,'history retained');
  assert.equal(await card.locator('[data-saver-hire]').isDisabled(),false);
  // Run now can discover a zero closed month. A refused run must refresh its
  // newly retired status and restore correct Hire availability immediately.
  await card.locator('[data-saver-hire]').click();await card.locator('[data-saver-run]').waitFor();
  runRetireOnce=true;await card.locator('[data-saver-run]').click();
  await page.waitForFunction(()=>!document.querySelector('[data-saver-run]')&&document.querySelectorAll('.saver-card').length===2);
  assert.equal(await card.locator('[data-saver-hire]').isDisabled(),false);
  assert.match(await card.locator('.saver-card').last().innerText(),/Retired: saved \$0 in 2026-11/);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),width,'no page overflow');
  assert.deepEqual(await page.evaluate(()=>__csp),[]);assert.deepEqual(errors,[]);
  await page.close();
 }
 console.log('PASS: Saver hire/run/review/retire, verified vs potential, currency separation, history, escaped feed and note, CSP, desktop + phone. Synthetic API only.');
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(async()=>{await browser?.close();await new Promise(r=>server?server.close(r):r());});
