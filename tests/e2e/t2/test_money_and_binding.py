"""Hostile amounts, approval binding, request replay and real TCP concurrency."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from uuid import uuid4

import pytest


@pytest.mark.parametrize("amount", ["0", "-1", "1e3", "1.001", "99999999999999999999",
                                   "١.٠٠", "１.００", 1.0, 1, True, "NaN", "Infinity",
                                   "+1.00", " 1.00", "1.00\n"])
def test_invalid_money_never_reserves(world, amount):
    response = world.payment(amount=amount)
    assert response.status_code == 422, (response.status_code, response.json().get("error"))


@pytest.mark.parametrize("currency", ["usd", "USD ", "USＤ", "US\x00", "XXX", "BTC"])
def test_currency_tricks_fail_closed(world, currency):
    response = world.payment(currency=currency)
    assert response.status_code in (400, 409, 422)


def test_approval_digest_and_immutable_terms(world):
    original = world.payment().json()
    path = "/v1/payment-requests/" + original["request_id"]
    bad = world.call("POST", path + "/approve", "owner_a", json={"terms_sha256": "0" * 64})
    assert (bad.status_code, bad.json()["error"]) == (409, "terms_mismatch")
    for field, value in [("amount", "0.01"), ("currency", "GBP"),
                         ("merchant_name", "Attacker"), ("merchant_paypal_email", "evil@example.com"),
                         ("purpose", "Override owner approval"), ("receiver_email", "evil@example.com")]:
        response = world.call("POST", path + "/approve", "owner_a",
                              json={"terms_sha256": original["terms_sha256"], field: value})
        assert response.status_code == 422, field
    approved = world.call("POST", path + "/approve", "owner_a",
                          json={"terms_sha256": original["terms_sha256"]})
    assert approved.status_code == 200
    assert world.call("PATCH", path, "a", json={"amount": "0.01"}).status_code == 405
    # Unspecified execute body is not authority: the persisted terms must win.
    executed = world.call("POST", path + "/execute", "a", json={"amount": "0.01",
                          "merchant_paypal_email": "evil@example.com", "terms_sha256": "0" * 64})
    assert executed.status_code == 200
    stored = world.call("GET", path, "owner_a").json()
    assert stored["terms"] == original["terms"]
    assert stored["terms_sha256"] == original["terms_sha256"]
    other = world.payment().json()
    response = world.call("POST", "/v1/payment-requests/" + other["request_id"] + "/approve",
                          "owner_a", json={"terms_sha256": original["terms_sha256"]})
    assert response.status_code == 409


def test_idempotency_body_binding_and_agent_scope(world):
    body = dict(amount="0.01", currency="USD", merchant_name="Northwind", purpose="T2 replay")
    headers = {"Idempotency-Key": "t2-" + uuid4().hex}
    first = world.call("POST", "/v1/payment-requests", "a", json=body, headers=headers)
    replay = world.call("POST", "/v1/payment-requests", "a", json=body, headers=headers)
    assert (first.status_code, replay.status_code) == (201, 200)
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert first.json() == replay.json()
    for field, value in [("amount", "0.02"), ("currency", "GBP"), ("merchant_name", "Attacker"),
                         ("merchant_paypal_email", "evil@example.com"), ("purpose", "changed")]:
        response = world.call("POST", "/v1/payment-requests", "a", json={**body, field: value},
                              headers=headers)
        assert (response.status_code, response.json()["error"]) == (409, "idempotency_key_reused")
    second_agent = world.call("POST", "/v1/payment-requests", "b", json=body, headers=headers)
    assert second_agent.status_code == 201
    assert second_agent.json()["request_id"] != first.json()["request_id"]


def test_parallel_reservations_cannot_exceed_wallet_budget(world):
    world.register("race", "owner_a", budget="5.00")
    def reserve(_):
        return world.payment("race", amount="1.00")
    with ThreadPoolExecutor(max_workers=12) as pool:
        responses = list(pool.map(reserve, range(12)))
    statuses = [r.status_code for r in responses]
    # Creation may persist a blocked assessment as HTTP 201. HTTP success does
    # not mean policy acceptance: count lifecycle states and ledger holds.
    accepted = [r for r in responses if r.status_code == 201 and r.json()["status"] == "awaiting_approval"]
    assert len(accepted) == 5, [(r.status_code, r.json().get("status")) for r in responses]
    assert all(s in (201, 409) for s in statuses), statuses
    allowance = world.call("GET", "/v1/me/wallet", "race").json()["wallets"][0]["allowance"]
    assert Decimal(allowance["committed"]) == Decimal("5.00")
    assert Decimal(allowance["available"]) == 0


def test_parallel_approval_execute_and_single_use(world):
    world.register("approvalrace", "owner_a")
    payment = world.payment("approvalrace").json()
    path = "/v1/payment-requests/" + payment["request_id"]
    def approve(_):
        return world.call("POST", path + "/approve", "owner_a",
                          json={"terms_sha256": payment["terms_sha256"]})
    with ThreadPoolExecutor(max_workers=4) as pool:
        approvals = list(pool.map(approve, range(4)))
    assert all(r.status_code in (200, 409) for r in approvals)
    def execute(_):
        return world.call("POST", path + "/execute", "approvalrace")
    with ThreadPoolExecutor(max_workers=8) as pool:
        executions = list(pool.map(execute, range(8)))
    assert all(r.status_code in (200, 409) for r in executions)
    transactions = world.call("GET", "/v1/me/wallet/transactions", "approvalrace").json()
    transaction = next(t for t in transactions["transactions"] if t["request_id"] == payment["request_id"])
    assert sum(e["to_state"] == "pending" for e in transaction["events"]) == 1
    assert world.call("POST", path + "/approve", "owner_a",
                      json={"terms_sha256": payment["terms_sha256"]}).status_code == 409


def test_self_payout_and_receiver_binding(world):
    world.register("selfpayout", "owner_a", metadata={"email": "self@example.com"})
    body = dict(receiver_email="self@example.com", amount="1.00", currency="USD", purpose="T2 self")
    response = world.call("POST", "/v1/payout-requests", "selfpayout", json=body,
                          headers={"Idempotency-Key": "t2-" + uuid4().hex})
    assert response.status_code in (409, 422)
    body["receiver_email"] = "qa@example.com"
    headers = {"Idempotency-Key": "t2-" + uuid4().hex}
    created = world.call("POST", "/v1/payout-requests", "selfpayout", json=body, headers=headers)
    assert created.status_code == 201
    changed = world.call("POST", "/v1/payout-requests", "selfpayout",
                         json={**body, "receiver_email": "evil@example.com"}, headers=headers)
    assert changed.status_code == 409
    path = "/v1/payout-requests/" + created.json()["request_id"]
    for suffix in ("", "/approve", "/reject", "/reconciliation"):
        method = "GET" if not suffix else "POST"
        first = world.call(method, path + suffix, "owner_b",
                           json={"terms_sha256": created.json()["terms_sha256"]})
        second = world.call(method, "/v1/payout-requests/pay_" + "0" * 32 + suffix,
                            "owner_b", json={"terms_sha256": created.json()["terms_sha256"]})
        assert (first.status_code, first.content) == (404, second.content)


def test_sdk_real_http_and_float_refusal(world):
    from agentpal_sdk import AgentClient, OwnerClient
    async def scenario():
        async with AgentClient(base_url=str(world.client.base_url), credential=world.tokens["b"]) as agent:
            with pytest.raises(TypeError):
                await agent.create_payment_request(amount=1.0, currency="USD", merchant_name="T2",
                                                   purpose="Float must not reach server")
            payment = await agent.create_payment_request(amount=Decimal("0.01"), currency="USD",
                                                         merchant_name="T2", purpose="SDK external call")
        async with OwnerClient(base_url=str(world.client.base_url), credential=world.tokens["owner_b"]) as owner:
            result = await owner.approve_payment_request(payment["request_id"],
                                                         terms_sha256=payment["terms_sha256"])
            assert result["status"] == "approved"
    asyncio.run(scenario())
