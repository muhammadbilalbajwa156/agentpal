/* Client boundary checks: synthetic credentials, no network or provider calls. */
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const sourceDir=process.env.AGENTPAL_SCREEN_DIR||path.join(__dirname,'site');
function client(){
 const calls=[],session=new Map(),local=new Map();let reply={status:200,body:{}};
 const c={location:{pathname:'/app/'},sessionStorage:{getItem:k=>session.get(k),setItem:(k,v)=>session.set(k,v),removeItem:k=>session.delete(k)},localStorage:{getItem:k=>local.get(k),setItem:(k,v)=>local.set(k,v)},document:{addEventListener(){}},setInterval(){},setTimeout,console,Uint8Array,crypto:require('node:crypto').webcrypto,CustomEvent:class{},dispatchEvent(){},fetch:async(url,opts)=>{calls.push({url,...opts});return {status:reply.status,ok:reply.status<400,headers:{get:()=>null},json:async()=>reply.body};}};c.window=c;vm.createContext(c);vm.runInContext(fs.readFileSync(path.join(sourceDir,'api.js'),'utf8'),c);
 return {api:c.AgentPalAPI,calls,local,session,reply:v=>reply=v};
}
(async()=>{
 const h=client();assert.equal(typeof h.api.setupStatus,'function','Public setup status is required');
 await h.api.setupStatus();assert.equal(h.calls[0].url,'/v1/setup/status');assert.equal(h.calls[0].headers.Authorization,undefined);
 const code=require('node:crypto').randomUUID();await h.api.createOwner({owner_id:'test',display_name:'Test',code});
 assert.equal(h.calls[1].method,'POST');assert.equal(h.calls[1].headers.Authorization,undefined);assert.ok(!h.calls[1].url.includes(code));assert.equal(h.local.size,0);assert.equal(h.session.size,0);
 await assert.rejects(h.api.get('integrations'),e=>e.status===401);assert.equal(h.calls.length,2,'Setup must not make private calls public');
 const token='apo_'+require('node:crypto').randomBytes(20).toString('hex');await h.api.signIn(token);assert.equal(h.local.size,0);assert.equal(h.session.get('agentpal-owner-session'),token);
 await h.api.setupStatus();assert.equal(h.calls.at(-1).headers.Authorization,undefined,'Setup omits owner bearer even while signed in');
 await h.api.put('integrations/groq',{fields:{api_key:code}});const write=h.calls.at(-1);assert.equal(write.headers.Authorization,'Bearer '+token);assert.equal(write.credentials,'omit');assert.equal(write.redirect,'error');assert.equal(write.cache,'no-store');assert.equal(write.referrerPolicy,'no-referrer');assert.ok(!write.url.includes(code));
 h.reply({status:429,body:{error:'http_429'}});await assert.rejects(h.api.post('lab/errands',{}),e=>/Too many/.test(h.api.message(e)));assert.equal(h.calls.at(-1).method,'POST');
 const err=new h.api.ApiError(400,code,{error:code});assert.ok(!h.api.message(err).includes(code),'Unknown error text must not echo credential-shaped values');
 h.api.signOut();assert.equal(h.session.size,0);
 if(fs.existsSync(path.join(sourceDir,'lab-screen.js'))){
  const c={AgentPalAPI:{live:true},addEventListener(){},URL};c.window=c;vm.createContext(c);vm.runInContext(fs.readFileSync(path.join(sourceDir,'lab-screen.js'),'utf8'),c);
  for(const value of ['https://www.sandbox.paypal.com/checkoutnow?token=synthetic','https://sandbox.paypal.com/checkoutnow?token=synthetic'])assert.ok(c.LabScreen.sandboxURL(value));
  for(const value of ['https://www.paypal.com/checkoutnow','http://www.sandbox.paypal.com/checkoutnow','https://www.sandbox.paypal.com.evil.test/','https://user@www.sandbox.paypal.com/','https://www.sandbox.paypal.com:444/','javascript:alert(1)','/checkoutnow','not a URL'])assert.equal(c.LabScreen.sandboxURL(value),null,'Reject unsafe sandbox navigation');
 }
 console.log('PASS: public setup isolation, tab-only owner session, authenticated same-origin secret writes, no redirects/cookies/cache/referrer, safe errors and 429.');
})().catch(e=>{console.error(e);process.exitCode=1;});
