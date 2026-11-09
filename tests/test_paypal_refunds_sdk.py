"""Owner SDK additions preserve decimal strings, scoped paths and operation keys."""
import asyncio
from decimal import Decimal
import json
from pathlib import Path
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'sdk' / 'python'))
from agentpal_sdk.client import OwnerClient  # noqa: E402


def test_owner_sdk_refund_methods_and_automatic_keys():
    requests = []
    def handler(req):
        requests.append(req)
        return httpx.Response(200, json={'is_real_money': False})
    async def run():
        async with OwnerClient(base_url='http://127.0.0.1', credential='apo_fake_owner_token',
                               transport=httpx.MockTransport(handler)) as client:
            await client.refund_payment('pay_123', amount=Decimal('0.40'), reason='sandbox test')
            await client.refund_payment('pay_123', idempotency_key='explicit-refund-key')
            await client.list_payment_refunds('pay_123')
            await client.reconcile_payment_refunds('pay_123')
            await client.list_disputes()
    asyncio.run(run())
    assert json.loads(requests[0].content)['amount'] == '0.40'
    assert len(requests[0].headers['Idempotency-Key']) >= 8
    assert requests[1].headers['Idempotency-Key'] == 'explicit-refund-key'
    assert json.loads(requests[1].content) == {}
    assert [(r.method, r.url.path) for r in requests] == [
        ('POST', '/v1/payment-requests/pay_123/refunds'),
        ('POST', '/v1/payment-requests/pay_123/refunds'),
        ('GET', '/v1/payment-requests/pay_123/refunds'),
        ('POST', '/v1/payment-requests/pay_123/refunds/reconciliation'), ('GET', '/v1/disputes')]


def test_sdk_refuses_float_and_overlong_reason_before_transport():
    def handler(req):
        pytest.fail('invalid SDK input reached transport')
    async def run():
        async with OwnerClient(base_url='http://127.0.0.1', credential='apo_fake_owner_token',
                               transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(TypeError):
                await client.refund_payment('pay_123', amount=0.4)
            with pytest.raises(ValueError):
                await client.refund_payment('pay_123', reason='r'*256)
    asyncio.run(run())
