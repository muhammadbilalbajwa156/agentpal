"""Payout SDK wire contracts and the offline executable demo."""

import asyncio
from decimal import Decimal
import json
from pathlib import Path
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk" / "python"))
from agentpal_sdk import AgentClient, OwnerClient  # noqa: E402
from agentpal.demo.payout_flow import run_flow  # noqa: E402


def test_payout_sdk_routes_decimal_money_and_generated_idempotency():
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True})
    async def exercise():
        transport = httpx.MockTransport(handler)
        async with AgentClient(base_url="http://127.0.0.1", credential="apk_fake-agent",
                               transport=transport) as agent:
            await agent.create_payout_request(receiver_email="payee@example.com", amount=Decimal("1.00"),
                currency="USD", purpose="Sandbox", note="Test")
            await agent.execute_payout_request("payout_test")
            await agent.reconcile_payout_request("payout_test")
            with pytest.raises(TypeError):
                await agent.create_payout_request(receiver_email="payee@example.com", amount=1.0,
                    currency="USD", purpose="Sandbox")
        async with OwnerClient(base_url="http://127.0.0.1", credential="apo_fake-owner",
                               transport=transport) as owner:
            await owner.list_payout_requests()
            await owner.get_payout_request("payout_test")
            await owner.approve_payout_request("payout_test", terms_sha256="a" * 64)
            await owner.reject_payout_request("payout_test")
            await owner.reconcile_payout_request("payout_test")
    asyncio.run(exercise())
    assert len(requests) == 8
    assert json.loads(requests[0].content)["amount"] == "1.00"
    assert requests[0].headers["Idempotency-Key"]
    assert [r.url.path for r in requests] == ["/v1/payout-requests",
        "/v1/payout-requests/payout_test/execute", "/v1/payout-requests/payout_test/reconciliation",
        "/v1/payout-requests", "/v1/payout-requests/payout_test",
        "/v1/payout-requests/payout_test/approve", "/v1/payout-requests/payout_test/reject",
        "/v1/payout-requests/payout_test/reconciliation"]


def test_offline_demo_runs_real_app_and_redacts_identifiers(monkeypatch):
    monkeypatch.setenv("PAYPAL_SANDBOX_CLIENT_ID", "deliberately-ignored-in-offline-mode")
    monkeypatch.setenv("PAYPAL_SANDBOX_CLIENT_SECRET", "deliberately-ignored-in-offline-mode")
    report = asyncio.run(run_flow())
    assert report["mode"] == "offline_mocked_paypal"
    assert report["status"] == "provider_confirmed"
    assert report["verified_outcomes"] == report["create_invocations"] == 1
    assert report["batch_id_redacted"] == "...1234"
    assert report["is_real_money"] is False
