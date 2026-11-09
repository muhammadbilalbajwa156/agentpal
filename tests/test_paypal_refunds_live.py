"""LIVE SANDBOX ONLY: one USD capture, two refunds, verified provider reads.

Skips without app/buyer credentials. No screenshots, traces, credentials or raw
provider bodies are persisted. Browser evidence never settles the ledger.
"""
import os
import time
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from agentpal.api import create_app
from agentpal.auth import TokenRegistry
from agentpal.config import Settings
from agentpal.platform.wiring import build_platform
from agentpal.providers.paypal_refunds import PayPalRefunds

_REQUIRED = ('PAYPAL_SANDBOX_CLIENT_ID', 'PAYPAL_SANDBOX_CLIENT_SECRET',
             'PAYPAL_SANDBOX_BUYER_EMAIL', 'PAYPAL_SANDBOX_BUYER_PASSWORD')
pytestmark = pytest.mark.skipif(any(not os.environ.get(k) for k in _REQUIRED),
                                reason='LIVE sandbox refund: app and buyer credentials required')


def approve_sandbox_buyer(url):
    """Explicitly test-only persona action, separate from AgentPal owner approval.
    Lead integration 2026-11-03: delegates to the one reviewed harness
    (agentpal.demo.sandbox_buyer), which honours PLAYWRIGHT_CHROMIUM_EXECUTABLE."""
    from agentpal.demo.sandbox_buyer import approve_sandbox_buyer as harness
    result = harness(url)
    if result.status != 'approved':
        pytest.fail('LIVE sandbox buyer automation unavailable: ' + result.reason, pytrace=False)


def test_live_partial_then_full_refund_and_read(tmp_path):
    settings = Settings(db_path=str(tmp_path / 'risk.db'), auth_mode='required',
                        analyzer_chain=['heuristic'], allowed_hosts=['127.0.0.1'])
    deps = build_platform(settings=settings)
    try:
        identity = deps.owner_authenticator
        identity.create_owner('refund_live_owner')
        token, _ = identity.issue_token('refund_live_owner')
        owner = {'Authorization': 'Bearer ' + token}
        app = create_app(service=deps.risk_service, settings=settings, registry=TokenRegistry(), platform=deps)
        with TestClient(app, base_url='http://127.0.0.1') as client:
            def call(method, path, *, headers=owner, expected=200, **kw):
                response = client.request(method, path, headers=headers, **kw)
                if response.status_code not in (expected if isinstance(expected, tuple) else (expected,)):
                    pytest.fail(f'LIVE API operation returned HTTP {response.status_code}', pytrace=False)
                return response.json()
            registered = call('POST', '/v1/agents', expected=201, json={'name': 'LIVE refund test',
                'capabilities': ['wallet.read', 'payment.prepare', 'owner.request_decision', 'payment.execute_sandbox']})
            ah = {'Authorization': 'Bearer ' + registered['api_key']}
            wallet = call('POST', '/v1/wallets', expected=201, json={
                'agent_id': registered['agent']['agent_id'], 'currency': 'USD',
                'monthly_budget': '5.00', 'per_transaction_limit': '1.00'})
            pay = call('POST', '/v1/payment-requests', expected=201,
                headers={**ah, 'Idempotency-Key': 'live-' + uuid4().hex}, json={
                'amount': '1.00', 'currency': 'USD', 'merchant_name': 'Sandbox Test Store',
                'purpose': 'LIVE sandbox refund regression, no real money'})
            path = '/v1/payment-requests/' + pay['request_id']
            call('POST', path + '/approve', json={'terms_sha256': pay['terms_sha256']})
            call('POST', path + '/execute', headers=ah)
            refs = call('GET', path + '/reconciliation')['provider_references']
            approve_sandbox_buyer(refs[0]['payer_action_url'])
            for _attempt in range(6):  # PayPal may need seconds to register the buyer click;
                captured = call('POST', path + '/capture', expected=(200, 409))  # 409 = no POST sent
                if captured.get('status') == 'provider_confirmed':
                    break
                time.sleep(3)
            assert captured['status'] == 'provider_confirmed'
            first = call('POST', path + '/refunds', expected=201,
                         headers={**owner, 'Idempotency-Key': 'partial-' + uuid4().hex}, json={'amount': '0.40'})
            assert first['status'] == 'COMPLETED'
            assert call('GET', path)['status'] == 'provider_confirmed'
            second = call('POST', path + '/refunds', expected=201,
                          headers={**owner, 'Idempotency-Key': 'full-' + uuid4().hex}, json={})
            assert second['status'] == 'COMPLETED'
            assert call('GET', path)['status'] == 'reversed'
            adapter = PayPalRefunds(deps.provider)
            for refund in (first, second):
                read = adapter.read(refund['provider_refund_id'])
                assert read['status'] == 'COMPLETED'
                assert Decimal(read['amount']['value']) == Decimal(refund['amount'])
            listing = call('POST', path + '/refunds/reconciliation')
            assert Decimal(listing['refunded_total']) == Decimal('1.00')
            assert Decimal(call('GET', '/v1/wallets/' + wallet['wallet_id'])['allowance']['spent']) == 0
            print('LIVE SANDBOX capture/refunds verified; refund ids …' + first['provider_refund_id'][-4:]
                  + ', …' + second['provider_refund_id'][-4:] + '; total USD 1.00; reversed')
    except httpx.HTTPError:
        pytest.fail('LIVE provider network unavailable (details withheld)', pytrace=False)
    finally:
        deps.close()


def test_live_dispute_page_is_bounded_and_scoped(tmp_path):
    settings = Settings(db_path=str(tmp_path / 'risk.db'), auth_mode='required',
                        analyzer_chain=['heuristic'], allowed_hosts=['127.0.0.1'])
    deps = build_platform(settings=settings)
    try:
        from agentpal.platform.refunds import RefundDesk
        from agentpal.platform.runtime import Platform
        identity = deps.owner_authenticator
        identity.create_owner('empty_dispute_owner')
        token, _ = identity.issue_token('empty_dispute_owner')
        platform = Platform(deps, lambda: deps.risk_service)
        owner = identity.authenticate(token)
        assert RefundDesk(platform).disputes(owner)['disputes'] == []
        print('LIVE SANDBOX disputes read: HTTP success; unrelated disputes withheld')
    finally:
        deps.close()
