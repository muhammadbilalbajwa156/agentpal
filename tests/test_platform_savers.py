"""Real owner/agent HTTP boundaries; provider calls use counter-offer fixtures."""
from datetime import datetime, timezone

import httpx
import pytest

from test_platform_counter_offer import DealMarket, ask as ask_request, counter, deal, paypal_world as configure_paypal, workspace
from test_paypal_refunds import RefundPayPal
from test_platform_workspace import bearer, world


def ask(c, ah, **kw):
    return ask_request(c, ah, product_query='usb-c hub', **kw)


def paypal_world(d, backend):
    configure_paypal(d, backend)
    d.provider._now = lambda: d.risk_service.clock()


def hire(c, h):
    r = c.post('/v1/savers', headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def run(c, h, saver):
    r = c.post('/v1/savers/' + saver['saver_id'] + '/run', headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def set_clock(c, month=11, day=12):
    now = datetime(2026, month, day, 12, tzinfo=timezone.utc)
    c.app.state.service.clock = lambda: now
    deps = c.app.state.platform.deps
    deps.lifecycle.lifecycle.clock = lambda: now


def test_hire_list_run_owner_projection_and_timeline(tmp_path):
    with world(tmp_path) as (c, d):
        h, ah, _, _ = workspace(c, d)
        d.market_checker = DealMarket()
        request = ask(c, ah)
        saver = hire(c, h)
        assert saver['status'] == 'active'
        assert saver['manifesto'] == 'I exist to save you money. If I save you nothing this month, I retire myself.'
        assert c.post('/v1/savers', headers=h).status_code == 409
        score = run(c, h, saver)['current_period']
        assert score['suggestions_made'] == 1
        assert score['suggestions_used'] == 0
        assert score['verified_savings'] == {}
        assert score['unverified_potential'] == {'USD':'8.50'}
        assert len(run(c, h, saver)['feed']) == 1
        assert len(c.get('/v1/savers', headers=h).json()['savers']) == 1
        path = '/v1/payment-requests/' + request['request_id']
        suggestion = c.get(path, headers=h).json()['saver_suggestion']
        assert (suggestion['deal_index'], suggestion['price'], suggestion['saving']) == (0,'16.49','8.50')
        assert c.get(path, headers=ah).json()['saver_suggestion'] is None
        assert any(e['label'] == 'Saver agent suggested a counter-offer'
                   for e in c.get(path + '/timeline', headers=h).json()['events'])


@pytest.mark.parametrize('offers', [[], [deal(price='NaN')], [deal(price='-1')],
    [deal(price='0')], [deal(price='1e1')], [deal(price='9', currency='EUR')],
    [deal(price='25')], [deal(price='24.99')], [deal(retailer='')]])
def test_invalid_or_non_cheaper_deal(tmp_path, offers):
    with world(tmp_path) as (c, d):
        h, ah, _, _ = workspace(c, d)
        d.market_checker = DealMarket(offers)
        ask(c, ah)
        assert run(c, h, hire(c, h))['feed'] == []


def test_cheapest_stored_index_and_no_provider_calls(tmp_path, monkeypatch):
    with world(tmp_path) as (c, d):
        h, ah, _, _ = workspace(c, d)
        d.market_checker = DealMarket([deal(price='15', currency='EUR'), deal(price='17'), deal(price='11.50')])
        ask(c, ah)
        saver = hire(c, h)
        def forbidden(*a, **kw):
            pytest.fail('Saver called a provider')
        monkeypatch.setattr(d.market_checker, 'check', forbidden)
        real_send = httpx.Client.send

        def outbound(client, request, *a, **kw):
            # The in-process test client also uses httpx; only real hosts are forbidden.
            if request.url.host != 'testserver':
                pytest.fail('Saver called a provider: ' + request.url.host)
            return real_send(client, request, *a, **kw)
        monkeypatch.setattr('httpx.Client.send', outbound)
        suggestion = run(c, h, saver)['feed'][0]
        assert (suggestion['deal_index'], suggestion['saving']) == (2,'13.49')


@pytest.mark.parametrize('method,suffix', [('GET',''), ('POST','/run'), ('POST','/evaluate'), ('POST','/retire')])
def test_cross_owner_and_agent_credentials(tmp_path, method, suffix):
    with world(tmp_path) as (c, d):
        h, ah, _, _ = workspace(c, d)
        other, _, _, _ = workspace(c, d, 'bob')
        saver = hire(c, h)
        path = '/v1/savers/' + saver['saver_id'] + suffix
        assert c.request(method, path, headers=other).status_code == 404
        assert c.request(method, path, headers=ah).status_code == 403
        assert c.get('/v1/savers', headers=ah).status_code == 403
        assert c.post('/v1/savers', headers=ah).status_code == 403
        assert c.get('/v1/savers', headers=other).json()['savers'] == []


def test_saver_has_no_financial_authority_even_after_grant_attempt(tmp_path):
    with world(tmp_path) as (c, d):
        h, ah, _, _ = workspace(c, d)
        request = ask(c, ah)
        saver = hire(c, h)
        key, _ = c.app.state.platform.agents.rotate(
            d.owner_authenticator.authenticate(h['Authorization'][7:]), saver['agent_id'], 0)
        sh = bearer(key.api_key)
        assert c.get('/v1/me', headers=sh).json()['capabilities'] == []
        for cap in ['payment.prepare', 'payment.execute_sandbox', 'payout.request']:
            assert c.put('/v1/agents/' + saver['agent_id'] + '/permissions', headers=h,
                         json={'capabilities':[cap]}).status_code == 400
        base = '/v1/payment-requests/' + request['request_id']
        for suffix, body in [('approve', {'terms_sha256':request['terms_sha256']}),
                ('counter', {'deal_index':0}), ('capture', {}), ('refunds', {'amount':'1.00'}), ('execute', {})]:
            assert c.post(base + '/' + suffix, headers=sh, json=body).status_code in (403,404)
        assert c.post('/v1/payment-requests', headers={**sh,'Idempotency-Key':'saver-no-money'},
            json={'amount':'1.00','currency':'USD','merchant_name':'Shop','purpose':'No'}).status_code == 403
        # Even an owner-granted read capability cannot expose the buyer's requests.
        assert c.put('/v1/agents/' + saver['agent_id'] + '/permissions', headers=h,
                     json={'capabilities':['wallet.read']}).status_code == 200
        assert c.get('/v1/payment-requests', headers=sh).json()['payment_requests'] == []


def test_verified_capture_scorecard_and_survival(tmp_path):
    backend = RefundPayPal()
    with world(tmp_path) as (c, d):
        h, ah, _, _ = workspace(c, d)
        set_clock(c)
        paypal_world(d, backend)
        d.market_checker = DealMarket([deal(price='1.00')])
        request = ask(c, ah, amount='3.00')
        saver = hire(c, h)
        run(c, h, saver)
        new = counter(c, h, request['request_id'], {'deal_index':0}).json()['replacement']
        path = '/v1/payment-requests/' + new['request_id']
        def score():
            return c.get('/v1/savers/' + saver['saver_id'], headers=h).json()['current_period']
        assert score()['suggestions_used'] == 1
        assert score()['verified_savings'] == {}
        assert c.post(path + '/approve', headers=h, json={'terms_sha256':new['terms_sha256']}).status_code == 200
        assert c.post(path + '/execute', headers=ah).status_code == 200
        backend.approve()
        assert score()['verified_savings'] == {}
        assert c.post(path + '/capture', headers=h).json()['status'] == 'provider_confirmed'
        assert score()['verified_savings'] == {'USD':'2.00'}
        assert score()['unverified_potential'] == {}
        set_clock(c, 12, 1)
        out = c.post('/v1/savers/' + saver['saver_id'] + '/evaluate', headers=h).json()
        assert out['status'] == 'active'
        assert out['periods'][0]['verified_savings'] == {'USD':'2.00'}
        assert out['periods'][0]['report'] == 'This month I saved you $2.00 USD'


def test_zero_month_retires_revokes_idempotently_and_never_early(tmp_path):
    with world(tmp_path) as (c, d):
        h, _, _, _ = workspace(c, d)
        set_clock(c)
        saver = hire(c, h)
        p = c.app.state.platform
        key, _ = p.agents.rotate(d.owner_authenticator.authenticate(h['Authorization'][7:]), saver['agent_id'], 0)
        base = '/v1/savers/' + saver['saver_id']
        assert c.post(base + '/evaluate', headers=h).json()['status'] == 'active'
        assert c.get('/v1/me', headers=bearer(key.api_key)).status_code == 200
        set_clock(c, 12, 1)
        result = c.post(base + '/evaluate', headers=h).json()
        assert result['status'] == 'retired'
        assert result['retirement_reason'] == 'Retired: saved $0 in 2026-11'
        assert len(result['periods']) == 1
        assert c.post(base + '/evaluate', headers=h).json() == result
        assert c.get('/v1/me', headers=bearer(key.api_key)).status_code in (401,403)
        assert c.post(base + '/run', headers=h).status_code == 409
        assert c.post('/v1/agents/' + saver['agent_id'] + '/reactivate', headers=h).status_code == 409
        assert c.post('/v1/agents/' + saver['agent_id'] + '/credentials', headers=h).status_code == 409
        assert hire(c, h)['saver_id'] != saver['saver_id']


def test_manual_retire_preserves_history(tmp_path):
    with world(tmp_path) as (c, d):
        h, ah, _, _ = workspace(c, d)
        d.market_checker = DealMarket()
        ask(c, ah)
        saver = hire(c, h)
        feed = run(c, h, saver)['feed']
        result = c.post('/v1/savers/' + saver['saver_id'] + '/retire', headers=h).json()
        assert result['status'] == 'retired'
        assert result['feed'] == feed
        assert all(k['status'] == 'revoked' for k in c.app.state.platform.credentials.keys_of(saver['agent_id']))


def test_ticker_zero_disables_and_startup_runs(tmp_path, monkeypatch):
    from agentpal.platform.savers import SaverService, SaverTicker
    calls = []
    monkeypatch.setattr(SaverService, 'tick', lambda self: calls.append(True))
    monkeypatch.setenv('AGENTPAL_SAVER_INTERVAL_SECONDS', '0')
    with world(tmp_path) as (c, _):
        assert c.app.state.saver_ticker.task is None
        assert calls == []
    monkeypatch.setenv('AGENTPAL_SAVER_INTERVAL_SECONDS', '900')
    with world(tmp_path):
        assert calls == [True]
    assert SaverTicker.interval({'AGENTPAL_SAVER_INTERVAL_SECONDS':'0'}) == 0


@pytest.mark.parametrize('body', [{'amount':'16.49','merchant_name':'known-retailer.example',
    'merchant_domain':'known-retailer.example'}, {'deal_index':1}])
def test_manual_or_other_index_never_counts_as_suggestion_used(tmp_path, body):
    with world(tmp_path) as (c, d):
        h, ah, _, _ = workspace(c, d)
        d.market_checker = DealMarket([deal(), deal()])
        request = ask(c, ah)
        saver = hire(c, h)
        assert run(c, h, saver)['feed'][0]['deal_index'] == 0
        response = counter(c, h, request['request_id'], body)
        assert response.status_code == 201, response.text
        score = c.get('/v1/savers/' + saver['saver_id'], headers=h).json()['current_period']
        assert score['suggestions_used'] == 0
        assert score['verified_savings'] == {}


def test_changed_offer_at_same_index_never_counts_as_used(tmp_path):
    import json
    with world(tmp_path) as (c, d):
        h, ah, _, _ = workspace(c, d)
        d.market_checker = DealMarket()
        request = ask(c, ah)
        saver = hire(c, h)
        run(c, h, saver)
        with d.wallet_store.tx() as db:
            payload = DealMarket([deal(price='15.00')]).check()
            db.execute('UPDATE platform_market_checks SET payload_json=? WHERE request_id=?',
                       (json.dumps(payload), request['request_id']))
        assert counter(c, h, request['request_id'], {'deal_index':0}).status_code == 201
        score = c.get('/v1/savers/' + saver['saver_id'], headers=h).json()['current_period']
        assert score['suggestions_used'] == 0


def test_run_reads_only_own_workspace_and_only_pending_requests(tmp_path):
    with world(tmp_path) as (c, d):
        h, ah, _, _ = workspace(c, d)
        other, bh, _, _ = workspace(c, d, 'bob')
        d.market_checker = DealMarket()
        own = ask(c, ah)
        foreign = ask(c, bh)
        rejected = ask(c, ah, amount="1.00", merchant_name="Other Shop", merchant_domain="other.example")
        assert c.post('/v1/payment-requests/' + rejected['request_id'] + '/reject', headers=h).status_code == 200
        saver = hire(c, h)
        assert [s['request_id'] for s in run(c, h, saver)['feed']] == [own['request_id']]
        assert c.get('/v1/payment-requests/' + foreign['request_id'], headers=other).json()['saver_suggestion'] is None


def test_simulator_counter_is_potential_only_and_zero_month_retires(tmp_path):
    with world(tmp_path) as (c, d):
        h, ah, _, _ = workspace(c, d)
        set_clock(c)
        d.market_checker = DealMarket()
        request = ask(c, ah)
        saver = hire(c, h)
        run(c, h, saver)
        new = counter(c, h, request['request_id'], {'deal_index':0}).json()['replacement']
        base = '/v1/payment-requests/' + new['request_id']
        assert c.post(base + '/approve', headers=h, json={'terms_sha256':new['terms_sha256']}).status_code == 200
        assert c.post(base + '/execute', headers=ah).status_code == 200
        score = c.get('/v1/savers/' + saver['saver_id'], headers=h).json()['current_period']
        assert score['verified_savings'] == {}
        assert score['unverified_potential'] == {'USD':'8.50'}
        set_clock(c,12,1)
        assert c.post('/v1/savers/' + saver['saver_id'] + '/evaluate', headers=h).json()['status'] == 'retired'


def test_optional_groq_note_on_hire_only_and_template_fallback(tmp_path, monkeypatch):
    import httpx
    from agentpal.platform.savers import NoteWriter
    from agentpal.platform.integrations import IntegrationVerifier
    monkeypatch.setattr(IntegrationVerifier, 'verify', lambda *args: None)
    calls = []
    def handle(req):
        calls.append(req)
        assert str(req.url) == 'https://api.groq.com/openai/v1/chat/completions'
        return httpx.Response(200,json={'choices':[{'message':{'content':'I suggest; you decide.'}}]})
    monkeypatch.setattr(NoteWriter,'transport_factory',staticmethod(lambda: httpx.MockTransport(handle)))
    with world(tmp_path) as (c,d):
        h, _, _, _ = workspace(c,d)
        assert hire(c,h)['note_source'] == 'template'
        first = c.get('/v1/savers',headers=h).json()['savers'][0]
        c.post('/v1/savers/' + first['saver_id'] + '/retire',headers=h)
        assert c.put('/v1/integrations/groq',headers=h,json={'fields':{'api_key':'synthetic-groq-note-key'}}).status_code == 200
        saver = hire(c,h)
        assert saver['note_source'] == 'ai'
        assert saver['note'] == 'I suggest; you decide.'
        assert len(calls) == 1
        run(c,h,saver)
        assert len(calls) == 1


def test_ticker_repeat_and_shutdown():
    import asyncio
    from agentpal.platform.savers import SaverTicker
    class Policy:
        count = 0
        def tick(self):
            self.count += 1
    async def exercise():
        policy = Policy()
        ticker = SaverTicker(policy, 0.01)
        await ticker.start()
        for _ in range(100):
            if policy.count >= 2:
                break
            await asyncio.sleep(0.01)
        await ticker.close()
        assert policy.count >= 2
        assert ticker.task.done()
    asyncio.run(exercise())


def test_concurrent_hire_run_and_evaluate_are_single_effect(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    with world(tmp_path) as (c,d):
        h, ah, _, _ = workspace(c,d)
        set_clock(c)
        d.market_checker = DealMarket()
        ask(c,ah)
        with ThreadPoolExecutor(4) as pool:
            responses = list(pool.map(lambda _:c.post('/v1/savers',headers=h),range(4)))
        assert sorted(r.status_code for r in responses) == [201,409,409,409]
        saver = next(r.json() for r in responses if r.status_code == 201)
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda _:run(c,h,saver),range(4)))
        assert all(len(result['feed']) == 1 for result in results)
        set_clock(c,12,1)
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda _:c.post('/v1/savers/' + saver['saver_id'] + '/evaluate',headers=h).json(),range(4)))
        assert all(result['status'] == 'retired' and len(result['periods']) == 1 for result in results)
        assert len(d.wallet_store.query("SELECT * FROM platform_saver_events WHERE action LIKE 'Retired:%'")) == 1


def test_saver_history_survives_restart(tmp_path):
    with world(tmp_path) as (c,d):
        h, ah, _, _ = workspace(c,d)
        d.market_checker = DealMarket()
        ask(c,ah)
        saver = hire(c,h)
        feed = run(c,h,saver)['feed']
        c.post('/v1/savers/' + saver['saver_id'] + '/retire',headers=h)
    with world(tmp_path) as (c,d):
        restored = c.get('/v1/savers/' + saver['saver_id'],headers=h)
        assert restored.status_code == 200, restored.text
        assert restored.json()['feed'] == feed
        assert restored.json()['status'] == 'retired'


def test_savings_stay_in_first_verified_month_after_later_paypal_read(tmp_path, monkeypatch):
    backend = RefundPayPal()
    with world(tmp_path) as (c,d):
        h,ah,_,_ = workspace(c,d)
        set_clock(c)
        paypal_world(d,backend)
        d.market_checker=DealMarket([deal(price='1.00')])
        request=ask(c,ah,amount='3.00')
        saver=hire(c,h)
        run(c,h,saver)
        replacement=counter(c,h,request['request_id'],{'deal_index':0}).json()['replacement']
        path='/v1/payment-requests/'+replacement['request_id']
        c.post(path+'/approve',headers=h,json={'terms_sha256':replacement['terms_sha256']})
        c.post(path+'/execute',headers=ah)
        backend.approve()
        assert c.post(path+'/capture',headers=h).json()['status']=='provider_confirmed'
        set_clock(c,12,1)
        original=backend.order
        def later_order():
            return {**original(),'update_time':'2026-12-01T12:00:00Z'}
        monkeypatch.setattr(backend,'order',later_order)
        assert c.post(path+'/reconciliation',headers=h).status_code==200
        # Identical PayPal reads deduplicate. Seed an additional accepted receipt to
        # model a later authenticated observation with a different event identity.
        with d.wallet_store.tx() as db:
            db.execute('INSERT INTO platform_paypal_read_receipts (agent_id,request_id,event_id,at) VALUES (?,?,?,?)',
                       (replacement['agent_id'],replacement['request_id'],'synthetic-later-read','2026-12-01T12:00:00+00:00'))
        assert c.get(path,headers=h).json()['verification']['at'].startswith('2026-12')
        result=c.post('/v1/savers/'+saver['saver_id']+'/evaluate',headers=h).json()
        assert result['status']=='active'
        assert result['periods'][0]['verified_savings']=={'USD':'2.00'}
        assert result['current_period']['verified_savings']=={}


def test_capture_without_final_paypal_read_never_becomes_saver_credit(tmp_path,monkeypatch):
    backend=RefundPayPal()
    with world(tmp_path) as (c,d):
        h,ah,_,_=workspace(c,d)
        paypal_world(d,backend)
        d.market_checker=DealMarket([deal(price='1.00')])
        request=ask(c,ah,amount='3.00')
        saver=hire(c,h)
        run(c,h,saver)
        new=counter(c,h,request['request_id'],{'deal_index':0}).json()['replacement']
        path='/v1/payment-requests/'+new['request_id']
        c.post(path+'/approve',headers=h,json={'terms_sha256':new['terms_sha256']})
        c.post(path+'/execute',headers=ah)
        backend.approve()
        def unavailable(*a,**kw):
            raise RuntimeError('synthetic final read unavailable')
        monkeypatch.setattr(d.lifecycle,'reconcile',unavailable)
        assert c.post(path+'/capture',headers=h).json()['status']=='provider_confirmed'
        assert c.get(path,headers=h).json()['counter_saving_verified'] is False
        score=c.get('/v1/savers/'+saver['saver_id'],headers=h).json()['current_period']
        assert score['verified_savings']=={}
        assert score['unverified_potential']=={'USD':'2.00'}


def test_savings_totals_preserve_fractional_minor_units():
    from decimal import Decimal
    from agentpal.platform.savers import totals
    assert totals([('KWD',Decimal('0.001')),('USD',Decimal('2.00'))])=={'KWD':'0.001','USD':'2.00'}


def test_negative_counter_difference_reduces_verified_total_before_clamping():
    from decimal import Decimal
    from agentpal.platform.savers import totals
    # Re-countering a dearer owner counter still measures against the agent's
    # ORIGINAL request. Never drop its negative difference before summing.
    assert totals([('USD',Decimal('8.00')),('USD',Decimal('-3.00'))]) == {'USD':'5.00'}
    assert totals([('USD',Decimal('3.00')),('USD',Decimal('-8.00'))]) == {}
