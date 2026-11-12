(()=>{
// Live mode exits before demo fixtures or browser storage are read.
if(window.AgentPalAPI?.live){
  const visual=window.AgentPalVisuals;
  let transactionGrid;
  const api=window.AgentPalAPI, escape=value=>App.escape(String(value??'—'));
  const sandbox='<p class="f-sandbox-note">PayPal sandbox · no real money</p>';
  const loadNotice=()=>api.data.errors?.length?'<p class="f-live-warning" role="alert">Some live data could not be loaded. Reload the dashboard to retry.</p>':'';
  const inflight=new Set();
  let detailVersion=0;
  const endpoint=(kind,id)=>(kind==='payout'?'payout-requests/':'payment-requests/')+encodeURIComponent(id);
  const agentName=id=>api.data.agents.find(a=>a.agent_id===id)?.name||id;
  const human=value=>String(value??'unavailable').replace(/_/g,' ');
  const statusText=value=>value==='reconciliation_required'?'needs reconciliation: no money assumed':value==='blocked_by_rule'?'Blocked by rule':human(value);
  const badge=value=>`<span class="f-badge">${escape(statusText(value))}</span>`;
  function row(value,kind){
    const terms=kind==='payout'?value:value.terms||{};
    return {id:value.request_id,kind,amount:terms.amount,currency:terms.currency,
      agent:agentName(terms.agent_id||value.agent_id),agent_id:terms.agent_id||value.agent_id,wallet:value.wallet_id,
      description:kind==='payout'?value.receiver_email:terms.merchant_name,
      payee:kind==='payout'?value.receiver_email:terms.merchant_paypal_email||value.merchant_paypal_email,
      company:visual.company(api.data.agents.find(a=>a.agent_id===(terms.agent_id||value.agent_id))),
      purpose:terms.purpose,status:value.policy_block?'blocked_by_rule':value.status,date:value.created_at,created_at:value.created_at,raw:value};
  }
  const rows=()=>[...api.data.payments.map(r=>row(r,'payment')),...api.data.payouts.map(r=>row(r,'payout'))]
    .sort((a,b)=>String(b.created_at).localeCompare(String(a.created_at)));
  const pendingRows=()=>[...api.data.approvals.map(r=>row(r,'payment')),
    ...api.data.payouts.filter(r=>r.status==='awaiting_approval').map(r=>row(r,'payout'))];
  const displayMoney=r=>escape(api.money(r.amount,r.currency))+' '+escape(r.currency);
  // Exact decimal aggregation for display, grouped by currency. Never combine currencies.
  function sum(items,amount=r=>r.amount,currency=r=>r.currency){
    const groups=new Map();
    for(const item of items){
      const text=String(amount(item)??'');
      if(!/^\d+(\.\d{1,8})?$/.test(text))continue;
      const [whole,part='']=text.split('.'),key=currency(item);
      groups.set(key,(groups.get(key)||0n)+BigInt(whole)*100000000n+BigInt(part.padEnd(8,'0')));
    }
    return [...groups].map(([currency,value])=>({currency,amount:(value/100000000n).toString()+'.'+(value%100000000n).toString().padStart(8,'0')}));
  }
  const totalText=items=>items.length?items.map(displayMoney).join(' · '):'No data';
  const allowance=field=>sum(api.data.wallets,w=>w.allowance?.[field],w=>w.currency);
  const stat=(title,value,ic)=>{const tone=/Pending|reconciliation/.test(title)?'orange':/Rejected|spending/.test(title)?'purple':/Approved|Confirmed/.test(title)?'mint':'blue';return visual.live.statCard(escape(title),value,tone,ic,'sandbox · current records',visual.live.records(),'finance');};
  const pair=(label,value)=>`<dt>${escape(label)}</dt><dd>${escape(value)}</dd>`;
  const amountPair=r=>`<dt>Amount</dt><dd>${displayMoney(r)}</dd>`;
  function payeeLabel(value,kind){
    const r=row(value,kind);
    if(r.payee)return r.payee;
    return value.provider_mode==='simulated'?'Local simulator (no PayPal)':value.provider_mode==='paypal_sandbox'?"AgentPal's PayPal sandbox business account":'Sandbox payee (provider unavailable)';
  }
  function termsHTML(value,kind){
    const r=row(value,kind),terms=value.terms||{},hash=String(value.terms_sha256||'');
    const demoPayee=kind==='payment'&&!r.payee&&['paypal_sandbox','simulated'].includes(value.provider_mode);
    const payee=`<dt>Pay to</dt><dd>${escape(payeeLabel(value,kind))}${demoPayee?"<small class=\"f-payee-demo-note\">Sandbox demo: payment goes to AgentPal's PayPal sandbox account; no retailer order is placed.</small>":''}</dd>`;
    const merchant=`<dt>Merchant</dt><dd>${escape(terms.merchant_name)}${terms.merchant_domain?` <span class="f-merchant-domain">${escape(terms.merchant_domain)}</span>`:''}</dd>`;
    return `<section class="f-exact-terms" aria-label="Exact terms"><div class="f-purchase"><div><small>EXACT TERMS</small><h3>${escape(r.purpose)}</h3></div><div class="f-dialog-amount">${displayMoney(r)}</div></div><dl class="f-detail-list">${amountPair(r)}${payee}${pair('Purpose',r.purpose)}${kind==='payment'?merchant:pair('Note',value.note)}<dt>Fingerprint</dt><dd class="f-fingerprint"><code title="${escape(hash)}">${escape(hash.slice(0,12))}…</code><button type="button" class="link" data-copy-fingerprint="${escape(hash)}" aria-label="Copy full terms fingerprint">Copy</button></dd>${value.policy_block?pair('Blocked by rule',value.policy_block.rule+' · '+value.policy_block.label):''}</dl>${sandbox}${badge(r.status)}${kind==='payment'?'<p class="f-sandbox-note">This sandbox payment does not order the item from the retailer.</p>':''}${kind==='payment'?counterLinkHTML(value):''}${verificationHTML(value.verification)}</section>`;
  }
  // Labels describe recorded evidence, never payment authority. Warnings take priority
  // over a cheaper offer so that a bargain cannot hide a concern.
  function verdictHTML(value){
    const market=value.market_check,page=value.page_check;
    const warnings=[...(value.risk_signals||[]),
      ...(market?.signals||[]).filter(s=>['warning','high'].includes(s.severity)),
      ...(page?.signals||[]).filter(s=>['warning','high'].includes(s.severity))];
    const ratio=market?.market?.claimed_vs_median;
    let tone='fine',label=(!market&&!page)?'No warnings found':'Looks fine',reason='No advisory warnings reported; '+(!market&&!page?'price and page not checked.':'review the recorded checks below.');
    if(value.policy_block||value.status==='blocked_by_rule'){
      tone='blocked';label='Blocked by rule';reason=value.policy_block?.label||'The request status is blocked by rule.';
    }else if(warnings.length||(ratio!=null&&Number(ratio)<=0.5)||[market,page].some(c=>['review_recommended','high_risk'].includes(c?.advisory_level))||!value.terms?.merchant_name||/^unknown\b/i.test(value.terms.merchant_name)){
      tone='careful';label='Check carefully';reason=warnings[0]?.message||(ratio!=null&&Number(ratio)<=0.5?'The requested price is far below the recorded market median.':!value.terms?.merchant_name||/^unknown\b/i.test(value.terms.merchant_name)?'The merchant is unknown in the request terms.':'A recorded check recommends review.');
    }else if(market?.better_deals?.length){
      tone='cheaper';label='Cheaper elsewhere';reason='Channel3 recorded alternative offers; compare the price and retailer below.';
    }
    return `<section class="f-verdict f-verdict-${escape(tone)}" aria-label="Advisory verdict" data-verdict="${escape(tone)}"><strong>${escape(label)}</strong><p>${escape(reason)}</p><small>advisory: rules and your approval decide</small></section>`;
  }
  const comparableDeals=value=>(value.market_check?.better_deals||[]).map((deal,index)=>({...deal,index}))
    .filter(d=>d.currency===value.terms?.currency&&/^\d+(\.\d{1,8})?$/.test(String(d.price))&&Number(d.price)>0&&d.retailer);
  function priceHTML(value){
    if(!value.market_check)return '<section class="f-price-card"><h3>Price not compared</h3><p>No market check recorded.</p></section>';
    const recorded=value.market_check.checked_at?`<p class="f-price-recorded">Recorded <time datetime="${escape(value.market_check.checked_at)}">${escape(visual.date(value.market_check.checked_at))}</time></p>`:'';
    const deals=comparableDeals(value),best=deals.reduce((a,d)=>!a||Number(d.price)<Number(a.price)?d:a,null);
    const asked=Number(value.terms?.amount);
    if(!best||!Number.isFinite(asked)||asked<=0)return `<section class="f-price-card"><h3>Price comparison</h3>${recorded}<p>No comparable same-currency Channel3 offer recorded. Price not compared.</p></section>`;
    const price=Number(best.price),diff=asked-price,pct=Math.abs(diff)/asked*100,max=Math.max(asked,price);
    // Widths are coarse visual guides; exact prices and differences are text. Classes
    // keep dynamic values out of inline styles under the dashboard CSP.
    const bar=(label,amount,tone)=>`<div class="f-price-row"><div><span>${escape(label)}</span><strong>${displayMoney({amount,currency:value.terms.currency})}</strong></div><div class="f-price-track" aria-hidden="true"><div class="f-price-bar ${escape(tone)} f-width-${escape(Math.max(5,Math.round(Number(amount)/max*20)*5))}"></div></div></div>`;
    return `<section class="f-price-card" aria-label="Price comparison"><h3>Price comparison <small>Channel3 · advisory</small></h3>${recorded}${bar("Agent's price",value.terms.amount,'f-price-agent')}${bar('Best offer · '+best.retailer,best.price,'f-price-best')}<p class="f-price-difference">${displayMoney({amount:Math.abs(diff).toFixed(2),currency:value.terms.currency})} (${escape(pct.toFixed(1))}%) ${diff>=0?'less':'more'} than the agent's price. Recorded offer; confirm availability.</p></section>`;
  }
  function journeyHTML(value){
    const status=value.status,blocked=!!value.policy_block||status==='blocked_by_rule';
    const ended=blocked?'Blocked by rule':status==='rejected'?(value.countered_by?'Countered by owner':'Rejected by owner'):status==='expired'?'Expired':status==='failed'?'Provider failed':status==='reversed'?'Reversed':status==='reconciliation_required'?'Needs reconciliation: no money assumed':'';
    const verified=['paypal_read','paypal_webhook'].includes(value.verification?.verified_by);
    let current=({prepared:2,awaiting_approval:2,approved:3,submitted_to_provider:3,provider_pending:3,provider_confirmed:5,reversed:5,rejected:2,expired:2,failed:3,reconciliation_required:3})[status]??1;
    if(blocked)current=1;
    const finished=!blocked&&['provider_confirmed','reversed'].includes(status)&&verified;
    const steps=['Requested','Rules checked','Your approval','PayPal buyer approval','Captured','Verified by PayPal'];
    return `<section class="f-journey" aria-label="Payment journey"><h3>Payment journey</h3><ol>${steps.map((label,i)=>{const state=finished||i<current?'done':i===current?'current':'not-reached';return `<li data-step="${escape(i)}" data-state="${escape(state)}"${state==='current'?' aria-current="step"':''}><span class="f-step-dot" aria-hidden="true">${state==='done'?'✓':escape(i+1)}</span><span>${escape(label)}<small>${escape(state==='done'?'Done':state==='current'?(ended||'Current'):'Not reached')}</small></span></li>`;}).join('')}</ol>${ended?`<p class="f-journey-end">${escape(ended)}</p>`:''}</section>`;
  }
  function checksHTML(value){
    const chips=[['Risk signals',(value.risk_signals||[]).length?value.risk_signals.length+' reported':'None reported'],['Market check',value.market_check?human(value.market_check.advisory_level):'Not checked'],['Page check',value.page_check?human(value.page_check.advisory_level):'Not checked']];
    if(value.countered_by||value.counter_of)chips.push(['Counter-offer',value.countered_by?'Original replaced':'Replacement · '+human(value.counter_source)]);
    return `<section class="f-checks" aria-label="Checks"><h3>Checks <small>advisory only</small></h3><dl>${chips.map(([label,text])=>pair(label,text)).join('')}</dl></section>`;
  }
  // Owner counter-offer ("Buy this instead"). The difference is between two recorded amounts
  // in one currency. It is called a saving only when the server reports that PayPal verified
  // the replacement's capture; nothing here is estimated in the browser.
  function savingHTML(value){
    const text=String(value.counter_saving??'');
    if(!/^-?\d+(\.\d{1,8})?$/.test(text))return '';
    const amount=displayMoney({amount:text.replace(/^-/,''),currency:value.terms?.currency});
    if(!/[1-9]/.test(text))return `<p class="f-counter-saving">Same amount as the agent's request.</p>`;
    if(text.startsWith('-'))return `<p class="f-counter-saving">${amount} more than the agent's request.</p>`;
    return value.counter_saving_verified===true
      ?`<p class="f-counter-saving" data-saving="verified"><strong>Saved ${amount}</strong> against the agent's request <span class="ap-pill good">verified by PayPal</span></p>`
      :`<p class="f-counter-saving" data-saving="unverified">${amount} below the agent's request <span class="ap-pill waiting">not a saving until PayPal verifies the payment</span></p>`;
  }
  function counterLinkHTML(value){
    const link=id=>`<button type="button" class="link" data-counter-open="${escape(id)}">${escape(id)}</button>`;
    if(value.countered_by)return `<p class="f-counter-link"><span class="ap-pill waiting">Countered by owner</span> Declined and replaced by ${link(value.countered_by)}. The replacement needs its own approval.</p>`;
    if(value.counter_of)return `<p class="f-counter-link"><span class="ap-pill">Owner counter-offer</span> Replaces ${link(value.counter_of)}. You wrote these terms; the agent can only execute them unchanged.</p>${savingHTML(value)}`;
    return '';
  }
  function counterHTML(value){
    const terms=value.terms||{};
    const deals=comparableDeals(value);
    const field=(name,label,v,extra='')=>`<label>${label}<input name="${name}" type="text" autocomplete="off" value="${escape(v??'')}" ${extra}></label>`;
    return `<section class="f-counter" aria-label="Counter-offer">${priceHTML(value)}${window.SaverAgents?.suggestionHTML(value)||''}<h3>Not this? Buy this instead</h3>${deals.length?`<ul class="f-counter-deals">${deals.map(d=>`<li><div><strong>${escape(d.retailer)}</strong><span>${displayMoney({amount:d.price,currency:d.currency})}${/^\d+(\.\d{1,8})?$/.test(String(d.you_save))?` · ${displayMoney({amount:d.you_save,currency:d.currency})} less`:''} · Channel3 offer, advisory</span></div><button type="button" class="f-secondary" data-counter-deal="${d.index}">Counter with this deal</button></li>`).join('')}</ul>`:''}<p class="f-counter-note">A counter approves nothing. It declines this request and creates a replacement for the same agent, checked by the same spending rules. You then approve the replacement's own fingerprint; the agent can only execute it unchanged.</p><form id="f-live-counter-form" hidden novalidate>${field('amount','Amount ('+escape(terms.currency)+')',terms.amount,'inputmode="decimal" required')}${field('merchant_name','Merchant name',terms.merchant_name,'maxlength="200" required')}${field('merchant_domain','Merchant domain (optional)',terms.merchant_domain,'maxlength="253"')}${field('merchant_paypal_email','PayPal payee email (optional)',terms.merchant_paypal_email,'maxlength="127"')}<p class="f-counter-note">Currency and purpose stay as the agent asked: ${escape(terms.currency)} · ${escape(terms.purpose)}</p><button class="f-secondary" type="submit">Counter with these terms</button></form><p id="f-live-counter-error" role="alert"></p></section>`;
  }
  const counterMessage=err=>({not_awaiting_decision:'This request is no longer awaiting your decision. Nothing was countered.',already_countered:'This request already has a counter-offer. Open its replacement instead.',counter_same_terms:'These are the terms the agent asked for. Change the amount, merchant or payee, or approve the request as it is.',invalid_deal:'That offer is not in the stored market check any more. Use the Counter form instead.',counter_recurring_unsupported:'A recurring request cannot be countered. Reject it and ask the agent for a new request.',invalid_request:'Check the amount, merchant name, domain and PayPal email.'}[err.code]||api.message(err));
  let revealCounter=false;
  function bindCounterLinks(){
    const modal=document.querySelector('#modal');
    // Keep the initial decision/counter/evidence order. Once the decision row scrolls
    // away on a phone, dock that same row at the dialog bottom (no duplicate buttons).
    modal.onscroll=()=>{
      const slot=modal.querySelector('.f-decision-slot'),actions=slot?.querySelector('.f-request-actions');
      if(actions)actions.classList.toggle('f-actions-docked',matchMedia('(max-width:600px)').matches&&slot.getBoundingClientRect().top<modal.querySelector('.modal-head').getBoundingClientRect().bottom);
    };
    document.querySelectorAll('#modal [data-copy-fingerprint]').forEach(b=>{
      b.onclick=async()=>{
        try{await navigator.clipboard.writeText(b.dataset.copyFingerprint);App.toast('Full terms fingerprint copied.');}
        catch(_){App.toast('Copy unavailable. The full fingerprint is in Details.');}
      };
    });
    document.querySelectorAll('#modal [data-counter-open]').forEach(b=>{b.onclick=()=>openRequest(b.dataset.counterOpen,'payment');});
  }
  async function sendCounter(id,body,button){
    const error=document.querySelector('#f-live-counter-error');if(error)error.textContent='';
    let result=null,failure=null;
    const done=await write(button,'counter:'+id,async()=>{try{result=await api.counterRequest(id,body);}catch(err){failure=err;}});
    if(done===false&&!failure&&!result)return;
    if(failure){const text=counterMessage(failure);if(error?.isConnected)error.textContent=text;else App.toast(text);return;}
    const next=result?.replacement;if(!next?.request_id)return;
    const blocked=!!next.policy_block;
    App.toast(blocked?'Counter recorded, but a spending rule blocked the replacement.':'Counter recorded. Nothing is approved yet.');
    await openRequest(next.request_id,'payment',!blocked&&next.status==='awaiting_approval',blocked?'A spending rule blocked your counter-offer. The original request was declined; nothing was approved.':'You countered: the original request was declined. Review the replacement and approve its own fingerprint.');
  }
  function bindCounter(id){
    const form=document.querySelector('#f-live-counter-form'),toggle=document.querySelector('#f-live-counter'),error=document.querySelector('#f-live-counter-error');
    if(!form||!toggle)return;
    const show=open=>{form.hidden=!open;toggle.setAttribute('aria-expanded',String(open));if(open)form.querySelector('[name=amount]').focus();};
    toggle.onclick=()=>show(form.hidden);
    document.querySelectorAll('#modal [data-counter-deal]').forEach(b=>{b.onclick=()=>sendCounter(id,{deal_index:Number(b.dataset.counterDeal)},b);});
    form.onsubmit=e=>{
      e.preventDefault();
      const fields=new FormData(form),text=name=>String(fields.get(name)||'').trim(),amount=text('amount'),name=text('merchant_name');
      if(!/^\d{1,10}(\.\d{1,2})?$/.test(amount)||!/[1-9]/.test(amount)){error.textContent='Enter a positive amount with up to two decimal places.';return;}
      if(!name){error.textContent='Enter the merchant name.';return;}
      const body={amount,merchant_name:name};
      for(const key of ['merchant_domain','merchant_paypal_email']){if(text(key))body[key]=text(key);}
      sendCounter(id,body,form.querySelector('button[type=submit]'));
    };
    if(revealCounter){revealCounter=false;show(true);}
  }
  function provenanceHTML(value,kind){
    const r=row(value,kind),terms=value.terms||{};
    return `<dl class="f-detail-list">${pair('Request',r.id)}${pair('Agent',r.agent)}${pair('Agent ID',r.agent_id)}${pair('Wallet',r.wallet)}${pair('Created',visual.date(r.created_at))}${kind==='payment'?pair('Domain',terms.merchant_domain)+pair('Recommendation (advisory)',value.recommendation)+pair('Risk score',value.risk_score):''}${pair('Terms SHA-256',value.terms_sha256)}${value.owner_decision?pair('Owner decision',human(value.owner_decision.decision)+' · '+visual.date(value.owner_decision.at)):''}</dl>`;
  }
  const verificationLabel=method=>({paypal_read:'Verified by PayPal read',paypal_webhook:'Verified by PayPal webhook'}[method]||'');
  function verificationHTML(value){const label=verificationLabel(value?.verified_by);return label?`<p class="f-verification"><span class="ap-pill good">${label}</span> · ${escape(visual.date(value.at))}</p>`:'';}
  async function timeline(kind,id){try{return await api.get(endpoint(kind,id)+'/timeline');}catch(err){if(err.status===404)return null;return {unavailable:true};}}
  function timelineHTML(value){
    if(!value)return '';
    if(value.unavailable)return '<section class="f-evidence"><h3>Timeline</h3><p>Timeline could not be loaded. Reopen details to retry.</p></section>';
    const actors={agent:['bot','Agent'],owner:['shield','Owner'],policy:['shield','Policy'],paypal:['wallet','PayPal'],system:['settings','System']};
    return `<section class="f-evidence"><h3>Timeline</h3><ol class="f-timeline">${(value.events||[]).map(e=>{const [icon,label]=actors[e.actor]||actors.system;const verified=verificationLabel(e.verified_by);return `<li><span class="f-timeline-actor" aria-label="${label}">${App.icon(icon)}</span><div><small>${label} · <time>${escape(visual.date(e.at))}</time></small><p>${escape(e.label)}</p>${e.detail?`<p>${escape(e.detail)}</p>`:''}${verified?`<span class="ap-pill good">${verified}</span>`:''}</div></li>`;}).join('')||'<li>No recorded events.</li>'}</ol></section>`;
  }
  function marketSummary(value){
    const ratio=Number(value?.market?.claimed_vs_median),hasRatio=value?.market?.claimed_vs_median!=null&&Number.isFinite(ratio);
    const below=hasRatio&&ratio<=0.5,above=hasRatio&&ratio>=1.5;
    const checked=hasRatio&&!['no_data','no_match'].includes(value?.advisory_level);
    return `<p class="f-market-summary"><span class="ap-pill ${below||above?'bad':checked?'good':'waiting'}">${below?'Below market — check seller':above?'Above market — review price':checked?'Price normal':'Not checked'}</span>${hasRatio?` · ${escape(ratio)}× market median`:''} · advisory only</p>`;
  }
  // Advisory evidence is rendered only as escaped text. URLs never become navigation.
  function evidenceHTML(label,value){
    if(!value)return `<section class="f-evidence"><h3>${label} · advisory only</h3><p>No data available.</p></section>`;
    function fields(object){
      return Object.entries(object).map(([key,v])=>{
        const name=human(key);
        if(Array.isArray(v))return `<dt>${escape(name)}</dt><dd>${v.length?v.map(item=>item&&typeof item==='object'?`<dl class="f-detail-list">${fields(item)}</dl>`:escape(item)).join('<br>'):'None reported'}</dd>`;
        if(v&&typeof v==='object')return `<dt>${escape(name)}</dt><dd><dl class="f-detail-list">${fields(v)}</dl></dd>`;
        if(v!==null&&object.currency&&['price','claimed_amount','compare_at_price','was_price','you_save','low','median','high'].includes(key))return `<dt>${escape(name)}</dt><dd>${displayMoney({amount:v,currency:object.currency})}</dd>`;
        return pair(name,v);
      }).join('');
    }
    return `<section class="f-evidence"><h3>${label} · advisory only</h3><dl class="f-detail-list">${fields(value)}</dl></section>`;
  }
  function requestHTML(value,kind,events=null,decisionHTML=''){
    const signals=(value.risk_signals||[]).map(s=>`<li><strong>${escape(s.code)}</strong>: ${escape(s.message)}</li>`).join('');
    const said=value.agent_context;
    const agentSaid=kind==='payment'&&said?`<section class="f-evidence f-agent-said"><h3>What the agent said · unverified</h3><dl class="f-detail-list">${pair('Justification',said.business_justification||'None given')}${said.product_query?pair('Product',said.product_query):''}${said.product_url?pair('Product page (text only)',said.product_url):''}</dl></section>`:'';
    return (kind==='payment'?verdictHTML(value):'')+termsHTML(value,kind)+decisionHTML+(kind==='payment'?(decisionHTML?'':priceHTML(value))+journeyHTML(value)+checksHTML(value):'')+(kind==='payment'?marketSummary(value.market_check):'')+`<details class="f-advisory"><summary>Details · Evidence (advisory) <span class="ap-pill">${escape(kind==='payment'?(value.market_check?'Market data recorded':'Market not checked')+' · '+(value.page_check?'Page checked':'Page not checked'):'Provider provenance')}</span></summary>${provenanceHTML(value,kind)}${agentSaid}${kind==='payment'?`<section class="f-evidence"><h3>Risk signals</h3><ul>${signals||'<li>No risk signals reported.</li>'}</ul></section>`+evidenceHTML('Market check',value.market_check)+evidenceHTML('Page check',value.page_check):''}</details>`+timelineHTML(events);
  }
  function pendingCards(){const pending=pendingRows();return `${pending.map(r=>`<article class="f-request"><div class="f-request-main"><div class="f-symbol blue">${App.icon('bot')}</div><div><div class="f-request-title"><h3>${escape(r.agent)}</h3>${badge(r.status)}${r.raw.counter_of?'<span class="ap-pill">Owner counter-offer</span>':''}</div><p>${escape(r.purpose)} · ${escape(r.description)}</p><small>${escape(r.id)} · ${escape(r.kind)} · ${escape(visual.date(r.created_at))}</small><p>Payee: ${escape(payeeLabel(r.raw,r.kind))}</p></div><div class="f-request-value"><strong>${displayMoney(r)}</strong></div></div><div class="f-request-bottom"><button class="link" data-live-details="${escape(r.id)}" data-kind="${escape(r.kind)}">View request details →</button><div><button class="f-reject" data-live-reject="${escape(r.id)}" data-kind="${escape(r.kind)}">Reject</button>${r.kind==='payment'?`<button class="f-secondary" data-live-counter="${escape(r.id)}" data-kind="payment">Counter</button>`:''}<button class="f-primary" data-live-approve="${escape(r.id)}" data-kind="${escape(r.kind)}">Approve →</button></div></div></article>`).join('')||'<div class="f-empty">No requests are awaiting your approval.</div>'}`;}
  let kindFilter='all',statusFilter='all',companyFilter='all';
  const filteredRows=()=>rows().filter(r=>(kindFilter==='all'||r.kind===kindFilter)&&(statusFilter==='all'||r.status===statusFilter)&&(companyFilter==='all'||r.company===companyFilter));
  const spendPanel=()=>`<div class="f-chart-grid"><section class="panel f-chart-panel"><div class="panel-title"><h3><span class="heading-icon">${App.icon('chart')}</span> Confirmed Spend Overview</h3><span class="f-badge">Last 30 Days</span></div><div class="f-chart-legend"><span>🔵 Confirmed payments and payouts · before refunds</span></div>${visual.live.areaSvg()}</section><section class="panel f-insight"><div class="f-insight-icon">${App.icon('shield')}</div><h3>Every payment. Full visibility.</h3><p>Only verified provider outcomes count as confirmed spending.</p><div><span>Awaiting owner review</span><strong>${pendingRows().length}</strong></div><button class="link" data-route="approvals">Review pending approvals →</button></section></div>`;
  function approvals(){
    const pending=pendingRows(),all=rows();
    return `<div class="finance-screen f-live" data-live-finance="approvals"><section class="f-heading"><div><h1>Approvals</h1><p>Review agent requests, authorize exact terms, and keep spending safe.</p></div><button class="f-primary" data-live-refresh>↻ Refresh</button></section>${loadNotice()}<section class="f-stats">${stat('Pending approvals',pending.length,'shield')}${stat('Pending amount',totalText(sum(pending)),'wallet')}${stat('Approved requests',all.filter(r=>r.raw.owner_decision?.decision==='approved').length,'shield')}${stat('Rejected requests',all.filter(r=>r.raw.owner_decision?.decision==='rejected'&&!r.raw.policy_block).length,'shield')}${stat('Blocked by rules',all.filter(r=>r.raw.policy_block).length,'shield')}</section><div class="f-approval-grid"><section class="f-list-panel f-approval-panel"><div class="f-toolbar"><div class="f-tabs"><button class="selected">Pending (${pending.length})</button></div><span class="f-badge">Exact-term review</span></div><div class="f-request-list">${pendingCards()}</div></section><div class="f-approval-side"><section class="panel f-history"><div class="panel-title"><h3>Approval History</h3><span class="f-badge">30 days</span></div><div class="f-chart-legend"><span>🔵 Approved: ${all.filter(r=>r.raw.owner_decision?.decision==='approved').length}</span><span>🟣 Rejected: ${all.filter(r=>r.raw.owner_decision?.decision==='rejected'&&!r.raw.policy_block).length}</span></div>${visual.live.areaSvg(visual.live.records().filter(r=>r.owner_decision?.decision==='approved').map(r=>({...r,created_at:r.owner_decision.at,amount:'1',currency:'requests'}))) }<p>Owner approvals by decision date</p></section><section class="panel f-security"><div class="f-security-title"><span class="icon mint">${App.icon('shield')}</span><div><h3>You're in control</h3><p>Approval is bound to exact terms.</p></div></div><ul><li>✓ Scoped agent permissions</li><li>✓ Deterministic spending limits</li><li>✓ Decisions recorded in the audit log</li></ul><button class="link" data-route="security">View Security →</button></section><div class="f-demo-note">PayPal sandbox · no real money</div></div></div><section class="f-list-panel ap-history"><div class="ap-section-heading"><div><h3>Decision history</h3><p>Owner decisions, rule blocks and expired requests; completion preserves approval history.</p></div></div>${visual.gridMarkup('decision-history','No owner decisions, rule blocks or expired requests yet.')}</section></div>`;
  }
  let report=null,reportAt=null;
  function reconciliationPanel(){const spend=visual.live.paymentSpend();return `<section class="panel f-reconciliation"><h3>Reconciliation & confirmed spend</h3><p>Gross confirmed payments: ${visual.live.totals(spend.gross)} · Refunds: ${spend.complete?visual.live.totals(spend.refunds):'Load refund totals'} · Net confirmed payments (captured minus refunds): ${spend.complete?visual.live.totals(spend.net):'Not calculated'} · Confirmed payouts: ${visual.live.totals(api.data.payouts.filter(p=>p.status==='provider_confirmed'))}</p><button class="f-secondary" data-live-net>Load refund / net totals</button><p id="reconciliation-summary">${report?reportSummary():'No comparison run in this tab. Read-only PayPal Orders + Transaction Search comparison; refunds and payouts have separate records.'}</p><button class="f-secondary" data-live-report>Compare last 30 days with PayPal</button><p>PayPal sandbox · no real money</p></section>`;}
  function reportSummary(){const rows=(report.rows||[]).filter(r=>r.reason!=='not_submitted'),count=k=>rows.filter(r=>r.result===k).length;return `Compared ${escape(visual.date(reportAt))} · Window ${escape(visual.date(report.start))} – ${escape(visual.date(report.end))} · ${count('matched')} matched · ${count('mismatch')} mismatched · ${count('missing_at_paypal')} missing at PayPal · ${count('pending')} pending. ${report.rows.length-rows.length} never submitted (excluded). Read-only; no funds moved.`;}
  async function loadNet(button){button.disabled=true;try{await Promise.all(api.data.payments.filter(p=>['provider_confirmed','reversed'].includes(p.status)).map(async p=>{const refunds=await api.refunds(p.request_id);(api.data.refunds??={})[p.request_id]=refunds;}));const root=document.querySelector('.f-reconciliation');if(root)root.outerHTML=reconciliationPanel();}catch(err){App.toast(api.message(err));}finally{button.disabled=false;}}
  async function loadReport(button){button.disabled=true;try{const end=new Date(),start=new Date(end.getTime()-30*86400000);report=await api.get('paypal/reconciliation?start='+encodeURIComponent(start.toISOString())+'&end='+encodeURIComponent(end.toISOString()));reportAt=new Date().toISOString();const panel=document.querySelector('#reconciliation-summary');if(panel)panel.innerHTML=reportSummary();}catch(err){const panel=document.querySelector('#reconciliation-summary');if(panel)panel.textContent=api.message(err);}finally{button.disabled=false;}}
  function transactions(){
    const all=rows(),spend=visual.live.paymentSpend();
    return `<div class="finance-screen f-live" data-live-finance="transactions"><section class="f-heading"><div><h1>Transactions</h1><p>Track every payment and payout with verified provider status.</p></div><button class="f-primary" data-live-export>↓ Export Transactions</button></section>${loadNotice()}<section class="f-stats">${stat('Requests',all.length,'transfer')}${stat('Confirmed payments',api.data.payments.filter(r=>r.status==='provider_confirmed').length,'shield')}${stat('Gross confirmed payments',visual.live.totals(spend.gross),'wallet')}${stat('Needs reconciliation',all.filter(r=>r.status==='reconciliation_required').length,'shield')}</section>${spendPanel()}${reconciliationPanel()}<section class="f-list-panel"><div class="f-toolbar"><div class="f-tabs">${[['all','All Transactions'],['payment','Payments'],['payout','Payouts']].map(([kind,label])=>`<button data-live-kind="${kind}" class="${kindFilter===kind?'selected':''}">${label}</button>`).join('')}</div><div class="f-filters"><input id="transaction-search" type="search" aria-label="Search transactions" placeholder="⌕ Search transactions..."><select id="transaction-status" aria-label="Transaction status"><option value="all">All Statuses</option>${[...new Set(all.map(r=>r.status))].map(s=>`<option value="${escape(s)}" ${statusFilter===s?'selected':''}>${escape(statusText(s))}</option>`).join('')}</select><select id="transaction-company" aria-label="Transaction company"><option value="all">All Companies</option>${[...new Set(all.map(r=>r.company))].map(c=>`<option value="${escape(c)}" ${companyFilter===c?'selected':''}>${escape(c)}</option>`).join('')}</select></div></div>${visual.gridMarkup('transaction-ledger','No payment requests or payouts yet.')}</section><p class="f-sandbox-note">PayPal sandbox · no real money</p></div>`;
  }
  function rerender(){
    const root=document.querySelector('[data-live-finance]');if(!root)return;
    const template=document.createElement('template');
    template.innerHTML=root.dataset.liveFinance==='approvals'?approvals():transactions();
    root.querySelector('.f-stats')?.replaceWith(template.content.querySelector('.f-stats'));
    if(root.dataset.liveFinance==='approvals'){
      root.querySelector('.f-request-list').innerHTML=pendingCards();
      root.querySelector('.f-history')?.replaceWith(template.content.querySelector('.f-history'));
    }else{
      root.querySelector('.f-reconciliation')?.replaceWith(template.content.querySelector('.f-reconciliation'));
      root.querySelector('.f-chart-grid')?.replaceWith(template.content.querySelector('.f-chart-grid'));
    }
  }
  async function write(button,key,action){
    if(inflight.has(key))return false;
    inflight.add(key);button.disabled=true;
    let ok=false;
    try{await action();ok=true;}catch(err){App.toast(api.message(err));}
    finally{
      try{await api.refresh();}catch(err){App.toast(api.message(err));}
      window.dispatchEvent(new CustomEvent('finance-updated'));
      window.dispatchEvent(new CustomEvent('wallets-updated'));
      rerender();inflight.delete(key);button.disabled=false;
    }
    return ok;
  }
  async function openRequest(id,kind,confirm=false,notice=''){
    const version=++detailVersion;
    try{
      const value=await api.get(endpoint(kind,id));
      if(version!==detailVersion)return;
      const events=await timeline(kind,id);
      if(version!==detailVersion)return;
      renderRequest(id,kind,value,confirm,notice,events);
    }catch(err){App.toast(api.message(err));}
  }
  function renderRequest(id,kind,value,confirm,notice,events=null){
    const canDecide=value.status==='awaiting_approval';
    const decision=canDecide?`<p class="f-approval-binding">Approval binds to these exact terms. Terms fingerprint: <code>${escape(String(value.terms_sha256||'').slice(0,12))}</code>. Any change to amount, payee or purpose voids this approval.</p><div class="f-decision-slot"><div class="dialog-actions f-request-actions"><button class="f-reject" id="f-live-reject">Reject</button>${kind==='payment'?'<button class="f-secondary" id="f-live-counter" type="button" aria-expanded="false" aria-controls="f-live-counter-form">Counter</button>':''}<button class="f-primary" id="f-live-confirm">${confirm?'Confirm Approval':'Review approval →'}</button></div></div>${kind==='payment'?counterHTML(value):''}`:'';
    App.modal(confirm?'Confirm sandbox approval':'Request details',`<div class="finance-dialog f-live">${notice?`<p role="alert" class="f-live-warning">${escape(notice)}</p>`:''}${requestHTML(value,kind,events,decision)}</div>`);
    bindCounterLinks();
    if(!canDecide){revealCounter=false;return;}
    if(kind==='payment')bindCounter(id);
    document.querySelector('#f-live-reject').onclick=e=>reject(id,kind,e.currentTarget);
    document.querySelector('#f-live-confirm').onclick=async e=>{
      if(!confirm){await openRequest(id,kind,true);return;}
      const button=e.currentTarget;
      let changed=false;
      const ok=await write(button,endpoint(kind,id),async()=>{
        const fresh=await api.get(endpoint(kind,id));
        if(!fresh.terms_sha256||fresh.terms_sha256!==value.terms_sha256){
          changed=true;renderRequest(id,kind,fresh,true,'The terms changed. Review the updated details before confirming again.',events);return;
        }
        try{await api.post(endpoint(kind,id)+'/approve',{terms_sha256:fresh.terms_sha256});}
        catch(err){
          if(err.status===409||['terms_changed','approval_terms_mismatch'].includes(err.code)){
            changed=true;
            await openRequest(id,kind,true,'The terms changed or the request is no longer awaiting approval. Review the latest details.');
          }
          throw err;
        }
      });
      if(ok&&!changed){if(button.isConnected)document.querySelector('#modal').close();App.toast('Sandbox approval recorded. Payment is not yet confirmed.');}
    };
  }
  async function reject(id,kind,button){
    if(await write(button,endpoint(kind,id),()=>api.post(endpoint(kind,id)+'/reject',{}))){
      if(button.isConnected&&button.closest('#modal'))document.querySelector('#modal').close();App.toast('Request rejected.');
    }
  }
  const refundFailure=reason=>({merchant_permission_required:"PayPal needs the merchant's permission for this refund.",provider_rejected:'PayPal declined this refund.',paypal_unavailable:'PayPal could not be reached. The refund is not confirmed.'}[reason]||human(reason));
  async function openTransaction(id,kind){
    const version=++detailVersion;
    try{
      const value=await api.get(endpoint(kind,id));
      let reconciliation=null,refunds=[],referenceError='',refundError='';
      if(kind==='payment'&&['submitted_to_provider','provider_pending','provider_confirmed','reconciliation_required','reversed'].includes(value.status)){
        const results=await Promise.allSettled([api.get(endpoint(kind,id)+'/reconciliation'),api.refunds(id)]);
        // 409 = not executed/captured yet: nothing to show, not an error (UI-T1, UI-T2 INFO-3).
        if(results[0].status==='fulfilled')reconciliation=results[0].value;else if(results[0].reason?.status!==409)referenceError=api.message(results[0].reason);
        if(results[1].status==='fulfilled')refunds=results[1].value;else if(results[1].reason?.status!==409)refundError=api.message(results[1].reason);
      }
      const events=await timeline(kind,id);
      if(version!==detailVersion)return;
      const needs=value.status==='reconciliation_required'||reconciliation?.reconciliation_required;
      App.modal('Transaction details',`<div class="finance-dialog f-live">${requestHTML(value,kind,events)}${needs?'<p class="f-live-warning">Needs reconciliation: no money assumed.</p><button class="f-primary" id="f-live-reconcile">Reconcile sandbox status</button>':''}${kind==='payment'?`<section class="f-evidence"><h3>PayPal order references</h3>${referenceError?`<p role="alert">${escape(referenceError)}</p>`:(reconciliation?.provider_references||[]).map(r=>`<p>${escape(r.provider)} · ${escape(r.provider_order_id)} · ${escape(statusText(r.status))}</p>`).join('')||'<p>No provider order recorded.</p>'}</section><section class="f-evidence"><h3>Refunds</h3>${refundError?`<p role="alert">${escape(refundError)}</p>`:refunds.map(r=>`<article class="f-refund"><strong>${displayMoney(r)}</strong> · ${escape(statusText(r.status))}<p>${escape(r.refund_id)}${r.failure_reason?' · '+escape(refundFailure(r.failure_reason)):''}</p></article>`).join('')||'<p>No refunds recorded.</p>'}</section>${['submitted_to_provider','provider_pending'].includes(value.status)?`<section class="f-evidence"><h3>Waiting for the buyer on PayPal</h3><p>The buyer approves on PayPal's sandbox checkout page. AgentPal captures only after an authenticated PayPal read confirms that exact approval.</p>${(reconciliation?.provider_references||[]).filter(r=>r.payer_action_url).map(r=>`<p>Buyer approval link (sandbox): <code class="f-reference">${escape(r.payer_action_url)}</code></p>`).join('')}<button class="f-primary" id="f-live-capture">Capture after buyer approval</button></section>`:''}${value.status==='provider_confirmed'?'<form id="f-live-refund"><label>Refund amount (optional; blank refunds the remaining amount)<input name="amount" type="text" inputmode="decimal" autocomplete="off" placeholder="0.00" pattern="[0-9]{1,7}(\\.[0-9]{1,2})?"></label><p id="f-live-refund-error" role="alert"></p><button class="f-primary" type="submit">Request sandbox refund</button></form>':''}`:`<dl class="f-detail-list">${pair('PayPal batch ID',value.provider_batch_id)}${pair('Provider item status',value.provider_item_status)}${pair('Warning',value.warning)}${pair('Unclaimed',value.unclaimed)}</dl>`}</div>`);
      bindCounterLinks();
      if(needs)document.querySelector('#f-live-reconcile').onclick=async e=>{
        if(await write(e.currentTarget,endpoint(kind,id),()=>api.post(endpoint(kind,id)+'/reconciliation',{}))){App.toast('Reconciliation completed. Review the current status.');await openTransaction(id,kind);}
      };
      if(kind==='payment'&&['submitted_to_provider','provider_pending'].includes(value.status))document.querySelector('#f-live-capture').onclick=async e=>{
        let pending=false;
        const ok=await write(e.currentTarget,endpoint(kind,id),async()=>{
          try{await api.post(endpoint(kind,id)+'/capture',{});}
          catch(err){if(err.status===409){pending=true;return;}throw err;}
        });
        if(pending)App.toast('PayPal has not recorded the buyer approval yet. Nothing was captured; try again in a few seconds.');
        else if(ok)App.toast('Capture requested. Only a verified PayPal outcome counts as confirmed.');
        await openTransaction(id,kind);
      };
      if(kind==='payment'&&value.status==='provider_confirmed')document.querySelector('#f-live-refund').onsubmit=async e=>{
        e.preventDefault();const amount=new FormData(e.target).get('amount')||'';
        if(amount!==''&&(!/^\d{1,7}(\.\d{1,2})?$/.test(amount)||!/[1-9]/.test(amount))){document.querySelector('#f-live-refund-error').textContent='Enter a positive amount with up to 7 digits and 2 decimal places, or leave it blank.';return;}
        const button=e.target.querySelector('button[type="submit"]');
        if(await write(button,endpoint(kind,id),()=>api.post(endpoint(kind,id)+'/refunds',amount===''?{}:{amount},{idempotency:api.newKey('rf')}))){App.toast('Refund request recorded. Review its status below.');await openTransaction(id,kind);}
      };
    }catch(err){App.toast(api.message(err));}
  }
  function exportCSV(){transactionGrid?.exportDataAsCsv();}
  function columns(history=false){return [
    {field:'description',headerName:'Merchant / receiver',minWidth:190},
    {field:'agent',headerName:'Agent',minWidth:150},
    {field:'company',headerName:'Company',minWidth:170,filter:'agTextColumnFilter'},
    {field:'created_at',headerName:'Created',minWidth:175,valueFormatter:p=>visual.date(p.value)},
    visual.amountColumn(),{field:'currency',headerName:'Currency',minWidth:105,hide:true},
    {field:'status',headerName:'Status',minWidth:180,cellRenderer:visual.status,valueFormatter:p=>statusText(p.value)},
    {field:'kind',headerName:'Type',minWidth:100,maxWidth:130},
    {field:'owner_decision',headerName:'Owner decision',hide:!history,minWidth:230,valueGetter:p=>p.data.raw.owner_decision?(p.data.raw.countered_by?'countered by owner':human(p.data.raw.owner_decision.decision))+' · '+visual.date(p.data.raw.owner_decision.at):'No owner decision'},
    {field:'rule',headerName:'Rule',hide:!history,minWidth:200,valueGetter:p=>p.data.raw.policy_block?.rule||''},
    {field:'id',headerName:'Reference',hide:true},
    {colId:'actions',headerName:'',pinned:'right',lockPinned:true,flex:0,width:135,minWidth:135,sortable:false,filter:false,
      cellRenderer:visual.action('View details',r=>history?openRequest(r.id,r.kind):openTransaction(r.id,r.kind))}
  ];}
  function mount(){
    const root=document.querySelector('[data-live-finance]');if(!root)return;
    if(root.dataset.liveFinance==='transactions'){
      transactionGrid=visual.grid('transaction-ledger',columns(),filteredRows);
      const search=root.querySelector('#transaction-search');
      search.oninput=()=>transactionGrid.setGridOption('quickFilterText',search.value);
      root.querySelector('#transaction-company').onchange=e=>{companyFilter=e.target.value;transactionGrid.setGridOption('rowData',filteredRows());};
      root.querySelector('#transaction-status').onchange=e=>{statusFilter=e.target.value;transactionGrid.setGridOption('rowData',filteredRows());};
    }else visual.grid('decision-history',columns(true),()=>rows().filter(r=>r.raw.owner_decision||r.raw.policy_block||r.status==='expired'));
    root.onclick=async e=>{
      const b=e.target.closest('button');if(!b||b.disabled)return;
      const d=b.dataset;
      if(b.hasAttribute('data-live-net')){await loadNet(b);return;}
      if(b.hasAttribute('data-live-report')){await loadReport(b);return;}
      if(d.liveKind){kindFilter=d.liveKind;root.querySelectorAll('[data-live-kind]').forEach(t=>t.classList.toggle('selected',t.dataset.liveKind===kindFilter));transactionGrid.setGridOption('rowData',filteredRows());return;}
      if(b.hasAttribute('data-live-export')){exportCSV();return;}
      if(b.hasAttribute('data-live-refresh')){b.disabled=true;try{await api.refresh();App.toast('Up to date.');}catch(err){App.toast(api.message(err));}finally{b.disabled=false;}return;}
      b.disabled=true;
      try{
        if(d.liveDetails)await openRequest(d.liveDetails,d.kind);
        else if(d.liveApprove)await openRequest(d.liveApprove,d.kind,true);
        else if(d.liveCounter){revealCounter=true;await openRequest(d.liveCounter,'payment');}
        else if(d.liveReject)await reject(d.liveReject,d.kind,b);
        else if(d.liveTransaction)await openTransaction(d.liveTransaction,d.kind);
      }finally{b.disabled=false;}
    };
  }
  window.addEventListener('agentpal-live',event=>{if(event.detail?.type==='refreshed')rerender();});
  window.FinanceScreens = {transactions,approvals,mount,pendingCount:()=>pendingRows().length,
    recent:()=>rows().slice(0,4),summary:()=>({pending:pendingRows().length,transactions:rows(),requests:pendingRows(),confirmedSpend:sum(rows().filter(r=>r.status==='provider_confirmed'))}),
    // Only replacements the server marks PayPal-verified; negative differences never match sum().
    verifiedSavings:()=>sum(api.data.payments.filter(p=>p.counter_saving_verified===true&&p.status==='provider_confirmed'&&p.counter_of),p=>p.counter_saving,p=>p.terms?.currency),
    verifiedSavingRows:()=>api.data.payments.filter(p=>p.counter_saving_verified===true&&p.status==='provider_confirmed'&&p.counter_of),
    allowance,totalsText:totalText,openRequest,timelineHTML};
  return;
}
const money=n=>new Intl.NumberFormat('en-US',{style:'currency',currency:'USD'}).format(n),esc=s=>window.App.escape(s);
const initialTransactions=[
['TX-1048','2025-10-31','Payment received from Agent Nova','Agent Nova','PayPal Wallet',1250,'Completed','Agent payment'],
['TX-1047','2025-10-31','Payment to OpenAI','ChatGPT Agent','PayPal Wallet',-200,'Completed','API service'],
['TX-1046','2025-10-31','Wallet funded via PayPal','Tanveer','PayPal Wallet',2000,'Completed','Wallet funding'],
['TX-1045','2025-10-30','Agent Subscription','Claude Agent','Business Bank',-49,'Completed','Subscription'],
['TX-1044','2025-10-30','Subscription renewed','Gemini Agent','Card Wallet',-99,'Completed','Subscription'],
['TX-1043','2025-10-29','Funds Added','Tanveer','PayPal Wallet',1000,'Completed','Wallet funding'],
['TX-1042','2025-10-28','Payment to Stripe','Agent Nova','PayPal Wallet',-150,'Completed','API service'],
['TX-1041','2025-10-25','Design subscription','Midjourney Agent','Card Wallet',-30,'Failed','Subscription'],
['TX-1040','2025-10-23','Campaign reimbursement','Gemini Agent','Business Bank',650,'Completed','Refund'],
['TX-1039','2025-10-20','Research API credits','Claude Agent','Card Wallet',-120,'Completed','API service'],
['TX-1038','2025-10-15','Agent hosting','Meta Agent','Business Bank',-85,'Completed','Hosting'],
['TX-1037','2025-10-08','Wallet deposit','Tanveer','PayPal Wallet',1500,'Completed','Wallet funding'],
['TX-1036','2025-10-03','Automation tools','Meta Agent','Card Wallet',-75,'Completed','Subscription']
].map(([id,date,description,agent,wallet,amount,status,category])=>({id,date,description,agent,wallet,amount,status,category}));
const initialRequests=[
{id:'REQ-1008',agent:'Agent Nova',vendor:'Stripe',amount:450,wallet:'PayPal Wallet',purpose:'API service',risk:'Low',status:'Pending',date:'2025-10-31',time:'2 min ago',note:'Monthly API credits for customer payment processing.'},
{id:'REQ-1007',agent:'Gemini Agent',vendor:'Meta Ads',amount:125,wallet:'Business Bank',purpose:'Marketing campaign',risk:'Medium',status:'Pending',date:'2025-10-31',time:'12 min ago',note:'New campaign spend above this agent’s automatic approval threshold.'},
{id:'REQ-1006',agent:'Claude Agent',vendor:'Research API',amount:89,wallet:'Card Wallet',purpose:'Research credits',risk:'Low',status:'Pending',date:'2025-10-30',time:'1 hour ago',note:'Additional research credits for the monthly reporting workflow.'},
{id:'REQ-1005',agent:'ChatGPT Agent',vendor:'OpenAI',amount:200,wallet:'PayPal Wallet',purpose:'API service',risk:'Low',status:'Approved',date:'2025-10-31',time:'Today',note:'Monthly inference budget.',decidedOn:'2025-10-31'},
{id:'REQ-1004',agent:'Claude Agent',vendor:'Agent Subscription',amount:49,wallet:'Business Bank',purpose:'Subscription',risk:'Low',status:'Approved',date:'2025-10-30',time:'Yesterday',note:'Research agent subscription.',decidedOn:'2025-10-30'},
{id:'REQ-1003',agent:'Meta Agent',vendor:'Unknown provider',amount:320,wallet:'Card Wallet',purpose:'New integration',risk:'High',status:'Rejected',date:'2025-10-28',time:'Oct 28',note:'Unverified payment destination.',decidedOn:'2025-10-28',reason:'Provider could not be verified.'}
];
const validTransaction=t=>t&&['id','date','description','agent','wallet','category'].every(k=>typeof t[k]==='string')&&/^\d{4}-\d{2}-\d{2}$/.test(t.date)&&Number.isFinite(Date.parse(t.date))&&Number.isFinite(t.amount)&&['Completed','Failed'].includes(t.status);
const validRequest=r=>r&&['id','agent','vendor','wallet','purpose','date','time','note'].every(k=>typeof r[k]==='string')&&/^\d{4}-\d{2}-\d{2}$/.test(r.date)&&Number.isFinite(Date.parse(r.date))&&Number.isFinite(r.amount)&&r.amount>0&&['Low','Medium','High'].includes(r.risk)&&['Pending','Approved','Rejected'].includes(r.status)&&(!r.reason||typeof r.reason==='string')&&(!r.decidedOn||/^\d{4}-\d{2}-\d{2}$/.test(r.decidedOn));
let data={transactions:initialTransactions,requests:initialRequests};try{const saved=JSON.parse(localStorage.getItem('agentpal-finance-v1'));if(saved&&Array.isArray(saved.transactions)&&Array.isArray(saved.requests)&&saved.transactions.length<5000&&saved.requests.length<5000&&saved.transactions.every(validTransaction)&&saved.requests.every(validRequest)&&new Set(saved.transactions.map(t=>t.id)).size===saved.transactions.length&&new Set(saved.requests.map(r=>r.id)).size===saved.requests.length)data=saved}catch{}
const state={transactions:{query:'',status:'All',wallet:'All',days:30,direction:'All',sort:'newest',page:1},approvals:{query:'',status:'Pending',risk:'All',days:30}};
function persist(){try{localStorage.setItem('agentpal-finance-v1',JSON.stringify(data))}catch{App.reportStorageIssue?App.reportStorageIssue():App.toast('Changes apply to this session; browser storage is unavailable.')}window.dispatchEvent(new CustomEvent('finance-updated'))}
function pending(){return data.requests.filter(r=>r.status==='Pending')}
function dateText(d){return new Date(d+'T12:00:00Z').toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric',timeZone:'UTC'})}
function inDays(date,days){return Date.parse(date+'T00:00:00Z')>=Date.parse('2025-11-01T00:00:00Z')-days*86400000}
function transactionRows(){const s=state.transactions;return data.transactions.filter(t=>(s.status==='All'||t.status===s.status)&&(s.wallet==='All'||t.wallet===s.wallet)&&(s.direction==='All'||(s.direction==='Incoming'?t.amount>0:t.amount<0))&&inDays(t.date,s.days)&&[t.description,t.agent,t.id,t.wallet].some(v=>v.toLowerCase().includes(s.query.toLowerCase()))).sort((a,b)=>s.sort==='amount'?Math.abs(b.amount)-Math.abs(a.amount):b.date.localeCompare(a.date)||b.id.localeCompare(a.id))}
function requestRows(){const s=state.approvals;return data.requests.filter(r=>(s.status==='All'||r.status===s.status)&&(s.risk==='All'||r.risk===s.risk)&&[r.agent,r.vendor,r.id,r.purpose].some(v=>v.toLowerCase().includes(s.query.toLowerCase())))}
function totals(rows){const complete=rows.filter(r=>r.status==='Completed');return {incoming:complete.filter(r=>r.amount>0).reduce((a,r)=>a+r.amount,0),outgoing:-complete.filter(r=>r.amount<0).reduce((a,r)=>a+r.amount,0)}}
function stat(label,value,color,ic,sub){return `<article class="f-stat"><div class="icon ${color}">${App.icon(ic)}</div><div><label>${label}</label><strong>${value}</strong><small>${sub}</small></div><div class="f-spark">${App.spark({blue:'#2466ef',purple:'#9762eb',mint:'#2bb892',orange:'#f1a441'}[color])}</div></article>`}
function badge(s){return `<span class="f-badge ${s.toLowerCase()}">● ${s}</span>`}
function select(label,key,values,current){return `<select aria-label="${label}" data-f-filter="${key}">${values.map(([v,t])=>`<option value="${v}" ${String(current)===String(v)?'selected':''}>${t}</option>`).join('')}</select>`}
function chart(type,days){const start=Date.parse('2025-11-01T00:00:00Z')-days*86400000,bins=Array.from({length:7},()=>[0,0]);if(type==='cash'){data.transactions.filter(t=>t.status==='Completed'&&inDays(t.date,days)).forEach(t=>{const i=Math.min(6,Math.max(0,Math.floor((Date.parse(t.date+'T00:00:00Z')-start)/(days*86400000)*7)));bins[i][t.amount>0?0:1]+=Math.abs(t.amount)})}else data.requests.filter(r=>r.status!=='Pending'&&inDays(r.decidedOn||r.date,days)).forEach(r=>{const i=Math.min(6,Math.max(0,Math.floor((Date.parse((r.decidedOn||r.date)+'T00:00:00Z')-start)/(days*86400000)*7)));bins[i][r.status==='Approved'?0:1]++});const max=Math.max(1,...bins.flat()),cap=type==='cash'?Math.ceil(max/1000)*1000:max;const pts=n=>bins.map((b,i)=>[45+i*99,150-b[n]/cap*115]);const line=n=>pts(n).map(([x,y],i)=>(i?'L':'M')+x+' '+y).join(' ');return `<svg class="f-chart" viewBox="0 0 680 190" role="img" aria-label="${type==='cash'?'Incoming and outgoing payments':'Approved and rejected requests'} over the last ${days} days"><defs><linearGradient id="f-${type}" x2="0" y2="1"><stop stop-color="#296dff" stop-opacity=".24"/><stop offset="1" stop-color="#296dff" stop-opacity=".02"/></linearGradient></defs>${[0,1,2,3].map(i=>`<path d="M45 ${35+i*38.33}H645" stroke="#e9edf8"/><text x="0" y="${39+i*38.33}" fill="#64718b" font-size="10">${type==='cash'?'$':''}${Math.round(cap*(3-i)/3)}</text>`).join('')}<path d="${line(0)} L639 150H45Z" fill="url(#f-${type})"/><path d="${line(0)}" fill="none" stroke="#1764f2" stroke-width="2.5"/><path d="${line(1)}" fill="none" stroke="#9b6be9" stroke-width="2.5"/>${[0,1].map(n=>pts(n).map(([x,y],i)=>`<circle cx="${x}" cy="${y}" r="3" fill="${n?'#9b6be9':'#1764f2'}"><title>${type==='cash'?money(bins[i][n]):bins[i][n]} ${type==='cash'?(n?'outgoing':'incoming'):(n?'rejected':'approved')}</title></circle>`).join('')).join('')}${[0,2,4,6].map(i=>`<text x="${45+i*99}" y="179" text-anchor="middle" font-size="11" fill="#64718b">${new Date(start+(i+.5)*days/7*86400000).toLocaleDateString('en-US',{month:'short',day:'numeric',timeZone:'UTC'})}</text>`).join('')}</svg>`}
function transactionTable(){const rows=transactionRows(),s=state.transactions;const pages=Math.max(1,Math.ceil(rows.length/6));s.page=Math.min(s.page,pages);return `<div class="f-table-scroll"><table class="f-table"><thead><tr><th>Transaction</th><th>Agent</th><th>Wallet</th><th>Date</th><th>Amount</th><th>Status</th><th>Details</th></tr></thead><tbody>${rows.slice((s.page-1)*6,s.page*6).map(t=>`<tr><td><div class="f-cell"><span class="f-symbol ${t.amount>0?'mint':'blue'}">${App.icon(t.amount>0?'wallet':'transfer')}</span><div><strong>${esc(t.description)}</strong><small>${esc(t.id)} · ${esc(t.category)}</small></div></div></td><td>${esc(t.agent)}</td><td>${esc(t.wallet)}</td><td>${dateText(t.date)}</td><td class="f-amount ${t.amount>0?'positive':''}">${t.amount>0?'+':'−'}${money(Math.abs(t.amount))}</td><td>${badge(t.status)}</td><td><button class="f-more" data-f-tx="${esc(t.id)}" aria-label="View ${esc(t.id)}">•••</button></td></tr>`).join('')||'<tr><td colspan="7" class="f-empty">No transactions match these filters.</td></tr>'}</tbody></table></div><div class="f-table-footer"><span>${rows.length?`Showing ${(s.page-1)*6+1}–${Math.min(s.page*6,rows.length)} of ${rows.length} transactions`:'0 transactions'} · Demo data</span><div><button data-f-page="prev" ${s.page===1?'disabled':''}>← Previous</button><b>${s.page} / ${pages}</b><button data-f-page="next" ${s.page===pages?'disabled':''}>Next →</button></div></div>`}
function transactions(){const s=state.transactions,all=data.transactions.filter(t=>inDays(t.date,s.days)),v=totals(all),completed=all.filter(t=>t.status==='Completed').length;return `<div class="finance-screen" data-finance="transactions"><section class="f-heading"><div><h1>Transactions</h1><p>Track every payment, monitor cash flow, and stay in control.</p></div><button class="f-primary" data-f-export>↓ Export Transactions</button></section><section class="f-stats">${stat('Total Incoming',money(v.incoming),'blue','wallet',`${s.days}-day received payments`)}${stat('Total Outgoing',money(v.outgoing),'purple','transfer',`${s.days}-day completed spending`)}${stat('Net Cash Flow',money(v.incoming-v.outgoing),'mint','chart','Incoming minus outgoing')}${stat('Completed Payments',completed,'orange','shield',`${all.length?Math.round(completed/all.length*100):0}% success rate`)}</section><div class="f-chart-grid"><section class="panel f-chart-panel"><div class="panel-title"><h3><span class="heading-icon">${App.icon('chart')}</span> Cash Flow Overview</h3>${select('Transaction date range','days',[[7,'Last 7 Days'],[30,'Last 30 Days'],[90,'Last 90 Days']],s.days)}</div><div class="f-chart-legend"><span>🔵 Incoming <b>${money(v.incoming)}</b></span><span>🟣 Outgoing <b>${money(v.outgoing)}</b></span></div>${chart('cash',s.days)}</section><section class="panel f-insight"><div class="f-insight-icon">${App.icon('shield')}</div><h3>Every payment. Full visibility.</h3><p>Your agents' payments are tracked across all connected wallets.</p><div><span>Payment success</span><strong>${all.length?Math.round(completed/all.length*100):0}%</strong></div><div class="f-meter"><i style="width:${all.length?completed/all.length*100:0}%"></i></div><button class="link" data-route="approvals">Review pending approvals →</button></section></div><section class="f-list-panel"><div class="f-toolbar"><div class="f-tabs">${['All','Incoming','Outgoing'].map(t=>`<button data-f-direction="${t}" class="${s.direction===t?'selected':''}">${t==='All'?'All Transactions':t}</button>`).join('')}</div><div class="f-filters"><input id="f-search" aria-label="Search transactions" placeholder="⌕  Search transactions..." value="${esc(s.query)}">${select('Transaction status','status',[['All','All Statuses'],['Completed','Completed'],['Failed','Failed']],s.status)}${select('Transaction wallet','wallet',[['All','All Wallets'],['PayPal Wallet','PayPal Wallet'],['Business Bank','Business Bank'],['Card Wallet','Card Wallet']],s.wallet)}${select('Sort transactions','sort',[['newest','Newest first'],['amount','Largest amount']],s.sort)}<button class="f-reset" data-f-reset>Reset</button></div></div><div id="f-table-content">${transactionTable()}</div></section></div>`}
function approvalList(){const rows=requestRows();return `<div class="f-request-list">${rows.map(r=>`<article class="f-request"><div class="f-request-main"><div class="f-symbol ${r.risk==='Medium'?'purple':'blue'}">${App.icon('bot')}</div><div><div class="f-request-title"><h3>${esc(r.agent)}</h3>${badge(r.status)}</div><p>${esc(r.purpose)} <span>•</span> ${esc(r.vendor)}</p><small>${esc(r.id)} · ${esc(r.wallet)} · ${esc(r.time)}</small></div><div class="f-request-value"><strong>${money(r.amount)}</strong><span class="f-risk ${r.risk.toLowerCase()}">◈ ${r.risk} Risk</span></div></div><div class="f-request-bottom"><button class="link" data-f-request="${esc(r.id)}">View request details →</button><div>${r.status==='Pending'?`<button class="f-reject" data-f-decision="Rejected" data-id="${esc(r.id)}">Reject</button><button class="f-primary" data-f-decision="Approved" data-id="${esc(r.id)}">Approve →</button>`:`<span class="f-reviewed">✓ Reviewed ${dateText(r.decidedOn||r.date)}</span>`}</div></div></article>`).join('')||'<div class="f-empty"><strong>All clear here</strong><p>No requests match your selection.</p></div>'}</div>`}
function approvals(){const s=state.approvals,p=pending(),approved=data.requests.filter(r=>r.status==='Approved'),rejected=data.requests.filter(r=>r.status==='Rejected');return `<div class="finance-screen" data-finance="approvals"><section class="f-heading"><div><h1>Approvals</h1><p>Review agent requests, authorize payments, and keep spending safe.</p></div><button class="f-primary" data-f-policy>${App.icon('shield')} Approval Settings</button></section><section class="f-stats">${stat('Pending Approvals',p.length,'orange','shield','Awaiting your review')}${stat('Pending Amount',money(p.reduce((a,r)=>a+r.amount,0)),'blue','wallet','Across connected wallets')}${stat('Approved Payments',approved.length,'mint','shield',money(approved.reduce((a,r)=>a+r.amount,0))+' authorized')}${stat('Rejected Requests',rejected.length,'purple','shield','Reviewed and declined')}</section><div class="f-approval-grid"><section class="f-list-panel f-approval-panel"><div class="f-toolbar"><div class="f-tabs">${['Pending','Approved','Rejected','All'].map(t=>`<button data-f-status="${t}" class="${s.status===t?'selected':''}">${t} ${t==='Pending'?`(${p.length})`:''}</button>`).join('')}</div><div class="f-filters"><input id="f-search" aria-label="Search approval requests" placeholder="⌕  Search requests..." value="${esc(s.query)}">${select('Request risk','risk',[['All','All Risks'],['Low','Low Risk'],['Medium','Medium Risk'],['High','High Risk']],s.risk)}<button class="f-reset" data-f-reset>Reset</button></div></div><div id="f-request-content">${approvalList()}</div></section><div class="f-approval-side"><section class="panel f-history"><div class="panel-title"><h3>Approval History</h3>${select('Approval history range','days',[[7,'Last 7 Days'],[30,'Last 30 Days'],[90,'Last 90 Days']],s.days)}</div><div class="f-chart-legend"><span>🔵 Approved</span><span>🟣 Rejected</span></div>${chart('approvals',s.days)}<p>Review activity for the selected period</p></section><section class="panel f-security"><div class="f-security-title"><span class="icon mint">${App.icon('shield')}</span><div><h3>You're in control</h3><p>Every request gets your attention.</p></div></div><ul><li>✓ Agent permissions verified</li><li>✓ Spending limits checked</li><li>✓ Decisions recorded in history</li></ul><button class="link" data-f-policy>Manage approval rules →</button></section><div class="f-demo-note">ⓘ Demo workspace · Decisions update local demo records. No real payments are made.</div></div></div></div>`}
function refresh(){const r=document.querySelector('[data-finance]');if(!r)return;const type=r.dataset.finance;r.outerHTML=type==='transactions'?transactions():approvals();mount()}
function decide(id,status,reason=''){if(!['Approved','Rejected'].includes(status))return false;if(status==='Rejected'&&!String(reason).trim())return false;const r=data.requests.find(r=>r.id===id);if(!r||r.status!=='Pending')return false;if(status==='Approved'&&window.WalletScreen?.authorizePayment){const payment=WalletScreen.authorizePayment(r.wallet,r.amount,r.id);if(!payment.ok){App.toast(payment.error);return false}}r.status=status;r.reason=String(reason).trim().slice(0,300);r.decidedOn='2025-10-31';r.time='Just now';if(status==='Approved')data.transactions.unshift({id:'TX-'+id,date:'2025-10-31',description:'Payment to '+r.vendor,agent:r.agent,wallet:r.wallet,amount:-r.amount,status:'Completed',category:r.purpose});persist();return true}
function decisionDialog(id,status){const r=data.requests.find(r=>r.id===id);if(!r||r.status!=='Pending')return;App.modal(status==='Approved'?'Approve Payment':'Reject Request',`<div class="finance-dialog"><div class="f-dialog-amount">${money(r.amount)}</div><p><strong>${esc(r.agent)}</strong> → ${esc(r.vendor)}<br>${esc(r.purpose)} · ${esc(r.wallet)}</p>${r.wallet==='Card Wallet'?'<p class="f-demo-note">This demo card is funded by your Business Bank Account.</p>':''}<span class="f-risk ${r.risk.toLowerCase()}">${r.risk} Risk</span><form id="f-decision-form">${status==='Rejected'?'<label>Reason for rejection<textarea name="reason" required maxlength="300" placeholder="Explain why this request was declined"></textarea></label>':''}<p class="f-demo-note">Demo only. This updates your local approval history${status==='Approved'?' and creates a demo transaction':''}.</p><div class="dialog-actions"><button type="button" class="f-secondary" id="f-cancel">Cancel</button><button type="submit" class="${status==='Approved'?'f-primary':'f-reject'}">${status==='Approved'?'Confirm Approval':'Confirm Rejection'}</button></div></form></div>`);document.querySelector('#f-cancel').onclick=()=>document.querySelector('dialog[open]').close();document.querySelector('#f-decision-form').onsubmit=e=>{e.preventDefault();const reason=new FormData(e.target).get('reason')||'';if(status==='Rejected'&&!reason.trim()){App.toast('Enter a reason for rejection.');return}if(decide(id,status,reason)){document.querySelector('dialog[open]').close();App.toast('Demo request '+status.toLowerCase());refresh()}}}
function details(id,request){const r=(request?data.requests:data.transactions).find(r=>r.id===id);if(!r)return;App.modal(request?'Payment Request':'Transaction Details',`<div class="finance-dialog"><div class="f-dialog-amount">${request?'':r.amount>0?'+':'−'}${money(Math.abs(r.amount))}</div>${badge(r.status)}<dl class="f-detail-list"><dt>Reference</dt><dd>${esc(r.id)}</dd><dt>Agent</dt><dd>${esc(r.agent)}</dd><dt>${request?'Recipient':'Description'}</dt><dd>${esc(request?r.vendor:r.description)}</dd><dt>Wallet</dt><dd>${esc(r.wallet)}</dd><dt>Date</dt><dd>${dateText(r.date)}</dd><dt>Purpose</dt><dd>${esc(r.purpose||r.category)}</dd></dl>${request?`<p>${esc(r.note)}</p>${r.reason?`<p><strong>Rejection reason:</strong> ${esc(r.reason)}</p>`:''}${r.status==='Pending'?`<div class="dialog-actions"><button class="f-reject" data-f-modal-decision="Rejected" data-id="${esc(r.id)}">Reject</button><button class="f-primary" data-f-modal-decision="Approved" data-id="${esc(r.id)}">Approve →</button></div>`:''}`:'<p class="f-demo-note">Demo transaction · No real money was moved.</p>'}</div>`)}
function exportCSV(){const rows=transactionRows();const quote=v=>'"'+String(v).replace(/"/g,'""')+'"';const csv=[['Reference','Date','Description','Agent','Wallet','Amount USD','Status'],...rows.map(r=>[r.id,r.date,r.description,r.agent,r.wallet,r.amount.toFixed(2),r.status])].map(r=>r.map(quote).join(',')).join('\r\n');const url=URL.createObjectURL(new Blob(['\uFEFF'+csv],{type:'text/csv;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download='agentpal-transactions.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);App.toast(`Exported ${rows.length} filtered transactions`)}
function policies(){let rules={threshold:100,unknown:true,notify:true};try{const saved=JSON.parse(localStorage.getItem('agentpal-approval-rules'));if(saved&&Number.isFinite(saved.threshold)&&saved.threshold>=0)rules={threshold:saved.threshold,unknown:!!saved.unknown,notify:!!saved.notify}}catch{}App.modal('Approval Settings',`<div class="finance-dialog"><form id="f-policy-form"><label>Review threshold (USD)<input type="number" name="threshold" min="0" step="0.01" required value="${Number(rules.threshold)}"></label><label class="f-check"><input name="unknown" type="checkbox" ${rules.unknown?'checked':''}> Flag new payment recipients for review</label><label class="f-check"><input name="notify" type="checkbox" ${rules.notify?'checked':''}> Show pending approval notifications</label><p class="f-demo-note">Saved demo preferences for future requests. Existing requests remain in the review queue.</p><button class="f-primary">Save Settings</button></form></div>`);document.querySelector('#f-policy-form').onsubmit=e=>{e.preventDefault();const d=new FormData(e.target);if(!Number.isFinite(+d.get('threshold'))||+d.get('threshold')<0){App.toast('Enter a valid threshold.');return}try{localStorage.setItem('agentpal-approval-rules',JSON.stringify({threshold:+d.get('threshold'),unknown:d.has('unknown'),notify:d.has('notify')}))}catch{}document.querySelector('dialog[open]').close();App.toast('Demo approval preferences saved')}}
function mount(){const root=document.querySelector('[data-finance]');if(!root)return;const type=root.dataset.finance,s=state[type];root.onclick=e=>{const b=e.target.closest('button');if(!b)return;if(b.hasAttribute('data-f-export'))exportCSV();else if(b.hasAttribute('data-f-policy'))policies();else if(b.dataset.fTx)details(b.dataset.fTx,false);else if(b.dataset.fRequest)details(b.dataset.fRequest,true);else if(b.dataset.fDecision)decisionDialog(b.dataset.id,b.dataset.fDecision);else if(b.dataset.fDirection){s.direction=b.dataset.fDirection;s.page=1;refresh()}else if(b.dataset.fStatus){s.status=b.dataset.fStatus;refresh()}else if(b.dataset.fPage){s.page+=b.dataset.fPage==='next'?1:-1;document.querySelector('#f-table-content').innerHTML=transactionTable()}else if(b.hasAttribute('data-fReset')||b.hasAttribute('data-f-reset')){Object.assign(s,type==='transactions'?{query:'',status:'All',wallet:'All',days:30,direction:'All',sort:'newest',page:1}:{query:'',status:'Pending',risk:'All',days:30});refresh()}};root.onchange=e=>{if(e.target.dataset.fFilter){const key=e.target.dataset.fFilter;s[key]=key==='days'?+e.target.value:e.target.value;s.page=1;refresh()}};document.querySelector('#f-search').oninput=e=>{s.query=e.target.value;s.page=1;document.querySelector(type==='transactions'?'#f-table-content':'#f-request-content').innerHTML=type==='transactions'?transactionTable():approvalList()}}
document.addEventListener('click',e=>{const b=e.target.closest('[data-f-modal-decision]');if(b)decisionDialog(b.dataset.id,b.dataset.fModalDecision)});
window.addEventListener('wallet-transaction',event=>{const d=event.detail;if(!d||!Number.isFinite(d.amount)||typeof d.wallet!=='string')return;const id='TX-W-'+Date.now();data.transactions.unshift({id,date:'2025-10-31',description:String(d.description||'Wallet transaction'),agent:'Tanveer',wallet:d.wallet,amount:d.amount,status:'Completed',category:d.destination?'Wallet transfer':'Wallet funding'});if(d.destination)data.transactions.unshift({id:id+'-IN',date:'2025-10-31',description:'Transfer received',agent:'Tanveer',wallet:String(d.destination),amount:Math.abs(d.amount),status:'Completed',category:'Wallet transfer'});persist()});
window.FinanceScreens={transactions,approvals,mount,recent:()=>data.transactions.slice(0,4).map(t=>({...t})),pendingCount:()=>pending().length,summary:()=>({pending:pending().length,transactions:data.transactions.map(t=>({...t})),requests:data.requests.map(r=>({...r})),...totals(data.transactions)})};
})();
