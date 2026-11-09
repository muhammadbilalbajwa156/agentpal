"""Owner isolation and reconciliation against authenticated PayPal records."""
from datetime import timedelta
import json

import httpx
import pytest

from test_platform_fixtures import build_world, ALL_PAY, OWNER_A, OWNER_B, START
from test_paypal_ops_reporting import reporting


def setup_ops(*, amount='49.99', status='CREATED', search=None, missing=False):
    w = build_world()
    _, key, _ = w.funded_agent(caps=ALL_PAY)
    payment = w.pay(key).json()
    rid = payment['request_id']
    w.client.post(f'/v1/payment-requests/{rid}/approve', headers=w.h(OWNER_A),
                  json={'terms_sha256': payment['terms_sha256']})
    w.client.post(f'/v1/payment-requests/{rid}/execute', headers=w.h(key))
    unit = {'amount': {'value': amount, 'currency_code': 'USD'}}
    if status == 'COMPLETED':
        unit['payments'] = {'captures': [{'id': 'CAPTURE-1', 'status': 'COMPLETED',
            'amount': unit['amount'], 'update_time': START.isoformat()}]}
    order = {'id': 'ORDER-0001', 'status': status, 'purchase_units': [unit]}
    seen = []
    def handler(req):
        seen.append(req)
        if req.url.path == '/v1/reporting/transactions':
            return httpx.Response(200, json={'transaction_details': search or [],
                                             'total_pages': 1, 'page': 1})
        return httpx.Response(404 if missing else 200, json=order)
    r = reporting(handler)
    w.deps.provider = r.provider
    return w, rid, key, seen, order


def get(w, token=OWNER_A):
    return w.client.get('/v1/paypal/reconciliation', params={
        'start': (START - timedelta(hours=1)).isoformat(),
        'end': (START + timedelta(hours=1)).isoformat()}, headers=w.h(token))


def test_reconciliation_owner_only_and_empty_owner_never_reads_foreign_orders():
    w, rid, key, seen, _ = setup_ops()
    assert get(w, key).status_code == 403
    response = get(w, OWNER_B)
    assert response.status_code == 200
    assert response.json()['rows'] == []
    assert not seen
    assert rid not in response.text


def test_reconciliation_reads_are_read_only_and_no_foreign_transaction_leak():
    w, rid, _, seen, _ = setup_ops(search=[{'transaction_info': {
        'transaction_id': 'FOREIGN-SECRET', 'payer_email': 'private@example.test'}}])
    before = json.dumps(w.risk.store.ledger_events(rid), sort_keys=True)
    response = get(w)
    assert response.status_code == 200
    assert response.json()['rows'][0]['result'] == 'pending'
    assert 'FOREIGN-SECRET' not in response.text and 'private@example.test' not in response.text
    assert all(r.method == 'GET' for r in seen)
    assert json.dumps(w.risk.store.ledger_events(rid), sort_keys=True) == before


def test_reconciliation_amount_mismatch_and_missing_order():
    w, _, _, _, _ = setup_ops(amount='50.00')
    assert get(w).json()['rows'][0]['reason'] == 'amount_mismatch'
    w, _, _, _, _ = setup_ops(missing=True)
    assert get(w).json()['rows'][0]['result'] == 'missing_at_paypal'


def test_reconciliation_lifecycle_mismatch_is_authoritative_despite_search_lag():
    w, _, _, _, _ = setup_ops(status='COMPLETED')
    row = get(w).json()['rows'][0]
    assert row['result'] == 'mismatch' and row['reason'] == 'lifecycle_mismatch'


def test_reconciliation_search_lag_then_missing_after_three_hours(monkeypatch):
    w, _, _, _, _ = setup_ops(status='COMPLETED')
    from agentpal.infra_contracts import PaymentLifecycle
    monkeypatch.setattr(w.platform.payments, 'status', lambda *a: PaymentLifecycle.provider_confirmed)
    assert get(w).json()['rows'][0]['reason'] == 'paypal_search_pending'
    w.clock.advance(hours=4)
    assert get(w).json()['rows'][0]['reason'] == 'paypal_search_missing'


def test_reconciliation_capture_search_exact_amount_and_currency(monkeypatch):
    w, _, _, _, order = setup_ops(status='COMPLETED', search=[{'transaction_info': {
        'transaction_id': 'CAPTURE-1', 'transaction_status': 'S',
        'transaction_amount': {'value': '49.99', 'currency_code': 'USD'}}}])
    from agentpal.infra_contracts import PaymentLifecycle
    monkeypatch.setattr(w.platform.payments, 'status', lambda *a: PaymentLifecycle.provider_confirmed)
    assert get(w).json()['rows'][0]['result'] == 'matched'
    order['purchase_units'][0]['payments']['captures'][0]['amount'] = {'value': '49.99', 'currency_code': 'EUR'}
    assert get(w).json()['rows'][0]['reason'] == 'currency_mismatch'


@pytest.mark.parametrize('amount', ['NaN', 'Infinity', '1e9000', 49.99, '-1.00'])
def test_reconciliation_malformed_money_never_matches(amount):
    w, _, _, _, _ = setup_ops(amount=amount)
    assert get(w).json()['rows'][0]['result'] == 'pending'


def test_reconciliation_order_shape_ambiguity_never_matches():
    w, _, _, _, order = setup_ops()
    order['purchase_units'].append(order['purchase_units'][0])
    assert get(w).json()['rows'][0]['reason'] == 'paypal_order_ambiguous'


def test_reconciliation_window_invalid_and_request_count_bounded():
    w, _, _, seen, _ = setup_ops()
    response = w.client.get('/v1/paypal/reconciliation', params={
        'start': START.isoformat(), 'end': (START + timedelta(days=32)).isoformat()}, headers=w.h(OWNER_A))
    assert response.status_code == 422 and not seen


def test_reconciliation_recent_search_unavailable_is_labelled_pending(monkeypatch):
    from agentpal.platform.paypal_ops import compare
    view = {'terms': {'amount': __import__('decimal').Decimal('1.00'), 'currency': 'USD'},
            'status': 'provider_confirmed'}
    order = {'status': 'COMPLETED', 'purchase_units': [{'amount': {'value': '1.00', 'currency_code': 'USD'},
        'payments': {'captures': [{'id': 'CAPTURE-1', 'status': 'COMPLETED',
            'amount': {'value': '1.00', 'currency_code': 'USD'}, 'update_time': START.isoformat()}]}}]}
    assert compare(view, order, None, START) == ('pending', 'paypal_search_pending')
    assert compare(view, order, None, START + timedelta(hours=4)) == ('pending', 'paypal_search_unavailable')


def test_reconciliation_request_limit_fails_before_provider_io():
    w, rid, _, seen, _ = setup_ops()
    meta = w.platform.payments.meta(rid)
    with w.wallet_store.tx() as c:
        for i in range(100):
            c.execute('INSERT INTO platform_payment_meta (request_id, agent_id, owner_id, wallet_id, '
                'decision_requested, created_at) VALUES (?,?,?,?,?,?)',
                (f'pay_limit_{i}', meta['agent_id'], meta['owner_id'], meta['wallet_id'], 0, meta['created_at']))
    response = get(w)
    assert response.status_code == 422 and response.json()['error'] == 'reconciliation_request_limit'
    assert not seen


def test_reconciliation_rechecks_owner_before_reading_a_selected_reference(monkeypatch):
    w, rid, _, seen, _ = setup_ops()
    original = w.platform.payments.store.query
    def stale_selection(sql, params=()):
        if sql.startswith('SELECT request_id FROM platform_payment_meta WHERE owner_id='):
            return [{'request_id': rid}]
        return original(sql, params)
    monkeypatch.setattr(w.platform.payments.store, 'query', stale_selection)
    response = get(w, OWNER_B)
    assert response.status_code == 404 and response.json() == {'error': 'not_found'}
    assert not seen


def test_compare_completed_requires_single_real_capture_and_search_success():
    from decimal import Decimal
    from agentpal.platform.paypal_ops import compare
    view = {'terms': {'amount': Decimal('1.00'), 'currency': 'USD'}, 'status': 'provider_confirmed'}
    unit = {'amount': {'value': '1.00', 'currency_code': 'USD'}}
    order = {'status': 'COMPLETED', 'purchase_units': [unit]}
    assert compare(view, order, [], START) == ('mismatch', 'lifecycle_mismatch')
    capture = {'id': 'CAPTURE-1', 'status': 'COMPLETED', 'amount': unit['amount']}
    unit['payments'] = {'captures': [capture]}
    row = {'transaction_id': 'CAPTURE-1', 'transaction_status': 'S',
           'transaction_amount': unit['amount']}
    assert compare(view, order, [row], START)[0] == 'matched'
    row['transaction_amount'] = {'value': '2.00', 'currency_code': 'USD'}
    assert compare(view, order, [row], START)[1] == 'search_amount_mismatch'
    row['transaction_amount'] = {'value': '1.00', 'currency_code': 'EUR'}
    assert compare(view, order, [row], START)[1] == 'search_currency_mismatch'
    row['transaction_amount'] = unit['amount']
    row['transaction_status'] = 'P'
    assert compare(view, order, [row], START)[1] == 'search_status_mismatch'
    assert compare(view, order, [row, row], START)[1] == 'paypal_search_ambiguous'
    capture['id'] = '../unsafe'
    assert compare(view, order, [row], START)[1] == 'paypal_capture_invalid'


def test_reconciliation_foreign_provider_reference_is_not_read():
    w, rid, _, seen, _ = setup_ops()
    original = w.lifecycle.refs[rid]
    w.lifecycle.refs[rid] = original.model_copy(update={'provider': 'other_provider'})
    assert get(w).json()['rows'][0]['reason'] == 'provider_reference_ambiguous'
    assert not seen


def test_reconciliation_status_string_is_allowlisted():
    w, _, _, _, order = setup_ops()
    order['status'] = 'PRIVATE-PROVIDER-TEXT'
    response = get(w)
    assert response.json()['rows'][0]['paypal_status'] is None
    assert 'PRIVATE-PROVIDER-TEXT' not in response.text


def test_multiple_captures_are_ambiguous():
    w, _, _, _, order = setup_ops(status='COMPLETED')
    captures = order['purchase_units'][0]['payments']['captures']
    captures.append(captures[0])
    assert get(w).json()['rows'][0]['reason'] == 'paypal_order_ambiguous'


def test_owner_sdk_reconciliation_encodes_window_and_uses_owner_token():
    import asyncio
    from pathlib import Path
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'sdk' / 'python'))
    from agentpal_sdk.client import OwnerClient
    seen = []
    async def handler(request):
        seen.append(request)
        assert request.url.path == '/v1/paypal/reconciliation'
        assert request.headers['Authorization'] == 'Bearer apo_fake_owner_token'
        assert request.url.params['start'] == START.isoformat()
        assert request.url.params['end'] == (START + timedelta(days=1)).isoformat()
        return httpx.Response(200, json={'rows': [], 'read_only': True})
    async def run():
        async with OwnerClient(base_url='http://127.0.0.1', credential='apo_fake_owner_token',
                               transport=httpx.MockTransport(handler)) as owner:
            assert await owner.reconcile_with_paypal(start=START.isoformat(),
                end=(START + timedelta(days=1)).isoformat()) == {'rows': [], 'read_only': True}
    asyncio.run(run())
    assert len(seen) == 1
