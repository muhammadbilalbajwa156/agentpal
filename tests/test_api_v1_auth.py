"""/v1 authentication: credential types never cross over, legacy tokens and /v1 keys stay
in their own surfaces, and loopback / Host / body-size / rate limits apply to /v1."""

from __future__ import annotations

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from agentpal.api import create_app
from agentpal.infra_contracts import AuthenticatedOwner

from conftest import VALID
from test_platform_fixtures import (
    LEGACY_AGENT,
    LEGACY_OPS,
    OWNER_A,
    build_world,
    settings,
)


def test_open_v1_paths_need_no_credential():
    w = build_world()
    assert w.client.get("/v1/health").status_code == 200
    assert w.client.get("/v1/me").json() == {"error": "authentication_required"}


def test_agent_key_cannot_act_as_owner_and_owner_cannot_act_as_agent():
    w = build_world()
    agent_id, key = w.register()
    for method, path in [("get", "/v1/agents"), ("post", "/v1/agents"),
                         ("get", f"/v1/agents/{agent_id}"),
                         ("put", f"/v1/agents/{agent_id}/permissions"),
                         ("post", f"/v1/agents/{agent_id}/credentials"),
                         ("post", "/v1/wallets"), ("get", "/v1/approvals"),
                         ("post", "/v1/payment-requests/pay_x123/approve")]:
        r = w.client.request(method, path, json={}, headers=w.h(key))
        assert r.status_code == 403 and r.json() == {"error": "owner_credential_required"}, \
            path
    for method, path in [("get", "/v1/me"), ("get", "/v1/me/wallet"),
                         ("post", "/v1/payment-requests"),
                         ("post", "/v1/payment-requests/pay_x123/execute")]:
        r = w.client.request(method, path, json={}, headers=w.h(OWNER_A))
        assert r.status_code == 403 and r.json() == {"error": "agent_credential_required"}, \
            path
    # an owner credential is never even offered to the agent verifier, and vice versa
    calls = w.owner_auth.calls
    w.client.get("/v1/me", headers=w.h(key))
    assert w.owner_auth.calls == calls


def test_legacy_tokens_are_refused_on_v1_and_v1_keys_on_legacy_routes():
    w = build_world()
    _, key = w.register()
    for token in (LEGACY_AGENT, LEGACY_OPS):
        r = w.client.get("/v1/me", headers=w.h(token))
        assert r.status_code == 401 and r.json() == {"error": "invalid_token"}
        assert w.client.get("/v1/agents", headers=w.h(token)).status_code == 401
    for token in (key, OWNER_A):
        r = w.client.post("/assess", json={**VALID, "request_id": "req_cross_1"},
                          headers=w.h(token))
        assert r.status_code == 401
    # the legacy surface keeps working unchanged
    r = w.client.post("/assess", json={**VALID, "request_id": "req_legacy_ok"},
                      headers=w.h(LEGACY_AGENT))
    assert r.status_code == 200
    assert w.client.get("/assessments/req_legacy_ok",
                        headers=w.h(LEGACY_OPS)).status_code == 200


def test_v1_always_authenticates_even_in_insecure_dev_mode():
    w = build_world(auth_mode="disabled_insecure_dev")
    assert w.client.get("/v1/agents").status_code == 401
    assert w.client.get("/v1/me", headers=w.h("apk_whatever")).status_code == 401
    assert w.client.get("/v1/health").status_code == 200
    # legacy dev-mode behaviour is unchanged
    assert w.client.get("/model-info").status_code == 200


def test_without_platform_there_are_no_v1_routes():
    w = build_world()
    app = create_app(service=w.risk, settings=settings())
    assert not [r for r in app.routes if isinstance(r, APIRoute) and r.path.startswith("/v1")]
    c = TestClient(app, client=("testclient", 50000))
    assert c.get("/v1/health").status_code == 401        # legacy gate: no token
    assert c.get("/v1/health", headers=w.h(OWNER_A)).status_code == 401


def test_host_allow_list_and_loopback_apply_to_v1():
    w = build_world()
    local = TestClient(w.app, client=("127.0.0.1", 50001))
    assert local.get("/v1/health", headers={"Host": "127.0.0.1:8000"}).status_code == 200
    r = local.get("/v1/health", headers={"Host": "evil.example"})
    assert r.status_code == 400 and r.json() == {"error": "forbidden_host"}
    remote = TestClient(w.app, client=("203.0.113.9", 50002))
    r = remote.post("/v1/providers/paypal/webhooks", content=b"{}",
                    headers={"Host": "127.0.0.1"})
    assert r.status_code == 403 and r.json() == {"error": "forbidden_non_loopback_client"}


def test_per_credential_rate_limit():
    w = build_world(rate_limit_per_token_per_minute=3)
    _, k1 = w.register()            # owner request 1
    _, k2 = w.register()            # owner request 2
    for _ in range(3):
        assert w.client.get("/v1/me", headers=w.h(k1)).status_code == 200
    r = w.client.get("/v1/me", headers=w.h(k1))
    assert r.status_code == 429 and r.json() == {"error": "rate_limited"}
    assert "retry-after" in r.headers
    assert w.client.get("/v1/me", headers=w.h(k2)).status_code == 200
    assert w.client.get("/v1/agents", headers=w.h(OWNER_A)).status_code == 200
    assert w.client.get("/v1/agents", headers=w.h(OWNER_A)).status_code == 429


def test_body_size_limit_applies_to_v1():
    w = build_world(max_request_bytes=1024)
    r = w.client.post("/v1/agents", content=b"{" + b" " * 4096 + b"}",
                      headers={**w.h(OWNER_A), "Content-Type": "application/json"})
    assert r.status_code == 413 and r.json() == {"error": "request_too_large"}


def test_authenticator_fault_fails_closed():
    w = build_world()

    class Boom:
        def authenticate(self, bearer: str) -> AuthenticatedOwner:
            raise RuntimeError("identity store down")

    w.deps.owner_authenticator = Boom()
    r = w.client.get("/v1/agents", headers=w.h(OWNER_A))
    assert r.status_code == 401 and r.json() == {"error": "invalid_token"}


def test_refusals_are_audited_without_the_credential():
    w = build_world()
    _, key = w.register()
    w.client.get("/v1/agents", headers=w.h(key))
    w.client.get("/v1/me", headers=w.h("apk_" + "0" * 16 + "_" + "x" * 43))
    rows = w.risk.store.audit_records()
    statuses = [r["status"] for r in rows]
    assert "rejected:v1_owner_credential_required" in statuses
    assert "rejected:v1_invalid_token" in statuses
    text = repr(rows)
    assert key not in text and "x" * 43 not in text
