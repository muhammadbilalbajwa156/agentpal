"""Offline TCP regression evidence; no external credentials required.

Confirmed defects deliberately remain strict xfails. Run with --runxfail to
observe the actual failures on the acceptance baseline.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import uuid

import httpx
import pytest

from run_acceptance import Journey, ROOT, redacted, server
from agentpal_sdk import OwnerClient


@pytest.fixture(scope="module")
def tcp(tmp_path_factory):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    directory = tmp_path_factory.mktemp("t1-tcp") / "databases"
    with server(directory, port=port) as (client, owner, base):
        report = {k: [] for k in ("http", "passes", "allowances")}
        journey = Journey(client, owner, report)
        agents = journey.onboard()
        yield journey, agents, base


def test_real_tcp_onboarding_three_roles_credentials_and_freeze(tcp):
    j, agents, _ = tcp
    assert len(agents) == 3
    allowance = j.allowance(agents[0][1], "offline initial")
    assert Decimal(allowance["spent"]) == 0
    assert Decimal(allowance["reserved"]) == 0
    assert Decimal(allowance["available"]) == 10000


def test_exact_owner_terms_replay_rejection_and_allowance(tcp):
    j, agents, _ = tcp
    key = agents[2][1]
    idem = uuid.uuid4().hex
    body = j.payment_body()
    p = j.call("POST", "/v1/payment-requests", key, body, key=idem)
    rid = p["request_id"]
    reserved = j.allowance(key, "reserved")
    assert reserved["reserved"] == "1.00"
    assert reserved["available"] == "99.00"
    assert p["status"] == "awaiting_approval"
    replay = j.call("POST", "/v1/payment-requests", key, body, key=idem)
    assert replay["request_id"] == rid
    j.call("POST", "/v1/payment-requests", key, {**body, "amount": "2.00"}, key=idem, expected=(409,))
    j.call("POST", f"/v1/payment-requests/{rid}/approve", key,
           {"terms_sha256": p["terms_sha256"]}, expected=(403,))
    j.call("POST", f"/v1/payment-requests/{rid}/execute", key, expected=(409,))
    j.call("POST", f"/v1/payment-requests/{rid}/approve",
           body={"terms_sha256": "0" * 64}, expected=(409,))
    assert j.allowance(key, "failed attacks") == reserved
    j.call("POST", f"/v1/payment-requests/{rid}/reject")
    released = j.allowance(key, "rejected")
    assert Decimal(released["reserved"]) == 0
    assert released["available"] == "100.00"


def test_simulator_never_becomes_verified_money(tcp):
    j, agents, _ = tcp
    key = agents[2][1]
    p = j.call("POST", "/v1/payment-requests", key, j.payment_body(purpose="Different SDK hosting purpose"),
               key=uuid.uuid4().hex)
    rid = p["request_id"]
    j.call("POST", f"/v1/payment-requests/{rid}/approve", body={"terms_sha256": p["terms_sha256"]})
    result = j.call("POST", f"/v1/payment-requests/{rid}/execute", key)
    assert result["status"] == "provider_pending"
    assert result["is_real_money"] is False
    a = j.allowance(key, "simulator")
    assert Decimal(a["spent"]) == 0
    assert a["pending"] == "1.00"
    assert a["available"] == "99.00"
    j.call("POST", f"/v1/payment-requests/{rid}/execute", key, expected=(409,))
    assert j.allowance(key, "duplicate execute") == a


@pytest.mark.parametrize("field,value", [("amount", 1.25), ("amount", 1), ("status", "provider_confirmed")])
def test_wire_amounts_and_forged_status_refused(tcp, field, value):
    j, agents, _ = tcp
    j.call("POST", "/v1/payment-requests", agents[2][1], {**j.payment_body(), field: value},
           key=uuid.uuid4().hex, expected=(422,))


def test_per_transaction_and_monthly_budget_refuse_without_extra_holds(tcp):
    j, _, _ = tcp
    reg = j.call("POST", "/v1/agents", body={"name": "T1 bounded wallet"})
    key = reg["api_key"]
    j.call("POST", "/v1/wallets", body={"agent_id": reg["agent"]["agent_id"],
        "currency": "USD", "monthly_budget": "2.00", "per_transaction_limit": "2.00"})
    denied = j.call("POST", "/v1/payment-requests", key, j.payment_body(amount="3.00"),
                    key=uuid.uuid4().hex)
    assert denied["status"] == "rejected"
    assert denied["recommendation"] == "block"
    p = j.call("POST", "/v1/payment-requests", key, j.payment_body(amount="2.00"), key=uuid.uuid4().hex)
    denied = j.call("POST", "/v1/payment-requests", key,
           j.payment_body(amount="1.00", purpose="Another merchant invoice", merchant_domain="other.example"),
           key=uuid.uuid4().hex)
    assert denied["status"] == "rejected"
    assert denied["recommendation"] == "block"
    held = j.allowance(key, "budget refuses extra holds")
    assert Decimal(held["reserved"]) == 2
    assert Decimal(held["available"]) == 0
    j.call("POST", f"/v1/payment-requests/{p['request_id']}/reject")
    assert Decimal(j.allowance(key, "budget release")["available"]) == 2


# Fixed by the lead 2026-11-05 (was strict xfail E2E-T1-001-1).
def test_owner_sdk_can_capture_the_documented_paypal_purchase():
    """Expected public SDK operation sends the owner capture request over HTTP."""
    async def exercise():
        seen = []

        def respond(request):
            seen.append((request.method, request.url.path))
            return httpx.Response(200, json={"status": "provider_confirmed", "is_real_money": False})

        async with OwnerClient(base_url="http://127.0.0.1", credential="apo_" + "placeholder",
                               transport=httpx.MockTransport(respond)) as owner:
            result = await owner.capture_payment_request("request-placeholder")
        assert result["status"] == "provider_confirmed"
        assert seen == [("POST", "/v1/payment-requests/request-placeholder/capture")]
    asyncio.run(exercise())


# Fixed by the lead 2026-11-05 (was strict xfail E2E-T1-001-2).
def test_owner_sdk_can_initiate_payment_provider_reconciliation():
    async def exercise():
        async with OwnerClient(base_url="http://127.0.0.1", credential="apo_" + "placeholder",
                               transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"status": "provider_pending"}))) as owner:
            result = await owner.reconcile_payment_request("request-placeholder")
        assert result["status"] == "provider_pending"
    asyncio.run(exercise())


# Fixed by the lead 2026-11-05 (was strict xfail E2E-T1-002-1).
def test_documented_or_cli_owner_bootstrap_exists_for_new_developer():
    docs = "\n".join((ROOT / p).read_text() for p in (
        "README.md", "docs/DEVELOPER_API.md", "docs/SDK.md"))
    cli = subprocess.run([sys.executable, "-m", "agentpal", "--help"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    # Require a concrete provisioning recipe, not just 'obtain a token'.
    assert ("create_owner(" in docs and "issue_token(" in docs) or "new-owner" in cli


# Fixed by the lead 2026-11-05 (was strict xfail E2E-T1-003-1).
def test_readme_current_authentication_and_execution_claims_match_api_contract():
    spec = json.loads((ROOT / "docs/api/openapi.json").read_text())
    assert "/v1/payment-requests/{request_id}/execute" in spec["paths"]
    readme = (ROOT / "README.md").read_text()
    assert "there is no authentication, authorization, rate limiting" not in readme
    assert "* It never executes payments." not in readme


def test_evidence_redacts_resource_and_provider_identifiers_and_tokens():
    sample = {"api_key": "sensitive-placeholder", "request_id": "pay_" + "a" * 32,
              "order_id": "12345678901234567", "payer_action_url": "https://sandbox.paypal.com/?token=secret"}
    result = redacted(sample)
    assert result == {"api_key": "[omitted]", "request_id": "...aaaa", "order_id": "...4567",
                      "payer_action_url": "[omitted]"}


def test_sdk_float_refusal_happens_before_network():
    async def exercise():
        def forbidden(_):
            pytest.fail("A float amount reached the HTTP transport")
        from agentpal_sdk import AgentClient
        async with AgentClient(base_url="http://127.0.0.1", credential="apk_" + "placeholder",
                               transport=httpx.MockTransport(forbidden)) as agent:
            with pytest.raises(TypeError):
                await agent.create_payment_request(amount=1.1, currency="USD",
                    merchant_name="T1", purpose="float refusal")
    asyncio.run(exercise())


# Fixed by the lead 2026-11-05 (was strict xfail E2E-T1-004-1).
def test_live_refund_of_verified_northwind_capture():
    """Reuse T1's captured $1 order; create NO new order and never retry a refund key.

    Explicit opt-in avoids spending provider credits during ordinary test runs.
    The owning identity is bootstrapped in memory, never stored as plaintext.
    """
    directory = os.environ.get("AGENTPAL_T1_REFUND_DB")
    if not directory or not all(os.environ.get(k) for k in (
            "PAYPAL_SANDBOX_CLIENT_ID", "PAYPAL_SANDBOX_CLIENT_SECRET")):
        pytest.skip("Set AGENTPAL_T1_REFUND_DB to this tester's captured-order DB; sandbox credentials required")
    with server(Path(directory), live=True, resume=True) as (client, owner, _):
        report = {"http": []}
        j = Journey(client, owner, report)
        payments = j.call("GET", "/v1/payment-requests")["payment_requests"]
        p = next(p for p in payments if p["terms"]["merchant_name"] == "Northwind Office Supply")
        assert p["status"] == "provider_confirmed"
        rid = p["request_id"]
        result = j.call("POST", f"/v1/payment-requests/{rid}/refunds",
                        body={"amount": "0.40", "reason": "T1 acceptance regression"}, key=uuid.uuid4().hex)
        # Lead decision 2026-11-05: PayPal only lets this app refund captures paid to its OWN
        # merchant account; a routed merchant (Northwind) must grant refund permission (PayPal
        # partner onboarding). The honest outcome is a definite failure with that reason.
        assert result["status"] == "FAILED"
        assert result["failure_reason"] == "merchant_permission_required"


# Fixed by the lead 2026-11-05 (was strict xfail E2E-T1-005-1).
def test_refresh_does_not_upgrade_uncertain_name_match_to_exact_price_advice():
    """Offline reproduction of the LIVE Bose name search -> owner refresh defect.

    The same catalogue identity and offers are returned by both mocked endpoints;
    refreshing price data supplies no new evidence linking it to the requested item.
    """
    from agentpal.intelligence.market_check import MarketChecker
    from agentpal.tools.channel3 import Channel3Client
    from test_platform_fixtures import ALL_PAY, OWNER_A, build_world

    product = {"id": "t1-bose", "title": "QuietComfort Ultra Headphones",
               "brands": [{"name": "Bose"}], "offers": [{"domain": "bose.com",
                   "url": "https://www.bose.com/p/headphones/quietcomfort-ultra.html",
                   "availability": "InStock", "condition": "new",
                   "price": {"price": "768.00", "currency": "USD"}}]}
    calls = []

    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"products": [product]} if request.url.path.endswith("/search") else product)

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        checker = MarketChecker(Channel3Client("synthetic-placeholder", client=http))
        w = build_world(market_checker=checker)
        _, key, _ = w.funded_agent(caps=ALL_PAY, monthly_budget="1000.00", per_transaction_limit="500.00")
        p = w.pay(key, amount="429.00", currency="USD", merchant_name="Bose",
                  merchant_domain="bose.com", purpose="Office headphones",
                  product_query="Bose QuietComfort Ultra Headphones").json()
        initial = p["market_check"]
        assert initial["product"]["match_confidence"] == "medium"
        assert initial["market"] is None
        r = w.client.post(f"/v1/payment-requests/{p['request_id']}/market-check", headers=w.h(OWNER_A))
        assert r.status_code == 200
        refreshed = r.json()
        assert calls == ["/v1/search", "/v1/products/t1-bose"]
        # This is the actual safety invariant: no new identity proof, no price claims.
        assert refreshed["market"] is None, "Refreshing a medium name match exposed a market price comparison"
        assert refreshed["better_deals"] == []
