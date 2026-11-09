"""Isolation: an agent never reaches another agent's or another owner's resources, an
owner never reaches another owner's, and an agent can never grant itself anything."""

from __future__ import annotations

import pytest

from test_platform_fixtures import ALL_PAY, OWNER_A, OWNER_B, build_world


@pytest.fixture
def two():
    w = build_world()
    a_id, a_key, a_wal = w.funded_agent(OWNER_A, caps=ALL_PAY)
    a2_id, a2_key, _ = w.funded_agent(OWNER_A, caps=ALL_PAY)       # same owner, other agent
    b_id, b_key, b_wal = w.funded_agent(OWNER_B, caps=ALL_PAY)
    pa = w.pay(a_key).json()
    pb = w.pay(b_key).json()
    return w, dict(a=(a_id, a_key, a_wal), a2=(a2_id, a2_key), b=(b_id, b_key, b_wal),
                   pa=pa, pb=pb)


def test_agent_cannot_read_other_agents_payments(two):
    w, t = two
    _, a2_key = t["a2"]
    _, b_key, _ = t["b"]
    for key in (a2_key, b_key):
        r = w.client.get(f"/v1/payment-requests/{t['pa']['request_id']}", headers=w.h(key))
        assert r.status_code == 404 and r.json() == {"error": "not_found"}
    unknown = w.client.get("/v1/payment-requests/pay_doesnotexist", headers=w.h(b_key))
    assert unknown.status_code == 404 and unknown.json() == {"error": "not_found"}
    listed = w.client.get("/v1/payment-requests", headers=w.h(b_key)).json()
    assert [p["request_id"] for p in listed["payment_requests"]] == [t["pb"]["request_id"]]
    codes = {r["code"] for r in w.wallet_store.permission_audit(t["a2"][0])}
    assert "OTHER_AGENT" in codes


def test_agent_cannot_execute_or_request_decision_for_another_agent(two):
    w, t = two
    pa = t["pa"]
    w.client.post(f"/v1/payment-requests/{pa['request_id']}/approve",
                  json={"terms_sha256": pa["terms_sha256"]}, headers=w.h(OWNER_A))
    for key in (t["a2"][1], t["b"][1]):
        r = w.client.post(f"/v1/payment-requests/{pa['request_id']}/execute",
                          headers=w.h(key))
        assert r.status_code == 404
        r = w.client.post(f"/v1/payment-requests/{pa['request_id']}/request-decision",
                          headers=w.h(key))
        assert r.status_code == 404
    assert w.lifecycle.begun == []
    assert w.risk.store.ledger_state(pa["request_id"]).value == "approved"


def test_owner_cannot_reach_another_owners_resources(two):
    w, t = two
    a_id, _, a_wal = t["a"]
    pa = t["pa"]
    probes = [("get", f"/v1/agents/{a_id}", None),
              ("put", f"/v1/agents/{a_id}/permissions", {"capabilities": []}),
              ("post", f"/v1/agents/{a_id}/credentials", {}),
              ("post", f"/v1/agents/{a_id}/suspend", None),
              ("get", f"/v1/wallets/{a_wal['wallet_id']}", None),
              ("get", f"/v1/payment-requests/{pa['request_id']}", None),
              ("post", f"/v1/payment-requests/{pa['request_id']}/approve",
               {"terms_sha256": pa["terms_sha256"]}),
              ("post", f"/v1/payment-requests/{pa['request_id']}/reject", None),
              ("get", f"/v1/payment-requests/{pa['request_id']}/reconciliation", None)]
    for method, path, body in probes:
        r = w.client.request(method, path, json=body, headers=w.h(OWNER_B))
        assert r.status_code == 404 and r.json() == {"error": "not_found"}, path
    assert w.approvals.calls == []
    assert w.risk.store.ledger_state(pa["request_id"]).value == "reserved"
    approvals = w.client.get("/v1/approvals", headers=w.h(OWNER_B)).json()
    assert [p["request_id"] for p in approvals["payment_requests"]] == [
        t["pb"]["request_id"]]
    assert w.client.get(f"/v1/agents/{a_id}", headers=w.h(OWNER_A)).json()[
        "status"] == "active"
    statuses = [r["status"] for r in w.risk.store.audit_records()]
    assert "rejected:v1_cross_owner_payment" in statuses


def test_agent_cannot_grant_itself_or_approve(two):
    w, t = two
    a_id, a_key, _ = t["a"]
    pa = t["pa"]
    r = w.client.put(f"/v1/agents/{a_id}/permissions",
                     json={"capabilities": ["research.search"]}, headers=w.h(a_key))
    assert r.status_code == 403
    r = w.client.post(f"/v1/payment-requests/{pa['request_id']}/approve",
                      json={"terms_sha256": pa["terms_sha256"]}, headers=w.h(a_key))
    assert r.status_code == 403 and r.json() == {"error": "owner_credential_required"}
    r = w.client.post(f"/v1/agents/{a_id}/reactivate", headers=w.h(a_key))
    assert r.status_code == 403
    assert w.approvals.calls == []
    # the advisory capabilities of an AgentContext grant nothing
    from agentpal.contracts import AgentContext, Capability
    forged = AgentContext(agent_id=a_id, owner_id="owner_alice", role="external",
                          capabilities=frozenset(Capability))
    assert not w.platform.checker.check(forged, Capability.research_search).allowed


def test_agents_of_one_owner_only_see_their_own_wallet(two):
    w, t = two
    _, a2_key = t["a2"]
    mine = w.client.get("/v1/me/wallet", headers=w.h(a2_key)).json()["wallets"]
    assert [x["agent_id"] for x in mine] == [t["a2"][0]]
    txs = w.client.get("/v1/me/wallet/transactions", headers=w.h(a2_key)).json()
    assert txs["transactions"] == []
