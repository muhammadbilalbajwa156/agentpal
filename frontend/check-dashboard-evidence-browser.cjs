/* Real vendored grids/charts under the dashboard CSP. API fixtures only; no provider calls. */
const fs=require('node:fs'),path=require('node:path'),http=require('node:http'),assert=require('node:assert/strict');
const {chromium}=require('playwright');
const shots=process.env.AGENTPAL_TEST_SHOTS;if(shots)fs.mkdirSync(shots,{recursive:true});
const site=path.join(__dirname,'site'),nonce='fxb-test-nonce',today=new Date().toISOString(),calls=[];
const hostile='<img src=x onerror=alert(1)>',hash='123456789abc'+'d'.repeat(52);
const agents=['Atlas','Orbit','Scout'].map((name,i)=>({agent_id:'a'+i,name,description:i===0?'Northstar Digital · Buyer':'',role:'external',status:'active',created_at:today,capabilities:['payment.prepare'],keys:[{key_id:'key-'+i,status:'live',created_at:today}]}));
const payment=(id,status,extra={})=>({request_id:id,agent_id:'a0',provider_mode:'paypal_sandbox',wallet_id:'w0',created_at:today,status,decision_requested:true,terms_sha256:hash,terms:{amount:'10.00',currency:'USD',merchant_name:id,merchant_paypal_email:null,purpose:'Demo supplies'},...extra});
const payments=[payment('captured','provider_confirmed',{owner_decision:{decision:'approved',at:today},verification:{verified_by:'paypal_webhook',at:today}}),payment('blocked','rejected',{policy_block:{rule:'SPLIT_TRANSACTION_EXCEEDS_HARD_LIMIT',label:'Split limit exceeded'}}),payment('declined','rejected',{owner_decision:{decision:'rejected',at:today}}),payment('pending','awaiting_approval',{market_check:{advisory_level:'high_risk',market:{claimed_vs_median:'0.04',median:'250.00',currency:'USD'}}}),payment('old','expired')];
const payouts=[{request_id:'payout',agent_id:'a1',wallet_id:'w1',amount:'3.00',currency:'USD',receiver_email:'sandbox@example.test',purpose:'Demo payout',status:'provider_confirmed',created_at:today,terms_sha256:hash,owner_decision:{decision:'approved',at:today},verification:{verified_by:'paypal_read',at:today}}];
const wallets=agents.map((a,i)=>({wallet_id:'w'+i,agent_id:a.agent_id,currency:'USD',status:'active',limits:{monthly_budget:'100.00',per_transaction_limit:'20.00'},allowance:{spent:i===0?'8.00':i===1?'3.00':'0.00',available:'80.00',reserved:'10.00',pending:'0.00'}}));
let oldTimeline=false,slowReport=false,server,browser,origin;
function json(res,value,status=200){res.writeHead(status,{'Content-Type':'application/json'});res.end(JSON.stringify(value));}
function serve(req,res){
 const url=new URL(req.url,origin||'http://localhost');
 if(url.pathname.startsWith('/v1/')){
  calls.push({method:req.method,path:url.pathname});assert.equal(req.method,'GET','Browser checks never write');
  const p=url.pathname.slice(4);
  const lists={savers:{savers:[]},'setup/status':{mode:'hosted',needs_setup:false,signup:'disabled'},integrations:{integrations:[]},agents:{agents},wallets:{wallets},'payment-requests':{payment_requests:payments},'payout-requests':{payout_requests:payouts},approvals:{approvals:payments.filter(p=>p.status==='awaiting_approval')},health:{status:'ok',payment_environment:'sandbox',is_real_money:false},disputes:{disputes:[]}};
  if(lists[p])return json(res,lists[p]);
  if(p==='paypal/reconciliation'&&slowReport){slowReport=false;return setTimeout(()=>json(res,{start:today,end:today,rows:[]}),1500);}
  if(p==='paypal/reconciliation')return json(res,{start:new Date(Date.now()-86400000).toISOString(),end:today,rows:[{result:'matched',reason:'verified_order_and_search'},{result:'matched',reason:'not_submitted'},{result:'pending',reason:'refund_extension_required'},{result:'mismatch',reason:'amount_mismatch'}]});
  if(p.endsWith('/timeline'))return oldTimeline?json(res,{error:'not_found'},404):json(res,{request_id:p.split('/')[1],events:[{at:today,actor:'agent',kind:'created',label:'Request created',detail:hostile,verified_by:null},{at:today,actor:'paypal',kind:'verified',label:'Sandbox outcome verified',detail:null,verified_by:p.startsWith('payout')?'paypal_read':'paypal_webhook'}]});
  if(p.endsWith('/refunds'))return json(res,{refunds:[{refund_id:'refund',amount:'2.00',currency:'USD',status:'COMPLETED'}]});
  if(p.endsWith('/reconciliation'))return json(res,{provider_references:[{provider:'paypal',provider_order_id:'sandbox-order',status:'completed'}]});
  if(p.endsWith('/transactions'))return json(res,{transactions:[]});
  const record=[...payments,...payouts].find(r=>r.request_id===p.split('/')[1]);if(record)return json(res,record);
  return json(res,{error:'not_found'},404);
 }
 const name=url.pathname.replace(/^\/app\//,'')||'index.html';
 if(name.includes('..')){res.writeHead(404);return res.end();}
 const downloads={'openapi.json':path.join(__dirname,'../docs/api/openapi.json'),'agentpal.postman_collection.json':path.join(__dirname,'../postman/v1.collection.json')};
 const file=downloads[name]||path.join(site,name);if(!fs.existsSync(file)){res.writeHead(404);return res.end();}
 const ext=path.extname(file),type={'.js':'text/javascript','.css':'text/css','.html':'text/html','.json':'application/json','.svg':'image/svg+xml'}[ext]||'text/plain';
 res.writeHead(200,{'Content-Type':type,'Content-Security-Policy':`default-src 'none'; script-src 'self'; style-src 'self' 'nonce-${nonce}'; style-src-attr 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`});
 res.end(ext==='.html'?fs.readFileSync(file,'utf8').replace('__CSP_NONCE__',nonce):fs.readFileSync(file));
}
(async()=>{
 server=http.createServer(serve);await new Promise(r=>server.listen(0,'127.0.0.1',r));origin='http://127.0.0.1:'+server.address().port;
 browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE||undefined});
 const page=await browser.newPage({viewport:{width:1600,height:1000}}),errors=[],csp=[],external=[];
 page.on('pageerror',e=>errors.push(e.message));page.on('console',m=>{if(m.type()==='warning'||m.type()==='error')errors.push(m.text());});page.on('request',r=>{if(!r.url().startsWith(origin))external.push(r.url());});
 await page.addInitScript(()=>{window.__violations=[];document.addEventListener('securitypolicyviolation',e=>window.__violations.push(e.violatedDirective));});
 await page.goto(origin+'/app/');await page.locator('#signin-token').fill('apo_'+'f'.repeat(32));await page.locator('.auth-submit').click();await page.waitForFunction(()=>window.App?.ready);await page.getByRole('button',{name:'Skip setup',exact:true}).click();
 // Capture actual chart options and grid APIs without replacing either library.
 await page.evaluate(()=>{window.__grids={};window.__charts={};const original=agGrid;window.agGrid={...original,createGrid:(el,options)=>{const api=original.createGrid(el,options);__grids[el.id]=api;return api;}};const chart=agCharts.AgCharts.create;agCharts.AgCharts.create=options=>{__charts[options.container.id]=options;return chart.call(agCharts.AgCharts,options);};});
 const navigate=async route=>{await page.evaluate(r=>App.navigate(r),route);await page.waitForTimeout(150);assert.equal(await page.getByText('This page could not load',{exact:true}).count(),0,route+' '+JSON.stringify(errors));};
 await navigate('approvals');assert.match(await page.locator('.f-stats').innerText(),/Approved requests\s+2/);assert.match(await page.locator('.f-stats').innerText(),/Rejected requests\s+1/);assert.match(await page.locator('.f-stats').innerText(),/Blocked by rules\s+1/);
 assert.match(await page.locator('#decision-history').innerText(),/captured/);assert.match(await page.locator('#decision-history').innerText(),/Blocked by rule/);
 await page.locator('[data-live-approve=pending]').click();await page.locator('dialog[open] .f-timeline').waitFor();const approval=await page.locator('dialog').innerText();assert.match(approval,/123456789abc/);assert.match(approval,/Any change to amount, payee or purpose voids this approval\./);assert.match(approval,/AgentPal's PayPal sandbox business account/);assert.match(approval,/Below market — check seller/);assert.ok((await page.locator('dialog').innerHTML()).includes('&lt;img'));assert.equal(await page.locator('dialog img').count(),0);if(shots)await page.screenshot({path:path.join(shots,'approval.png'),fullPage:true});await page.locator('#close-modal').click();
 await navigate('transactions');assert.equal(await page.locator('#transaction-company option').count(),3);await page.locator('#transaction-company').selectOption('Lumen Cloud');assert.equal(await page.locator('#transaction-ledger .ag-row').count(),1);await page.locator('#transaction-company').selectOption('all');
 await page.locator('[data-live-net]').click();await page.waitForFunction(()=>document.querySelector('.f-reconciliation')?.textContent.includes('$8.00'));assert.match(await page.locator('.f-reconciliation').innerText(),/Net confirmed payments \(captured minus refunds\): \$8.00/);assert.match(await page.locator('.f-reconciliation').innerText(),/Confirmed payouts: \$3.00/);
 await page.locator('[data-live-report]').click();await page.waitForFunction(()=>document.querySelector('#reconciliation-summary')?.textContent.includes('1 matched'));assert.match(await page.locator('#reconciliation-summary').innerText(),/1 mismatched.*1 pending.*1 never submitted/);if(shots)await page.screenshot({path:path.join(shots,'transactions-desktop.png'),fullPage:true});
 {const before=errors.length;slowReport=true;await page.locator('[data-live-report]').click();await page.evaluate(()=>{location.hash='overview'});await page.waitForTimeout(2200);assert.deepEqual(errors.slice(before),[],'Leaving Transactions while a comparison loads must not throw');await page.evaluate(()=>{location.hash='transactions'});await page.waitForSelector('#reconciliation-summary');}
 async function details(id){await page.locator(`#transaction-ledger .ag-row[row-id="${id}"] button`).click();await page.locator('dialog[open] .f-timeline').waitFor();}
 await details('captured');assert.match(await page.locator('dialog').innerText(),/Verified by PayPal webhook/);await page.locator('#close-modal').click();
 await details('payout');assert.match(await page.locator('dialog').innerText(),/Verified by PayPal read/);await page.locator('#close-modal').click();
 for(const id of ['blocked','declined','pending','old']){await details(id);await page.locator('#close-modal').click();assert.ok(!calls.some(c=>c.path===`/v1/payment-requests/${id}/reconciliation`),'No reconciliation read before execution: '+id);}
 // Marker is necessary for a single-day series. Company grouping preserves separate series/currencies.
 await navigate('analytics');const options=await page.evaluate(()=>({daily:__charts['daily-confirmed-spend'],company:__charts['spend-by-company']}));assert.equal(options.daily.data.length,1);assert.equal(options.daily.series[0].marker.enabled,true);assert.deepEqual(options.company.data.map(r=>r.name).sort(),['Lumen Cloud','Northstar Digital']);
 await navigate('agents');assert.equal(await page.locator('#a-company option').count(),4);await page.locator('#a-company').selectOption('Independent');assert.equal(await page.locator('#connected-agents .ag-row').count(),1);await page.locator('#a-company').selectOption('all');
 await page.locator('.a-more').first().click();assert.doesNotMatch(await page.locator('dialog').innerText(),/ · live/);await page.locator('#close-modal').click();
 await navigate('security');assert.equal(await page.locator('.f-principles li').count(),12);assert.doesNotMatch(await page.locator('.ad-live-credential').first().innerText(),/ · live/);
 await navigate('integrations');const text=await page.locator('#screen').innerText();for(const name of ['Built with','PayPal sandbox','Channel3','Kernel','Render + Render Workflows','Astropods','AG Grid / AG Charts','Groq','Postman'])assert.ok(text.includes(name),name);assert.equal(await page.locator('.f-runtime article').count(),2);
 await page.locator('[data-action=documentation]').click();assert.equal(await page.locator('[data-admin=api]').count(),1);for(const href of ['./openapi.json','./agentpal.postman_collection.json']){assert.equal(await page.locator(`a[href="${href}"]`).count(),1);const response=await page.request.get(origin+'/app/'+href.slice(2));assert.match(response.headers()['content-type'],/application\/json/);assert.equal(response.status(),200);}assert.match(await page.locator('#screen').innerText(),/sdk\/python/);
 for(const width of [393,390]){
  await page.setViewportSize({width,height:844});for(const [route,id,fields] of [['transactions','transaction-ledger',['amount','status']],['approvals','decision-history',['amount','status']],['agents','connected-agents',['spent','status']]]){await navigate(route);for(const field of fields){const box=await page.locator(`#${id} .ag-row`).first().locator(`[col-id="${field}"]`).boundingBox();assert.ok(box&&box.x>=0&&box.x+box.width<=width,`${route} ${field} visible at ${width}: ${JSON.stringify(box)}`);}assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),width,route+' no phone overflow');if(shots&&width===393)await page.screenshot({path:path.join(shots,route+'-phone.png'),fullPage:true});}
 }
 await page.setViewportSize({width:1600,height:1000});await navigate('transactions');await page.setViewportSize({width:393,height:844});await page.waitForTimeout(100);const order=await page.evaluate(()=>__grids['transaction-ledger'].getColumnState().map(c=>c.colId));assert.deepEqual(order.slice(0,2),['amount','status'],'Existing grid adapts on resize');
 csp.push(...await page.evaluate(()=>window.__violations));assert.deepEqual(csp,[]);assert.deepEqual(external,[]);assert.deepEqual(errors,[]);
 // Older API: hide the absent timeline; the intentional 404 is the only resource error expected here.
 oldTimeline=true;await page.setViewportSize({width:1600,height:1000});await navigate('transactions');await page.locator('#transaction-ledger .ag-row[row-id="old"] button').click();await page.getByRole('heading',{name:'Transaction details',exact:true}).waitFor();assert.equal(await page.locator('dialog .f-timeline').count(),0);assert.match(await page.locator('dialog').innerText(),/old/);
 console.log('PASS: dashboard contract, owner counts vs rule blocks, escaped payment/payout timelines and 404 fallback, exact approval terms, verification, payee and market labels, read-only reconciliation, gross/refund/net/payout totals, company filters/chart, one-day markers, resources/principles/runtime panels; real grids at 393/390px, resize, strict CSP and no external scripts.');
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(async()=>{await browser?.close();await new Promise(r=>server?server.close(r):r());});
