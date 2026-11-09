"""Each capability is enforced server-side by the registry-backed PermissionChecker: denied
by default, allowed only when the owner granted it, every decision audited."""

from __future__ import annotations

import pytest

from test_platform_fixtures import ALL_PAY, OWNER_A, build_world


@pytest.fixture
def w():
    return build_world()


def _audit(w, agent_id):
    return [(r["capability"], bool(r["allowed"]), r["code"], r["action"])
            for r in w.wallet_store.permission_audit(agent_id)]


def test_wallet_read(w):
    agent_id, key, _ = w.funded_agent(caps=["payment.prepare"])
    r = w.client.get("/v1/me/wallet", headers=w.h(key))
    assert r.status_code == 403 and r.json() == {"error": "permission_denied"}
    assert w.client.get("/v1/me/wallet/transactions", headers=w.h(key)).status_code == 403
    w.grant(agent_id, ["payment.prepare", "wallet.read"])
    assert w.client.get("/v1/me/wallet", headers=w.h(key)).status_code == 200
    audit = _audit(w, agent_id)
    assert ("wallet.read", False, "NOT_GRANTED", "v1.me.wallet") in audit
    assert ("wallet.read", True, "ALLOWED", "v1.me.wallet") in audit


def test_payment_prepare(w):
    agent_id, key, _ = w.funded_agent(caps=["wallet.read", "owner.request_decision"])
    r = w.pay(key)
    assert r.status_code == 403 and r.json() == {"error": "permission_denied"}
    assert w.wallet_store.query("SELECT * FROM platform_idempotency") == []
    assert w.wallet_store.query("SELECT * FROM wallet_payment_requests") == []
    w.grant(agent_id, ["wallet.read", "owner.request_decision", "payment.prepare"])
    assert w.pay(key).status_code == 201
    assert ("payment.prepare", False, "NOT_GRANTED", "v1.payment.create") in _audit(
        w, agent_id)


def test_owner_request_decision(w):
    agent_id, key, _ = w.funded_agent(caps=["wallet.read", "payment.prepare"])
    assert w.pay(key).status_code == 403
    r = w.pay(key, request_owner_decision=False)
    assert r.status_code == 201
    p = r.json()
    assert p["status"] == "prepared" and p["decision_requested"] is False
    assert w.client.get("/v1/approvals", headers=w.h(OWNER_A)).json()[
        "payment_requests"] == []
    # the owner cannot approve something the agent never asked them to decide
    r = w.client.post(f"/v1/payment-requests/{p['request_id']}/approve",
                      json={"terms_sha256": p["terms_sha256"]}, headers=w.h(OWNER_A))
    assert r.status_code == 409 and r.json() == {"error": "decision_not_requested"}
    r = w.client.post(f"/v1/payment-requests/{p['request_id']}/request-decision",
                      headers=w.h(key))
    assert r.status_code == 403
    w.grant(agent_id, ["wallet.read", "payment.prepare", "owner.request_decision"])
    r = w.client.post(f"/v1/payment-requests/{p['request_id']}/request-decision",
                      headers=w.h(key))
    assert r.status_code == 200 and r.json()["status"] == "awaiting_approval"
    queue = w.client.get("/v1/approvals", headers=w.h(OWNER_A)).json()["payment_requests"]
    assert [q["request_id"] for q in queue] == [p["request_id"]]


def test_payment_execute_sandbox_is_opt_in(w):
    agent_id, key, _ = w.funded_agent()           # external defaults: no execution
    p = w.pay(key).json()
    w.client.post(f"/v1/payment-requests/{p['request_id']}/approve",
                  json={"terms_sha256": p["terms_sha256"]}, headers=w.h(OWNER_A))
    r = w.client.post(f"/v1/payment-requests/{p['request_id']}/execute", headers=w.h(key))
    assert r.status_code == 403 and r.json() == {"error": "permission_denied"}
    assert w.lifecycle.begun == []
    w.grant(agent_id, ALL_PAY)
    r = w.client.post(f"/v1/payment-requests/{p['request_id']}/execute", headers=w.h(key))
    assert r.status_code == 200 and r.json()["status"] == "submitted_to_provider"


def test_research_is_grantable_to_external_agents_but_purchase_propose_is_not(w):
    agent_id, _ = w.register()
    w.grant(agent_id, ["research.search", "research.fetch"])
    r = w.client.put(f"/v1/agents/{agent_id}/permissions",
                     json={"capabilities": ["purchase.propose"]}, headers=w.h(OWNER_A))
    assert r.status_code == 400
    caps = w.client.get(f"/v1/agents/{agent_id}/permissions", headers=w.h(OWNER_A)).json()
    assert caps["capabilities"] == ["research.fetch", "research.search"]


def test_revocation_takes_effect_immediately_for_an_issued_key(w):
    agent_id, key, _ = w.funded_agent()
    assert w.client.get("/v1/me/wallet", headers=w.h(key)).status_code == 200
    w.grant(agent_id, [])
    assert w.client.get("/v1/me/wallet", headers=w.h(key)).status_code == 403
    assert w.client.get("/v1/me", headers=w.h(key)).json()["capabilities"] == []


def test_suspended_agent_is_denied_everywhere(w):
    agent_id, key, _ = w.funded_agent(caps=ALL_PAY)
    w.client.post(f"/v1/agents/{agent_id}/suspend", headers=w.h(OWNER_A))
    assert w.pay(key).status_code == 401
    ctx = w.platform.registry.context_for(agent_id)
    from agentpal.contracts import Capability
    assert w.platform.checker.check(ctx, Capability.payment_prepare).code == \
        "AGENT_SUSPENDED"
