"""Agent registration, owner-scoped listing, metadata, permissions within the role ceiling."""

from __future__ import annotations

import pytest

from test_platform_fixtures import OWNER_A, OWNER_B, OWNER_DOTTED, build_world


@pytest.fixture
def w():
    return build_world()


def test_register_returns_agent_and_one_time_key(w):
    r = w.client.post("/v1/agents", json={"name": "procurement", "description": "buys",
                                          "metadata": {"framework": "claude"}},
                      headers=w.h(OWNER_A))
    assert r.status_code == 201
    body = r.json()
    agent = body["agent"]
    assert agent["agent_id"].startswith("agt_") and agent["owner_id"] == "owner_alice"
    assert agent["role"] == "external" and agent["status"] == "active"
    assert agent["capabilities"] == ["owner.request_decision", "payment.prepare",
                                     "wallet.read"]
    assert agent["metadata"] == {"framework": "claude"}
    assert body["api_key"].startswith(f"apk_{body['key_id']}_")
    got = w.client.get(f"/v1/agents/{agent['agent_id']}", headers=w.h(OWNER_A)).json()
    assert "api_key" not in got and got["keys"][0]["key_id"] == body["key_id"]


def test_client_cannot_choose_the_agent_id_or_owner(w):
    for extra in ({"agent_id": "agt_" + "0" * 32}, {"owner_id": "owner_bob"},
                  {"status": "active"}):
        r = w.client.post("/v1/agents", json={"name": "x", **extra}, headers=w.h(OWNER_A))
        assert r.status_code == 422


def test_list_and_get_are_owner_scoped(w):
    a1, _ = w.register(OWNER_A)
    a2, _ = w.register(OWNER_A)
    b1, _ = w.register(OWNER_B)
    ids = [a["agent_id"] for a in w.client.get("/v1/agents", headers=w.h(OWNER_A)).json()[
        "agents"]]
    assert sorted(ids) == sorted([a1, a2])
    assert w.client.get(f"/v1/agents/{b1}", headers=w.h(OWNER_A)).status_code == 404
    unknown = "agt_" + "f" * 32
    assert w.client.get(f"/v1/agents/{unknown}", headers=w.h(OWNER_A)).json() == {
        "error": "not_found"}
    refused = w.wallet_store.query("SELECT detail FROM wallet_grant_events WHERE agent_id=? "
                                   "AND action='refused'", (b1,))
    assert refused and refused[0]["detail"].startswith("CROSS_OWNER")


def test_registration_capabilities_must_fit_the_role(w):
    r = w.client.post("/v1/agents", json={"name": "x", "role": "research",
                                          "capabilities": ["payment.prepare"]},
                      headers=w.h(OWNER_A))
    assert r.status_code == 400 and r.json() == {"error": "above_role_ceiling"}
    assert w.client.get("/v1/agents", headers=w.h(OWNER_A)).json()["agents"] == []


def test_invalid_metadata_and_capability_names_are_422(w):
    for body in ({"name": "x", "metadata": {"Bad Key": "v"}},
                 {"name": "x", "metadata": {"k": "v" * 201}},
                 {"name": "x", "capabilities": ["payment.approve"]},
                 {"name": ""}):
        assert w.client.post("/v1/agents", json=body, headers=w.h(OWNER_A)).status_code == 422


def test_set_permissions_diffs_grants_and_audits(w):
    agent_id, _ = w.register()
    r = w.client.put(f"/v1/agents/{agent_id}/permissions",
                     json={"capabilities": ["wallet.read", "research.search",
                                            "payment.execute_sandbox"]},
                     headers=w.h(OWNER_A))
    assert r.status_code == 200
    assert r.json()["capabilities"] == ["payment.execute_sandbox", "research.search",
                                        "wallet.read"]
    assert "purchase.propose" not in r.json()["ceiling"]
    events = [(e["action"], e["capability"]) for e in w.wallet_store.query(
        "SELECT action, capability FROM wallet_grant_events WHERE agent_id=? ORDER BY id",
        (agent_id,))]
    assert ("grant", "research.search") in events
    assert ("revoke", "payment.prepare") in events
    assert ("revoke", "owner.request_decision") in events


def test_permissions_above_ceiling_change_nothing(w):
    agent_id, _ = w.register()
    before = w.client.get(f"/v1/agents/{agent_id}/permissions", headers=w.h(OWNER_A)).json()
    r = w.client.put(f"/v1/agents/{agent_id}/permissions",
                     json={"capabilities": ["wallet.read", "purchase.propose"]},
                     headers=w.h(OWNER_A))
    assert r.status_code == 400 and r.json() == {"error": "above_role_ceiling"}
    after = w.client.get(f"/v1/agents/{agent_id}/permissions", headers=w.h(OWNER_A)).json()
    assert before == after
    refused = w.wallet_store.query("SELECT detail FROM wallet_grant_events WHERE agent_id=? "
                                   "AND action='refused'", (agent_id,))
    assert any("purchase.propose" in r["detail"] for r in refused)


def test_owner_decision_audit_records_the_owner_session(w):
    agent_id, _ = w.register()
    actors = {e["actor"] for e in w.wallet_store.query(
        "SELECT actor FROM wallet_grant_events WHERE agent_id=?", (agent_id,))}
    assert actors == {"owner_alice|session:sess_owner_alicex"}


def test_owner_id_outside_safe_pattern_is_refused(w):
    r = w.client.get("/v1/agents", headers=w.h(OWNER_DOTTED))
    assert r.status_code == 403 and r.json() == {"error": "owner_id_unsupported"}


def test_agent_limit_per_owner():
    from agentpal.platform import PlatformLimits
    w = build_world(limits=PlatformLimits(max_agents_per_owner=2))
    w.register()
    w.register()
    r = w.client.post("/v1/agents", json={"name": "x"}, headers=w.h(OWNER_A))
    assert r.status_code == 409 and r.json() == {"error": "agent_limit_reached"}
