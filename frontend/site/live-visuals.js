/* Same-origin, community-only owner reports. No API requests originate here. */
(() => {
  'use strict';
  const grids = new Map(), charts = new Map();
  const api = () => window.AgentPalAPI;
  const human = v => String(v ?? 'Unavailable').replace(/_/g, ' ');
  const nonce = () => {
    const value = document.querySelector('meta[name="csp-nonce"]')?.content;
    return value && value !== '__CSP_NONCE__' ? value : undefined;
  };
  const escape = v => window.App.escape(String(v ?? ''));
  const date = v => {
    const d = new Date(v);
    return v && Number.isFinite(d.getTime()) ? new Intl.DateTimeFormat(undefined, {dateStyle:'medium', timeStyle:'short'}).format(d) : 'Unavailable';
  };
  const csvSafe = v => /^[=+\-@\t\r]/.test(String(v ?? '')) ? "'" + v : String(v ?? '');
  const text = (value, className = '') => {
    const span = document.createElement('span');
    span.className = className; span.textContent = String(value ?? '—'); span.title = span.textContent;
    return span;
  };
  const status = p => {
    const value = p.value === 'blocked_by_rule' ? 'Blocked by rule' : p.value === 'reconciliation_required' ? 'needs reconciliation' : human(p.value);
    const tone = ['active','approved','provider_confirmed','completed'].includes(p.value) ? 'good' : ['rejected','blocked_by_rule','expired','suspended','failed'].includes(p.value) ? 'bad' : 'waiting';
    return text(value, 'ap-pill ' + tone);
  };
  const tags = p => {
    const host = document.createElement('div'); host.className = 'ap-tags';
    for (const tag of p.value || []) host.append(text(tag, 'ap-tag'));
    if (!host.childNodes.length) host.append(text('No capabilities granted'));
    host.title = (p.value || []).join(', '); return host;
  };
  const action = (label, fn) => p => {
    const b = document.createElement('button'); b.type = 'button'; b.className = 'ap-row-action'; b.textContent = label;
    b.setAttribute('aria-label', label + ' · ' + (p.data.name || p.data.description || p.data.id || 'request'));
    b.onclick = async e => { e.stopPropagation(); b.disabled = true; try { await fn(p.data); } finally { b.disabled = false; } };
    return b;
  };
  const gridMarkup = (id, empty = 'No records yet.') => `<div class="ap-grid-shell"><div id="${id}" class="ap-grid ag-theme-quartz" aria-label="${escape(id.replace(/-/g,' '))}" data-empty="${escape(empty)}"></div></div>`;
  function destroyWithin(root) {
    for (const [el, entry] of grids) if (!el.isConnected || root?.contains(el)) { window.removeEventListener?.('resize',entry.onResize); entry.grid.destroy(); grids.delete(el); }
    for (const [el, entry] of charts) if (!el.isConnected || root?.contains(el)) { entry.chart?.destroy(); charts.delete(el); }
  }
  // The vendored no-font theme needs self-contained icons for visible controls.
  const svg = path => '<svg class="ap-grid-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="'+path+'"/></svg>';
  const icons = {
    first:svg('M6 5v14M18 5l-7 7 7 7'),previous:svg('m15 5-7 7 7 7'),
    next:svg('m9 5 7 7-7 7'),last:svg('M18 5v14M6 5l7 7-7 7'),
    sortAscending:svg('m6 10 6-6 6 6M12 4v16'),sortDescending:svg('m6 14 6 6 6-6M12 20V4'),
    sortUnSort:svg('m4 8 4-4 4 4M8 4v16m4-4 4 4 4-4M16 20V4'),
    filter:svg('M3 5h18l-7 8v6l-4 2v-8z'),filterActive:svg('M3 5h18l-7 8v6l-4 2v-8z'),
    selectOpen:svg('m6 9 6 6 6-6'),menu:svg('M4 6h16M4 12h16M4 18h16'),
    menuAlt:svg('M4 6h16M4 12h16M4 18h16'),close:svg('m6 6 12 12M6 18 18 6'),
    search:svg('M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14m5 12 6 6')
  };
  function grid(id, columnDefs, rows, options = {}) {
    const el = document.getElementById(id); if (!el) return;
    destroyWithin();
    if (grids.has(el)) return grids.get(el).grid;
    const responsiveColumns = () => {
      const phone=window.matchMedia?.('(max-width: 600px)').matches;
      if(!phone || !['transaction-ledger','decision-history','connected-agents'].includes(id))return columnDefs;
      const priority=id==='connected-agents'?['spent','status']:['amount','status'];
      return [...priority.map(field=>({...columnDefs.find(c=>c.field===field),minWidth:field==='status'?142:116,width:field==='status'?142:116,flex:0,pinned:null})),
        ...columnDefs.filter(c=>!priority.includes(c.field)).map(c=>({...c,pinned:null}))];
    };
    const instance = window.agGrid.createGrid(el, {
      theme:'legacy', icons, styleNonce:nonce(), columnDefs:responsiveColumns(), rowData:rows(),
      defaultColDef:{cellDataType:false, sortable:true, filter:true, resizable:true, minWidth:145, flex:1,
        tooltipValueGetter:p => String(p.valueFormatted ?? p.value ?? ''),
        cellRenderer:p => text(p.valueFormatted ?? p.value)},
      getRowId:p => String(p.data.id), pagination:true, paginationPageSize:10,
      paginationPageSizeSelector:[10,25,50], rowHeight:62, headerHeight:46,
      animateRows:false, ensureDomOrder:true, suppressCellFocus:false,
      overlayNoRowsTemplate:'<span class="ap-empty">'+escape(el.dataset.empty)+'</span>',
      ...options,
      defaultCsvExportParams:{fileName:'agentpal-sandbox-'+id+'.csv',
        processCellCallback:p => csvSafe(p.value), columnKeys:columnDefs.filter(c=>c.field).map(c=>c.field)},
    });
    let phone=window.matchMedia?.('(max-width: 600px)').matches;
    const onResize=()=>{const next=window.matchMedia?.('(max-width: 600px)').matches;if(next!==phone){phone=next;instance.setGridOption('columnDefs',responsiveColumns());}};
    window.addEventListener('resize',onResize);
    grids.set(el, {grid:instance, rows, onResize}); return instance;
  }
  const money = p => p.value == null ? 'Unavailable' : api().money(String(p.value), p.data.currency) + ' ' + p.data.currency;
  const amountColumn = (field = 'amount', headerName = 'Amount') => ({field, headerName, minWidth:165, cellClass:'ap-number', headerClass:'ap-number-header',
    filter:'agTextColumnFilter', valueFormatter:money, comparator:compareDecimal});
  function units(value) {
    const m = /^(\d+)(?:\.(\d{1,8}))?$/.exec(String(value ?? ''));
    return m ? BigInt(m[1])*100000000n + BigInt((m[2] || '').padEnd(8,'0')) : null;
  }
  function compareDecimal(a,b) { const x=units(a), y=units(b); return x===y?0:x===null?-1:y===null?1:x<y?-1:1; }
  function decimal(value) { return (value/100000000n)+'.'+(value%100000000n).toString().padStart(8,'0'); }
  function aggregate(rows, key) {
    const groups = new Map();
    for (const r of rows) {
      const n=units(r.amount); if(n===null || !r.currency)continue;
      const name=key(r); if(!name)continue;
      const id=JSON.stringify([name,r.currency]), g=groups.get(id)||{name,currency:r.currency,value:0n};
      g.value+=n; groups.set(id,g);
    }
    return [...groups.values()].map(g=>({name:g.name,currency:g.currency,amount:decimal(g.value),value:Number(decimal(g.value)),label:g.name+' · '+g.currency}));
  }
  const payments = () => [...api().data.payments.filter(p=>['provider_confirmed','reversed'].includes(p.status)).map(p=>({amount:p.terms?.amount,currency:p.terms?.currency,created_at:p.created_at,agent_id:p.agent_id||p.terms?.agent_id})),...(api().data.payouts||[]).filter(p=>['provider_confirmed','reversed'].includes(p.status))];
  const daily = () => aggregate(payments(), r => {
    const d=new Date(r.created_at); if(!Number.isFinite(d.getTime()))return null;
    return [d.getFullYear(),String(d.getMonth()+1).padStart(2,'0'),String(d.getDate()).padStart(2,'0')].join('-');
  }).sort((a,b)=>a.name.localeCompare(b.name));
  function company(agent={}) {
    const metadata=agent.metadata||{};
    const explicit=metadata.company||metadata.company_name||agent.company;
    if(typeof explicit==='string'&&explicit.trim())return explicit.trim();
    for(const value of [agent.role,metadata.role,agent.description])if(typeof value==='string'&&/[·|:]/.test(value))return value.split(/[·|:]/)[0].trim()||'Independent';
    const name=String(agent.name||'').trim().split(/\s+/)[0];
    return ['Atlas','Forge','Muse','Ledger'].includes(name)?'Northstar Digital':['Orbit','Pulse','Keel'].includes(name)?'Lumen Cloud':'Independent';
  }
  const agentRows = () => api().data.agents.map(a => {
    const w=api().data.wallets.find(w=>w.agent_id===a.agent_id);
    return {...a,id:a.agent_id,company:company(a),currency:w?.currency,budget:w?.limits?.monthly_budget,spent:w?.allowance?.spent,
      available:w?.allowance?.available,period:w?.allowance?.period,key_ids:(a.keys||[]).map(k=>k.key_id+' · '+(k.status==='live'?'active':k.status)).join(', ')};
  });
  function chartMarkup(id, empty) {
    return `<div class="ap-chart-wrap"><div id="${id}" class="ap-chart" aria-label="${escape(id.replace(/-/g,' '))}"></div><p class="ap-chart-empty" id="${id}-empty">${escape(empty)}</p></div>`;
  }
  const colors = ['#2868ed','#9a73e8','#32b49d','#f4ae52','#e8798f','#5b9dcb'];
  function chart(id, build) {
    const el=document.getElementById(id); if(!el)return;
    destroyWithin(); if(charts.has(el))return;
    charts.set(el,{build,chart:null}); updateChart(el,charts.get(el));
  }
  function updateChart(el, entry) {
    const options=entry.build(), empty=!options.data?.length;
    document.getElementById(el.id+'-empty').hidden=!empty; el.hidden=empty;
    if(empty){entry.chart?.destroy();entry.chart=null;return;}
    const full={container:el,styleNonce:nonce(),background:{fill:'transparent'},
      theme:{palette:{fills:colors,strokes:colors}},
      legend:{position:'bottom'},padding:{top:15,right:22,bottom:15,left:12}, ...options};
    if(entry.chart)entry.chart.update(full); else entry.chart=window.agCharts.AgCharts.create(full);
  }
  function bars(data, seriesName='Requests') {
    return {data,series:[{type:'bar',direction:'horizontal',xKey:'label',yKey:'value',yName:seriesName}],legend:{enabled:false}};
  }
  function spendOptions(data, direction = 'vertical') {
    const currencies=[...new Set(data.map(r=>r.currency))];
    const names=[...new Set(data.map(r=>r.name))];
    return {data:names.map(name=>Object.assign({name},...data.filter(r=>r.name===name).map(r=>({[r.currency]:r.value})))),
      series:currencies.map(currency=>({type:'bar',direction,xKey:'name',yKey:currency,yName:currency})),legend:{enabled:currencies.length>1},
      subtitle:{text:'Gross payments and payouts · currencies shown separately'}};
  }
  function mountDaily(id) { chart(id,()=>{const options=spendOptions(daily());return {...options,series:options.series.map(s=>({...s,type:'area',direction:undefined,fillOpacity:.2,marker:{enabled:true,size:6}})),subtitle:{text:'Daily gross payments and payouts · currencies shown separately'}};}); }
  function refreshChart(id){const el=document.getElementById(id),entry=charts.get(el);if(entry)updateChart(el,entry);}
  function mountAnalytics() {
    chart('spend-by-company',()=>spendOptions(aggregate(payments(),r=>company(api().data.agents.find(a=>a.agent_id===r.agent_id))),'horizontal'));
    chart('spend-by-agent',()=>spendOptions(aggregate(payments(),r=>r.agent_id).map(g=>{const agent=api().data.agents.find(a=>a.agent_id===g.name);return {...g,name:agent ? agent.name+(api().data.agents.filter(a=>a.name===agent.name).length>1?' · '+g.name.slice(-6):'') : g.name};}),'horizontal'));
    chart('request-outcomes',()=>{
      const labels={provider_confirmed:'Confirmed',prepared:'Awaiting',awaiting_approval:'Awaiting',approved:'Awaiting',submitted_to_provider:'Awaiting',provider_pending:'Awaiting',rejected:'Rejected',expired:'Expired',cancelled:'Cancelled',failed:'Failed',reconciliation_required:'Needs reconciliation',reversed:'Reversed'};
      const counts=new Map();for(const r of [...api().data.payments,...api().data.payouts]){const label=r.policy_block?'Blocked by rule':labels[r.status]||human(r.status);counts.set(label,(counts.get(label)||0)+1);}
      return {data:[...counts].map(([label,value])=>({label,value})),series:[{type:'donut',angleKey:'value',calloutLabelKey:'label',sectorLabelKey:'value',innerRadiusRatio:.72}],legend:{enabled:true}};
    });
    chart('risk-recommendations',()=>{const counts=new Map();for(const p of api().data.payments){const label=human(p.recommendation);counts.set(label,(counts.get(label)||0)+1);}return bars([...counts].map(([label,value])=>({label,value})));});
    mountDaily('daily-confirmed-spend');
    grid('agent-summary',[{field:'name',headerName:'Agent',minWidth:200},{field:'period',headerName:'Budget period'},amountColumn('spent','Net spend'),amountColumn('budget','Monthly budget'),amountColumn('available','Available allowance')],agentRows);
  }
  window.addEventListener('agentpal-live',e=>{if(e.detail?.type!=='refreshed')return;destroyWithin();for(const entry of grids.values())entry.grid.setGridOption('rowData',entry.rows());for(const [el,entry] of charts)updateChart(el,entry);});
  // Shared live presentation. All series are zero-filled UTC days from actual records;
  // money stays in exact decimal units and currencies are never added together.
  const escapeText = value => window.App.escape(String(value ?? ''));
  const records = () => [...(api().data.payments||[]).map(p=>({...p,amount:p.terms?.amount,currency:p.terms?.currency,agent_id:p.agent_id||p.terms?.agent_id})),...(api().data.payouts||[])];
  const confirmed = () => records().filter(p=>['provider_confirmed','reversed'].includes(p.status));
  function paymentSpend(){
    const captured=(api().data.payments||[]).filter(p=>['provider_confirmed','reversed'].includes(p.status));
    const gross=captured.map(p=>({amount:p.terms?.amount,currency:p.terms?.currency}));
    const refunds=captured.map(p=>{const known=api().data.refunds?.[p.request_id];
      if(p.status==='reversed')return {amount:p.terms?.amount,currency:p.terms?.currency};
      if(!known)return null;
      const amount=known.filter(r=>r.status==='COMPLETED').reduce((n,r)=>n+(units(r.amount)||0n),0n);
      return {amount:decimal(amount),currency:p.terms?.currency};});
    const complete=refunds.every(Boolean);
    const net=complete?gross.map((p,i)=>({amount:decimal((units(p.amount)||0n)-(units(refunds[i].amount)||0n)),currency:p.currency})):[];
    return {gross,refunds:refunds.filter(Boolean),net,complete};
  }
  const monthStart = () => {const d=new Date();return Date.UTC(d.getUTCFullYear(),d.getUTCMonth(),1);};
  const thisMonth = rows => rows.filter(p=>Date.parse(p.created_at)>=monthStart());
  function totals(rows, value=r=>r.amount, currency=r=>r.currency) {
    const grouped=new Map();
    for(const r of rows){const n=units(value(r)),c=currency(r);if(n===null||!c)continue;grouped.set(c,(grouped.get(c)||0n)+n);}
    if(!grouped.size)for(const w of api().data.wallets||[])if(w.currency)grouped.set(w.currency,0n);
    return [...grouped].map(([c,n])=>escapeText(api().money(decimal(n),c))).join(' · ')||'No data';
  }
  const allowanceText = key => totals(api().data.wallets,w=>w.allowance?.[key],w=>w.currency);
  const budgetText = () => totals(api().data.wallets,w=>w.limits?.monthly_budget,w=>w.currency);
  const holdsText = () => totals(api().data.wallets,w=>decimal((units(w.allowance?.reserved)||0n)+(units(w.allowance?.pending)||0n)),w=>w.currency);
  const percent = (n,d) => d>0?Math.min(100,Math.max(0,Math.round(n/d*100))):0;
  function usage(wallets=api().data.wallets){const groups=new Map();for(const w of wallets){const g=groups.get(w.currency)||[0,0];g[0]+=Number(w.allowance?.spent)||0;g[1]+=Number(w.limits?.monthly_budget)||0;groups.set(w.currency,g);}return groups.size===1?percent(...[...groups.values()][0]):null;}
  function availability(wallets=api().data.wallets,remaining=false){const groups=new Map();for(const w of wallets){const g=groups.get(w.currency)||[0,0];g[0]+=Number(w.allowance?.available)||0;g[1]+=remaining?(Number(w.allowance?.available)||0)+(Number(w.allowance?.reserved)||0)+(Number(w.allowance?.pending)||0):Number(w.limits?.monthly_budget)||0;groups.set(w.currency,g);}return groups.size===1?percent(...[...groups.values()][0]):null;}
  const bar = (value,color='#0755ed') => `<svg viewBox="0 0 100 5" preserveAspectRatio="none" aria-hidden="true"><rect width="${Math.max(0,Math.min(100,Number(value)||0))}" height="5" rx="2.5" fill="${color}"/></svg>`;
  function series(rows=confirmed(),days=30,counts=false){
    const end=new Date();end.setUTCHours(0,0,0,0);const start=end.getTime()-(days-1)*86400000;
    const currencies=[...new Set(rows.map(r=>r.currency).filter(Boolean))];
    // A sparkline cannot represent mixed currencies. Use counts for that case.
    const count=counts||currencies.length>1;
    const points=Array.from({length:days},(_,i)=>({date:new Date(start+i*86400000).toISOString().slice(0,10),value:0}));
    for(const r of rows){const when=Date.parse(r.created_at),i=Math.floor((when-start)/86400000);if(i>=0&&i<days)points[i].value+=count?1:Number(r.amount)||0;}
    return points;
  }
  function sparkline(rows=confirmed(),color='#2466ef',counts=false){const values=series(rows,14,counts).map(p=>p.value),max=Math.max(...values,1),points=values.map((v,i)=>`${(i*120/(values.length-1)).toFixed(2)},${(42-v/max*36).toFixed(2)}`).join(' ');return `<svg viewBox="0 0 120 48" preserveAspectRatio="none" aria-label="Daily activity"><polygon points="0,48 ${points} 120,48" fill="${color}" opacity=".09"/><polyline points="${points}" fill="none" stroke="${color}" stroke-width="1.7" vector-effect="non-scaling-stroke"/></svg>`;}
  const palette=['blue','purple','orange','mint'];
  const avatar=(a,i=0,cls='provider-symbol')=>`<span class="${cls} live-agent-avatar ${palette[i%palette.length]}" aria-hidden="true">${escapeText(String(a.name||'A').slice(0,1).toUpperCase())}</span>`;
  const relative = value => {const t=Date.parse(value);if(!Number.isFinite(t))return 'Unavailable';const minutes=Math.max(0,Math.floor((Date.now()-t)/60000));return minutes<1?'Just now':minutes<60?`${minutes} min ago`:minutes<1440?`${Math.floor(minutes/60)} hour${Math.floor(minutes/60)===1?'':'s'} ago`:`${Math.floor(minutes/1440)} day${Math.floor(minutes/1440)===1?'':'s'} ago`;};
  const latest = a => records().filter(p=>p.agent_id===a.agent_id).map(p=>p.created_at).concat(a.created_at).filter(v=>Number.isFinite(Date.parse(v))).sort((a,b)=>Date.parse(b)-Date.parse(a))[0];
  const friendly = c => ({'payment.prepare':'Payments','payment.execute_sandbox':'Payments','research.fetch':'Web Access','research.search':'Search','payout.request':'Payouts','wallet.read':'Wallet','owner.request_decision':'Approvals','intelligence.assess':'Analysis','finance.monitor':'Monitoring','evidence.record':'Evidence','purchase.propose':'Purchases','comms.draft':'Drafting','comms.send_approved':'Messages'}[c]||human(c));
  function areaSvg(rows=confirmed(),days=30){const points=series(rows,days),currencies=[...new Set(rows.map(r=>r.currency).filter(Boolean))],counts=currencies.length>1,max=Math.max(...points.map(p=>p.value),1),coords=points.map((p,i)=>`${i?'L':'M'}${(48+i*716/(days-1)).toFixed(1)} ${(153-p.value/max*125).toFixed(1)}`).join(' ');return `<svg viewBox="0 0 800 190" role="img" aria-label="Daily confirmed spend${counts?' · request counts, multiple currencies':''}"><defs><linearGradient id="live-area" x2="0" y2="1"><stop stop-color="#2569f6" stop-opacity=".27"/><stop offset="1" stop-color="#2569f6" stop-opacity=".03"/></linearGradient></defs>${[0,1,2,3].map(i=>`<path d="M48 ${28+i*41.7}H784" stroke="#eceef7"/><text x="0" y="${32+i*41.7}" fill="#4e5a7c" font-size="11">${escapeText((max*(3-i)/3).toLocaleString('en-US',{maximumFractionDigits:max<5?1:0}))}</text>`).join('')}<path d="${coords}L764 153H48Z" fill="url(#live-area)"/><path d="${coords}" stroke="#0861ed" stroke-width="2.5" fill="none"/>${[0,Math.floor((days-1)/4),Math.floor((days-1)/2),Math.floor((days-1)*.75),days-1].map(i=>`<text x="${48+i*716/(days-1)}" y="182" text-anchor="middle" fill="#4e5a7c" font-size="12">${escapeText(new Date(points[i].date+'T00:00:00Z').toLocaleDateString('en-US',{month:'short',day:'numeric',timeZone:'UTC'}))}</text>`).join('')}</svg>`;}
  function statCard(title,value,color,ic,note='this month',rows=confirmed(),family='overview',progress=null){const s=sparkline(rows,({blue:'#2466ef',purple:'#9557e9',mint:'#2cb895',orange:'#f3ac4a'})[color],['Total agents','Active agents','Pending approvals','Connected agents','Tracked agents','Page checks','Market checks','Payment requests','Payout requests','Requests','Approved requests','Rejected requests','Active credentials'].includes(title));if(family==='agents')return `<article><div class="a-stat-icon ${color}">${window.App.icon(ic)}</div><div class="${progress===null?'':'a-limit'}"><label>${title}</label><strong>${value}</strong>${progress===null?`<small>${note}</small>`:`<div class="a-progress">${bar(progress)}</div>`}</div>${progress===null?`<div class="a-stat-spark">${s}</div>`:''}</article>`;if(family==='wallets')return `<article class="ws-stat"><div class="ws-stat-icon ${color}">${window.App.icon(ic)}</div><div class="ws-stat-copy"><span>${title}</span><strong>${value}</strong>${progress===null?`<small>${note}</small>`:`<div class="ws-available"><i>${bar(progress,'#05a762')}</i><em>${progress}%</em></div>`}</div>${progress===null?`<div class="ws-spark">${s}</div>`:''}</article>`;if(['finance','insights','admin'].includes(family)){const p={finance:'f',insights:'i',admin:'ad'}[family];return `<article class="${p}-stat"><span class="${p==='i'?'i-symbol':'icon'} ${color}">${window.App.icon(ic)}</span><div><label>${title}</label><strong>${value}</strong><small>${note}</small></div><span class="${p}-spark">${s}</span></article>`;}return `<article class="stat"><div class="icon ${color}">${window.App.icon(ic)}</div><div><label>${title}</label><h2>${value}</h2><p class="green">${note}</p></div><div class="spark">${s}</div></article>`;}
  const live={records,confirmed,paymentSpend,thisMonth,totals,allowanceText,budgetText,holdsText,percent,usage,availability,bar,series,sparkline,avatar,relative,latest,friendly,areaSvg,statCard};
  window.AgentPalVisuals={live,company,chart,refreshChart,grid,gridMarkup,chartMarkup,mountDaily,mountAnalytics,agentRows,destroyWithin,date,status,tags,action,amountColumn,csvSafe,units,decimal,aggregate,daily,compareDecimal};
})();
