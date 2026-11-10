/* AgentPal live API client (lead, 2026-11-06).
 *
 * Live mode = the dashboard is served by AgentPal itself at /app/. It then talks ONLY to the
 * same-origin /v1 API with the owner's bearer token. Anywhere else (local preview server,
 * file://) the original demo data is used and nothing is sent anywhere.
 *
 * Token handling: kept in sessionStorage for this tab only (never localStorage, never a
 * cookie, never in a URL). Any 401 signs out. Requests never follow redirects to another
 * origin, never send cookies, and are never cached.
 */
(() => {
  const live = location.pathname === '/app/' || location.pathname.startsWith('/app/');
  const KEY = 'agentpal-owner-session';
  const TOKEN_RE = /^apo_[A-Za-z0-9_-]{16,200}$/;
  const listeners = new Set();
  let token = null;
  try { token = live ? sessionStorage.getItem(KEY) : null; } catch (_) { token = null; }

  const data = {
    health: null, agents: [], wallets: [], payments: [], approvals: [],
    payouts: [], disputes: [], integrations: [], transactions: {}, refunds: {}, loadedAt: null, errors: [],
  };

  class ApiError extends Error {
    constructor(status, code, detail) {
      super(code || ('http_' + status));
      this.status = status; this.code = code || ('http_' + status); this.detail = detail;
    }
  }

  // Plain-language messages for the API's error codes; never echo server text into HTML.
  const MESSAGES = {
    hosted_amount_limit_exceeded: 'Server PayPal limits exceeded. Review the hosted per-transaction and monthly limits in Wallets.',
    server_errands_daily_limit: 'Your daily allowance for server-provided shopping errands is used up. Try again tomorrow or connect your own provider keys.',
    server_model_calls_daily_limit: 'Your daily allowance for server-provided AI calls is used up. Try again tomorrow or connect your own model key.',
    server_market_checks_daily_limit: 'Your daily allowance for server-provided catalogue checks is used up. Try again tomorrow or connect your own Channel3 key.',
    server_page_checks_daily_limit: 'Your daily allowance for server-provided page checks is used up. Try again tomorrow or connect your own Kernel key.',
    server_paypal_checks_daily_limit: 'Your daily allowance for server-provided PayPal credential checks is used up. Try again tomorrow.',
    server_quota_unavailable: 'Provider usage limits could not be checked. Try again later.',
    authentication_required: 'Please sign in again.',
    invalid_token: 'That access token was not accepted. Please sign in again.',
    rate_limited: 'Too many requests. Wait a few seconds and try again.',
    terms_changed: 'The payment terms changed since you opened them. Review the request again.',
    approval_terms_mismatch: 'The payment terms changed since you opened them. Review the request again.',
    insufficient_allowance: 'This wallet does not have enough allowance left.',
    not_found: 'That item no longer exists.',
    request_timeout: 'The server took too long to answer. Try again.',
    merchant_permission_required: 'PayPal requires the merchant to grant permission for this refund.',
    paypal_unavailable: 'PayPal could not be reached. Nothing was changed; try again.',
    invalid_refund_amount: 'That refund is more than the amount left to refund on this payment.',
    invalid_setup_code: 'That setup or invite code was not accepted. Check the code and try again.',
    signup_disabled: 'Workspace creation is disabled. Ask the operator for an owner token.',
    setup_complete: 'This server already has an owner. Sign in with your owner token.',
    owner_exists: 'That owner ID is already taken. Choose another ID.',
    integration_required: 'Connect the required service in Integrations, then try again.',
    policy_block: 'A spending rule blocked this request. Review the limits and request terms.',
    policy_blocked: 'A spending rule blocked this request. Review the limits and request terms.',
    verification_failed: 'The service could not verify these credentials. Check the key and try again.',
  };
  function message(err) {
    if (!(err instanceof ApiError)) return 'Could not reach AgentPal. Check your connection and try again.';
    if (MESSAGES[err.code]) return MESSAGES[err.code];
    if (err.code === 'server_paypal_global_daily_limit') return 'Today\'s demo budget for the shared AgentPal PayPal sandbox account is used up. It resets within 24 hours; add your own PayPal sandbox keys under Integrations to keep testing now.';
    if (/_daily_limit$/.test(String(err.code))) return 'The daily demo limit for this shared AgentPal service is reached. It resets within 24 hours; add your own key under Integrations to keep testing now.';
    if (err.status === 429) return MESSAGES.rate_limited;
    if (err.status === 409) return 'That action is not allowed in the request\'s current state.';
    if (err.status === 403) return 'Your account is not allowed to do that.';
    if (err.status === 422) return 'Some details are not valid. Check the form and try again.';
    if (err.status >= 500) return 'AgentPal could not finish that action. Nothing was assumed; try again.';
    return 'That action could not be completed. Refresh the status before trying again.';
  }

  function newKey(prefix) {
    const bytes = new Uint8Array(16); crypto.getRandomValues(bytes);
    return prefix + '-' + Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
  }

  async function call(method, path, body, opts = {}) {
    if (!live) throw new ApiError(0, 'demo_mode');
    const publicSetup = opts.publicSetup === true &&
      ((method === 'GET' && path === 'setup/status') || (method === 'POST' && path === 'setup/owner'));
    if (!token && !publicSetup) { signOut('expired'); throw new ApiError(401, 'authentication_required'); }
    if (typeof path !== 'string' || !/^[A-Za-z0-9_\-/?=&.%]+$/.test(path) || path.includes('..')) {
      throw new ApiError(0, 'bad_path');
    }
    const headers = { Accept: 'application/json' };
    if (!publicSetup) headers.Authorization = 'Bearer ' + token;
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    if (opts.idempotency) headers['Idempotency-Key'] = opts.idempotency;
    let res;
    try {
      res = await fetch('/v1/' + path.replace(/^\/+/, ''), {
        method, headers, body: body === undefined ? undefined : JSON.stringify(body),
        credentials: 'omit', cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer',
      });
    } catch (_) { throw new ApiError(0, 'network_error'); }
    if (res.status === 429 && method === 'GET' && !opts.retried) {
      // Honour the server's limit: one bounded wait, reads only. Writes are never retried.
      const wait = Math.min(Math.max(Number(res.headers.get('Retry-After')) || 2, 1), 10);
      await new Promise(r => setTimeout(r, wait * 1000));
      return call(method, path, body, { ...opts, retried: true });
    }
    let json = null;
    try { json = await res.json(); } catch (_) { json = null; }
    if (res.status === 401 && !publicSetup) { signOut('expired'); throw new ApiError(401, (json && json.error) || 'authentication_required'); }
    if (!res.ok) throw new ApiError(res.status, json && typeof json.error === 'string' ? json.error : null, json);
    return json;
  }

  const get = path => call('GET', path);
  const post = (path, body, opts) => call('POST', path, body === undefined ? {} : body, opts);
  const patch = (path, body) => call('PATCH', path, body);
  const put = (path, body) => call('PUT', path, body);
  const del = path => call('DELETE', path);
  const setupStatus = () => call('GET', 'setup/status', undefined, { publicSetup: true });
  const createOwner = body => call('POST', 'setup/owner', body, { publicSetup: true });

  // Rate-limit friendly (owner tokens get 60 requests/minute): concurrent refreshes share one
  // round of requests; the pending-approval queue is derived from the payment list exactly as
  // the server derives /v1/approvals (decision requested + awaiting_approval); health and
  // disputes change rarely and reload at most once a minute.
  let inFlight = null, slowAt = 0;
  function refresh(opts = {}) {
    if (!live || !token) return Promise.resolve(data);
    if (inFlight) return inFlight;
    inFlight = doRefresh(opts).finally(() => { inFlight = null; });
    return inFlight;
  }
  async function doRefresh(opts) {
    const errors = [];
    const grab = async (path, pick, fallback) => {
      try { return pick(await get(path)); } catch (e) {
        if (e.status === 401) throw e;
        errors.push(path); return fallback;
      }
    };
    const slow = opts.full || !data.loadedAt || Date.now() - slowAt > 60000;
    const [agents, wallets, payments, payouts, health, disputes, integrations] = await Promise.all([
      grab('agents', x => x.agents || [], []),
      grab('wallets', x => x.wallets || [], []),
      grab('payment-requests', x => x.payment_requests || [], []),
      grab('payout-requests', x => x.payout_requests || [], []),
      slow ? grab('health', x => x, null) : data.health,
      slow ? grab('disputes', x => x.disputes || [], []) : data.disputes,
      slow ? grab('integrations', x => x.integrations || [], []) : data.integrations,
    ]);
    if (slow) slowAt = Date.now();
    const approvals = errors.includes('payment-requests') ? [] : payments.filter(
      p => p.decision_requested === true && p.status === 'awaiting_approval');
    if (errors.includes('payment-requests')) errors.push('approvals');
    Object.assign(data, { health, agents, wallets, payments, approvals, payouts, disputes, integrations,
      loadedAt: new Date(), errors });
    emit('refreshed');
    return data;
  }

  async function walletTransactions(walletId) {
    const r = await get('wallets/' + encodeURIComponent(walletId) + '/transactions');
    data.transactions[walletId] = r.transactions || [];
    return data.transactions[walletId];
  }

  async function refunds(requestId) {
    const r = await get('payment-requests/' + encodeURIComponent(requestId) + '/refunds');
    data.refunds[requestId] = r.refunds || [];
    return data.refunds[requestId];
  }

  async function signIn(candidate) {
    const value = String(candidate || '').trim();
    if (!TOKEN_RE.test(value)) throw new ApiError(0, 'token_format');
    token = value;
    try {
      // /v1/approvals is owner-only: an agent key is refused there.
      try { await get('approvals'); } catch (e) {
        if (e.status === 403) { signOut(); throw new ApiError(403, 'not_an_owner_token'); }
        throw e;
      }
      try { sessionStorage.setItem(KEY, value); } catch (_) { /* tab-only session */ }
      await refresh({ full: true });
      emit('signed-in');
      return true;
    } catch (e) { if (token === value && e.code !== 'not_an_owner_token') signOut(); throw e; }
  }

  function signOut(reason = 'signed-out') {
    const had = !!token;
    token = null;
    try { sessionStorage.removeItem(KEY); } catch (_) { /* ignore */ }
    Object.assign(data, { agents: [], wallets: [], payments: [], approvals: [],
      payouts: [], disputes: [], integrations: [], transactions: {}, refunds: {}, loadedAt: null });
    if (had) emit('signed-out', reason);
  }

  // UI-T1 MAJOR-1: new agent requests appear without a reload. Light refresh (4 requests)
  // every 30 s while the tab is visible and signed in, plus on returning to the tab.
  if (live) {
    const tick = () => { if (token && document.visibilityState === 'visible' && data.loadedAt) refresh().catch(() => {}); };
    setInterval(tick, 30000);
    document.addEventListener('visibilitychange', tick);
  }

  function on(fn) { listeners.add(fn); return () => listeners.delete(fn); }
  function emit(type, reason) {
    listeners.forEach(fn => { try { fn(type, reason); } catch (_) { console.error('Dashboard listener failed'); } });
    window.dispatchEvent(new CustomEvent('agentpal-live', { detail: { type, reason } }));
  }

  // Money from the API arrives as decimal strings. Never use floats for decisions.
  const money = (amount, currency) => {
    const n = Number(amount);
    if (!Number.isFinite(n)) return '—';
    try { return new Intl.NumberFormat('en-US', { style: 'currency', currency: currency || 'USD' }).format(n); }
    catch (_) { return n.toFixed(2) + ' ' + (currency || ''); }
  };

  window.AgentPalAPI = Object.freeze({
    live, data, get, post, patch, put, delete: del, refresh, walletTransactions, refunds, signIn, signOut,
    on, message, money, newKey, ApiError, setupStatus, createOwner,
    get signedIn() { return !!token; },
  });
})();
