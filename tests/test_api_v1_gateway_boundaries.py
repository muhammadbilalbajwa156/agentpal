"""Protocol-only decisions/execution and sandbox configuration boundaries."""

from agentpal.infra_contracts import AuthenticatedOwner, PaymentLifecycle

from test_platform_fixtures import ALL_PAY, OWNER_A, build_world, sign


def test_real_owner_identity_reaches_approval_gateway_unchanged():
    w = build_world()
    _, key, _ = w.funded_agent()
    p = w.pay(key).json()
    seen = []
    original = w.approvals.approve

    def approve(owner, request_id, *, terms_sha256):
        assert isinstance(owner, AuthenticatedOwner)
        seen.append((owner.session_id, owner.auth_method, terms_sha256))
        return original(owner, request_id, terms_sha256=terms_sha256)

    w.approvals.approve = approve
    r = w.client.post(f"/v1/payment-requests/{p['request_id']}/approve",
                      json={"terms_sha256": p["terms_sha256"]}, headers=w.h(OWNER_A))
    assert r.status_code == 200
    assert seen == [("sess_owner_alicex", "owner_api_token", p["terms_sha256"])]


def test_gateway_status_and_submission_remain_authoritative():
    w = build_world()
    agent_id, key, _ = w.funded_agent(caps=ALL_PAY)
    p = w.pay(key).json()
    calls = []
    ticket = object()

    def begin(aid, rid, terms, provider):
        calls.append((aid, rid, terms.digest(), provider))
        return ticket

    def submit(got, provider):
        assert got is ticket and provider is w.provider
        return PaymentLifecycle.provider_pending

    w.lifecycle.begin_execution = begin
    w.lifecycle.submit_to_provider = submit
    r = w.client.post(f"/v1/payment-requests/{p['request_id']}/execute", headers=w.h(key))
    assert r.json()["status"] == "provider_pending"
    assert calls == [(agent_id, p["request_id"], p["terms_sha256"], w.provider)]
    assert w.provider.orders == []
    w.lifecycle.status = lambda *args, **kwargs: PaymentLifecycle.reversed
    assert w.client.get(f"/v1/payment-requests/{p['request_id']}", headers=w.h(key)).json()[
        "status"] == "reversed"
    assert w.client.get(f"/v1/payment-requests/{p['request_id']}/reconciliation",
                        headers=w.h(OWNER_A)).json()["status"] == "reversed"
    tables = {r["name"] for r in w.wallet_store.query(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "platform_provider_orders" not in tables


def test_webhook_refuses_non_sandbox_provider_before_verification():
    w = build_world()
    w.provider.environment = "production"
    raw = b'{"id":"event-1","order":"order-1"}'
    r = w.client.post("/v1/providers/paypal/webhooks", content=raw,
                      headers={"x-fake-signature": sign(raw)})
    assert r.status_code == 503
    assert w.sink.events == []


def test_status_is_not_reinterpreted_when_decision_has_not_been_requested():
    w = build_world()
    _, key, _ = w.funded_agent()
    w.lifecycle.status = lambda *args, **kwargs: PaymentLifecycle.awaiting_approval
    p = w.pay(key, request_owner_decision=False).json()
    assert p["status"] == "awaiting_approval"
    assert p["decision_requested"] is False
    requested = w.client.post(f"/v1/payment-requests/{p['request_id']}/request-decision",
                              headers=w.h(key))
    assert requested.status_code == 200
    assert requested.json()["decision_requested"] is True
