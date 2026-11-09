"""Hosted (Render) mode: the front door trusts Render's proxy only there, opens only the
authenticated /v1 API and health to remote clients, and keeps every other guard (lead,
2026-11-05)."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from agentpal.api import create_app
from agentpal.config import UnsafeConfiguration
from test_platform_fixtures import OWNER_A, build_world, settings as base_settings

HOST = "agentpal-test.onrender.com"


def hosted_settings(tmp_path, **kw):
    return base_settings(environment="production", hosted_proxy="render",
                         api_tokens_file=str(tmp_path / "tokens.json"),
                         db_path=str(tmp_path / "ledger.db"),
                         rate_limit_db_path=str(tmp_path / "rl.db"),
                         analyzer_chain=["heuristic"],
                         allowed_hosts=["127.0.0.1", "localhost", HOST], **kw)


@pytest.fixture
def hosted(tmp_path, monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("AGENTPAL_DATA_DIR", str(tmp_path))
    (tmp_path / "tokens.json").write_text('{"tokens": []}')
    w = build_world()
    app = create_app(service=w.risk, settings=hosted_settings(tmp_path), registry=None,
                     platform=w.deps)
    return w, TestClient(app, base_url=f"https://{HOST}", client=("10.1.2.3", 40000))


def via_render(client_ip="203.0.113.7", proto="https", **extra):
    return {"Host": HOST, "X-Forwarded-Proto": proto, "X-Forwarded-For": client_ip, **extra}


def test_authenticated_v1_works_through_the_render_proxy(hosted):
    _, c = hosted
    r = c.get("/v1/agents", headers={**via_render(), "Authorization": "Bearer " + OWNER_A})
    assert r.status_code == 200, r.text


def test_without_credentials_v1_still_requires_authentication(hosted):
    _, c = hosted
    assert c.get("/v1/agents", headers=via_render()).status_code == 401


@pytest.mark.parametrize("headers,status", [
    ({"Host": HOST}, 403),                                            # no proxy headers
    (via_render(proto="http"), 403),                                  # plain http
    ({"Host": HOST, "X-Forwarded-Proto": "https"}, 400),             # no client address
    (via_render(client_ip="not-an-ip"), 400),
    (via_render(client_ip="203.0.113.7, 127.0.0.1"), 400),           # forwarded loopback
    ({**via_render(), "X-Forwarded-Proto": "https"}, 200),           # (dict merge: still one)
])
def test_front_door_requires_one_https_proto_and_a_valid_client(hosted, headers, status):
    _, c = hosted
    # A protected route: only Render's own health probe may arrive without proxy headers.
    r = c.get("/v1/agents", headers={**headers, "Authorization": "Bearer " + OWNER_A})
    assert r.status_code == status, (headers, r.text)


def test_duplicate_forwarded_proto_is_refused(hosted):
    _, c = hosted
    raw = [("host", HOST), ("x-forwarded-proto", "https"), ("x-forwarded-proto", "https"),
           ("x-forwarded-for", "203.0.113.7")]
    r = c.get("/v1/health", headers=raw)
    assert r.status_code == 403


def test_client_is_the_rightmost_forwarded_entry(hosted):
    """A client-supplied 127.0.0.1 earlier in the chain must not unlock loopback routes."""
    _, c = hosted
    r = c.post("/assess", headers=via_render(client_ip="127.0.0.1, 203.0.113.7"), json={})
    assert r.status_code == 403
    assert r.json()["error"] == "forbidden_non_loopback_client"


@pytest.mark.parametrize("path", ["/assess", "/docs", "/openapi.json", "/model-info"])
def test_non_v1_routes_stay_loopback_only_for_remote_clients(hosted, path):
    _, c = hosted
    r = c.get(path, headers=via_render())
    assert r.status_code == 403


def test_encoded_path_variants_are_not_opened(hosted):
    _, c = hosted
    r = c.get("/v1%2Fagents", headers={**via_render(), "Authorization": "Bearer " + OWNER_A})
    assert r.status_code in (403, 404)
    assert r.status_code != 200


def test_wrong_host_is_refused_even_through_the_proxy(hosted):
    _, c = hosted
    r = c.get("/v1/health", headers={**via_render(), "Host": "evil.example"})
    assert r.status_code == 400


def test_render_health_probe_without_proxy_headers_reaches_health_only(hosted):
    _, c = hosted
    assert c.get("/health", headers={"Host": "127.0.0.1"}).status_code == 200
    assert c.get("/v1/agents", headers={"Host": "127.0.0.1",
                                        "Authorization": "Bearer " + OWNER_A}).status_code == 403


def test_websockets_are_refused(hosted):
    _, c = hosted
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/v1/health", headers=via_render()):
            pass


def test_without_hosted_mode_forwarding_headers_are_ignored(tmp_path):
    """Outside hosted mode a remote peer stays refused whatever headers it sends."""
    w = build_world()
    c = TestClient(w.app, client=("203.0.113.9", 1))
    r = c.get("/v1/health", headers={"Host": "127.0.0.1", "X-Forwarded-Proto": "https",
                                     "X-Forwarded-For": "127.0.0.1"})
    assert r.status_code == 403


@pytest.mark.parametrize("change,message", [
    ({}, "only valid on Render"),
    ({"environment": "development"}, "AGENTPAL_ENV=production"),
    ({"allowed_hosts": ["127.0.0.1", "evil.example"]}, "onrender.com"),
    ({"allowed_hosts": ["127.0.0.1", "*.onrender.com"]}, "onrender.com"),
    ({"rate_limit_db_path": ":memory:"}, "durable shared SQLite"),
])
def test_hosted_mode_refuses_unsafe_configuration(tmp_path, monkeypatch, change, message):
    if change:
        monkeypatch.setenv("RENDER", "true")
    else:
        monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setenv("AGENTPAL_DATA_DIR", str(tmp_path))
    (tmp_path / "tokens.json").write_text('{"tokens": []}')
    s = hosted_settings(tmp_path)
    s = s.model_copy(update=change)
    with pytest.raises(UnsafeConfiguration, match=message):
        s.check_runtime(host="0.0.0.0", spend_policy_configuration="operator")


def test_hosted_mode_allows_a_proxied_bind_without_local_tls(tmp_path, monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("AGENTPAL_DATA_DIR", str(tmp_path))
    (tmp_path / "tokens.json").write_text('{"tokens": []}')
    s = hosted_settings(tmp_path, public_webhook_enabled=True, public_webhook_hosts=[HOST])
    s.check_runtime(host="0.0.0.0", spend_policy_configuration="operator")


def test_hosted_webhook_hosts_must_be_allowed_hosts(tmp_path, monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("AGENTPAL_DATA_DIR", str(tmp_path))
    (tmp_path / "tokens.json").write_text('{"tokens": []}')
    s = hosted_settings(tmp_path, public_webhook_enabled=True,
                        public_webhook_hosts=["other.onrender.com"])
    with pytest.raises(UnsafeConfiguration, match="webhook hosts"):
        s.check_runtime(host="0.0.0.0", spend_policy_configuration="operator")


def test_import_token_is_idempotent_and_never_revives_a_revoked_token(tmp_path):
    from agentpal.identity import OwnerIdentityService
    from agentpal.identity.service import IdentityError
    from agentpal.infra_contracts import OwnerAuthError
    from agentpal.identity.store import IdentityStore
    ident = OwnerIdentityService(IdentityStore(str(tmp_path / "id.db")))
    ident.create_owner("alice")
    token = "apo_" + "a1" * 12 + "_" + "S" * 43
    assert ident.import_token("alice", token) is True
    assert ident.authenticate(token).owner_id == "alice"
    assert ident.import_token("alice", token) is False
    ident.revoke_token("a1" * 12)
    assert ident.import_token("alice", token) is False
    with pytest.raises(OwnerAuthError):
        ident.authenticate(token)
    with pytest.raises(IdentityError):
        ident.import_token("alice", "apo_short")
    with pytest.raises(IdentityError):
        ident.import_token("alice", "apo_" + "b2" * 12 + "_" + "S" * 43, ttl=timedelta(days=400))


# ---------------------------------------------------------------- review fixes (2026-11-05)
@pytest.mark.parametrize("path", ["/v1/providers/paypal/webhooks",
                                  "/v1/providers/paypal/webhooks?x=1"])
def test_webhook_is_never_reachable_through_the_generic_v1_rule(hosted, path):
    """The webhook must only pass PublicWebhookIngress (here disabled): no verifier call."""
    w, c = hosted
    calls = []
    w.deps.provider.verify_webhook = lambda *a, **k: calls.append(1)
    r = c.post(path, headers=via_render(), content=b"{}")
    assert r.status_code == 403 and not calls


def test_more_than_one_forwarded_for_header_is_refused(hosted):
    _, c = hosted
    raw = [("host", HOST), ("x-forwarded-proto", "https"), ("x-forwarded-for", "203.0.113.7"),
           ("x-forwarded-for", "198.51.100.1"), ("authorization", "Bearer " + OWNER_A)]
    assert c.get("/v1/agents", headers=raw).status_code == 400


@pytest.mark.parametrize("ip", ["fe80::1%lo", "0.0.0.0", "::", "169.254.1.1", "224.0.0.1"])
def test_unusual_forwarded_clients_are_refused(hosted, ip):
    _, c = hosted
    r = c.get("/v1/agents", headers={**via_render(client_ip=ip), "Authorization": "Bearer " + OWNER_A})
    assert r.status_code == 400


def test_render_health_probe_works_with_an_internal_host_header(hosted):
    _, c = hosted
    assert c.get("/health", headers={"Host": "10.0.0.5:10000"}).status_code == 200


def test_hosted_mode_requires_databases_on_the_persistent_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("AGENTPAL_DATA_DIR", str(tmp_path / "disk"))
    (tmp_path / "tokens.json").write_text('{"tokens": []}')
    with pytest.raises(UnsafeConfiguration, match="AGENTPAL_DATA_DIR"):
        hosted_settings(tmp_path).check_runtime(host="0.0.0.0", spend_policy_configuration="operator")


@pytest.mark.parametrize("headers", [
    {"Host": "10.238.22.253:10000"},
    {"Host": "10.238.22.253:10000", "X-Forwarded-For": "10.238.22.253"},
    {"Host": "agentpal-api.onrender.com", "X-Forwarded-Proto": "http"},
])
def test_render_health_checker_always_gets_a_fixed_liveness_answer(hosted, headers):
    """Deploy 1 on Render: the checker's /health got 400. /health is now answered by the
    front door with a fixed body, whatever the headers; it reveals nothing."""
    _, c = hosted
    r = c.get("/health", headers=headers)
    assert r.status_code == 200 and r.json() == {"status": "ok"}
