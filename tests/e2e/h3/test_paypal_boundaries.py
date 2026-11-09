"""Hostile upstream evidence through the real approval, provider and ledger layers."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import json

import httpx
import pytest

from agentpal.infra_contracts import ProviderError
from agentpal.providers.paypal_refunds import PayPalRefunds
from test_provider_paypal_support import (
    CERT_URL, NOW, capture_event, make_provider, make_signing_cert,
    make_trusted_signing_cert, signed_headers, trust_test_root,
)


@pytest.fixture(scope='module')
def signer():
    return make_trusted_signing_cert()


def webhook(w, signer, *, event_id='WH-H3-COMPLETED', status='COMPLETED',
            event_type='PAYMENT.CAPTURE.COMPLETED', **header_changes):
    key, pem = signer
    # Freeze only signature freshness/PKI time; financial approval/read clocks stay real.
    w.provider._webhook_verifier()._now = lambda: NOW
    trust_test_root(w.provider)
    w.fake.routes[('GET', httpx.URL(CERT_URL).path)] = lambda r: httpx.Response(200, content=pem)
    data = json.loads(capture_event(event_type, status=status, event_id=event_id))
    data['resource']['id'] = 'CAPTURE-H3'
    data['resource']['amount'] = {'value': '1.00', 'currency_code': 'USD'}
    data['resource']['supplementary_data']['related_ids']['order_id'] = 'ORDER-H3'
    data['create_time'] = NOW.isoformat()
    raw = json.dumps(data).encode()
    headers = signed_headers(key, raw, transmission_time=NOW.isoformat())
    headers.update(header_changes)
    return raw, headers


@pytest.mark.parametrize('attack', ['unsigned', 'bad_signature', 'wrong_webhook_id',
    'stale', 'future', 'evil_cert', 'userinfo_cert', 'lookalike_cert', 'self_signed',
    'expired', 'wrong_san', 'duplicate_header'])
def test_webhook_rejection_never_reaches_financial_sink(h3_world, signer, attack):
    w = h3_world
    payment, _ = w.pending()
    used_signer = signer
    if attack == 'self_signed':
        used_signer = make_signing_cert()
    elif attack == 'expired':
        used_signer = make_signing_cert(not_after=NOW - timedelta(days=1))
    elif attack == 'wrong_san':
        used_signer = make_trusted_signing_cert(hostname='attacker.example')
    raw, headers = webhook(w, used_signer)
    if attack == 'unsigned':
        headers = {}
    elif attack == 'bad_signature':
        headers['PAYPAL-TRANSMISSION-SIG'] = 'AAAA'
    elif attack == 'wrong_webhook_id':
        headers = signed_headers(signer[0], raw, webhook_id='OTHER-WEBHOOK',
                                transmission_time=NOW.isoformat())
    elif attack in ('stale', 'future'):
        when = NOW + timedelta(seconds=301 if attack == 'future' else -301)
        headers = signed_headers(signer[0], raw, transmission_time=when.isoformat())
    elif attack in ('evil_cert', 'userinfo_cert', 'lookalike_cert'):
        host = {'evil_cert': 'evil.example', 'userinfo_cert': 'user@api.sandbox.paypal.com',
                'lookalike_cert': 'api.sandbox.paypal.com.evil.example'}[attack]
        headers['PAYPAL-CERT-URL'] = 'https://' + host + '/v1/notifications/certs/C'
    elif attack == 'duplicate_header':
        headers = list(headers.items()) + [('paypal-transmission-sig', 'AAAA')]
    response = w.client.post('/v1/providers/paypal/webhooks', content=raw, headers=headers)
    assert response.status_code == 400
    ledger = w.deps.risk_service.store
    assert ledger.ledger_state(payment['request_id']).value == 'pending'
    assert ledger.query('SELECT * FROM provider_events') == []
    assert ledger.query('SELECT * FROM payment_outcomes') == []
    if attack in ('unsigned', 'evil_cert', 'userinfo_cert', 'lookalike_cert', 'duplicate_header'):
        assert not any('/certs/' in r.url.path for r in w.fake.requests)


def test_signed_duplicates_replays_and_reordering_settle_once(h3_world, signer):
    w = h3_world
    payment, _ = w.pending()
    # Signed capture evidence + independent exact identity read; no capture POST.
    raw, headers = webhook(w, signer)
    for _ in range(3):
        response = w.client.post('/v1/providers/paypal/webhooks', content=raw, headers=headers)
        assert response.status_code == 200
    for event_id, event_type, status in [('WH-H3-PENDING', 'PAYMENT.CAPTURE.PENDING', 'PENDING'),
        ('WH-H3-APPROVED', 'CHECKOUT.ORDER.APPROVED', 'APPROVED')]:
        if event_type.startswith('CHECKOUT'):
            data = {'id': event_id, 'event_type': event_type, 'resource': deepcopy(w.state.order)}
            raw = json.dumps(data).encode()
            headers = signed_headers(signer[0], raw, transmission_time=NOW.isoformat())
        else:
            raw, headers = webhook(w, signer, event_id=event_id, event_type=event_type, status=status)
        assert w.client.post('/v1/providers/paypal/webhooks', content=raw,
                             headers=headers).status_code == 200
    ledger = w.deps.risk_service.store
    assert ledger.ledger_state(payment['request_id']).value == 'completed'
    assert len(ledger.query('SELECT * FROM payment_outcomes')) == 1
    assert len(ledger.query('SELECT * FROM provider_events')) == 3
    assert not any(r.url.path.endswith('/capture') for r in w.fake.requests)


@pytest.mark.parametrize('change', ['email', 'merchant_id', 'custom_id', 'reference_id',
    'unit_amount', 'unit_currency', 'units', 'capture_amount', 'capture_currency',
    'capture_id', 'status', 'missing_capture'])
def test_hostile_capture_representation_quarantines_without_retry(h3_world, change):
    w = h3_world
    payment, path = w.pending()
    data = deepcopy(w.state.order)
    data['status'] = 'COMPLETED'
    unit = data['purchase_units'][0]
    unit['payments'] = {'captures': [{'id': 'CAPTURE-H3', 'status': 'COMPLETED',
        'amount': {'value': '1.00', 'currency_code': 'USD'}}]}
    cap = unit['payments']['captures'][0]
    if change == 'email':
        unit['payee']['email_address'] = 'substitute@example.com'
    elif change == 'merchant_id':
        unit['payee']['merchant_id'] = 'OTHER-MERCHANT'
    elif change in ('custom_id', 'reference_id'):
        unit[change] = 'other-request'
    elif change.startswith('unit_'):
        unit['amount']['value' if change == 'unit_amount' else 'currency_code'] = (
            '2.00' if change == 'unit_amount' else 'EUR')
    elif change == 'units':
        data['purchase_units'].append(deepcopy(unit))
    elif change == 'capture_amount':
        cap['amount']['value'] = '2.00'
    elif change == 'capture_currency':
        cap['amount']['currency_code'] = 'EUR'
    elif change == 'capture_id':
        cap['id'] = 'CAPTURE-\u202e-OTHER'
    elif change == 'status':
        cap['status'] = 'COMPLETED\u200b'
    else:
        unit['payments'] = {}
    w.state.overrides['capture'] = data
    r = w.client.post(path + '/capture', headers=w.owner)
    assert r.status_code == 200
    assert r.json()['status'] == 'reconciliation_required'
    assert w.client.post(path + '/capture', headers=w.owner).status_code == 409
    ledger = w.deps.risk_service.store
    assert ledger.query('SELECT * FROM payment_outcomes') == []
    assert ledger.spend_breakdown(w.aid, 'USD', ledger.clock())['committed'] == Decimal('1.00')
    assert sum(r.url.path.endswith('/capture') for r in w.fake.requests) == 1


@pytest.mark.parametrize('payload', [b'{}', b'[]', b'not-json',
    b'{"pad":"' + b'x' * (1024 * 1024) + b'"}',
    b'{"id":"OTHER-ORDER","status":"COMPLETED"}',
    b'{"x":' + b'[' * 6000 + b'0' + b']' * 6000 + b'}'])
def test_bad_create_keeps_hold_and_one_provider_invocation(h3_world, payload):
    w = h3_world
    payment = w.prepare()
    w.approve(payment)
    w.state.overrides['create'] = httpx.Response(201, content=payload)
    path = '/v1/payment-requests/' + payment['request_id']
    r = w.client.post(path + '/execute', headers=w.agent)
    assert r.status_code == 200
    assert r.json()['status'] == 'reconciliation_required'
    assert w.client.post(path + '/execute', headers=w.agent).status_code == 409
    ledger = w.deps.risk_service.store
    assert ledger.query('SELECT * FROM payment_outcomes') == []
    assert ledger.spend_breakdown(w.aid, 'USD', ledger.clock())['committed'] == Decimal('1.00')
    assert sum(r.url.path == '/v2/checkout/orders' for r in w.fake.requests) == 1


@pytest.mark.parametrize('payload', [
    b'{"id":"ORDER-H3","links":[{"rel":[],"href":"https://evil.example"}]}',
])
# Fixed by the lead 2026-11-05 (was strict xfail E2E-H3-2).
def test_malformed_create_has_controlled_response_and_keeps_hold(h3_world, payload):
    w = h3_world
    payment = w.prepare()
    w.approve(payment)
    w.state.overrides['create'] = httpx.Response(201, content=payload)
    path = '/v1/payment-requests/' + payment['request_id']
    response = w.client.post(path + '/execute', headers=w.agent)
    ledger = w.deps.risk_service.store
    # Financial fallback already holds; missing boundary is controlled HTTP behavior.
    assert ledger.ledger_state(payment['request_id']).value == 'reconciliation_required'
    assert ledger.query('SELECT * FROM payment_outcomes') == []
    assert w.client.post(path + '/execute', headers=w.agent).status_code == 409
    assert w.client.get('/v1/health').status_code == 200
    assert sum(r.url.path == '/v2/checkout/orders' for r in w.fake.requests) == 1
    assert response.status_code in (200, 502, 503)


@pytest.mark.parametrize('change', ['amount', 'currency', 'capture_link', 'refund_id', 'status'])
def test_refund_verifier_refuses_inexact_evidence(change):
    amount = {'value': '0.40', 'currency_code': 'USD'}
    data = {'id': 'REFUND-H3', 'amount': amount, 'status': 'COMPLETED', 'links': [
        {'rel': 'up', 'href': 'https://api-m.sandbox.paypal.com/v2/payments/captures/CAPTURE-H3'}]}
    if change == 'amount':
        amount['value'] = '1.01'
    elif change == 'currency':
        amount['currency_code'] = 'EUR'
    elif change == 'capture_link':
        data['links'][0]['href'] += '?x=1'
    elif change == 'refund_id':
        data['id'] = 'OTHER-REFUND'
    else:
        data['status'] = 'COMPLETED\u200b'
    with pytest.raises(ProviderError):
        PayPalRefunds.verify(data, capture_id='CAPTURE-H3', amount=Decimal('0.40'),
                             currency='USD', refund_id='REFUND-H3')


@pytest.mark.parametrize('target', ['https://evil.example/', 'file:///tmp/test',
    'https://user:pass@sandbox.paypal.com/', 'https://sandbox.paypal.com.evil.example/'])
def test_paypal_payer_link_is_dropped_without_fetch(target):
    provider, fake = make_provider()
    try:
        assert provider._payer_link({'links': [{'rel': 'approve', 'href': target}]}) is None
        assert fake.requests == []
    finally:
        provider.http_client.close()
