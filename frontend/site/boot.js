(()=>{
 const release='20261112-fin-1';
 const files=['ag-grid-community.min.noStyle.js','ag-charts-community.min.js','live-visuals.js','icons.js','saver-agents.js','agents-screen.js','wallets-screen.js','finance-screens.js','lab-screen.js','workspace-services.js','insights-screens.js','admin-screens.js','core.js'];
 let loaded=0,running=false;
 let setup=null,offerServices=false;
 const host=()=>document.getElementById('screen');
 function failure(message){const h=host();h.setAttribute('aria-busy','false');h.innerHTML='<section class="app-fallback" role="alert"><span>↻</span><h1>Let’s get your dashboard open</h1><p></p><button id="boot-retry" class="primary">Try again</button><a href="./">Reload dashboard</a></section>';h.querySelector('p').textContent=message;document.getElementById('boot-retry').onclick=run}
 function loadScript(name){return new Promise((resolve,reject)=>{const script=document.createElement('script');script.src='./'+name+'?v='+release;script.onload=resolve;script.onerror=()=>{script.remove();reject(new Error('A dashboard file could not load. Check your connection, then try again.'))};document.head.append(script)})}
 const AUTH_ASIDE='<div class="auth-brand"><a class="brand" href="#"><span class="brandmark">A</span><div>AgentPal<small>AI Agent Finance</small></div></a><h2>Let AI agents shop.<br>Keep every dollar in your hands.</h2><ul><li><b>✓</b><span><strong>You approve exact terms</strong>Amount, payee and purpose are bound to your approval. Any change needs a new one.</span></li><li><b>✓</b><span><strong>Agents never approve themselves</strong>Deterministic limits run first. AI risk checks are advisory only.</span></li><li><b>✓</b><span><strong>Only PayPal-verified outcomes count</strong>Nothing is marked paid until PayPal confirms it.</span></li></ul><p class="auth-sandbox"><i></i>PayPal sandbox · no real money moves</p></div>';
 function authMode(on){if(on)document.body.dataset.auth='1';else delete document.body.dataset.auth}
 function signInScreen(note,kind){
  const h=host();h.setAttribute('aria-busy','false');authMode(true);document.title='Sign in — AgentPal';
  h.innerHTML='<div class="auth-page">'+AUTH_ASIDE+'<section class="auth-card" aria-labelledby="signin-title"><span class="auth-lock" aria-hidden="true">🔒</span><h1 id="signin-title">Sign in to your workspace</h1>'+
   '<p class="auth-lead">Paste your owner access token. It stays in this browser tab only and is cleared when you close the tab or sign out.</p>'+
   '<p id="signin-note" class="auth-note" role="status" hidden></p>'+
   '<form id="signin-form" autocomplete="off" novalidate><label for="signin-token">Owner access token</label>'+
   '<div class="auth-input"><input id="signin-token" name="token" type="password" spellcheck="false" autocapitalize="off" placeholder="apo_…" aria-describedby="signin-help" required><button type="button" id="signin-reveal" aria-label="Show token" aria-pressed="false">Show</button></div>'+
   '<p id="signin-error" class="signin-error" role="alert" hidden></p><button class="primary auth-submit" type="submit"><span>Sign in</span></button></form>'+
   '<p id="signin-help" class="auth-help">No token? Ask the operator for an owner token. Agent keys cannot sign in here.</p>'+(setup?.mode==='hosted'&&setup.signup==='invite_code'?'<button type="button" class="link" id="create-workspace">Create a workspace</button>':'')+'</section></div>';
  const create=document.getElementById('create-workspace');if(create)create.onclick=setupScreen;
  if(note){const n=document.getElementById('signin-note');n.textContent=note;n.dataset.kind=kind||'info';n.hidden=false}
  const input=document.getElementById('signin-token'),err=document.getElementById('signin-error'),reveal=document.getElementById('signin-reveal');
  reveal.onclick=()=>{const show=input.type==='password';input.type=show?'text':'password';reveal.textContent=show?'Hide':'Show';reveal.setAttribute('aria-pressed',String(show));reveal.setAttribute('aria-label',show?'Hide token':'Show token');input.focus()};
  document.getElementById('signin-form').onsubmit=async e=>{
   e.preventDefault();const button=e.target.querySelector('.auth-submit');
   if(!input.value.trim()){err.textContent='Paste your owner access token to continue.';err.hidden=false;input.focus();return}
   button.disabled=true;button.classList.add('busy');button.firstChild.textContent='Checking…';err.hidden=true;
   try{await window.AgentPalAPI.signIn(input.value);input.value='';offerServices=true;await run()}
   catch(x){err.textContent=x.code==='token_format'?'That does not look like an owner token (it starts with apo_).':x.code==='not_an_owner_token'?'That token belongs to an agent, not an owner.':window.AgentPalAPI.message(x);err.hidden=false;button.disabled=false;button.classList.remove('busy');button.firstChild.textContent='Sign in';input.focus();input.select()}
  };
  input.focus();
 }
 function setupScreen(){
  const h=host(),hosted=setup?.mode==='hosted';authMode(true);h.setAttribute('aria-busy','false');document.title='Create your workspace — AgentPal';
  h.innerHTML='<div class="auth-page">'+AUTH_ASIDE+'<section class="auth-card"><h1>Create your workspace</h1><p class="auth-lead">Your workspace keeps your agents, approvals and service keys together.</p><form id="setup-form" autocomplete="off"><label for="setup-owner">Owner ID</label><input id="setup-owner" name="owner_id" required maxlength="100" autocapitalize="off" spellcheck="false"><label for="setup-name">Display name</label><input id="setup-name" name="display_name" required maxlength="100"><label for="setup-code">'+(hosted?'Invite code':'Setup code')+'</label><input id="setup-code" name="code" type="password" required autocomplete="off" aria-describedby="setup-help"><p id="setup-help" class="auth-help">'+(hosted?'Ask your operator for the workspace invite code.':'Find the one-time setup code in the server console where AgentPal was started. The operator can also find it in the protected setup-code file in the server data directory. First setup must be opened on the server’s own computer (loopback).')+'</p><p id="setup-error" role="alert"></p><button class="primary auth-submit">Create workspace</button></form>'+(hosted?'<button type="button" class="link" id="setup-back">Back to sign in</button>':'')+'</section></div>';
  const back=document.getElementById('setup-back');if(back)back.onclick=()=>signInScreen();
  document.getElementById('setup-form').onsubmit=async e=>{
   e.preventDefault();const form=e.currentTarget,button=form.querySelector('button');if(button.disabled)return;button.disabled=true;
   const fields=new FormData(form),body={owner_id:String(fields.get('owner_id')).trim(),display_name:String(fields.get('display_name')).trim(),code:String(fields.get('code'))};
   form.elements.code.value='';fields.delete('code');
   try{const result=await window.AgentPalAPI.createOwner(body);body.code='';showOwnerToken(result.token)}
   catch(err){document.getElementById('setup-error').textContent=window.AgentPalAPI.message(err);button.disabled=false}
   finally{body.code=''}
  };
 }
 function showOwnerToken(value){
  let once=value;
  host().innerHTML='<div class="auth-page">'+AUTH_ASIDE+'<section class="auth-card"><h1>Save your owner token now</h1><p class="auth-note" role="alert">Save this now in your password manager. This token is shown only once. Anyone with it can access your workspace.</p><label for="owner-token-once">Owner access token</label><textarea id="owner-token-once" readonly autocomplete="off" spellcheck="false"></textarea><button class="i-secondary" id="copy-owner-token">Copy token</button><p id="copy-status" role="status"></p><button class="primary auth-submit" id="owner-token-saved">I saved it — open workspace</button><p id="setup-signin-error" role="alert"></p></section></div>';
  const field=document.getElementById('owner-token-once');field.value=once;
  document.getElementById('copy-owner-token').onclick=async()=>{try{await navigator.clipboard.writeText(once);document.getElementById('copy-status').textContent='Copied. Save it in your password manager.'}catch(_){field.focus();field.select();document.getElementById('copy-status').textContent='Copy the selected token and save it now.'}};
  document.getElementById('owner-token-saved').onclick=async e=>{
   const button=e.currentTarget;if(button.disabled)return;button.disabled=true;field.value='';document.getElementById('copy-owner-token').disabled=true;
   try{await window.AgentPalAPI.signIn(once);once=null;offerServices=true;await run()}
   catch(err){once=null;signInScreen('Sign in with the token you saved. '+window.AgentPalAPI.message(err),'warn')}
  };
 }
 function loadingScreen(){
  const h=host();h.setAttribute('aria-busy','true');authMode(true);document.title='Loading — AgentPal';
  h.innerHTML='<div class="auth-page">'+AUTH_ASIDE+'<section class="auth-card auth-loading" role="status" aria-live="polite"><span class="auth-spinner" aria-hidden="true"></span><h1>Opening your live workspace</h1><ol class="auth-steps"><li class="done">Access token verified</li><li id="step-data" class="active">Loading agents, wallets and payments</li><li id="step-ui">Preparing your dashboard</li></ol></section></div>';
 }
 function step(id){const el=document.getElementById(id);if(!el)return;el.previousElementSibling?.classList.replace('active','done');el.classList.add('active')}
 function sessionNote(){let v=null;try{v=sessionStorage.getItem('agentpal-auth-note');sessionStorage.removeItem('agentpal-auth-note')}catch(_){}return v==='signed-out'?{text:'You signed out. Sign in again to continue.',kind:'info'}:v==='expired'?{text:'Your session ended. Please sign in again.',kind:'warn'}:null}
 function remember(note){try{sessionStorage.setItem('agentpal-auth-note',note)}catch(_){}}
 function liveShell(){
  const api=window.AgentPalAPI;
  document.getElementById('connection-status').textContent='Live · PayPal sandbox';
  document.getElementById('connection-detail').textContent='No real money moves';
  const label=document.querySelector('.app-demo-label');if(label)label.textContent='Live sandbox';
  if(!document.getElementById('signout')){const b=document.createElement('button');b.id='signout';b.className='signout';b.type='button';b.textContent='Sign out';b.onclick=()=>api.signOut();document.querySelector('.system').append(b);const m=document.createElement('button');m.className='signout-mobile';m.type='button';m.textContent='Sign out';m.onclick=()=>api.signOut();document.querySelector('.profile').append(m)}
 }
 async function run(){
  if(running)return;running=true;host().setAttribute('aria-busy','true');
  try{
   if(!window.AgentPalAPI)await loadScript('api.js');
   const api=window.AgentPalAPI;
   if(api.live){
    if(!api.signedIn){setup=await api.setupStatus();running=false;if(setup.needs_setup&&setup.signup==='setup_code'){setupScreen();return}const note=sessionNote();signInScreen(note?.text,note?.kind);return}
    loadingScreen();
    await api.refresh();
    step('step-ui');
    liveShell();
   }
   for(;loaded<files.length;loaded++)await loadScript(files[loaded]);
   if(!window.App?.ready)throw new Error('The dashboard could not start. Reload the page to try again.');
   authMode(false);
   if(offerServices&&window.WorkspaceServices){offerServices=false;window.WorkspaceServices.offer();}
   host().setAttribute('aria-busy','false');
  }catch(e){
   if(e&&e.status===401){running=false;signInScreen('Your session ended. Please sign in again.','warn');return}
   failure(e&&e.code?window.AgentPalAPI.message(e):e.message)
  }finally{running=false}
 }
 window.addEventListener('agentpal-live',e=>{if(e.detail.type==='signed-out'&&window.App?.ready){remember(e.detail.reason==='expired'?'expired':'signed-out');location.hash='';location.reload()}});
 window.addEventListener('error',e=>{if(e.target!==window)return;if(!window.App?.ready)return;window.App.reportError?.('That action could not finish. You can retry it or reload this page.');console.error('Dashboard action failed')});
 window.addEventListener('unhandledrejection',e=>{window.App?.reportError?.(window.AgentPalAPI&&e.reason&&e.reason.code?window.AgentPalAPI.message(e.reason):'That action could not finish. Please try again.');console.error('Dashboard action failed')});
 if(location.protocol==='file:'){document.getElementById('connection-status').textContent='Local demo'}
 run();
})();
