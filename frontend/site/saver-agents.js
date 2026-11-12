/* Owner-only Saver dashboard and a small request-view helper. No payment actions. */
(() => {
  const safe = value => window.App.escape(String(value ?? ''));
  const api = () => window.AgentPalAPI;
  let session = 0, revision = 0;
  const money = (amount, currency) => safe(api().money(String(amount), currency)) + ' ' + safe(currency);
  const totals = values => Object.entries(values || {}).map(([currency, amount]) => money(amount, currency)).join(' · ') || '$0';
  const path = id => 'savers/' + encodeURIComponent(id);
  function suggestionHTML(value) {
    const s = value.saver_suggestion;
    return s ? `<p class="saver-suggestion">Saver suggests: Counter with ${safe(s.retailer)} at ${money(s.price,s.currency)}. Stored Channel3 offer; review before countering.</p>` : '';
  }
  function card() {
    return '<section class="saver-agents a-agents-panel" aria-label="Saver agents"><h2>Saver agents</h2><p role="status">Loading Saver agents…</p></section>';
  }
  function content(savers) {
    const active = savers.some(s => s.status === 'active');
    return `<h2>Saver agents</h2><p class="saver-manifesto">I exist to save you money. If I save you nothing this month, I retire myself.</p><p>Runs automatically while the AgentPal server is running.</p><p>Deterministic policy · PayPal Sandbox only · You decide every counter and approval.</p><button class="a-primary" data-saver-hire ${active?'disabled':''}>Hire Saver</button>${savers.length?'':'<p>No Saver hired yet. It reads stored offers without making new market calls.</p>'}${savers.map(s => {
      const score = s.current_period || {}, retired = s.status === 'retired';
      return `<article class="saver-card"><div class="saver-heading"><h3>Saver</h3><span class="a-status ${retired?'suspended':'active'}">${retired?'Retired':'Active'}</span></div>${retired?`<p>${safe(s.retirement_reason)}</p>`:''}<p>${safe(score.period)} · UTC</p><dl class="saver-metrics"><div><dt>This month's verified savings</dt><dd>${totals(score.verified_savings)}</dd></div><div><dt>Suggestions</dt><dd>${safe(score.suggestions_made ?? 0)}</dd></div><div><dt>Used</dt><dd>${safe(score.suggestions_used ?? 0)}</dd></div><div><dt>Days left</dt><dd>${safe(s.days_left)}</dd></div></dl><p>Unverified potential: ${totals(score.unverified_potential)}. Only PayPal-verified captures count as savings.</p><p>${safe(score.report)}</p>${s.automatic_interval_seconds===0?'<p>Automatic runs are disabled on this server. Use Run now.</p>':''}<p>${s.note_source==='ai'?'AI-written note':'Template note'}: ${safe(s.note)}</p>${retired?'':`<div class="saver-actions"><button class="a-secondary" data-saver-run="${safe(s.saver_id)}">Run now</button><button class="a-secondary" data-saver-retire="${safe(s.saver_id)}">Retire Saver</button></div>`}${(s.periods||[]).map(p=>`<p class="saver-period">${safe(p.period)}: ${safe(p.report)}</p>`).join('')}<h4>Suggestions feed</h4><ul class="saver-feed">${(s.feed||[]).map(f=>`<li><div><strong>Counter with ${safe(f.retailer)} at ${money(f.price,f.currency)}, saves ${money(f.saving,f.currency)}</strong><small>${safe(f.created_at)}</small></div><button class="a-secondary" data-saver-review="${safe(f.request_id)}">Review</button></li>`).join('')||'<li>No suggestions yet. Run now to check requests awaiting approval.</li>'}</ul></article>`;
    }).join('')}<p class="saver-error" role="alert"></p>`;
  }
  async function mount() {
    const host = document.querySelector('.saver-agents');
    if (!host || !api()?.live) return;
    const current = session, load = ++revision;
    const valid = () => current === session && load === revision && host.isConnected;
    try {
      const result = await api().get('savers');
      if (!valid()) return;
      host.innerHTML = content(result.savers || []);
      host.onclick = async e => {
        const button = e.target.closest('button');
        if (!button || button.disabled) return;
        if (button.dataset.saverReview) return window.FinanceScreens.openRequest(button.dataset.saverReview, 'payment');
        const target = button.hasAttribute('data-saver-hire') ? 'savers' : button.dataset.saverRun ? path(button.dataset.saverRun) + '/run' : button.dataset.saverRetire ? path(button.dataset.saverRetire) + '/retire' : null;
        if (!target) return;
        host.querySelectorAll('button').forEach(b=>b.disabled=true);
        try {
          await api().post(target, {});
          if (!valid()) return;
          await api().refresh();
          if (valid()) await mount();
        } catch (err) {
          if (valid()) {
            const message = api().message(err);
            // A run may evaluate a closed month and retire before refusing to act.
            // Reload status and button availability after any refused write.
            await mount();
            if (current === session && host.isConnected) {
              const alert = host.querySelector('[role="alert"]');
              if (alert) alert.textContent = message;
            }
          }
        }
      };
    } catch (err) {
      if (valid()) {
        host.innerHTML = '<h2>Saver agents</h2><p role="alert"></p><button class="a-secondary" data-saver-retry>Retry</button>';
        host.querySelector('p').textContent = api().message(err);
        host.querySelector('button').onclick = mount;
      }
    }
  }
  window.SaverAgents = {card, mount, suggestionHTML};
  window.addEventListener('agentpal-live', e => {
    if (e.detail?.type === 'signed-out') {
      session++;
      const host = document.querySelector('.saver-agents');
      if (host) { host.onclick=null; host.replaceChildren(); }
    } else if (e.detail?.type === 'refreshed') mount();
  });
})();
