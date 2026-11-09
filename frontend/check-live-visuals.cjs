/* Fail-first boundaries: text-only API cells, spreadsheet formulas, currency isolation and lifecycle. */
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=process.env.AGENTPAL_VISUAL_SOURCE||'site/live-visuals.js';
let creates=0,destroys=0,updates=0,chartCreates=0,chartUpdates=0,chartDestroys=0;
const listeners={},nodes=new Map();
function node(id){if(!nodes.has(id))nodes.set(id,{id,isConnected:true,dataset:{empty:'No records'},textContent:'',childNodes:[],contains:()=>false,append(child){this.childNodes.push(child);},setAttribute(k,v){this[k]=v;}});return nodes.get(id);}
const payload='<img src=x onerror=alert(1)>',rowData=[{id:'one',name:payload,amount:'0.10',currency:'USD'}];
const data={agents:[{agent_id:'agt',name:payload}],wallets:[],payments:[],payouts:[]};
const c={console,Intl,Date,Map,BigInt,Number,String,Set,Array,JSON,window:null,document:{querySelector:()=>({content:'nonce-test'}),getElementById:node,createElement:()=>node('element-'+nodes.size)},App:{escape:v=>String(v).replaceAll('<','&lt;').replaceAll('>','&gt;')},AgentPalAPI:{data,money:(a,c)=>c+' '+a},matchMedia:()=>({matches:true}),addEventListener:(type,fn)=>listeners[type]=fn,
 agGrid:{createGrid(el,options){creates++;el.options=options;return {setGridOption(key,value){updates++;options[key]=value;},destroy(){destroys++;}};}},
 agCharts:{AgCharts:{create(options){chartCreates++;node('chart').options=options;return {update(){chartUpdates++;},destroy(){chartDestroys++;}};}}}};
c.window=c;vm.createContext(c);vm.runInContext(fs.readFileSync(source,'utf8'),c);const v=c.AgentPalVisuals;
const options=v.grid('ledger',[{field:'name'}],()=>rowData);assert.equal(creates,1);v.grid('ledger',[],()=>[]);assert.equal(creates,1);
const grid=node('ledger').options;assert.equal(grid.styleNonce,'nonce-test');assert.equal(grid.theme,'legacy');assert.equal(grid.defaultColDef.cellRenderer({value:payload}).textContent,payload);assert.equal(grid.defaultColDef.cellRenderer({value:payload}).innerHTML,undefined);
assert.equal(v.status({value:payload}).textContent,payload);assert.equal(v.tags({value:[payload]}).childNodes[0].textContent,payload);
for(const prefix of ['=','+','-','@','\t','\r'])assert.equal(grid.defaultCsvExportParams.processCellCallback({value:prefix+'1+1'}),"'"+prefix+'1+1');
assert.equal(v.csvSafe('plain'),'plain');assert.equal(v.csvSafe('12.30'),'12.30');
assert.equal(v.compareDecimal('9007199254740992.01','9007199254740992.02'),-1);
const grouped=v.aggregate([{amount:'0.10',currency:'USD',name:'A'},{amount:'0.20',currency:'USD',name:'A'},{amount:'1.00',currency:'EUR',name:'A'}],r=>r.name);
assert.equal(grouped[0].amount,'0.30000000');assert.equal(grouped[1].currency,'EUR');assert.equal(grouped[1].amount,'1.00000000');
assert.equal(v.date('invalid'),'Unavailable');assert.ok(!v.date('2026-11-06T12:00:00Z').includes('T12:00'));
listeners['agentpal-live']({detail:{type:'refreshed'}});assert.equal(updates,1);assert.equal(creates,1);
rowData.push({id:'two'});listeners['agentpal-live']({detail:{type:'refreshed'}});assert.equal(grid.rowData.length,2);assert.equal(creates,1);
// No fabricated spend; approved payments do not enter confirmed plots.
data.payments=[{status:'approved',created_at:'2026-11-06T12:00:00Z',terms:{amount:'50.00',currency:'USD',agent_id:'agt'}}];assert.equal(v.daily().length,0);
v.mountDaily('chart');assert.equal(chartCreates,0);assert.equal(node('chart-empty').hidden,false);
data.payments[0].status='provider_confirmed';listeners['agentpal-live']({detail:{type:'refreshed'}});assert.equal(chartCreates,1);assert.equal(node('chart').options.styleNonce,'nonce-test');assert.equal(node('chart').options.animation,undefined); // Community build: no animation module (Enterprise-only), so nothing animates
listeners['agentpal-live']({detail:{type:'refreshed'}});assert.equal(chartCreates,1);assert.equal(chartUpdates,1);
assert.equal(node('chart').options.series[0].marker.enabled,true,'A single-day series needs a visible point');
assert.equal(v.company({metadata:{company:'Explicit company'},name:'Atlas'}),'Explicit company');
assert.equal(v.company({role:'Acme · Buyer',name:'Scout'}),'Acme');
for(const name of ['Atlas','Forge','Muse','Ledger'])assert.equal(v.company({name}),'Northstar Digital');
for(const name of ['Orbit','Pulse','Keel'])assert.equal(v.company({name}),'Lumen Cloud');
assert.equal(v.company({name:'Scout'}),'Independent');
assert.equal(v.live.relative(new Date(Date.now()-86400000).toISOString()),'1 day ago');
data.payments=[{request_id:'captured',status:'provider_confirmed',terms:{amount:'10.00',currency:'USD'}},{request_id:'reversed',status:'reversed',terms:{amount:'2.00',currency:'EUR'}}];data.refunds={captured:[{status:'COMPLETED',amount:'1.25'},{status:'PENDING',amount:'4.00'},{status:'FAILED',amount:'3.00'}]};
const spend=v.live.paymentSpend();assert.equal(spend.complete,true);assert.equal(spend.net[0].amount,'8.75000000');assert.equal(spend.net[1].amount,'0.00000000');assert.equal(spend.refunds[1].amount,'2.00');
delete data.refunds.captured;assert.equal(v.live.paymentSpend().complete,false,'Do not assume absent refund data means zero refunds');
const root={contains:()=>true};v.destroyWithin(root);assert.equal(destroys,1);assert.equal(chartDestroys,1);
c.document.querySelector=()=>({content:'__CSP_NONCE__'});v.grid('ledger',[],()=>[]);assert.equal(node('ledger').options.styleNonce,undefined);
console.log('PASS: AG text renderers, six CSV formula prefixes, exact decimal aggregation/comparison, currency separation, local dates, nonce, reduced motion, truthful empty states, in-place refresh and disposal.');
