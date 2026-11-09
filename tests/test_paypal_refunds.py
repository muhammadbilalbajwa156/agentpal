"""Real services with MockTransport PayPal; no financial checks mocked."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from agentpal.api import create_app
from agentpal.auth import TokenRegistry
from agentpal.config import Settings
from agentpal.demo.paypal_flow import OfflinePayPal, ORDER_ID
from agentpal.platform.wiring import build_platform
from agentpal.providers.config import PayPalSandboxConfig
from agentpal.providers.paypal_sandbox import PayPalSandboxProvider

BASE = 'https://api-m.sandbox.paypal.com'
CAPTURE = 'DEMO-CAPTURE-1'


class RefundPayPal(OfflinePayPal):
    def __init__(self):
        super().__init__()
        self.refunds = []
        self.mode = None
        self.disputes = []

    def handler(self, req):
        path = req.url.path
        if path == f'/v2/payments/captures/{CAPTURE}' and req.method == 'GET':
            self.requests.append(req)
            if self.mode == 'preflight_down':
                return httpx.Response(503, json={'name': 'SERVICE_UNAVAILABLE'})
            if self.mode == 'preflight_timeout':
                raise httpx.ReadTimeout('fake preflight timeout', request=req)
            return httpx.Response(200, json={'id': CAPTURE, 'status': 'COMPLETED', 'amount': self.amount})
        if path == f'/v2/payments/captures/{CAPTURE}/refund':
            self.requests.append(req)
            body = json.loads(req.content)
            if self.mode == 'decline':
                return httpx.Response(422, json={'name': 'UNPROCESSABLE_ENTITY'})
            if self.mode == 'not_authorized':
                return httpx.Response(403, json={'name': 'NOT_AUTHORIZED'})
            out = {'id': f'REFUND-{len(self.refunds)+1}', 'status': 'COMPLETED',
                   'amount': body['amount'], 'invoice_id': body['invoice_id'],
                   'links': [{'rel': 'up', 'method': 'GET',
                              'href': f'{BASE}/v2/payments/captures/{CAPTURE}'}]}
            self.refunds.append(out)
            if self.mode == 'timeout':
                raise httpx.ReadTimeout('fake lost response', request=req)
            if self.mode == 'server':
                return httpx.Response(503, json={'secret': 'never-expose-provider-body'})
            if self.mode == 'pending':
                out['status'] = 'PENDING'
            if self.mode == 'amount':
                out = {**out, 'amount': {'value': '0.99', 'currency_code': 'USD'}}
            if self.mode == 'currency':
                out = {**out, 'amount': {'value': body['amount']['value'], 'currency_code': 'EUR'}}
            if self.mode == 'capture':
                out = {**out, 'links': [{'rel': 'up', 'href': f'{BASE}/v2/payments/captures/OTHER'}]}
            if self.mode == 'status':
                out = {**out, 'status': 'SURPRISE'}
            return httpx.Response(201, json=out)
        if path.startswith('/v2/payments/refunds/'):
            self.requests.append(req)
            return httpx.Response(200, json=next(r for r in self.refunds if r['id'] == path.rsplit('/', 1)[1]))
        if path == f'/v2/checkout/orders/{ORDER_ID}' and req.method == 'GET':
            self.requests.append(req)
            out = self.order()
            if self.refunds:
                out['purchase_units'][0]['payments']['refunds'] = self.refunds
                if getattr(self, 'realistic_capture_status', False):
                    # Like PayPal: a refunded capture reports PARTIALLY_REFUNDED / REFUNDED.
                    done = sum(Decimal(r['amount']['value']) for r in self.refunds
                               if r['status'] == 'COMPLETED')
                    capture = out['purchase_units'][0]['payments']['captures'][0]
                    capture['status'] = ('REFUNDED' if done >= Decimal(self.amount['value'])
                                         else 'PARTIALLY_REFUNDED')
            return httpx.Response(200, json=out)
        if path == '/v1/customer/disputes':
            self.requests.append(req)
            return httpx.Response(200, json={'items': self.disputes})
        return super().handler(req)


@pytest.fixture
def world(tmp_path):
    backend = RefundPayPal()
    settings = Settings(db_path=str(tmp_path / 'risk.db'), auth_mode='required',
                        analyzer_chain=['heuristic'], allowed_hosts=['127.0.0.1'],
                        rate_limit_per_token_per_minute=1000, rate_limit_per_peer_per_minute=1000)
    deps = build_platform(settings=settings, environ={})
    deps.provider = PayPalSandboxProvider(
        PayPalSandboxConfig(client_id='fake-client', client_secret='fake-secret'),
        http_client=httpx.Client(transport=httpx.MockTransport(backend.handler)))
    deps.lifecycle.provider = deps.provider
    identity = deps.owner_authenticator
    identity.create_owner('refund_owner')
    token, _ = identity.issue_token('refund_owner')
    identity.create_owner('other_owner')
    other, _ = identity.issue_token('other_owner')
    app = create_app(service=deps.risk_service, settings=settings, registry=TokenRegistry(), platform=deps)
    client = TestClient(app, base_url='http://127.0.0.1')
    h = {'Authorization': 'Bearer ' + token}
    def call(method, path, *, expected=200, headers=h, **kw):
        response = client.request(method, path, headers=headers, **kw)
        assert response.status_code == expected, response.text
        return response.json()
    agent = call('POST', '/v1/agents', expected=201, json={'name': 'refund test',
                 'capabilities': ['wallet.read', 'payment.prepare', 'owner.request_decision', 'payment.execute_sandbox']})
    ah = {'Authorization': 'Bearer ' + agent['api_key']}
    wallet = call('POST', '/v1/wallets', expected=201, json={
        'agent_id': agent['agent']['agent_id'], 'currency': 'USD',
        'monthly_budget': '5.00', 'per_transaction_limit': '1.00'})
    pay = call('POST', '/v1/payment-requests', expected=201,
               headers={**ah, 'Idempotency-Key': 'new-payment-123'}, json={
                   'amount': '1.00', 'currency': 'USD', 'merchant_name': 'Sandbox store',
                   'purpose': 'Refund security regression'})
    path = '/v1/payment-requests/' + pay['request_id']
    call('POST', path + '/approve', json={'terms_sha256': pay['terms_sha256']})
    call('POST', path + '/execute', headers=ah)
    backend.approve()
    call('POST', path + '/capture')
    assert deps.lifecycle.status(pay['request_id']).value == 'provider_confirmed'
    def refund(amount='0.40', key='refund-key-123', owner=h):
        return client.post(path + '/refunds', json={} if amount is None else {'amount': amount},
                           headers={**owner, 'Idempotency-Key': key})
    yield SimpleNamespace(client=client, call=call, deps=deps, backend=backend, headers=h,
                          agent_headers=ah, other_headers={'Authorization': 'Bearer ' + other},
                          payment=pay, path=path, wallet=wallet, refund=refund, app=app)
    client.close()
    deps.close()


def posts(w):
    return [r for r in w.backend.requests if r.method == 'POST' and r.url.path.endswith('/refund')]


def test_owner_partial_full_and_exactly_once_credit(world):
    w = world
    first = w.refund()
    assert first.status_code == 201
    assert first.json()['status'] == 'COMPLETED'
    assert w.refund().json()['refund_id'] == first.json()['refund_id']
    assert len(posts(w)) == 1
    wallet = w.call('GET', '/v1/wallets/' + w.wallet['wallet_id'])
    assert Decimal(wallet['allowance']['spent']) == Decimal('0.60')
    assert Decimal(wallet['allowance']['available']) == Decimal('4.40')
    listing = w.call('GET', w.path + '/refunds')
    assert Decimal(listing['refunded_total']) == Decimal('0.40')
    assert listing['status'] == 'provider_confirmed'
    second = w.refund(None, 'refund-full-rest')
    assert second.status_code == 201
    assert Decimal(second.json()['amount']) == Decimal('0.60')
    assert w.call('GET', w.path)['status'] == 'reversed'
    assert Decimal(w.call('GET', '/v1/wallets/' + w.wallet['wallet_id'])['allowance']['spent']) == 0
    assert w.refund(None, 'refund-full-rest').json() == second.json()
    assert len(posts(w)) == 2
    keys = [r.headers['PayPal-Request-Id'] for r in posts(w)]
    assert len(set(keys)) == 2
    assert all(x.startswith('agentpal-refund-') for x in keys)


@pytest.mark.parametrize('method', ['GET', 'POST'])
def test_agents_cannot_refund_or_list(world, method):
    assert world.client.request(method, world.path + '/refunds', json={}, headers=world.agent_headers).status_code == 403
    assert not posts(world)


def test_cross_owner_and_missing_indistinguishable(world):
    for method in ('GET', 'POST'):
        responses = [world.client.request(method, path, json={},
                     headers={**world.other_headers, 'Idempotency-Key': 'cross-key-123'})
                     for path in (world.path + '/refunds', '/v1/payment-requests/pay_missing/refunds')]
        assert [r.status_code for r in responses] == [404, 404]
        assert all(r.json() == {'error': 'not_found'} for r in responses)
    assert not posts(world)


def test_mfa_required(world):
    world.deps.require_mfa_for_approval = True
    assert world.refund().status_code == 403
    assert not posts(world)


@pytest.mark.parametrize('amount', ['0', '-1', 'NaN', 'Infinity', '1e-1', '0.001', '1.01', 0.4, '9'*200, {'value': '0.4'}])
def test_invalid_or_excess_amount_never_calls_provider(world, amount):
    assert world.refund(amount).status_code in (409, 422)
    assert not posts(world)


def test_required_key_and_payload_binding(world):
    assert world.client.post(world.path + '/refunds', json={}, headers=world.headers).status_code == 400
    assert world.refund(key='tiny').status_code == 422
    assert world.refund().status_code == 201
    assert world.refund('0.50').status_code == 409
    assert len(posts(world)) == 1


@pytest.mark.parametrize('mode', ['amount', 'currency', 'capture', 'status', 'timeout', 'server'])
def test_inexact_or_unknown_never_credits_and_replay_never_posts(world, mode):
    w = world
    w.backend.mode = mode
    r = w.refund()
    assert r.status_code == 201
    assert r.json()['status'] == 'reconciliation_required'
    assert w.refund().json()['refund_id'] == r.json()['refund_id']
    assert len(posts(w)) == 1
    assert Decimal(w.call('GET', '/v1/wallets/' + w.wallet['wallet_id'])['allowance']['spent']) == 1
    assert 'never-expose-provider-body' not in r.text


def test_unknown_recovered_only_by_reads(world):
    w = world
    w.backend.mode = 'timeout'
    assert w.refund().json()['status'] == 'reconciliation_required'
    w.backend.mode = None
    result = w.call('POST', w.path + '/refunds/reconciliation')
    assert result['refunds'][0]['status'] == 'COMPLETED'
    assert len(posts(w)) == 1
    assert Decimal(result['refunded_total']) == Decimal('0.40')
    w.call('POST', w.path + '/refunds/reconciliation')
    assert Decimal(w.call('GET', '/v1/wallets/' + w.wallet['wallet_id'])['allowance']['spent']) == Decimal('0.60')


def test_pending_reserves_remaining_without_credit(world):
    w = world
    w.backend.mode = 'pending'
    assert w.refund('0.70').json()['status'] == 'PENDING'
    assert w.refund('0.40', 'another-refund-key').status_code == 409
    assert Decimal(w.call('GET', '/v1/wallets/' + w.wallet['wallet_id'])['allowance']['spent']) == 1


def test_concurrent_refunds_cannot_exceed_capture(world):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda key: world.refund('0.70', key), ['concurrent-key-1', 'concurrent-key-2']))
    assert sorted(r.status_code for r in results) == [201, 409]
    assert len(posts(world)) == 1


def test_unverified_capture_is_not_refundable(world):
    with world.deps.risk_service.store._tx() as c:
        c.execute('UPDATE provider_events SET verified=0')
    assert world.refund().status_code == 409
    assert not posts(world)


def test_disputes_only_own_verified_capture_and_no_raw_fields(world):
    w = world
    w.backend.disputes = [
        {'dispute_id': 'OWN-DISPUTE', 'reason': 'MERCHANDISE_OR_SERVICE_NOT_RECEIVED',
         'status': 'OPEN', 'disputed_transactions': [{'seller_transaction_id': CAPTURE}],
         'buyer': {'email': 'private@example.com'}},
        {'dispute_id': 'OTHER-DISPUTE', 'status': 'OPEN',
         'disputed_transactions': [{'seller_transaction_id': 'OTHER-CAPTURE'}]}]
    result = w.call('GET', '/v1/disputes')
    assert len(result['disputes']) == 1
    assert result['disputes'][0]['dispute_id'] == 'OWN-DISPUTE'
    assert 'private@example.com' not in json.dumps(result)
    assert w.call('GET', '/v1/disputes', headers=w.other_headers)['disputes'] == []
    assert w.client.get('/v1/disputes', headers=w.agent_headers).status_code == 403
    calls = [r for r in w.backend.requests if r.url.path == '/v1/customer/disputes']
    assert calls and all(r.url.params['page_size'] == '20' for r in calls)


def test_dispute_summary_requires_verified_detail_mapping(world):
    w = world
    w.backend.disputes = [{'dispute_id': 'OWN-DISPUTE', 'status': 'OPEN'}]
    original = w.backend.handler
    def handler(req):
        if req.url.path == '/v1/customer/disputes/OWN-DISPUTE':
            w.backend.requests.append(req)
            return httpx.Response(200, json={'dispute_id': 'OWN-DISPUTE', 'status': 'OPEN',
                'disputed_transactions': [{'seller_transaction_id': CAPTURE}]})
        return original(req)
    # Swap only the transport, retaining all real financial/security services.
    w.deps.provider._http = httpx.Client(transport=httpx.MockTransport(handler))
    result = w.call('GET', '/v1/disputes')
    assert len(result['disputes']) == 1
    assert result['disputes'][0]['request_id'] == w.payment['request_id']


@pytest.mark.parametrize('status', ['FAILED', 'CANCELLED'])
def test_verified_terminal_failure_reserves_nothing(world, status):
    w = world
    original = w.backend.handler
    def handler(req):
        response = original(req)
        if req.method == 'POST' and req.url.path.endswith('/refund'):
            data = response.json()
            data['status'] = status
            return httpx.Response(201, json=data)
        return response
    w.deps.provider._http = httpx.Client(transport=httpx.MockTransport(handler))
    assert w.refund().json()['status'] == status
    listing = w.call('GET', w.path + '/refunds')
    assert Decimal(listing['reserved_total']) == Decimal(listing['refunded_total']) == 0
    assert Decimal(w.call('GET', '/v1/wallets/' + w.wallet['wallet_id'])['allowance']['spent']) == 1


def test_crash_between_ledger_and_wallet_commit_never_double_credits(world, monkeypatch):
    from agentpal.platform.refunds import RefundDesk
    w = world
    audit = RefundDesk._audit
    def crash(self, c, rid, action, actor):
        if action == 'verified_completed':
            raise RuntimeError('fake crash after ledger commit')
        return audit(self, c, rid, action, actor)
    monkeypatch.setattr(RefundDesk, '_audit', crash)
    with pytest.raises(RuntimeError, match='fake crash'):
        w.refund()
    assert Decimal(w.call('GET', '/v1/wallets/' + w.wallet['wallet_id'])['allowance']['spent']) == Decimal('0.60')
    monkeypatch.setattr(RefundDesk, '_audit', audit)
    assert w.refund().json()['status'] == 'reconciliation_required'
    result = w.call('POST', w.path + '/refunds/reconciliation')
    assert Decimal(result['refunded_total']) == Decimal('0.40')
    assert Decimal(w.call('GET', '/v1/wallets/' + w.wallet['wallet_id'])['allowance']['spent']) == Decimal('0.60')
    assert len(posts(w)) == 1


def test_payment_must_be_provider_confirmed(world):
    with world.deps.risk_service.store._tx() as c:
        c.execute("UPDATE ledger_entries SET state='reconciliation_required'")
    assert world.refund().status_code == 409
    assert not posts(world)


def test_capture_read_must_match_before_post(world):
    w = world
    w.backend.amount = {'value': '1.00', 'currency_code': 'EUR'}
    assert w.refund().json()['status'] == 'reconciliation_required'
    assert not posts(world)


def test_reason_bound_and_unknown_fields(world):
    for body in ({'amount': '0.40', 'reason': 'r'*256}, {'amount': '0.40', 'currency': 'EUR'},
                 {'amount': '0.40', 'status': 'COMPLETED'}):
        r = world.client.post(world.path + '/refunds', json=body,
                              headers={**world.headers, 'Idempotency-Key': 'reason-key-123'})
        assert r.status_code == 422
    assert not posts(world)


def test_sandbox_provider_required(world):
    world.deps.provider.environment = 'production'
    assert world.refund().status_code == 503
    assert not posts(world)


@pytest.mark.parametrize('field,value', [('status', 'SURPRISE'), ('id', '../bad'),
    ('amount', {'value': '0.41', 'currency_code': 'USD'}),
    ('amount', {'value': '0.40', 'currency_code': 'EUR'}),
    ('links', [{'rel': 'up', 'href': BASE + '/v2/payments/captures/OTHER'}]),
    ('links', [{'rel': 'up', 'href': 'https://api-m.paypal.com/v2/payments/captures/' + CAPTURE}]),
    ('links', []), ('links', [{'rel': 'up', 'href': BASE + '/v2/payments/captures/' + CAPTURE}]*2)])
def test_adapter_rejects_inexact_evidence(field, value):
    from agentpal.infra_contracts import ProviderError
    from agentpal.providers.paypal_refunds import PayPalRefunds
    data = {'id': 'REFUND-1', 'status': 'COMPLETED',
            'amount': {'value': '0.40', 'currency_code': 'USD'},
            'links': [{'rel': 'up', 'href': BASE + '/v2/payments/captures/' + CAPTURE}]}
    data[field] = value
    with pytest.raises(ProviderError):
        PayPalRefunds.verify(data, capture_id=CAPTURE, amount=Decimal('0.40'), currency='USD')


def test_adapter_read_identity_is_bound():
    from agentpal.infra_contracts import ProviderError
    from agentpal.providers.paypal_refunds import PayPalRefunds
    data = {'id': 'REFUND-OTHER', 'status': 'COMPLETED',
            'amount': {'value': '0.40', 'currency_code': 'USD'},
            'links': [{'rel': 'up', 'href': BASE + '/v2/payments/captures/' + CAPTURE}]}
    with pytest.raises(ProviderError):
        PayPalRefunds.verify(data, capture_id=CAPTURE, amount=Decimal('0.40'), currency='USD', refund_id='REFUND-1')


def test_bounded_provider_response(world):
    from agentpal.providers.paypal_refunds import MAX_RESPONSE_BYTES
    w = world
    original = w.backend.handler
    def handler(req):
        if req.url.path.endswith('/refund'):
            w.backend.requests.append(req)
            return httpx.Response(201, content=b' '* (MAX_RESPONSE_BYTES + 1))
        return original(req)
    w.deps.provider._http = httpx.Client(transport=httpx.MockTransport(handler))
    assert w.refund().json()['status'] == 'reconciliation_required'
    assert len(posts(w)) == 1


def test_provider_never_follows_redirects(world):
    w = world
    original = w.backend.handler
    def handler(req):
        assert req.url.host == 'api-m.sandbox.paypal.com'
        if req.url.path.endswith('/refund'):
            w.backend.requests.append(req)
            return httpx.Response(302, headers={'Location': 'https://attacker.example/'})
        return original(req)
    w.deps.provider._http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    assert w.refund().json()['status'] == 'reconciliation_required'
    assert len(posts(w)) == 1


def test_operation_keys_bind_request_and_operation():
    from agentpal.providers.paypal_refunds import request_key
    from agentpal.providers.paypal_sandbox import paypal_request_id
    a = request_key('request-1', 'same-refund-key')
    assert a == request_key('request-1', 'same-refund-key')
    assert a != request_key('request-2', 'same-refund-key')
    assert a != request_key('request-1', 'other-refund-key')
    assert a not in {paypal_request_id('create', 'same-refund-key'), paypal_request_id('capture', 'same-refund-key')}


def test_reconciliation_owner_and_mfa_scoped(world):
    w = world
    assert w.client.post(w.path + '/refunds/reconciliation', headers=w.agent_headers).status_code == 403
    assert w.client.post(w.path + '/refunds/reconciliation', headers=w.other_headers).status_code == 404
    w.deps.require_mfa_for_approval = True
    assert w.client.post(w.path + '/refunds/reconciliation', headers=w.headers).status_code == 403


def test_two_wallet_connections_serialize_reservations(world):
    from copy import copy
    from dataclasses import replace
    from agentpal.platform.errors import PlatformError
    from agentpal.platform.refund_models import RefundCreate
    from agentpal.platform.refunds import RefundDesk
    from agentpal.wallet.store import WalletStore
    w = world
    stores = [WalletStore(w.deps.wallet_store.path), WalletStore(w.deps.wallet_store.path)]
    owner = w.deps.owner_authenticator.authenticate(w.headers['Authorization'][7:])
    def submit(i):
        p = copy(w.app.state.platform)
        p.deps = replace(p.deps, wallet_store=stores[i])
        try:
            RefundDesk(p).create(owner, w.payment['request_id'], RefundCreate(amount='0.70'), 'parallel-key-' + str(i))
            return 201
        except PlatformError as exc:
            return exc.status
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(submit, (0, 1))) == [201, 409]
        assert len(posts(w)) == 1
    finally:
        for store in stores:
            store.close()


def test_oversized_valid_json_cannot_establish_refund(world):
    from agentpal.providers.paypal_refunds import MAX_RESPONSE_BYTES
    w = world
    original = w.backend.handler
    def handler(req):
        response = original(req)
        if req.method == 'POST' and req.url.path.endswith('/refund'):
            return httpx.Response(201, content=response.content + b' ' * MAX_RESPONSE_BYTES)
        return response
    w.deps.provider._http = httpx.Client(transport=httpx.MockTransport(handler))
    assert w.refund().json()['status'] == 'reconciliation_required'
    assert Decimal(w.call('GET', '/v1/wallets/' + w.wallet['wallet_id'])['allowance']['spent']) == 1


def test_real_sandbox_reference_alias_is_verified_without_following_it():
    from agentpal.providers.paypal_refunds import PayPalRefunds
    data = {'id': 'REFUND-1', 'status': 'COMPLETED',
            'amount': {'value': '0.40', 'currency_code': 'USD'},
            'links': [{'rel': 'up', 'href': 'https://api.sandbox.paypal.com/v2/payments/captures/' + CAPTURE}]}
    result = PayPalRefunds.verify(data, capture_id=CAPTURE, amount=Decimal('0.40'), currency='USD')
    assert result['status'] == 'COMPLETED'


@pytest.mark.parametrize('href', [
    'https://api.sandbox.paypal.com.evil.example/v2/payments/captures/' + CAPTURE,
    'https://user@api.sandbox.paypal.com/v2/payments/captures/' + CAPTURE,
    'http://api.sandbox.paypal.com/v2/payments/captures/' + CAPTURE,
    'https://api.sandbox.paypal.com/v2/payments/captures/' + CAPTURE + '?other=1',
    'https://api.sandbox.paypal.com/v2/payments/captures/' + CAPTURE + '#other',
])
def test_reference_alias_remains_exact(href):
    from agentpal.infra_contracts import ProviderError
    from agentpal.providers.paypal_refunds import PayPalRefunds
    data = {'id': 'REFUND-1', 'status': 'COMPLETED',
            'amount': {'value': '0.40', 'currency_code': 'USD'},
            'links': [{'rel': 'up', 'href': href}]}
    with pytest.raises(ProviderError):
        PayPalRefunds.verify(data, capture_id=CAPTURE, amount=Decimal('0.40'), currency='USD')


def test_dispute_detail_must_name_requested_dispute(world):
    w = world
    w.backend.disputes = [{'dispute_id': 'OWN-DISPUTE', 'status': 'OPEN'}]
    original = w.backend.handler
    def handler(req):
        if req.url.path == '/v1/customer/disputes/OWN-DISPUTE':
            return httpx.Response(200, json={'dispute_id': 'WRONG-DISPUTE', 'status': 'OPEN',
                'disputed_transactions': [{'seller_transaction_id': CAPTURE}]})
        return original(req)
    w.deps.provider._http = httpx.Client(transport=httpx.MockTransport(handler))
    assert w.client.get('/v1/disputes', headers=w.headers).status_code == 502


def test_provider_dispute_page_limit_is_enforced(world):
    w = world
    w.backend.disputes = [{'dispute_id': 'OWN-DISPUTE', 'status': 'OPEN',
        'disputed_transactions': [{'seller_transaction_id': CAPTURE}]}] * 21
    assert w.client.get('/v1/disputes', headers=w.headers).status_code == 502


def test_full_refund_remains_compatible_with_existing_startup_consistency(world):
    from agentpal.lifecycle.recovery import check_consistency
    w = world
    assert w.refund(None).json()['status'] == 'COMPLETED'
    assert check_consistency(w.deps.risk_service.store, w.deps.wallet_store) == []


def test_concurrent_same_key_is_one_provider_mutation(world):
    w = world
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: w.refund(), (0, 1)))
    assert sorted(r.status_code for r in results) == [200, 201]
    assert len({r.json()['refund_id'] for r in results}) == 1
    assert len(posts(w)) == 1
    assert Decimal(w.call('GET', '/v1/wallets/' + w.wallet['wallet_id'])['allowance']['spent']) == Decimal('0.60')


def test_saved_refund_replay_needs_no_provider_connection(world):
    w = world
    first = w.refund()
    assert first.status_code == 201
    w.deps.provider = None
    replay = w.refund()
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert len(posts(w)) == 1


@pytest.mark.parametrize('failure', ['preflight_down', 'preflight_timeout'])
def test_preflight_read_failure_releases_the_unsent_refund(world, failure):
    """Lead audit 2026-11-03 (independent review finding 2): if the capture preflight READ
    fails, no refund POST was ever sent. The reservation must be released (FAILED), not
    left reconciliation_required forever, so the owner can retry once PayPal recovers."""
    w = world
    w.backend.mode = failure
    first = w.refund(amount=None, key='preflight-key-1')
    assert first.status_code == 201, first.text
    assert first.json()['status'] == 'FAILED'
    assert not any(r.url.path.endswith('/refund') for r in w.backend.requests)
    w.backend.mode = None
    retry = w.refund(amount=None, key='preflight-key-2')
    assert retry.status_code == 201, retry.text
    assert retry.json()['status'] == 'COMPLETED'
    assert sum(1 for r in w.backend.requests if r.url.path.endswith('/refund')) == 1


def test_payment_reconciliation_after_partial_refund_keeps_it_confirmed(world):
    """Lead audit 2026-11-03 (independent review finding 1): PayPal reports a partially
    refunded capture as PARTIALLY_REFUNDED with the REFUNDED amount. That verified read must
    not downgrade the confirmed payment, block the remaining refund, or break startup."""
    from agentpal.lifecycle.recovery import check_consistency
    w = world
    w.backend.realistic_capture_status = True
    assert w.refund('0.40', key='partial-reco-1').json()['status'] == 'COMPLETED'
    r = w.client.post(w.path + '/reconciliation', headers=w.headers)
    assert r.status_code == 200, r.text
    assert w.deps.lifecycle.status(w.payment['request_id']).value == 'provider_confirmed'
    assert not check_consistency(w.deps.risk_service.store, w.deps.wallet_store)
    rest = w.refund(amount=None, key='partial-reco-2')
    assert rest.status_code == 201, rest.text
    assert rest.json()['status'] == 'COMPLETED'
    assert w.deps.lifecycle.status(w.payment['request_id']).value == 'reversed'
    r = w.client.post(w.path + '/reconciliation', headers=w.headers)
    assert r.status_code in (200, 409), r.text   # reversed is terminal: may be refused
    assert w.deps.lifecycle.status(w.payment['request_id']).value == 'reversed'
    assert not check_consistency(w.deps.risk_service.store, w.deps.wallet_store)


def test_refund_of_another_merchants_capture_fails_with_an_actionable_reason(world):
    """E2E-T1-004 (live): PayPal returns 403 when the app refunds a capture paid to another
    merchant without that merchant's permission. It is a definite non-refund: FAILED with
    failure_reason=merchant_permission_required, nothing credited, reservation released."""
    w = world
    w.backend.mode = 'not_authorized'
    r = w.refund('0.40', key='perm-key-1')
    assert r.status_code == 201, r.text
    body = r.json()
    assert body['status'] == 'FAILED' and body['failure_reason'] == 'merchant_permission_required'
    assert w.deps.lifecycle.status(w.payment['request_id']).value == 'provider_confirmed'
    listed = w.call('GET', w.path + '/refunds')
    assert Decimal(listed['refunded_total']) == 0 and Decimal(listed['reserved_total']) == 0
