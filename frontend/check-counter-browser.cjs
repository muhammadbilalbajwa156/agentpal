/* Owner counter-offer ("Buy this instead") in the real dashboard under the production CSP,
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
async function serve(req,res){
 const url=new URL(req.url,origin||'http://localhost'),p=url.pathname.slice(4);
 if(url.pathname.startsWith('/v1/')){
  let raw='';for await(const chunk of req)raw+=chunk;const body=raw?JSON.parse(raw):null;calls.push({method:req.method,path:p,body});
  if(p==='setup/status')return json(res,{mode:'hosted',needs_setup:false,signup:'disabled'});
  assert.equal(req.headers.authorization,'Bearer apo_'+'f'.repeat(32));
  const all=[...records.values()];
  const lists={savers:{savers:[]},agents:{agents:[agent]},wallets:{wallets:[wallet]},'payment-requests':{payment_requests:all},'payout-requests':{payout_requests:[]},
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
const posts=(from,suffix)=>calls.slice(from).filter(c=>c.method==='POST'&&c.path.endsWith(suffix));
(async()=>{
 server=http.createServer((req,res)=>serve(req,res).catch(e=>{console.error(e);json(res,{error:'fixture_failure'},500);}));await new Promise(r=>server.listen(0,'127.0.0.1',r));origin='http://127.0.0.1:'+server.address().port;
 browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE||undefined});
 const page=await browser.newPage({viewport:{width:393,height:852}}),errors=[];
 page.on('pageerror',e=>errors.push(e.message));page.on('dialog',d=>{errors.push('unexpected dialog: '+d.message());d.dismiss();});
 await page.addInitScript(()=>{window.__csp=[];document.addEventListener('securitypolicyviolation',e=>__csp.push(e.violatedDirective));});
 await page.goto(origin+'/app/');await page.locator('#signin-token').fill('apo_'+'f'.repeat(32));await page.locator('.auth-submit').click();await page.waitForFunction(()=>window.App?.ready);
 const dialog=page.locator('dialog[open]'),refresh=()=>page.evaluate(()=>AgentPalAPI.refresh());

 // Overview: no verified saving exists yet, so none is claimed.
 const tile=page.locator('[data-verified-savings]');
 assert.equal(await tile.count(),1);assert.match(await tile.innerText(),/Verified savings/i);assert.match(await tile.innerText(),/None yet/);assert.doesNotMatch(await tile.innerText(),/\$/);
 assert.equal(await page.locator('.stats .stat').count(),4,'existing overview tiles are unchanged');

 // Approvals card -> exact-terms dialog: decision first, counter below it, deals listed.
 await page.evaluate(()=>App.navigate('approvals'));
 assert.equal(await page.locator('[data-live-counter="pay_orig"]').count(),1,'Counter button on the pending card');
 await page.locator('[data-live-approve="pay_orig"]').click();await page.locator('#f-live-confirm').waitFor();
 const box=async sel=>(await page.locator(sel).boundingBox());
 assert.ok((await box('#f-live-confirm')).y<852,'Approve stays reachable on a phone');
 const tops=await page.evaluate(()=>({confirm:document.querySelector('#f-live-confirm').getBoundingClientRect().top,counter:document.querySelector('.f-counter').getBoundingClientRect().top,evidence:document.querySelector('details.f-advisory').getBoundingClientRect().top}));
 assert.ok(tops.confirm<tops.counter&&tops.counter<tops.evidence,'decision, then counter, then advisory evidence');
 assert.match(await page.locator('.f-counter').innerText(),/A counter approves nothing/);
 const deals=page.locator('[data-counter-deal]');
 assert.equal(await deals.count(),2,'only same-currency deals can be countered');
 assert.deepEqual(await deals.evaluateAll(list=>list.map(b=>b.dataset.counterDeal)),['1','2'],'buttons carry the stored deal index');
 assert.doesNotMatch(await page.locator('.f-counter-deals').innerText(),/eur-shop/);
 assert.equal(await page.locator('.f-counter-deals img').count(),0,'retailer text is escaped');
 assert.equal(await page.locator('#f-live-counter-form').isVisible(),false,'manual form is closed until asked for');
 assert.match(await page.locator('.f-counter-deals li').first().innerText(),/known-retailer\.example[\s\S]*\$16\.49 USD[\s\S]*\$8\.50 USD less/);

 // One tap: "Counter with this deal". Only the index is sent; nothing is approved.
 let start=calls.length;
 await deals.first().click();
 await page.waitForFunction(()=>/You countered/.test(document.querySelector('dialog[open]')?.textContent||''));
 const sent=posts(start,'/counter');
 assert.equal(sent.length,1);assert.equal(sent[0].path,'payment-requests/pay_orig/counter');assert.deepEqual(sent[0].body,{deal_index:1});
 assert.equal(posts(start,'/approve').length,0,'countering approves nothing');
 const replacement=[...records.values()].find(r=>r.counter_of==='pay_orig');
 assert.equal(replacement.status,'awaiting_approval');
 let text=await dialog.innerText();
 assert.match(text,/Confirm sandbox approval/);assert.match(text,/\$16\.49 USD/);assert.match(text,/known-retailer\.example/);
 assert.match(text,/Owner counter-offer/);assert.match(text,/Replaces\s+pay_orig/);
 assert.ok(text.includes(replacement.terms_sha256.slice(0,12)),'the replacement shows its OWN fingerprint');
 assert.ok(!text.includes(records.get('pay_orig').terms_sha256.slice(0,12)));
 const saving=dialog.locator('.f-counter-saving');
 assert.equal(await saving.getAttribute('data-saving'),'unverified');
 assert.match(await saving.innerText(),/\$8\.50 USD below the agent's request/);assert.match(await saving.innerText(),/not a saving until PayPal verifies/);
 assert.doesNotMatch(text,/Saved \$|verified by PayPal\b(?! verifies)/i);

 // The owner approves the replacement's own digest through the unchanged approve call.
 start=calls.length;await page.locator('#f-live-confirm').click();await page.waitForFunction(()=>!document.querySelector('dialog[open]'));
 assert.deepEqual(posts(start,'/approve').map(c=>[c.path,c.body]),[['payment-requests/'+replacement.request_id+'/approve',{terms_sha256:replacement.terms_sha256}]]);
 assert.equal(records.get('pay_orig').status,'rejected');

 // Original: "Countered by owner" with a working link to the replacement. No counter UI.
 await page.evaluate(()=>FinanceScreens.openRequest('pay_orig','payment'));await dialog.locator('.f-counter-link').waitFor();
 text=await dialog.innerText();assert.match(text,/Countered by owner/);assert.match(text,/The owner countered with different terms\./);
 assert.equal(await dialog.locator('.f-counter,[data-counter-deal],#f-live-counter').count(),0,'a decided request cannot be countered from the UI');
 await dialog.locator('[data-counter-open]').click();
 await page.waitForFunction(id=>document.querySelector('dialog[open] .f-counter-link')?.textContent.includes('Replaces')&&document.querySelector('dialog[open]').textContent.includes(id),replacement.request_id);
 await page.locator('#close-modal').click();

 // Captured but NOT verified by PayPal: still no saving anywhere.
 replacement.status='provider_confirmed';await refresh();
 await page.evaluate(()=>App.navigate('overview'));
 assert.match(await tile.innerText(),/None yet/);
 // PayPal-verified: the server flag is the only thing that turns it into a saving.
 replacement.verification={verified_by:'paypal_read',at:today};replacement.counter_saving_verified=true;await refresh();
 await page.evaluate(()=>App.navigate('overview'));
 assert.match(await tile.innerText(),/\$8\.50 USD/);assert.match(await tile.innerText(),/PayPal-verified captures only/);
 await page.evaluate(id=>FinanceScreens.openRequest(id,'payment'),replacement.request_id);await dialog.locator('.f-counter-saving').waitFor();
 assert.equal(await dialog.locator('.f-counter-saving').getAttribute('data-saving'),'verified');
 assert.match(await dialog.locator('.f-counter-saving').innerText(),/Saved \$8\.50 USD against the agent's request[\s\S]*verified by PayPal/);
 await page.locator('#close-modal').click();
 // A dearer counter is never counted as a saving.
 add(make('pay_dear','30.00','Dear Shop',{status:'provider_confirmed',counter_of:'pay_x',counter_saving:'-5.01',counter_saving_verified:true,verification:{verified_by:'paypal_read',at:today}}));
 add(make('pay_plain','7.00','Plain Shop',{status:'provider_confirmed',verification:{verified_by:'paypal_read',at:today}}));
 await refresh();await page.evaluate(()=>App.navigate('overview'));
 assert.match(await tile.innerText(),/\$8\.50 USD/);assert.doesNotMatch(await tile.innerText(),/3\.49|13\.51|15\.50/);
 await page.evaluate(()=>FinanceScreens.openRequest('pay_dear','payment'));await dialog.locator('.f-counter-saving').waitFor();
 assert.match(await dialog.locator('.f-counter-saving').innerText(),/\$5\.01 USD more than the agent's request/);assert.doesNotMatch(await dialog.innerText(),/Saved/);
 await page.locator('#close-modal').click();

 // Manual Counter form from the pending card.
 add(make('pay_two','20.00','Second Shop'));await refresh();
 await page.evaluate(()=>App.navigate('approvals'));
 await page.locator('[data-live-counter="pay_two"]').click();await page.locator('#f-live-counter-form').waitFor({state:'visible'});
 assert.equal(await page.locator('#f-live-counter').getAttribute('aria-expanded'),'true');
 assert.equal(await dialog.locator('[data-counter-deal]').count(),0,'no market check, no deal buttons');
 const field=name=>page.locator(`#f-live-counter-form [name=${name}]`),submit=page.locator('#f-live-counter-form button[type=submit]'),alert=page.locator('#f-live-counter-error');
 assert.equal(await field('amount').inputValue(),'20.00');assert.equal(await field('merchant_name').inputValue(),'Second Shop');
 assert.ok((await box('#f-live-counter-form [name=amount]')).width<=393);
 for(const bad of ['0','-1','1e1','12.345','abc','']){start=calls.length;await field('amount').fill(bad);await submit.click();assert.match(await alert.innerText(),/positive amount/);assert.equal(posts(start,'/counter').length,0,'invalid amount "'+bad+'" is not sent');}
 await field('amount').fill('12.00');await field('merchant_name').fill('   ');start=calls.length;await submit.click();assert.match(await alert.innerText(),/merchant name/);assert.equal(posts(start,'/counter').length,0);
 // The server's refusal is shown in place and nothing changes.
 sameTermsOnce=true;await field('merchant_name').fill('Second Shop');await field('amount').fill('20.00');start=calls.length;await submit.click();
 await page.waitForFunction(()=>/same|terms the agent asked for/i.test(document.querySelector('#f-live-counter-error')?.textContent||''));
 assert.equal(records.get('pay_two').status,'awaiting_approval');assert.equal(posts(start,'/approve').length,0);
 await field('amount').fill('12.00');await field('merchant_name').fill('Better Shop');await field('merchant_domain').fill('better-shop.example');await field('merchant_paypal_email').fill('');
 start=calls.length;await submit.click();await page.waitForFunction(()=>/You countered/.test(document.querySelector('dialog[open]')?.textContent||''));
 assert.deepEqual(posts(start,'/counter').map(c=>c.body),[{amount:'12.00',merchant_name:'Better Shop',merchant_domain:'better-shop.example'}]);
 assert.equal(posts(start,'/approve').length,0);
 text=await dialog.innerText();assert.match(text,/\$12\.00 USD/);assert.match(text,/\$8\.00 USD below the agent's request/);
 await page.locator('#close-modal').click();
 assert.equal(await page.locator('.f-request .ap-pill').filter({hasText:'Owner counter-offer'}).count(),1,'pending replacement is badged');

 // Test Lab hand-over: the walkthrough follows the replacement; execute needs its approval.
 await page.evaluate(()=>App.navigate('lab'));
 await page.locator('#lab-amount').fill('24.99');await page.locator('#lab-merchant').fill('Unfamiliar Shop');await page.locator('#lab-purpose').fill('USB-C hub');await page.locator('#lab-payment-form button').click();
 await page.waitForFunction(()=>document.querySelector('#lab-result')?.textContent.includes('pay_lab'));
 await page.locator('[data-lab-action=approve]').click();await page.locator('[data-counter-deal="1"]').waitFor();
 start=calls.length;await page.locator('[data-counter-deal="1"]').click();await page.waitForFunction(()=>/You countered/.test(document.querySelector('dialog[open]')?.textContent||''));
 const labNew=[...records.values()].find(r=>r.counter_of==='pay_lab');
 await page.locator('#close-modal').click();
 await page.waitForFunction(()=>/The owner countered/.test(document.querySelector('#lab-result')?.textContent||''));
 assert.equal(await page.locator('[data-lab-action=execute]').isDisabled(),true,'the declined original cannot be executed');
 await page.locator('[data-lab-action=follow-counter]').click();
 await page.waitForFunction(id=>document.querySelector('#lab-result strong')?.textContent===id,labNew.request_id);
 assert.match(await page.locator('#lab-result').innerText(),/Owner counter-offer replacing pay_lab/);
 assert.equal(await page.locator('[data-lab-action=execute]').isDisabled(),true,'the counter alone does not make the replacement executable');
 assert.equal(posts(start,'/execute').length,0);
 await page.locator('[data-lab-action=approve]').click();await page.locator('#f-live-confirm').click();await page.waitForFunction(()=>!document.querySelector('dialog[open]'));
 await page.waitForFunction(()=>!document.querySelector('[data-lab-action=execute]')?.disabled);
 await page.locator('[data-lab-action=execute]').click();
 await page.waitForFunction(()=>/Submitted/.test(document.querySelector('.lab-steps')?.textContent||''));
 assert.deepEqual(posts(start,'/execute').map(c=>c.path),['lab/payment-requests/'+labNew.request_id+'/execute']);
 assert.equal(labNew.terms.amount,'16.49','executed exactly the countered terms');

 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),393);assert.deepEqual(await page.evaluate(()=>__csp),[]);assert.deepEqual(errors,[]);
 console.log('PASS: counter-offer UI — deal counter sends only the stored index, approves nothing, own-fingerprint approval, countered/replacement badges and links, savings shown only when PayPal-verified (request + Overview tile), manual form validation and server refusals, Test Lab hand-over, phone layout, escaping and CSP.');
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(async()=>{await browser?.close();await new Promise(r=>server?server.close(r):r());});
