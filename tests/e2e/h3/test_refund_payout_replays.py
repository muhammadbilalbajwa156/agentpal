"""Uncertain downstream money operations keep exposure and never re-POST."""
from copy import deepcopy
from decimal import Decimal
import json

import httpx
import pytest

from test_paypal_payouts import BODY, CAPS, PayoutHTTP
from agentpal.providers.paypal_sandbox import PayPalSandboxProvider
from test_provider_paypal_support import make_config


@pytest.mark.parametrize('attack', ['amount', 'currency', 'capture_link', 'status',
    'oversize', 'nested', 'timeout'])
def test_unknown_refund_never_credits_or_double_posts(h3_world, attack):
    w = h3_world
    _, path = w.pending()
    assert w.client.post(path + '/capture', headers=w.owner).json()['status'] == 'provider_confirmed'
    w.fake.routes[('GET', '/v2/payments/captures/CAPTURE-H3')] = lambda r: httpx.Response(200,
        json={'id': 'CAPTURE-H3', 'status': 'COMPLETED',
              'amount': {'value': '1.00', 'currency_code': 'USD'}})

    def refund(request):
        payload = {'id': 'REFUND-H3', 'status': 'COMPLETED',
            'amount': {'value': '0.40', 'currency_code': 'USD'},
            'links': [{'rel': 'up', 'href':
                'https://api-m.sandbox.paypal.com/v2/payments/captures/CAPTURE-H3'}]}
        if attack == 'amount':
            payload['amount']['value'] = '0.41'
        elif attack == 'currency':
            payload['amount']['currency_code'] = 'EUR'
        elif attack == 'capture_link':
            payload['links'][0]['href'] += '-OTHER'
        elif attack == 'status':
            payload['status'] = 'COMPLETED\u200b'
        elif attack == 'oversize':
            payload['padding'] = 'x' * (256 * 1024)
        elif attack == 'nested':
            return httpx.Response(201, content=b'{"x":' + b'[' * 6000 + b'0' + b']' * 6000 + b'}')
        else:
            raise httpx.ReadTimeout('offline response lost', request=request)
        return httpx.Response(201, json=payload)

    w.fake.routes[('POST', '/v2/payments/captures/CAPTURE-H3/refund')] = refund
    headers = {**w.owner, 'Idempotency-Key': 'h3-refund-replay'}
    for expected in (201, 200):
        r = w.client.post(path + '/refunds', headers=headers, json={'amount': '0.40'})
        assert r.status_code == expected
        assert r.json()['status'] == 'reconciliation_required'
    assert sum(r.url.path.endswith('/refund') for r in w.fake.requests) == 1
    ledger = w.deps.risk_service.store
    assert ledger.spend_breakdown(w.aid, 'USD', ledger.clock())['committed'] == Decimal('1.00')
    assert w.client.post(path + '/refunds', headers={**w.owner, 'Idempotency-Key': 'h3-over-refund'},
                         json={'amount': '0.61'}).status_code == 409
    assert sum(r.url.path.endswith('/refund') for r in w.fake.requests) == 1


@pytest.mark.parametrize('attack', ['receiver', 'amount', 'status', 'timeout', 'missing_batch'])
def test_unknown_payout_never_reposts_or_falsely_confirms(h3_world, attack):
    w = h3_world
    assert w.client.put('/v1/agents/' + w.aid + '/permissions', headers=w.owner,
                         json={'capabilities': CAPS}).status_code == 200
    backend = PayoutHTTP()

    def handler(request):
        result = backend.handler(request)
        if attack == 'missing_batch' and request.url.path == '/v1/payments/payouts':
            return httpx.Response(201, json={'batch_header': {'batch_status': 'SUCCESS'}})
        return result

    if attack == 'timeout':
        backend.post_error = 'timeout'
    else:
        def change(data):
            data = deepcopy(data)
            item = data['items'][0]
            if attack == 'receiver':
                item['payout_item']['receiver'] = 'other@example.com'
            elif attack == 'amount':
                item['payout_item']['amount']['value'] = '1.01'
            elif attack == 'status':
                item['transaction_status'] = 'SUCCESS\u200b'
            return data
        backend.change = change
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        original = w.deps.provider
        w.deps.provider = PayPalSandboxProvider(make_config(), http_client=http)
        try:
            r = w.client.post('/v1/payout-requests', headers={**w.agent,
                'Idempotency-Key': 'h3-payout-create'}, json=BODY)
            assert r.status_code == 201
            payment = r.json()
            path = '/v1/payout-requests/' + payment['request_id']
            assert w.client.post(path + '/approve', headers=w.owner,
                json={'terms_sha256': payment['terms_sha256']}).status_code == 200
            for _ in range(2):
                r = w.client.post(path + '/execute', headers=w.agent)
                assert r.status_code == 200
                assert r.json()['status'] == 'reconciliation_required'
            assert len(backend.posts) == 1
            ledger = w.deps.risk_service.store
            assert ledger.query('SELECT * FROM payment_outcomes') == []
            assert ledger.spend_breakdown(w.aid, 'USD', ledger.clock())['committed'] == Decimal('1.00')
            assert len(json.loads(backend.posts[0].content)['items']) == 1
        finally:
            w.deps.provider = original
