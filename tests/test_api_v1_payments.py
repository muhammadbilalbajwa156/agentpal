"""Payment requests through /v1: idempotency, validation, risk, owner approval bound to the
exact terms, sandbox execution through the lifecycle gateway, reconciliation."""

from __future__ import annotations

import pytest

from test_platform_fixtures import ALL_PAY, OWNER_A, build_world


@pytest.fixture
def w():
    return build_world()


def _approve(w, p, owner=OWNER_A, digest=None):
    return w.client.post(f"/v1/payment-requests/{p['request_id']}/approve",
                         json={"terms_sha256": digest or p["terms_sha256"]},
                         headers=w.h(owner))


def _execute(w, p, key):
    return w.client.post(f"/v1/payment-requests/{p['request_id']}/execute", headers=w.h(key))


def test_end_to_end_sandbox_flow(w):
    agent_id, key, _ = w.funded_agent(caps=ALL_PAY)
    r = w.pay(key)
    assert r.status_code == 201
    p = r.json()
    assert p["request_id"].startswith("pay_") and p["agent_id"] == agent_id
    assert p["terms"]["amount"] == "49.99" and p["requires_owner_approval"] is True
    assert p["status"] == "awaiting_approval" and p["is_real_money"] is False
    queue = w.client.get("/v1/approvals", headers=w.h(OWNER_A)).json()["payment_requests"]
    assert [q["terms_sha256"] for q in queue] == [p["terms_sha256"]]
    r = _approve(w, p)
    assert r.status_code == 200 and r.json() == {"request_id": p["request_id"],
                                                 "decision": "approved",
                                                 "status": "approved"}
    assert w.approvals.calls[0][0:2] == ("approve", "owner_alice")
    r = _execute(w, p, key)
    assert r.json() == {"request_id": p["request_id"], "status": "submitted_to_provider",
                        "payment_environment": "sandbox", "is_real_money": False}
    [(order_id, terms, _)] = w.provider.orders
    assert terms.request_id == p["request_id"] and str(terms.amount) == "49.99"
    rec = w.client.get(f"/v1/payment-requests/{p['request_id']}/reconciliation",
                       headers=w.h(OWNER_A)).json()
    assert rec["reconciliation_required"] is False
    assert rec["provider_references"][0]["provider_order_id"] == order_id
    assert w.client.get(f"/v1/payment-requests/{p['request_id']}",
                        headers=w.h(key)).json()["status"] == "submitted_to_provider"


def test_approval_must_carry_the_exact_terms_digest(w):
    _, key, _ = w.funded_agent()
    p = w.pay(key).json()
    r = _approve(w, p, digest="0" * 64)
    assert r.status_code == 409 and r.json() == {"error": "terms_mismatch"}
    assert w.approvals.calls == []
    assert w.risk.store.ledger_state(p["request_id"]).value == "reserved"
    assert _approve(w, p, digest="XYZ").status_code == 422


def test_approval_is_single_use(w):
    _, key, _ = w.funded_agent()
    p = w.pay(key).json()
    assert _approve(w, p).status_code == 200
    r = _approve(w, p)
    assert r.status_code == 409 and r.json() == {"error": "approval_refused"}


def test_execute_requires_approval_and_happens_once(w):
    _, key, _ = w.funded_agent(caps=ALL_PAY)
    p = w.pay(key).json()
    r = _execute(w, p, key)
    assert r.status_code == 409 and r.json() == {"error": "execution_refused"}
    _approve(w, p)
    assert _execute(w, p, key).status_code == 200
    assert _execute(w, p, key).status_code == 409
    assert len(w.provider.orders) == 1


def test_expired_approval_cannot_be_executed(w):
    _, key, _ = w.funded_agent(caps=ALL_PAY)
    p = w.pay(key).json()
    _approve(w, p)
    w.clock.advance(minutes=16)
    w.risk.store.expire_holds()
    r = _execute(w, p, key)
    assert r.status_code == 409
    assert w.provider.orders == []
    assert w.client.get(f"/v1/payment-requests/{p['request_id']}",
                        headers=w.h(key)).json()["status"] == "expired"


def test_reject_releases_the_hold(w):
    _, key, wal = w.funded_agent()
    p = w.pay(key).json()
    r = w.client.post(f"/v1/payment-requests/{p['request_id']}/reject", headers=w.h(OWNER_A))
    assert r.json()["decision"] == "rejected" and r.json()["status"] == "rejected"
    a = w.client.get(f"/v1/wallets/{wal['wallet_id']}", headers=w.h(OWNER_A)).json()
    assert a["allowance"]["available"] == "100.00"
    assert _approve(w, p).status_code == 409


def test_mfa_policy():
    w = build_world(require_mfa=True)
    _, key, _ = w.funded_agent()
    p = w.pay(key).json()
    r = _approve(w, p)
    assert r.status_code == 403 and r.json() == {"error": "mfa_required"}
    w.owner_auth.mfa = True
    assert _approve(w, p).status_code == 200


def test_idempotent_replay_returns_the_same_request(w):
    _, key, _ = w.funded_agent()
    first = w.pay(key, idem="order-0001")
    again = w.pay(key, idem="order-0001")
    assert first.status_code == 201 and again.status_code == 200
    assert again.headers["Idempotent-Replayed"] == "true"
    assert again.json()["request_id"] == first.json()["request_id"]
    assert len(w.wallet_store.query("SELECT * FROM wallet_payment_requests")) == 1


def test_idempotency_key_with_a_different_body_is_refused(w):
    _, key, _ = w.funded_agent()
    w.pay(key, idem="order-0002")
    r = w.pay(key, idem="order-0002", amount="10.00")
    assert r.status_code == 409 and r.json() == {"error": "idempotency_key_reused"}
    assert len(w.wallet_store.query("SELECT * FROM wallet_payment_requests")) == 1


def test_idempotency_keys_are_per_agent(w):
    _, k1, _ = w.funded_agent()
    _, k2, _ = w.funded_agent()
    a = w.pay(k1, idem="shared-key-1").json()
    b = w.pay(k2, idem="shared-key-1").json()
    assert a["request_id"] != b["request_id"]


def test_idempotency_header_rules(w):
    _, key, _ = w.funded_agent()
    payload = {"amount": "1.00", "currency": "USD", "merchant_name": "m", "purpose": "p"}
    r = w.client.post("/v1/payment-requests", json=payload, headers=w.h(key))
    assert r.status_code == 400 and r.json() == {"error": "idempotency_key_required"}
    r = w.client.post("/v1/payment-requests", json=payload,
                      headers=w.h(key, **{"Idempotency-Key": "short"}))
    assert r.status_code == 422


def test_in_progress_idempotency_record(w):
    agent_id, key, _ = w.funded_agent()
    now = w.clock()
    from agentpal.platform.models import PaymentCreate
    from agentpal.platform.payments import body_digest
    body = PaymentCreate(amount="49.99", currency="USD", merchant_name="Example Hosting",
                         merchant_domain="example.com", purpose="Monthly hosting plan")
    with w.wallet_store.tx() as c:
        c.execute("INSERT INTO platform_idempotency (agent_id, idem_key, body_sha256, status, "
                  "request_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                  (agent_id, "crashed-key", body_digest(body), "in_progress",
                   "pay_crashed00000000000000000000000",
                   now.isoformat(), now.isoformat()))
    r = w.pay(key, idem="crashed-key")
    assert r.status_code == 409 and r.json() == {"error": "idempotency_in_progress"}
    w.clock.advance(seconds=61)       # beyond the bounded in-progress timeout
    r = w.pay(key, idem="crashed-key")
    assert r.status_code == 201
    assert r.json()["request_id"] != "pay_crashed00000000000000000000000"
    assert w.pay(key, idem="crashed-key").status_code == 200


def test_a_failed_prepare_frees_the_idempotency_key(w):
    _, key, _ = w.funded_agent()
    r = w.pay(key, idem="bad-domain-1", merchant_domain="user@evil.example")
    assert r.status_code == 422
    assert w.wallet_store.query("SELECT * FROM platform_idempotency") == []


@pytest.mark.parametrize("change", [{"amount": 49.99}, {"amount": "49.999"},
                                    {"amount": "-1"}, {"status": "approved"},
                                    {"approved": True}, {"request_id": "pay_mine"},
                                    {"agent_id": "agt_" + "0" * 32},
                                    {"monthly_budget": "999999"}, {"currency": "usd"}])
def test_invalid_or_smuggled_fields_create_nothing(w, change):
    _, key, _ = w.funded_agent()
    r = w.pay(key, **change)
    assert r.status_code in (409, 422), r.text
    assert w.wallet_store.query("SELECT * FROM wallet_payment_requests") == []


def test_risk_block_is_rejected_and_not_queued(w):
    _, key, _ = w.funded_agent()
    p = w.pay(key, amount="99.00").json()      # above the per-transaction limit (60)
    assert p["status"] == "rejected" and p["recommendation"] == "block"
    assert w.client.get("/v1/approvals", headers=w.h(OWNER_A)).json()[
        "payment_requests"] == []


def test_provider_outcome_unknown_goes_to_reconciliation(w):
    _, key, _ = w.funded_agent(caps=ALL_PAY)
    p = w.pay(key).json()
    _approve(w, p)
    w.provider.fail_unknown = True
    r = _execute(w, p, key)
    assert r.status_code == 200 and r.json()["status"] == "reconciliation_required"
    rec = w.client.get(f"/v1/payment-requests/{p['request_id']}/reconciliation",
                       headers=w.h(OWNER_A)).json()
    assert rec["reconciliation_required"] is True and rec["provider_references"] == []


def test_unconfigured_dependencies_answer_503():
    w = build_world(configured=False)
    _, key, _ = w.funded_agent(caps=ALL_PAY)
    p = w.pay(key).json()
    assert p["status"] is None
    for method, path, token, body in [
            ("get", "/v1/approvals", OWNER_A, None),
            ("post", f"/v1/payment-requests/{p['request_id']}/approve", OWNER_A,
             {"terms_sha256": p["terms_sha256"]}),
            ("post", f"/v1/payment-requests/{p['request_id']}/reject", OWNER_A, None),
            ("post", f"/v1/payment-requests/{p['request_id']}/execute", key, None),
            ("get", f"/v1/payment-requests/{p['request_id']}/reconciliation", OWNER_A, None),
            ("post", "/v1/providers/paypal/webhooks", None, None)]:
        headers = w.h(token) if token else {}
        r = w.client.request(method, path, json=body, headers=headers)
        assert r.status_code == 503 and r.json() == {"error": "not_configured"}, path
    assert w.risk.store.ledger_state(p["request_id"]).value == "reserved"


def test_owner_reads_and_lists(w):
    _, key, _ = w.funded_agent()
    p = w.pay(key).json()
    got = w.client.get(f"/v1/payment-requests/{p['request_id']}", headers=w.h(OWNER_A))
    assert got.json()["terms_sha256"] == p["terms_sha256"]
    listed = w.client.get("/v1/payment-requests", headers=w.h(OWNER_A)).json()
    assert [x["request_id"] for x in listed["payment_requests"]] == [p["request_id"]]
