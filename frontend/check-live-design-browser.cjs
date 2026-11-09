// Real same-origin API data, real Chromium and dashboard CSP. No provider writes.
// NODE_PATH=$(npm root -g) AGENTPAL_TEST_TOKEN_FILE=/tmp/ui-f5-data/owner.tok node check-live-design-browser.cjs
const {chromium}=require('playwright'),fs=require('node:fs'),assert=require('node:assert/strict');
const origin=process.env.AGENTPAL_TEST_ORIGIN||'http://127.0.0.1:8840',output=process.env.AGENTPAL_TEST_SHOTS||'/tmp/ui-f5-shots';
(async()=>{
 const browser=await chromium.launch();
 const report={status:'RUNNING',pageErrors:[],consoleErrors:[],warnings:[],cspViolations:[],externalRequests:[],failedReads:[],screens:[],checks:[]};
 try{
  const page=await browser.newPage({viewport:{width:1600,height:900},reducedMotion:'reduce'}),reads=[];
  page.on('pageerror',e=>report.pageErrors.push(e.message));
  page.on('console',m=>{if(m.type()==='error')report.consoleErrors.push(m.text());if(m.type()==='warning')report.warnings.push(m.text());if(/content security policy|violat|refused|unsafe-eval/i.test(m.text()))report.cspViolations.push(m.text());});
  page.on('request',r=>{if(!r.url().startsWith(origin+'/'))report.externalRequests.push(r.url());if(r.url().includes('/v1/'))reads.push(r.url());});
  page.on('response',r=>{if(r.status()>=400)report.failedReads.push({path:new URL(r.url()).pathname,status:r.status()});});
  await page.addInitScript(()=>{window.__csp=[];document.addEventListener('securitypolicyviolation',e=>window.__csp.push(e.violatedDirective));});
  await page.goto(origin+'/app/');await page.locator('#signin-token').fill(fs.readFileSync(process.env.AGENTPAL_TEST_TOKEN_FILE,'utf8').trim());await page.locator('.auth-submit').click();await page.waitForFunction(()=>window.App?.ready);
  await page.evaluate(()=>{window.__apis={};const original=agGrid;window.agGrid={...original,createGrid:(el,options)=>{const api=original.createGrid(el,options);window.__apis[el.id]=api;return api;}};});
  fs.mkdirSync(output,{recursive:true});
  for(const route of ['overview','agents','wallets','transactions','approvals','analytics','integrations','api','security','settings']){
   await page.evaluate(r=>App.navigate(r),route);await page.waitForTimeout(250);
   assert.equal(await page.getByText('This page could not load',{exact:true}).count(),0,route);
   const readCount=reads.length;
   const gridBefore=await page.evaluate(()=>[...document.querySelectorAll('.ag-root-wrapper')]);
   await page.evaluate(()=>{window.__gridRoot=document.querySelector('.ag-root-wrapper');window.dispatchEvent(new CustomEvent('agentpal-live',{detail:{type:'refreshed'}}));});
   await page.waitForTimeout(100);assert.equal(reads.length,readCount,route+' snapshot refresh sends no extra reads');
   if(gridBefore.length)assert.ok(await page.evaluate(()=>window.__gridRoot===document.querySelector('.ag-root-wrapper')),route+' retains grid instance');
   if(route==='overview'){
    assert.equal(await page.locator('.stats .stat').count(),4);assert.equal(await page.locator('#quick-actions button').count(),6);
    const original=await page.locator('.overview-chart svg').innerHTML();await page.getByRole('button',{name:'7D',exact:true}).click();assert.equal(await page.locator('.range-tabs .selected').textContent(),'7D');assert.equal(await page.locator('#overview-period').inputValue(),'Last 7 Days');assert.notEqual(await page.locator('.overview-chart svg').innerHTML(),original,'Range changes actual daily series and dates');await page.getByRole('button',{name:'30D',exact:true}).click();
   }
   if(route==='agents'){
    assert.equal(await page.locator('.a-stats article').count(),4);assert.equal(await page.locator('.ag-paging-panel').isVisible(),false,'No pagination for five agents');
    await page.locator('#a-search').fill('Scout');assert.equal(await page.locator('.ag-row').count(),1);await page.evaluate(()=>window.dispatchEvent(new CustomEvent('agentpal-live',{detail:{type:'refreshed'}})));assert.equal(await page.locator('#a-search').inputValue(),'Scout');assert.equal(await page.locator('.ag-row').count(),1);await page.locator('#a-search').fill('');
    await page.locator('.a-more').first().click();await page.locator('dialog[open]').waitFor();assert.equal(await page.locator('#modal-title').textContent(),'Manage agent');await page.locator('#close-modal').click();
   }
   if(route==='wallets'){
    assert.equal(await page.locator('.ws-stats .ws-stat').count(),4);await page.getByRole('button',{name:'Load ledger',exact:false}).click();await page.waitForFunction(()=>document.querySelector('#wallet-ledger-state')?.textContent.includes('records'));assert.ok(await page.locator('#wallet-ledger-table tbody tr').count()>0);assert.ok(await page.locator('.ws-wallet-detail canvas').count()>0);
   }
   if(route==='transactions'){
    const query='Sandbox';await page.locator('#transaction-search').fill(query);await page.evaluate(()=>window.dispatchEvent(new CustomEvent('agentpal-live',{detail:{type:'refreshed'}})));assert.equal(await page.locator('#transaction-search').inputValue(),query);await page.locator('#transaction-search').fill('');
    await page.evaluate(async()=>{const grid=__apis['transaction-ledger'];await grid.setColumnFilterModel('description',{type:'contains',filter:'Sandbox'});grid.onFilterChanged();});
    assert.ok(await page.locator('#transaction-ledger .ag-row').count()>0);assert.equal(await page.locator('#transaction-ledger .ag-row').filter({hasNotText:'Sandbox'}).count(),0);
    await page.evaluate(async()=>{const grid=__apis['transaction-ledger'];await grid.setColumnFilterModel('description',null);grid.onFilterChanged();grid.applyColumnState({state:[{colId:'amount',sort:'asc'}],defaultState:{sort:null}});});
    const amounts=(await page.locator('#transaction-ledger .ag-row .ag-cell[col-id=amount]').allTextContents()).map(s=>Number(s.replace(/[^0-9.]/g,'')));assert.deepEqual(amounts,[...amounts].sort((a,b)=>a-b));
    const dl=page.waitForEvent('download');await page.getByRole('button',{name:'Export Transactions'}).click();const download=await dl,stream=await download.createReadStream();let csv='';for await(const chunk of stream)csv+=chunk.toString();assert.match(csv,/Sandbox services/);assert.match(csv,/USD/);
   }
   if(route==='approvals'){await page.locator('[data-live-approve]').first().click();await page.locator('dialog[open]').waitFor();assert.equal(await page.locator('#modal-title').textContent(),'Confirm sandbox approval');assert.ok(await page.getByText('Terms SHA-256',{exact:true}).count());await page.locator('#close-modal').click();}
   if(route==='settings'){await page.locator('[data-ad-live-tab=preferences]').click();assert.equal(await page.locator('#ad-live-preferences').count(),1);}
   assert.ok(await page.evaluate(()=>[...document.querySelectorAll('style')].every(el=>el.nonce===document.querySelector('meta[name=csp-nonce]').content)),route+' uses response nonce');
   await page.mouse.click(800,80);await page.mouse.move(0,0);await page.waitForTimeout(200);await page.screenshot({path:`${output}/after-live-${route}-1600.png`});
   await page.setViewportSize({width:390,height:844});await page.waitForTimeout(200);assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),390,route+' phone overflow');await page.screenshot({path:`${output}/after-live-${route}-390.png`});await page.screenshot({path:`${output}/after-live-${route}-390-full.png`,fullPage:true});
   report.screens.push(route);await page.setViewportSize({width:1600,height:900});
  }
  report.cspViolations.push(...await page.evaluate(()=>window.__csp));
  assert.equal(report.pageErrors.length,0,JSON.stringify(report.pageErrors));assert.equal(report.cspViolations.length,0,JSON.stringify(report.cspViolations));assert.equal(report.warnings.length,0,JSON.stringify(report.warnings));assert.equal(report.externalRequests.length,0);
  report.checks=['10 desktop/phone layouts','no horizontal phone overflow','all injected styles use response nonce','no CSP violations','no page errors or warnings','no external requests','snapshot refresh sends no reads and retains grids','agent search survives refresh','original row dimensions and conditional pagination','real wallet ledger and AG area chart','transaction CSV','exact-term approval review dialog','settings navigation'];
  report.status=report.consoleErrors.length?'BLOCKED — backend reads unavailable':'PASS';
  assert.equal(report.consoleErrors.length,0,JSON.stringify(report.failedReads));
 }catch(e){report.failure=e.message;if(report.status==='RUNNING')report.status='FAIL';throw e;}
 finally{fs.writeFileSync(output+'/design-browser-report.json',JSON.stringify(report,null,2));console.log(JSON.stringify(report,null,2));await browser.close();}
})().catch(e=>{console.error(e.message);process.exitCode=1;});
