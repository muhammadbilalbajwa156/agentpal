"""SH-A workspace boundaries. All upstream calls use synthetic credentials/transports."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from agentpal.api import create_app
from agentpal.auth import TokenRegistry
from agentpal.config import Settings
from agentpal.platform.wiring import build_platform
from agentpal.platform.errors import PlatformError


def bearer(token):
    return {"Authorization": "Bearer " + token}


@contextmanager
def world(tmp_path, env=None, peer="127.0.0.1"):
    settings = Settings(db_path=str(tmp_path / "risk.db"), auth_mode="required",
                        analyzer_chain=["heuristic"], allowed_hosts=["127.0.0.1"],
                        rate_limit_per_peer_per_minute=10000,
                        rate_limit_per_token_per_minute=10000, log_level="ERROR")
    deps = build_platform(settings=settings, environ=env or {})
    app = create_app(service=deps.risk_service, settings=settings,
                     registry=TokenRegistry(), platform=deps)
    with TestClient(app, base_url="http://127.0.0.1", client=(peer, 1234)) as client:
        yield client, deps
    deps.close()


def owner(deps, name="alice"):
    deps.owner_authenticator.create_owner(name)
    return deps.owner_authenticator.issue_token(name)[0]


def setup_body(code, name="alice"):
    return {"owner_id": name, "display_name": "Workspace", "code": code}


def test_setup_status_code_single_use_and_private_file(tmp_path):
    with world(tmp_path) as (c, d):
        assert c.get("/v1/setup/status").json() == {
            "mode": "self_hosted", "needs_setup": True, "signup": "setup_code"}
        code_path = tmp_path / "setup-code"
        assert code_path.stat().st_mode & 0o777 == 0o600
        code = code_path.read_text().strip()
        assert c.post("/v1/setup/owner", json=setup_body("incorrect")).status_code == 403
        r = c.post("/v1/setup/owner", json=setup_body(code))
        assert r.status_code == 201, r.text
        assert r.json()["token"].startswith("apo_")
        assert c.get("/v1/integrations", headers=bearer(r.json()["token"])).status_code == 200
        assert c.post("/v1/setup/owner", json=setup_body(code, "bob")).json() == {"error": "setup_complete"}
        assert c.get("/v1/setup/status").json()["needs_setup"] is False
        assert not code_path.exists()
        assert len(d.owner_authenticator.store.query("SELECT * FROM identity_owners")) == 1


def test_setup_race_across_connections(tmp_path):
    with world(tmp_path) as (c, d), world(tmp_path) as (c2, _):
        code = (tmp_path / "setup-code").read_text().strip()
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda pair: pair[0].post("/v1/setup/owner", json=setup_body(code, pair[1])),
                                    [(c, "alice"), (c2, "bob")]))
        assert sorted(r.status_code for r in results) == [201, 403]
        assert len(d.owner_authenticator.store.query("SELECT * FROM identity_owner_credentials")) == 1


def test_setup_requires_socket_loopback_even_forwarded(tmp_path):
    from agentpal.platform.setup import SetupService
    with world(tmp_path) as (_, d):
        code = (tmp_path / "setup-code").read_text().strip()
        with pytest.raises(PlatformError) as exc:
            d.setup.signup("alice", "Alice", code, "203.0.113.4")
        assert str(exc.value) == "loopback_required"
        assert isinstance(d.setup, SetupService)
    with world(tmp_path, peer="203.0.113.4") as (c, _):
        assert c.post("/v1/setup/owner", json=setup_body(code),
                      headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 403


@pytest.mark.parametrize("invite", [None, "synthetic-invite"])
def test_hosted_invite_and_duplicate_owner(tmp_path, invite):
    env = {"AGENTPAL_MODE": "hosted"}
    if invite:
        env["AGENTPAL_SIGNUP_INVITE_CODE"] = invite
    with world(tmp_path, env) as (c, _):
        assert c.get("/v1/setup/status").json() == {"mode": "hosted", "needs_setup": False,
            "signup": "invite_code" if invite else "disabled"}
        assert c.post("/v1/setup/owner", json=setup_body("wrong")).status_code == 403
        if invite:
            assert c.post("/v1/setup/owner", json=setup_body(invite)).status_code == 201
            assert c.post("/v1/setup/owner", json=setup_body(invite)).json() == {"error": "owner_exists"}
            assert c.post("/v1/setup/owner", json=setup_body(invite, "bob")).status_code == 201


def test_setup_rate_limit(tmp_path):
    with world(tmp_path) as (c, _):
        for _ in range(10):
            assert c.post("/v1/setup/owner", json=setup_body("wrong")).status_code == 403
        assert c.post("/v1/setup/owner", json=setup_body("wrong")).status_code == 429


def verify_transport(monkeypatch, status=200):
    from agentpal.platform.integrations import IntegrationVerifier
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(status, json={"access_token": "synthetic-access", "data": [], "products": []},
                              headers={"Location": "https://example.net/leak"})
    monkeypatch.setattr(IntegrationVerifier, "transport_factory", staticmethod(lambda: httpx.MockTransport(handle)))
    return calls


@pytest.mark.parametrize("provider,fields,host", [
    ("paypal_sandbox", {"client_id": "synthetic-client", "client_secret": "synthetic-private"}, "api-m.sandbox.paypal.com"),
    ("channel3", {"api_key": "synthetic-private"}, "api.trychannel3.com"),
    ("kernel", {"api_key": "synthetic-private"}, "api.onkernel.com"),
    ("groq", {"api_key": "synthetic-private"}, "api.groq.com"),
    ("openrouter", {"api_key": "synthetic-private"}, "openrouter.ai"),
])
def test_integrations_verified_once_encrypted_isolated_and_deleted(tmp_path, monkeypatch, caplog, provider, fields, host):
    calls = verify_transport(monkeypatch)
    with world(tmp_path) as (c, d):
        a, b = owner(d), owner(d, "bob")
        path = "/v1/integrations/" + provider
        r = c.put(path, headers=bearer(a), json={"fields": fields})
        assert r.status_code == 200, r.text
        assert len(calls) == 1 and calls[0].url.host == host
        assert r.json()["status"] == "verified" and r.json()["source"] == "workspace"
        assert "synthetic-private" not in r.text + caplog.text
        assert "synthetic-client" not in r.text
        assert all(i["source"] is None for i in c.get("/v1/integrations", headers=bearer(b)).json()["integrations"])
        assert d.integrations.fields("alice", provider) == fields
        assert d.integrations.fields("bob", provider) is None
        for path_on_disk in tmp_path.glob("*.db*"):
            assert b"synthetic-private" not in path_on_disk.read_bytes()
        assert (tmp_path / "secrets.key").stat().st_mode & 0o777 == 0o600
        assert c.post(path + "/verify", headers=bearer(a)).status_code == 200
        assert c.delete(path, headers=bearer(a)).status_code == 204
        assert d.integrations.fields("alice", provider) is None


@pytest.mark.parametrize("status", [302, 401, 500])
def test_verification_failure_never_stores_or_follows_redirect(tmp_path, monkeypatch, status):
    calls = verify_transport(monkeypatch, status)
    with world(tmp_path) as (c, d):
        token = owner(d)
        r = c.put("/v1/integrations/groq", headers=bearer(token), json={"fields": {"api_key": "synthetic-private"}})
        assert r.status_code == 422 and r.json()["error"] == "verification_failed"
        assert r.json()["error_code"]
        assert len(calls) == 1
        assert d.integrations.fields("alice", "groq") is None


def test_hosted_secrets_require_master_key(tmp_path, monkeypatch):
    verify_transport(monkeypatch)
    with world(tmp_path, {"AGENTPAL_MODE": "hosted"}) as (c, d):
        token = owner(d)
        r = c.put("/v1/integrations/groq", headers=bearer(token), json={"fields": {"api_key": "synthetic-private"}})
        assert r.status_code == 503
        assert not (tmp_path / "secrets.key").exists()


def test_verification_rate_limit_shared_across_owner_tokens(tmp_path, monkeypatch):
    calls = verify_transport(monkeypatch)
    with world(tmp_path) as (c, d):
        token = owner(d)
        other = d.owner_authenticator.issue_token("alice")[0]
        for i in range(11):
            r = c.put("/v1/integrations/groq", headers=bearer(token if i % 2 else other),
                      json={"fields": {"api_key": "synthetic-private"}})
            assert r.status_code == (200 if i < 10 else 429)
        assert len(calls) == 10


def test_agents_cannot_use_integrations_or_lab(tmp_path):
    with world(tmp_path) as (c, d):
        token = owner(d)
        key = c.post("/v1/agents", headers=bearer(token), json={"name": "external"}).json()["api_key"]
        for method, path, data in [("GET", "/v1/integrations", None), ("PUT", "/v1/integrations/groq", {"fields": {"api_key": "synthetic"}}),
            ("DELETE", "/v1/integrations/groq", None), ("POST", "/v1/integrations/groq/verify", None),
            ("POST", "/v1/lab/agent", None), ("POST", "/v1/lab/errands", {"task": "Buy pencils", "budget": "3.00"}),
            ("POST", "/v1/lab/payment-requests", payment()), ("POST", "/v1/lab/payment-requests/missing/execute", None)]:
            assert c.request(method, path, headers=bearer(key), json=data).status_code == 403


def payment(amount="1.00"):
    return {"amount": amount, "currency": "USD", "merchant_name": "Example Hosting",
            "merchant_domain": "example.com", "purpose": "Sandbox test purchase"}


def test_lab_idempotent_policy_exact_approval_and_simulator(tmp_path):
    with world(tmp_path) as (c, d):
        token = owner(d)
        h = bearer(token)
        lab = c.post("/v1/lab/agent", headers=h)
        assert lab.status_code == 200, lab.text
        assert set(lab.json()) == {"agent_id", "wallet_id"}
        assert c.post("/v1/lab/agent", headers=h).json() == lab.json()
        wallet = c.get("/v1/wallets/" + lab.json()["wallet_id"], headers=h).json()
        assert wallet["limits"]["monthly_budget"] == "200.00"
        blocked = c.post("/v1/lab/payment-requests", headers=h, json=payment("51.00"))
        assert blocked.status_code == 201, blocked.text
        assert blocked.json()["recommendation"] == "block"
        assert c.post("/v1/lab/payment-requests/" + blocked.json()["request_id"] + "/execute", headers=h).status_code == 409
        r = c.post("/v1/lab/payment-requests", headers=h, json=payment())
        assert r.status_code == 201, r.text
        rid = r.json()["request_id"]
        path = "/v1/lab/payment-requests/" + rid + "/execute"
        assert c.post(path, headers=h).status_code == 409
        assert c.post("/v1/payment-requests/" + rid + "/approve", headers=h,
                      json={"terms_sha256": "0" * 64}).status_code == 409
        assert c.post("/v1/payment-requests/" + rid + "/approve", headers=h,
                      json={"terms_sha256": r.json()["terms_sha256"]}).status_code == 200
        result = c.post(path, headers=h)
        assert result.status_code == 200, result.text
        assert result.json()["status"] == "provider_pending"
        assert result.json()["provider_mode"] == "simulated"
        bob = owner(d, "bob")
        assert c.post(path, headers=bearer(bob)).status_code == 404
        assert c.post("/v1/lab/errands", headers=h, json={"task": "Buy pencils", "budget": "3.00"}).json() == {
            "error": "integration_required", "provider": "channel3"}


def test_secret_ciphertext_cannot_move_between_workspaces(tmp_path):
    from agentpal.platform.secrets import SecretVault
    vault = SecretVault(Fernet.generate_key())
    blob = vault.encrypt("alice", "groq", {"api_key": "synthetic-private"})
    assert b"synthetic-private" not in blob
    assert vault.decrypt("alice", "groq", blob)["api_key"] == "synthetic-private"
    with pytest.raises(PlatformError):
        vault.decrypt("bob", "groq", blob)
    with pytest.raises(PlatformError):
        vault.decrypt("alice", "kernel", blob)


def test_secrets_persist_restart_and_wrong_master_fails_closed(tmp_path, monkeypatch):
    verify_transport(monkeypatch)
    master = Fernet.generate_key().decode()
    env = {"AGENTPAL_MODE": "hosted", "AGENTPAL_SECRETS_KEY": master}
    with world(tmp_path, env) as (c, d):
        token = owner(d)
        assert c.put("/v1/integrations/groq", headers=bearer(token),
                     json={"fields": {"api_key": "synthetic-persistent"}}).status_code == 200
    assert not (tmp_path / "secrets.key").exists()
    with world(tmp_path, env) as (_, d):
        assert d.integrations.fields("alice", "groq") == {"api_key": "synthetic-persistent"}
    with world(tmp_path, {**env, "AGENTPAL_SECRETS_KEY": Fernet.generate_key().decode()}) as (_, d):
        with pytest.raises(PlatformError, match="secrets_unavailable"):
            d.integrations.fields("alice", "groq")


def test_workspace_provider_resolution_and_server_fallback(tmp_path, monkeypatch):
    verify_transport(monkeypatch)
    env = {"PAYPAL_SANDBOX_CLIENT_ID": "synthetic-server", "PAYPAL_SANDBOX_CLIENT_SECRET": "synthetic-server-private",
           "CHANNEL3_API_KEY": "synthetic-server-channel", "KERNEL_API_KEY": "synthetic-server-kernel"}
    with world(tmp_path, env) as (c, d):
        token = owner(d)
        assert d.paypal_for("alice") is d.provider
        assert d.paypal_for("bob") is d.provider
        for provider, fields in [("paypal_sandbox", {"client_id": "synthetic-workspace", "client_secret": "synthetic-workspace-private"}),
                                 ("channel3", {"api_key": "synthetic-workspace-channel"}),
                                 ("kernel", {"api_key": "synthetic-workspace-kernel"})]:
            assert c.put("/v1/integrations/" + provider, headers=bearer(token), json={"fields": fields}).status_code == 200
        assert d.paypal_for("alice").config.client_id == "synthetic-workspace"
        assert d.paypal_for("bob").config.client_id == "synthetic-server"
        assert d.market_for("alice") is not d.market_for("bob")
        assert d.kernel_for("alice")[0] is not d.kernel_for("bob")[0]
        assert c.delete("/v1/integrations/paypal_sandbox", headers=bearer(token)).status_code == 204
        assert d.paypal_for("alice") is d.provider


@pytest.mark.parametrize("fields", [
    {"api_key": "synthetic-private", "base_url": "https://example.net"},
    {"api_key": "synthetic-private\r\nHeader: test"}, {}, {"api_key": 42},
])
def test_integration_invalid_input_never_reaches_network_or_echoes(tmp_path, monkeypatch, fields):
    calls = verify_transport(monkeypatch)
    with world(tmp_path) as (c, d):
        token = owner(d)
        r = c.put("/v1/integrations/groq", headers=bearer(token), json={"fields": fields})
        assert r.status_code == 422
        assert "synthetic-private" not in r.text
        assert not calls


def test_failed_replacement_preserves_verified_key(tmp_path, monkeypatch):
    verify_transport(monkeypatch)
    with world(tmp_path) as (c, d):
        token = owner(d)
        path = "/v1/integrations/groq"
        assert c.put(path, headers=bearer(token), json={"fields": {"api_key": "synthetic-original"}}).status_code == 200
        verify_transport(monkeypatch, 401)
        assert c.put(path, headers=bearer(token), json={"fields": {"api_key": "synthetic-replacement"}}).status_code == 422
        assert d.integrations.fields("alice", "groq") == {"api_key": "synthetic-original"}
        assert c.post(path + "/verify", headers=bearer(token)).status_code == 422
        item = c.get("/v1/integrations", headers=bearer(token)).json()["integrations"][3]
        assert item["status"] == "failed" and item["error_code"] == "credentials_rejected"


def test_lab_agent_race_suspension_and_encrypted_credential(tmp_path):
    with world(tmp_path) as (c, d):
        token = owner(d)
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: c.post("/v1/lab/agent", headers=bearer(token)), range(2)))
        assert results[0].json() == results[1].json()
        aid = results[0].json()["agent_id"]
        row = d.wallet_store.query("SELECT ciphertext FROM platform_lab_agents WHERE owner_id='alice'")[0]
        key = d.integrations.vault.decrypt("alice", "lab_agent", row["ciphertext"])["api_key"]
        for file in tmp_path.glob("*.db*"):
            assert key.encode() not in file.read_bytes()
        assert c.post("/v1/payment-requests/missing/approve", headers=bearer(key), json={"terms_sha256": "0" * 64}).status_code == 403
        assert c.post("/v1/agents/" + aid + "/suspend", headers=bearer(token)).status_code == 200
        assert c.post("/v1/lab/payment-requests", headers=bearer(token), json=payment()).status_code == 403


def test_lab_errand_reuses_shopper_plan_and_agent_policy(tmp_path, monkeypatch):
    from agentpal.agents.shopper.core import Shopper
    verify_transport(monkeypatch)
    seen = []
    def plan(self, task, budget):
        seen.append((task, budget, self._keys.copy()))
        return payment("51.00")
    monkeypatch.setattr(Shopper, "plan", plan)
    with world(tmp_path) as (c, d):
        token = owner(d)
        for provider in ("channel3", "groq"):
            assert c.put("/v1/integrations/" + provider, headers=bearer(token), json={"fields": {"api_key": "synthetic-" + provider}}).status_code == 200
        r = c.post("/v1/lab/errands", headers=bearer(token), json={"task": "Buy pencils", "budget": "30.00"})
        assert r.status_code == 200, r.text
        assert seen[0][0:2] == ("Buy pencils", "30.00")
        assert seen[0][2]["GROQ_API_KEY"] == "synthetic-groq"
        view = c.get("/v1/payment-requests/" + r.json()["request_id"], headers=bearer(token)).json()
        assert view["recommendation"] == "block"
        assert c.post("/v1/lab/payment-requests/" + view["request_id"] + "/execute", headers=bearer(token)).status_code == 409


def test_start_command_sets_local_defaults_and_clears_cached_settings(tmp_path, monkeypatch):
    from agentpal import __main__ as cli
    from agentpal.config import get_settings
    import os
    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENTPAL_DATA_DIR", raising=False)
    monkeypatch.delenv("AGENTPAL_DB_PATH", raising=False)
    seen = []
    monkeypatch.setattr(cli, "_cmd_serve", lambda args: seen.append((args, get_settings())))
    try:
        cli.main(["start"])
        args, settings = seen[0]
        assert args.platform and args.host == "127.0.0.1" and args.port == 8000
        assert settings.db_path == str(tmp_path / "data" / "agentpal.db")
    finally:
        get_settings.cache_clear()


def test_serve_platform_prints_dashboard_and_pending_setup_code(tmp_path, monkeypatch, capsys):
    from agentpal import __main__ as cli
    from agentpal.config import get_settings
    import uvicorn
    monkeypatch.setenv("AGENTPAL_DB_PATH", str(tmp_path / "risk.db"))
    monkeypatch.setenv("AGENTPAL_ANALYZER_CHAIN", "heuristic")
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: None)
    get_settings.cache_clear()
    try:
        cli.main(["serve", "--platform"])
        output = capsys.readouterr().out
        assert "http://127.0.0.1:8000/app/" in output
        assert (tmp_path / "setup-code").read_text().strip() in output
    finally:
        get_settings.cache_clear()


def test_workspace_execution_does_not_replace_server_webhook_reader(tmp_path, monkeypatch):
    from agentpal.demo.paypal_flow import OfflinePayPal
    verify_transport(monkeypatch)
    env = {"PAYPAL_SANDBOX_CLIENT_ID": "synthetic-server", "PAYPAL_SANDBOX_CLIENT_SECRET": "synthetic-server-private"}
    with world(tmp_path, env) as (c, d):
        token = owner(d)
        assert c.put("/v1/integrations/paypal_sandbox", headers=bearer(token), json={"fields": {
            "client_id": "synthetic-workspace", "client_secret": "synthetic-workspace-private"}}).status_code == 200
        provider = d.paypal_for("alice")
        backend = OfflinePayPal()
        provider._http.close()
        provider._http = httpx.Client(transport=httpx.MockTransport(backend.handler))
        h = bearer(token)
        r = c.post("/v1/lab/payment-requests", headers=h, json=payment())
        rid = r.json()["request_id"]
        assert c.post("/v1/payment-requests/" + rid + "/approve", headers=h,
                      json={"terms_sha256": r.json()["terms_sha256"]}).status_code == 200
        result = c.post("/v1/lab/payment-requests/" + rid + "/execute", headers=h)
        assert result.status_code == 200, result.text
        assert result.json()["payer_action_url"].startswith("https://www.sandbox.paypal.com/")
        assert backend.requests
        assert d.event_sink._paypal_provider is d.provider
        backend.approve()
        assert c.post("/v1/payment-requests/" + rid + "/capture", headers=h).status_code == 200
        assert c.post("/v1/payment-requests/" + rid + "/reconciliation", headers=h).status_code == 200
        assert d.event_sink._paypal_provider is d.provider
        assert c.get("/v1/payment-requests/" + rid, headers=h).json()["status"] == "provider_confirmed"


def test_server_configuration_refuses_live_paypal_url(tmp_path):
    with pytest.raises(ValueError, match="sandbox"):
        with world(tmp_path, {"PAYPAL_SANDBOX_CLIENT_ID": "synthetic-client",
                             "PAYPAL_SANDBOX_CLIENT_SECRET": "synthetic-private",
                             "PAYPAL_SANDBOX_BASE_URL": "https://api-m.paypal.com"}):
            pass


def test_private_key_file_atomic_and_symlink_safe(tmp_path):
    from agentpal.platform.secrets import private_file
    with ThreadPoolExecutor(8) as pool:
        values = list(pool.map(lambda _: private_file(tmp_path / "private", Fernet.generate_key()), range(8)))
    assert all(value == values[0] for value in values)
    assert len(values[0]) == 44
    (tmp_path / "link").symlink_to(tmp_path / "private")
    with pytest.raises(OSError):
        private_file(tmp_path / "link", b"unused")
