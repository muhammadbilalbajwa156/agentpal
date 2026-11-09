"""SQLite isolation across independent service/store instances, not just one RLock."""
from decimal import Decimal

from fastapi.testclient import TestClient
import pytest

from agentpal.api import create_app
from agentpal.lifecycle import check_consistency
from agentpal.platform.wiring import build_platform

from .test_money_integrity import parallel


def test_independent_connections_serialize_shared_payment_and_payout_holds(world):
    world.register("shared", budget="0.03")
    deps = build_platform(settings=world.settings, environ={})
    app = create_app(settings=world.settings, service=deps.risk_service, platform=deps)
    client = TestClient(app, base_url="http://127.0.0.1")
    def reserve(index):
        body = dict(amount="0.01", currency="USD", purpose="Shared connection hold")
        kind = "payment" if index % 2 == 0 else "payout"
        body.update(merchant_name="H2 shared merchant" if kind == "payment" else None)
        if kind == "payout":
            body.pop("merchant_name")
            body["receiver_email"] = "freelancer@example.com"
        selected = client if index % 3 else world.client
        return selected.post(f"/v1/{kind}-requests", json=body, headers={
            "Authorization": "Bearer " + world.tokens["shared"], "Idempotency-Key": f"h2-independent-{index}"})
    try:
        assert deps.wallet_store is not world.deps.wallet_store
        assert deps.risk_service.store is not world.deps.risk_service.store
        rs = parallel([lambda i=i: reserve(i) for i in range(8)])
        assert all(r.status_code in (201, 409) for r in rs)
        assert sum(r.status_code == 201 and r.json()["status"] == "awaiting_approval" for r in rs) == 3
        assert Decimal(world.allowance("shared")["committed"]) == Decimal("0.03")
        assert not check_consistency(deps.risk_service.store, deps.wallet_store)
    finally:
        client.close()
        deps.close()


def test_partial_refund_survives_factory_reopen_without_credit_replay(world):
    _, path = world.captured()
    r = world.call("POST", path + "/refunds", json={"amount": "0.40"},
                   headers={"Idempotency-Key": "h2-reopen-partial-refund"})
    assert r.status_code == 201 and r.json()["status"] == "COMPLETED"
    deps = build_platform(settings=world.settings, environ={})
    try:
        assert not check_consistency(deps.risk_service.store, deps.wallet_store)
        app = create_app(settings=world.settings, service=deps.risk_service, platform=deps)
        with TestClient(app, base_url="http://127.0.0.1") as client:
            result = client.get(path, headers={"Authorization": "Bearer " + world.tokens["owner_a"]})
            assert result.status_code == 200 and result.json()["status"] == "provider_confirmed"
        assert Decimal(world.allowance()["spent"]) == Decimal("0.60")
    finally:
        deps.close()


@pytest.mark.parametrize("field", ["amount", "currency", "payee"])
def test_provider_substitution_after_approved_order_never_captures(world, field):
    _, path = world.approved(merchant_paypal_email="app@business.example.com")
    assert world.call("POST", path + "/execute", "a").status_code == 200
    world.backend.approve()
    unit = next(iter(world.backend.orders.values()))["purchase_units"][0]
    if field == "amount":
        unit["amount"]["value"] = "2.00"
    elif field == "currency":
        unit["amount"]["currency_code"] = "EUR"
    else:
        unit["payee"]["email_address"] = "other@example.com"
    r = world.call("POST", path + "/capture")
    assert r.status_code == 409
    assert not world.backend.posts("/capture")
    assert world.call("GET", path).json()["status"] == "reconciliation_required"
    assert Decimal(world.allowance()["committed"]) == 1


@pytest.mark.parametrize("field,value", [("receiver", "other@example.com"), ("note", "changed"),
    ("recipient_type", "PHONE"), ("sender_item_id", "OTHERITEM"),
    ("currency", "EUR"), ("amount", "2.00")])
def test_payout_provider_observation_must_match_exact_approved_item(world, field, value, monkeypatch):
    original = world.backend.handler
    def changed(req):
        response = original(req)
        if req.method == "GET" and req.url.path.startswith("/v1/payments/payouts/"):
            body = response.json()
            item = body["items"][0]["payout_item"]
            if field in ("currency", "amount"):
                item["amount"]["currency" if field == "currency" else "value"] = value
            else:
                item[field] = value
            import httpx
            return httpx.Response(200, json=body)
        return response
    # Replace only the fake HTTP handler, never the provider's verification code.
    import httpx
    monkeypatch.setattr(world.deps.provider.http_client, "_transport", httpx.MockTransport(changed))
    _, path = world.approved("payout")
    r = world.call("POST", path + "/execute", "a")
    assert r.status_code == 200 and r.json()["status"] == "reconciliation_required"
    assert len(world.backend.posts("/v1/payments/payouts")) == 1
    assert Decimal(world.allowance()["spent"]) == 0
    assert Decimal(world.allowance()["committed"]) == 1
