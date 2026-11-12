/* Owner review under the production CSP, with synthetic same-origin API data only. */
const fs=require('node:fs'),path=require('node:path'),http=require('node:http'),assert=require('node:assert/strict');
const {chromium}=require('playwright');
const site=process.env.AGENTPAL_SCREEN_DIR||path.join(__dirname,'site'),nonce='approval-browser-nonce';
const shots='/tmp/agentpal-shots/ap-shots',today=new Date().toISOString(),hash='123456789abc'+'d'.repeat(52),hostile='<img src=x onerror=alert(1)>',calls=[];
fs.mkdirSync(shots,{recursive:true});
let server,browser,origin,fixtureError;
const market={provider:'channel3',checked_at:today,advisory_level:'no_concern',market:{currency:'USD',claimed_vs_median:'1.2'},signals:[],better_deals:[
 {retailer:'Wrong currency',price:'1.00',currency:'EUR'},
 {retailer:'Everyday Electronics',price:'18.00',currency:'USD',you_save:'6.00'},
 {retailer:'Hub Store',price:'20.00',currency:'USD',you_save:'4.00'}]};
const make=(id,extra={})=>({request_id:id,agent_id:'agent',wallet_id:'wallet',provider_mode:'paypal_sandbox',status:'awaiting_approval',terms_sha256:hash,created_at:today,decision_requested:true,recommendation:'allow',risk_score:0,risk_signals:[],verification:null,terms:{amount:'24.00',currency:'USD',merchant_name:'Supply Shop',merchant_domain:'supply.example',merchant_paypal_email:'merchant@example.test',purpose:'USB-C hub'},...extra});
const records=new Map([
 make('cheaper',{market_check:market}),make('fine'),make('risk',{risk_signals:[{code:'UNUSUAL',kind:'risk_indicator',message:'Unusual purchase for this agent.',points:10}]}),
 make('page',{page_check:{advisory_level:'review_recommended',signals:[{code:'PAGE_WARNING',severity:'warning',message:'The page price differs from the request.'}]}}),
 make('low',{market_check:{...market,better_deals:[],market:{claimed_vs_median:'0.2'}}}),
 make('unknown',{terms:{amount:'24.00',currency:'USD',merchant_name:'Unknown merchant',purpose:'USB-C hub'}}),
 make('blocked',{status:'rejected',policy_block:{rule:'HARD_LIMIT',label:'Amount exceeds the wallet limit.'}}),
 make('pending',{status:'provider_pending'}),make('submitted',{status:'submitted_to_provider'}),make('approved',{status:'approved'}),
 make('captured',{status:'provider_confirmed'}),make('verified',{status:'provider_confirmed',verification:{verified_by:'paypal_read',at:today}}),
 make('webhook',{status:'provider_confirmed',verification:{verified_by:'paypal_webhook',at:today}}),
 make('rejected',{status:'rejected'}),make('countered',{status:'rejected',countered_by:'cheaper'}),
 make('uncertain',{status:'reconciliation_required',verification:{verified_by:'paypal_read',at:today}}),
 make('hostile',{terms:{amount:'24.00',currency:'USD',merchant_name:hostile,merchant_domain:hostile,purpose:hostile},risk_signals:[{code:hostile,kind:'uncertainty',message:hostile}],market_check:{...market,better_deals:[{retailer:hostile,price:'18.00',currency:'USD'}]}}),
 make('changed')
].map(r=>[r.request_id,r]));
for(const mode of ['paypal_sandbox','simulated','unavailable']){
 const record=make('payee-'+mode,{provider_mode:mode});delete record.terms.merchant_paypal_email;records.set(record.request_id,record);
}
records.set('undated',make('undated',{market_check:{...market,checked_at:null}}));
const json=(res,body,status=200)=>{res.writeHead(status,{'Content-Type':'application/json'});res.end(JSON.stringify(body));};
async function serve(req,res){
 const url=new URL(req.url,origin||'http://localhost'),p=url.pathname.slice(4);
 if(url.pathname.startsWith('/v1/')){
  let raw='';for await(const chunk of req)raw+=chunk;const body=raw?JSON.parse(raw):null;
  calls.push({method:req.method,path:p,body});
  if(p==='setup/status')return json(res,{mode:'hosted',needs_setup:false,signup:'disabled'});
  assert.equal(req.headers.authorization,'Bearer apo_'+'f'.repeat(32));
  const all=[...records.values()],lists={agents:{agents:[{agent_id:'agent',name:'Shopping assistant',status:'active',capabilities:[],keys:[]}]},wallets:{wallets:[]},'payment-requests':{payment_requests:all},'payout-requests':{payout_requests:[]},approvals:{payment_requests:all.filter(r=>r.status==='awaiting_approval')},health:{status:'ok',payment_environment:'sandbox',is_real_money:false},disputes:{disputes:[]},integrations:{integrations:['paypal_sandbox','channel3','kernel','groq'].map(provider=>({provider,status:'verified',source:'server',fields:[]}))}};
  if(req.method==='GET'&&lists[p])return json(res,lists[p]);
  const match=/^payment-requests\/([^/]+)(?:\/(.+))?$/.exec(p),record=match&&records.get(match[1]);
  if(!record)return json(res,{error:'not_found'},404);
  if(req.method==='GET'&&!match[2])return json(res,record);
  if(req.method==='GET'&&match[2]==='timeline')return json(res,{events:[]});
  if(req.method==='POST'&&match[2]==='approve'){
   assert.equal(record.status,'awaiting_approval');assert.deepEqual(body,{terms_sha256:record.terms_sha256});
   record.status='approved';return json(res,{status:'approved'});
  }
  throw Error('Unexpected fixture call: '+req.method+' '+p);
 }
 const name=url.pathname.replace(/^\/app\//,'')||'index.html',file=path.join(site,name);
 if(name.includes('..')||!fs.existsSync(file)){res.writeHead(404);return res.end();}
 const ext=path.extname(file),type={'.js':'text/javascript','.css':'text/css','.html':'text/html','.svg':'image/svg+xml'}[ext]||'text/plain';
 res.writeHead(200,{'Content-Type':type,'Content-Security-Policy':`default-src 'none'; script-src 'self'; style-src 'self' 'nonce-${nonce}'; style-src-attr 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`});
 res.end(ext==='.html'?fs.readFileSync(file,'utf8').replace('__CSP_NONCE__',nonce):fs.readFileSync(file));
}
(async()=>{
 server=http.createServer((req,res)=>serve(req,res).catch(e=>{fixtureError=e;json(res,{error:'fixture_failure'},500);}));
 await new Promise(r=>server.listen(0,'127.0.0.1',r));origin='http://127.0.0.1:'+server.address().port;
 browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE||undefined});
 const context=await browser.newContext({viewport:{width:1440,height:1000},permissions:['clipboard-read','clipboard-write']}),page=await context.newPage(),errors=[],external=[];
 page.setDefaultTimeout(10000);
 page.on('pageerror',e=>errors.push(e.message));page.on('dialog',d=>{errors.push(d.message());d.dismiss();});
 page.on('request',r=>{if(!r.url().startsWith(origin))external.push(r.url());});
 await page.addInitScript(()=>{window.__csp=[];document.addEventListener('securitypolicyviolation',e=>__csp.push(e.violatedDirective));});
 await page.goto(origin+'/app/');await page.locator('#signin-token').fill('apo_'+'f'.repeat(32));await page.locator('.auth-submit').click();await page.waitForFunction(()=>window.App?.ready);
 const dialog=page.locator('dialog[open]');
 const open=async(id,confirm=false)=>{await page.evaluate(async({id,confirm})=>FinanceScreens.openRequest(id,'payment',confirm),{id,confirm});await dialog.locator('.f-verdict').waitFor();await page.locator('#modal').evaluate(m=>m.scrollTop=0);};
 const states=()=>dialog.locator('.f-journey li').evaluateAll(list=>list.map(li=>li.dataset.state));
 const posts=()=>calls.filter(c=>c.method==='POST');
 for(const width of [1440,393]){
  await page.setViewportSize({width,height:width===393?852:1000});
  for(const [id,label,tone] of [['fine','Looks fine','fine'],['cheaper','Cheaper elsewhere','cheaper'],['risk','Check carefully','careful'],['page','Check carefully','careful'],['low','Check carefully','careful'],['unknown','Check carefully','careful'],['blocked','Blocked by rule','blocked']]){
   await open(id,true);assert.equal(await dialog.locator('.f-verdict strong').innerText(),label);assert.equal(await dialog.locator('.f-verdict').getAttribute('data-verdict'),tone);
   assert.match(await dialog.locator('.f-verdict').innerText(),/advisory: rules and your approval decide/);
  }
  await open('cheaper',true);
  assert.deepEqual(await states(),['done','done','current','not-reached','not-reached','not-reached']);
  const card=dialog.locator('.f-price-card');assert.equal(await card.locator('.f-price-bar').count(),2);
  assert.equal(await dialog.locator('.f-exact-terms .f-merchant-domain').innerText(),'supply.example');
  assert.equal(await dialog.locator('.f-payee-demo-note').count(),0,'Explicit merchant payees have no AgentPal-account note');
  const recorded='Recorded '+await page.evaluate(at=>AgentPalVisuals.date(at),today);
  assert.equal(await card.locator('.f-price-recorded').innerText(),recorded);
  assert.equal(await card.locator('time').getAttribute('datetime'),today);
  assert.match(await card.innerText(),/Everyday Electronics[\s\S]*\$18\.00 USD/);assert.match(await card.innerText(),/\$6\.00 USD \(25\.0%\) less/);
  const bars=await card.locator('.f-price-bar').evaluateAll(list=>list.map(b=>b.getBoundingClientRect().width));assert.ok(Math.abs(bars[1]/bars[0]-.75)<.02);
  assert.deepEqual(await dialog.locator('[data-counter-deal]').evaluateAll(list=>list.map(b=>b.dataset.counterDeal)),['1','2']);
  assert.equal(await dialog.locator('[style]').count(),0,'no inline style introduced');
  await page.screenshot({path:path.join(shots,`awaiting-cheaper-${width}.png`)});
  if(width===393){
   await page.locator('#modal').evaluate(m=>m.scrollTop=m.scrollHeight);await page.waitForFunction(()=>document.querySelector('.f-actions-docked'));
   const b=await dialog.locator('#f-live-confirm').boundingBox(),m=await dialog.boundingBox();assert.ok(b.y>=m.y&&b.y+b.height<=m.y+m.height,'decision docked inside phone dialog');
   const hit=await page.evaluate(()=>{const b=document.querySelector('#f-live-confirm'),r=b.getBoundingClientRect();return document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)===b;});assert.equal(hit,true,'docked approval remains clickable');
  }
  for(const [id,expected] of [
   ['approved',['done','done','done','current','not-reached','not-reached']],['submitted',['done','done','done','current','not-reached','not-reached']],['pending',['done','done','done','current','not-reached','not-reached']],
   ['captured',['done','done','done','done','done','current']],['verified',Array(6).fill('done')],['webhook',Array(6).fill('done')],
   ['blocked',['done','current','not-reached','not-reached','not-reached','not-reached']],['rejected',['done','done','current','not-reached','not-reached','not-reached']],['countered',['done','done','current','not-reached','not-reached','not-reached']],
   ['uncertain',['done','done','done','current','not-reached','not-reached']]]){
   await open(id);assert.deepEqual(await states(),expected,id);
   if(['blocked','rejected','countered'].includes(id))assert.match(await dialog.locator('.f-journey').innerText(),/Blocked by rule|Rejected by owner|Countered by owner/);
   if(id==='verified'){await dialog.locator('.f-journey').scrollIntoViewIfNeeded();await page.screenshot({path:path.join(shots,`verified-${width}.png`)});}
  }
  await open('hostile',true);assert.equal(await dialog.locator('img,script,iframe').count(),0);assert.ok((await dialog.innerText()).includes(hostile));
  assert.equal(await dialog.locator('.f-exact-terms .f-merchant-domain').innerText(),hostile,'Hostile merchant domain is literal text beside the merchant');
  await dialog.locator('details.f-advisory summary').click();assert.ok((await dialog.locator('details').innerText()).includes(hostile));
  assert.equal(await dialog.evaluate(m=>m.scrollWidth<=m.clientWidth+1),true,'dialog has no horizontal overflow');assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),width);
  await open('fine',true);assert.match(await dialog.locator('.f-price-card').innerText(),/Price not compared/);assert.equal(await dialog.locator('.f-price-bar').count(),0);
  assert.equal(await dialog.locator('.f-price-recorded').count(),0);
  await dialog.locator('[data-copy-fingerprint]').click();assert.equal(await page.evaluate(()=>navigator.clipboard.readText()),hash);
  for(const mode of ['paypal_sandbox','simulated','unavailable']){
   await open('payee-'+mode,true);
   const note=dialog.locator('.f-exact-terms dd .f-payee-demo-note');
   assert.equal(await note.count(),mode==='unavailable'?0:1);
   if(mode!=='unavailable')assert.equal(await note.innerText(),"Sandbox demo: payment goes to AgentPal's PayPal sandbox account; no retailer order is placed.");
  }
  await open('unknown',true);assert.equal(await dialog.locator('.f-merchant-domain').count(),0,'Absent domains add no placeholder');
  await open('low',true);assert.equal(await dialog.locator('.f-price-recorded').innerText(),recorded,'Checks without comparable offers still show their recorded time');
  await open('undated',true);assert.equal(await dialog.locator('.f-price-recorded').count(),0,'No timestamp is invented');
 }
 assert.equal(posts().length,0,'opening details never approves');
 await open('changed',true);records.get('changed').terms_sha256='e'.repeat(64);records.get('changed').terms.amount='25.00';
 await dialog.locator('#f-live-confirm').click();await page.getByText('The terms changed. Review the updated details before confirming again.',{exact:true}).waitFor();assert.equal(posts().length,0,'changed terms require another review');
 await dialog.locator('#f-live-confirm').click();await page.waitForFunction(()=>!document.querySelector('dialog[open]'));
 assert.deepEqual(posts(),[{method:'POST',path:'payment-requests/changed/approve',body:{terms_sha256:'e'.repeat(64)}}]);
 await open('cheaper',true);await dialog.locator('#f-live-confirm').click();await page.waitForFunction(()=>!document.querySelector('dialog[open]'));
 assert.deepEqual(posts().at(-1),{method:'POST',path:'payment-requests/cheaper/approve',body:{terms_sha256:hash}});
 assert.deepEqual(await page.evaluate(()=>__csp),[]);assert.deepEqual(errors,[]);assert.deepEqual(external,[]);assert.equal(fixtureError,undefined);
 console.log('PASS: approval verdicts, same-currency price bars, journey/verification/stop states, exact and stale digest approval, clipboard, escaping, sticky phone actions, 1440/393px overflow and production CSP; four synthetic screenshots in '+shots);
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(async()=>{await browser?.close();await new Promise(r=>server?server.close(r):r());});
