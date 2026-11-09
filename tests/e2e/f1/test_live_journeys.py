"""Explicit opt-in; ten paid searches, five $1 orders, one $1 payout maximum.

Four purchases plus Newman's unapproved order. Ordinary catalogue/provider calls only.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
from uuid import uuid4

import pytest

from agentpal_sdk import AgentClient, ConflictError, OwnerClient, PermissionDeniedError
from agentpal.demo.sandbox_buyer import approve_sandbox_buyer
from .harness import BASE, CAPS, ROOT, save_evidence, server

REQUIRED = ("PAYPAL_SANDBOX_CLIENT_ID", "PAYPAL_SANDBOX_CLIENT_SECRET", "CHANNEL3_API_KEY",
            "PAYPAL_SANDBOX_BUYER_EMAIL", "PAYPAL_SANDBOX_BUYER_PASSWORD")
pytestmark = pytest.mark.skipif(os.getenv("F1_RUN_LIVE") != "1" or any(not os.getenv(k) for k in REQUIRED),
    reason="F1 live requires explicit F1_RUN_LIVE=1 and sandbox/buyer/Channel3 credentials")


@pytest.fixture(scope="module")
def live():
    with server("/tmp/m8-f1/live-" + uuid4().hex, live=True) as instance:
        yield instance


def test_live_ten_real_products_refresh_and_replay(live):
    cases = [
        ("Logitech MX Master 3S", "250.00", "USD", "officedepot.com"),
        ("Apple AirPods Pro 2", "249.00", "USD", "apple.com"),
        ("Apple AirPods Pro 2 Ear Tips", "9.99", "USD", "apple.com"),
        ("Apple iPhone 16 Pro 128GB", "999.00", "USD", "apple.com"),
        ("Apple iPhone 16 Pro Max 256GB", "1199.00", "USD", "apple.com"),
        ("Bose QuietComfort Ultra Headphones", "429.00", "USD", "bose.com"),
        ("Sony WH-1000XM5", "299.00", "USD", "sony.com"),
        ("Samsung T7 Shield 2TB", "199.00", "USD", "samsung.com"),
        ("Logitech MX Anywhere 3S", "89.99", "EUR", "logitech.com"),
        ("Raspberry Pi Pico 2", "4.80", "GBP", "thepihut.com"),
    ]
    owner = live.owners["f1_owner_a"]
    agents = {currency: live.agent(owner, currency=currency, budget="10000", limit="5000")[1]
              for currency in ("USD", "EUR", "GBP")}
    evidence = []
    try:
        for query, amount, currency, merchant in cases:
            token = agents[currency]
            idem = uuid4().hex
            opts = dict(amount=amount, currency=currency, merchant_domain=merchant,
                        merchant_name=merchant, purpose="F1 office purchase: " + query, product_query=query)
            p = live.payment(token, key=idem, **opts)
            path = "/v1/payment-requests/" + p["request_id"]
            initial = p["market_check"]
            assert initial["provider"] == "channel3"
            assert initial["advisory_only"] and not initial["authoritative"]
            assert initial["advisory_level"] != "unavailable"
            live.call("POST", path + "/market-check", token, expected=(403,))
            refreshed = live.call("POST", path + "/market-check", owner, expected=(200, 409))
            refresh_available = "error" not in refreshed
            after = live.call("GET", path, owner)
            for field in ("terms", "terms_sha256", "status", "risk_score", "recommendation"):
                assert after[field] == p[field]
            assert after["status"] == "awaiting_approval"
            assert live.payment(token, key=idem, **opts)["request_id"] == p["request_id"]
            for check in (initial, refreshed) if refresh_available else (initial,):
                assert check["discount_codes_available"] is False
                assert all(d["currency"] == currency for d in check["better_deals"])
                assert not check.get("product") or check["product"]["match_confidence"] != "exact"
                if check.get("market"):
                    assert check["market"]["currency"] == currency
            evidence.append({"query": query, "currency": currency,
                "level": initial["advisory_level"], "refresh_level": refreshed.get("advisory_level"),
                "refresh_error": refreshed.get("error"),
                "confidence": (initial.get("product") or {}).get("match_confidence"),
                "refreshed_confidence": (refreshed.get("product") or {}).get("match_confidence"),
                "signals": [s["code"] for s in initial["signals"]],
                "provider_evidence": bool(initial.get("evidence")), "terms_and_state_unchanged": True})
            live.call("POST", path + "/reject", owner)
    finally:
        save_evidence("market-evidence.json", evidence)
    assert len(evidence) == 10


def test_live_sdk_only_purchases_refunds_payout_and_reconciliation(live):
    people = json.loads((ROOT / "agentpal/demo/paypal_sandbox_personas.json").read_text())["personas"]
    merchants = [next(p for p in people if p["name"].startswith(name)) for name in ("Northwind", "Bluepeak", "CloudDesk")]
    recipient = next(p for p in people if p["name"].startswith("Priya"))
    evidence = {"purchases": [], "refunds": [], "payout": {}, "sdk_only": True}

    async def journey():
        async with OwnerClient(base_url=BASE, credential=live.owners["f1_owner_a"], timeout=65) as owner:
            reg = await owner.register_agent(name="F1 live SDK", capabilities=CAPS)
            live.tokens.append(reg["api_key"])
            await owner.create_wallet(agent_id=reg["agent"]["agent_id"], currency="USD",
                                      monthly_budget="50.00", per_transaction_limit="10.00")
            async with AgentClient(base_url=BASE, credential=reg["api_key"], timeout=65) as agent:
                async def purchase(name, email=None):
                    options = {"merchant_paypal_email": email} if email else {}
                    p = await agent.create_payment_request(amount="1.00", currency="USD", merchant_name=name,
                        merchant_domain="example.com", purpose="F1 labelled sandbox SDK purchase: " + name, **options)
                    rid = p["request_id"]
                    with pytest.raises(ConflictError):
                        await agent.execute_payment_request(rid)
                    await owner.approve_payment_request(rid, terms_sha256=p["terms_sha256"])
                    assert (await agent.execute_payment_request(rid))["status"] == "provider_pending"
                    refs = (await owner.get_reconciliation(rid))["provider_references"]
                    # Browser supplies buyer consent only; PayPal reads/capture establish payment evidence.
                    result = await asyncio.to_thread(approve_sandbox_buyer, refs[0]["payer_action_url"], timeout_s=90)
                    assert result.status == "approved", result.reason
                    captured = None
                    for _ in range(8):
                        try:
                            captured = await owner.capture_payment_request(rid)
                            break
                        except ConflictError as exc:
                            if exc.code not in {"order_not_approved", "payer_action_required"}:
                                raise
                            await asyncio.sleep(3)
                    assert captured and captured["status"] == "provider_confirmed"
                    assert (await owner.reconcile_payment_request(rid))["status"] == "provider_confirmed"
                    with pytest.raises(ConflictError):
                        await agent.execute_payment_request(rid)
                    with pytest.raises(ConflictError):
                        await owner.capture_payment_request(rid)
                    read = await owner.get_payment_request(rid)
                    if email:
                        assert read["merchant_paypal_email"] == email
                    evidence["purchases"].append({"merchant": name, "order_suffix": refs[0]["provider_order_id"][-4:],
                        "verified_status": captured["status"], "exact_payee_bound": bool(email),
                        "duplicate_execute_and_capture_refused": True})
                    save_evidence("sdk-evidence.json", evidence)
                    return rid

                for persona in merchants:
                    rid = await purchase(persona["name"], persona["email"])
                    if persona is merchants[0]:
                        failed = await owner.refund_payment(rid, amount="0.40", idempotency_key=uuid4().hex)
                        assert failed["status"] == "FAILED"
                        assert failed["failure_reason"] == "merchant_permission_required"
                        evidence["refunds"].append({"kind": "routed merchant", "status": failed["status"],
                                                     "reason": failed["failure_reason"]})

                rid = await purchase("F1 app-linked sandbox shop")
                first_key = uuid4().hex
                partial = await owner.refund_payment(rid, amount="0.40", idempotency_key=first_key)
                assert partial["status"] == "COMPLETED"
                spent = (await agent.get_wallet())["wallets"][0]["allowance"]["spent"]
                assert Decimal(spent) == Decimal("3.60")
                assert (await owner.refund_payment(rid, amount="0.40", idempotency_key=first_key))["refund_id"] == partial["refund_id"]
                assert (await agent.get_wallet())["wallets"][0]["allowance"]["spent"] == spent
                live.restart()
                assert (await owner.reconcile_payment_request(rid))["status"] == "provider_confirmed"
                await owner.reconcile_payment_refunds(rid)
                now = datetime.now(timezone.utc)
                report = await owner.reconcile_with_paypal(start=(now - timedelta(hours=1)).isoformat(), end=now.isoformat())
                rows = report["rows"]
                row = next(r for r in rows if r["request_id"] == rid)
                assert row["reason"] == "refund_extension_required" and row["result"] == "pending"
                remainder = await owner.refund_payment(rid, idempotency_key=uuid4().hex)
                assert remainder["status"] == "COMPLETED" and Decimal(remainder["amount"]) == Decimal("0.60")
                assert (await owner.get_payment_request(rid))["status"] == "reversed"
                with pytest.raises(ConflictError):
                    await owner.refund_payment(rid, amount="0.01", idempotency_key=uuid4().hex)
                assert Decimal((await owner.list_payment_refunds(rid))["refunded_total"]) == 1
                evidence["refunds"].append({"kind": "app-linked", "partial": "0.40", "remainder": "0.60",
                    "partial_suffix": partial["provider_refund_id"][-4:], "full_suffix": remainder["provider_refund_id"][-4:],
                    "partial_restart_and_reconciliation": True, "state": "reversed", "overrefund_refused": True})

                payout = await agent.create_payout_request(receiver_email=recipient["email"], amount="1.00",
                    currency="USD", purpose="F1 approved freelancer payout", note="F1 sandbox")
                pid = payout["request_id"]
                with pytest.raises(ConflictError):
                    await agent.execute_payout_request(pid)
                await owner.approve_payout_request(pid, terms_sha256=payout["terms_sha256"])
                payout = await agent.execute_payout_request(pid)
                for _ in range(20):
                    if payout["status"] in {"provider_confirmed", "failed"}:
                        break
                    await asyncio.sleep(3)
                    payout = await owner.reconcile_payout_request(pid)
                assert payout["status"] == "provider_confirmed" and payout["provider_item_status"] == "SUCCESS"
                # Payout execution is a local idempotent replay, never another provider POST.
                replay = await agent.execute_payout_request(pid)
                assert replay["provider_batch_id"] == payout["provider_batch_id"]
                evidence["payout"] = {"status": payout["status"], "item_status": payout["provider_item_status"],
                    "batch_suffix": payout["provider_batch_id"][-4:], "replay_same_batch": True}
                evidence["final_spent"] = (await agent.get_wallet())["wallets"][0]["allowance"]["spent"]
                assert Decimal(evidence["final_spent"]) == 4
                await owner.list_disputes()
                await agent.list_transactions()
                live.restart()
                assert Decimal((await agent.get_wallet())["wallets"][0]["allowance"]["spent"]) == 4
                evidence["final_restart"] = True
                # Plain HTTP refusal verifies agents cannot view owner reconciliation.
                live.call("GET", "/v1/paypal/reconciliation", reg["api_key"], expected=(403,))
                with pytest.raises(PermissionDeniedError):
                    await owner._request("POST", f"payment-requests/{rid}/execute")
    try:
        asyncio.run(journey())
    finally:
        save_evidence("sdk-evidence.json", evidence)


def test_postman_newman_security_collection(live):
    owner = live.owners["f1_owner_a"]
    variables = {"base_url": BASE, "owner_key": owner}
    for letter in ("a", "b"):
        aid, token, _ = live.agent(owner)
        variables.update({f"agent_{letter}_id": aid, f"agent_{letter}_key": token})
    collection = json.loads((ROOT / "postman/v1.collection.json").read_text())
    collection["item"] = [x for x in collection["item"] if x["name"] == "V1 security"]
    assert len(collection["item"]) == 1
    for item in collection["variable"]:
        if item["key"] in variables:
            item["value"] = variables[item["key"]]
    module = Path("/tmp/m8-f1/newman/node_modules/newman")
    if not module.is_dir():
        pytest.skip("BLOCKED: install pinned Newman under /tmp/m8-f1/newman")
    program = r"""
const newman = require('/tmp/m8-f1/newman/node_modules/newman');
let input=''; process.stdin.on('data', s => input += s);
process.stdin.on('end', () => newman.run({collection:JSON.parse(input),reporters:[],
 timeoutRequest:15000,timeoutScript:5000},(err,s)=>process.stdout.write(JSON.stringify({
 fatal:!!err,requests:s&&s.run.stats.requests,assertions:s&&s.run.stats.assertions,
 failures:s&&s.run.failures.map(f=>({name:f.error.test||f.error.name,source:f.source&&f.source.name}))}))));
"""
    result = subprocess.run(["node", "-e", program], input=json.dumps(collection), text=True,
                            capture_output=True, timeout=120)
    assert result.returncode == 0, "Newman process failed; raw output suppressed"
    stats = json.loads(result.stdout)
    save_evidence("newman-evidence.json", stats)
    assert not stats["fatal"] and not stats["failures"]
