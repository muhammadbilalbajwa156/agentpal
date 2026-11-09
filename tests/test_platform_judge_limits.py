"""FX-J regressions: shared resources belong to the server, not the workspace owner.

Only provider boundaries are stubbed; HTTP authorization, policy and stores are real.
"""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from test_platform_workspace import bearer, owner, payment, verify_transport, world


def env(**changes):
    return {"CHANNEL3_API_KEY": "synthetic-server-channel", "KERNEL_API_KEY": "synthetic-server-kernel",
            "GROQ_API_KEY": "synthetic-server-groq", **changes}


class Market:
    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    def check(self, **kw):
        self.calls += 1
        if self.fail:
            raise RuntimeError("synthetic upstream failure")
        return {"provider": "channel3", "checked_at": "2026-11-09T00:00:00+00:00",
                "product": {"id": "pencil", "title": "Pencil", "brands": [], "match_confidence": "high", "offer_count": 1},
                "input": {"product_query": "pencil", "currency": "USD"}, "advisory_level": "no_data",
                "better_deals": [], "signals": [], "disclosure": "Advisory", "verify_notice": "Verify",
                "switch_notice": "Review", "discount_codes_available": False, "discount_codes_note": "None"}


@pytest.mark.parametrize("fail", [False, True])
def test_rejected_manual_proposals_share_daily_market_budget(tmp_path, fail):
    with world(tmp_path, env(AGENTPAL_SERVER_MARKET_CHECKS_DAILY="2")) as (c, d):
        h = bearer(owner(d))
        d.market_checker = market = Market(fail)
        for i in range(3):
            r = c.post("/v1/lab/payment-requests", headers=h,
                       json={**payment("51.00"), "product_query": "pencil " + str(i)})
            assert r.status_code == (201 if i < 2 else 429), r.text
            if i < 2:
                assert r.json()["recommendation"] == "block"
            else:
                assert r.json() == {"error": "server_market_checks_daily_limit"}
                assert int(r.headers["Retry-After"]) > 0
        assert market.calls == 2


def test_market_refresh_and_initial_checks_share_budget(tmp_path):
    with world(tmp_path, env(AGENTPAL_SERVER_MARKET_CHECKS_DAILY="2")) as (c, d):
        h = bearer(owner(d))
        d.market_checker = market = Market()
        r = c.post("/v1/lab/payment-requests", headers=h,
                   json={**payment("51.00"), "product_query": "pencil"})
        path = "/v1/payment-requests/" + r.json()["request_id"] + "/market-check"
        assert c.post(path, headers=h).status_code == 200
        assert c.post(path, headers=h).status_code == 429
        assert market.calls == 2


def test_page_checks_are_capped_before_worker_and_include_failures(tmp_path, monkeypatch):
    monkeypatch.setattr("agentpal.platform.payments._browser_target", lambda _: None)
    with world(tmp_path, env(AGENTPAL_SERVER_PAGE_CHECKS_DAILY="2")) as (c, d):
        h = bearer(owner(d))
        page = Market(fail=True)
        d.page_checker = page
        p = c.post("/v1/lab/payment-requests", headers=h, json=payment("51.00")).json()
        path = "/v1/payment-requests/" + p["request_id"] + "/page-check"
        for i in range(3):
            r = c.post(path, headers=h, json={"url": "https://example.com/pencil"})
            assert r.status_code == (200 if i < 2 else 429), r.text
        assert r.json() == {"error": "server_page_checks_daily_limit"}
        assert page.calls == 2


@pytest.mark.parametrize("outcome", ["failed", "blocked"])
def test_errands_charge_even_when_failed_or_blocked(tmp_path, monkeypatch, outcome):
    from agentpal.agents.shopper.core import Shopper, ShopperError
    calls = []

    def plan(self, *args):
        calls.append(1)
        if outcome == "failed":
            raise ShopperError("synthetic failure")
        return payment("51.00")

    monkeypatch.setattr(Shopper, "plan", plan)
    with world(tmp_path, env(AGENTPAL_SERVER_ERRANDS_DAILY="2")) as (c, d):
        h = bearer(owner(d))
        for i in range(3):
            r = c.post("/v1/lab/errands", headers=h, json={"task": "Buy pencils", "budget": "30.00"})
            assert r.status_code == (200 if i < 2 else 429), r.text
        assert r.json() == {"error": "server_errands_daily_limit"}
        assert len(calls) == 2


def test_daily_admission_is_durable_atomic_and_shared_across_tokens(tmp_path):
    settings = env(AGENTPAL_SERVER_MARKET_CHECKS_DAILY="2")
    with world(tmp_path, settings) as (c, d), world(tmp_path, settings) as (c2, d2):
        a = owner(d)
        a2 = d.owner_authenticator.issue_token("alice")[0]
        for dep in (d, d2):
            dep.market_checker = Market()
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda i: (c if i % 2 else c2).post(
                "/v1/lab/payment-requests", headers=bearer(a if i % 2 else a2),
                json={**payment("51.00"), "product_query": "pencil " + str(i)}), range(4)))
        assert sorted(r.status_code for r in results) == [201, 201, 429, 429]
        assert d.market_checker.calls + d2.market_checker.calls == 2
        b = owner(d, "bob")
        assert c.post("/v1/lab/payment-requests", headers=bearer(b),
                      json={**payment("51.00"), "product_query": "bob"}).status_code == 201
    with world(tmp_path, settings) as (c, d):
        d.market_checker = market = Market()
        assert c.post("/v1/lab/payment-requests", headers=bearer(a),
                      json={**payment("51.00"), "product_query": "restart"}).status_code == 429
        assert market.calls == 0


def hosted(**changes):
    return {"AGENTPAL_MODE": "hosted", "AGENTPAL_SECRETS_KEY": Fernet.generate_key().decode(),
            "PAYPAL_SANDBOX_CLIENT_ID": "synthetic-server-client",
            "PAYPAL_SANDBOX_CLIENT_SECRET": "synthetic-server-private", **changes}


def agent_wallet(c, h, monthly="500.00", hard="50.00"):
    a = c.post("/v1/agents", headers=h, json={"name": "quota test", "capabilities": [
        "wallet.read", "payment.prepare", "owner.request_decision", "payment.execute_sandbox", "payout.request"]}).json()
    r = c.post("/v1/wallets", headers=h, json={"agent_id": a["agent"]["agent_id"], "currency": "USD",
        "monthly_budget": monthly, "per_transaction_limit": hard})
    return a, r


@pytest.mark.parametrize("monthly,hard", [("500.01", "50.00"), ("500.00", "50.01")])
def test_hosted_wallet_create_and_patch_enforce_server_caps(tmp_path, monthly, hard):
    with world(tmp_path, hosted()) as (c, d):
        h = bearer(owner(d))
        _, r = agent_wallet(c, h, monthly, hard)
        assert r.status_code == 422, r.text
        assert r.json() == {"error": "hosted_amount_limit_exceeded"}
        _, r = agent_wallet(c, h)
        assert r.status_code == 201, r.text
        r = c.patch("/v1/wallets/" + r.json()["wallet_id"], headers=h,
                    json={"monthly_budget": monthly, "per_transaction_limit": hard})
        assert r.status_code == 422, r.text


@pytest.mark.parametrize("kind", ["payment", "payout"])
def test_hosted_requests_cannot_escape_cap_via_old_wallet_or_routing(tmp_path, kind):
    with world(tmp_path, hosted()) as (c, d):
        h = bearer(owner(d))
        a, r = agent_wallet(c, h)
        assert r.status_code == 201
        # A pre-existing large wallet must not bypass admission after server fallback.
        with d.wallet_store.tx() as db:
            db.execute("UPDATE platform_wallets SET monthly_budget='1000',per_transaction_hard_limit='1000'")
        body = ({**payment("50.01"), "merchant_paypal_email": "sandbox@example.com"} if kind == "payment" else
                {"amount": "50.01", "currency": "USD", "receiver_email": "sandbox@example.com", "purpose": "test"})
        r = c.post("/v1/" + kind + "-requests", headers={**bearer(a["api_key"]), "Idempotency-Key": "hosted-cap-001"}, json=body)
        assert r.status_code == 422, r.text
        assert r.json() == {"error": "hosted_amount_limit_exceeded"}
        assert not d.risk_service.store.query("SELECT * FROM provider_orders")


def test_hosted_cap_config_and_metadata(tmp_path):
    with world(tmp_path, hosted(AGENTPAL_HOSTED_MAX_AMOUNT="12.34", AGENTPAL_HOSTED_MAX_MONTHLY_BUDGET="123.45")) as (c, d):
        h = bearer(owner(d))
        _, r = agent_wallet(c, h, "123.45", "12.34")
        assert r.status_code == 201
        item = next(i for i in c.get("/v1/integrations", headers=h).json()["integrations"] if i["provider"] == "paypal_sandbox")
        assert item["hosted_limits"] == {"max_amount": "12.34", "max_monthly_budget": "123.45"}
        assert agent_wallet(c, h, "123.45", "12.35")[1].status_code == 422


def test_workspace_keys_exempt_from_shared_caps(tmp_path, monkeypatch):
    verify_transport(monkeypatch)
    with world(tmp_path, hosted(**env(AGENTPAL_SERVER_MARKET_CHECKS_DAILY="1"))) as (c, d):
        h = bearer(owner(d))
        for provider, fields in [("paypal_sandbox", {"client_id": "synthetic-own", "client_secret": "synthetic-own-private"}),
                                 ("channel3", {"api_key": "synthetic-own-channel"})]:
            assert c.put("/v1/integrations/" + provider, headers=h, json={"fields": fields}).status_code == 200
        assert agent_wallet(c, h, "1000.00", "100.00")[1].status_code == 201
        market = Market()
        monkeypatch.setattr(d, "market_for", lambda _: market)
        for _ in range(3):
            assert c.post("/v1/lab/payment-requests", headers=h,
                          json={**payment("51.00"), "product_query": "pencil"}).status_code == 201
        assert market.calls == 3
        item = next(i for i in c.get("/v1/integrations", headers=h).json()["integrations"] if i["provider"] == "paypal_sandbox")
        assert item["hosted_limits"] is None


def test_server_verification_spends_same_provider_budget(tmp_path, monkeypatch):
    calls = verify_transport(monkeypatch)
    with world(tmp_path, env(AGENTPAL_SERVER_MARKET_CHECKS_DAILY="1")) as (c, d):
        h = bearer(owner(d))
        assert c.post("/v1/integrations/channel3/verify", headers=h).status_code == 200
        assert c.post("/v1/integrations/channel3/verify", headers=h).status_code == 429
        assert len(calls) == 1


def test_quota_storage_failure_fails_closed(tmp_path, monkeypatch):
    from agentpal.ratelimit import LimiterUnavailable, SQLiteSlidingWindowLimiter
    with world(tmp_path, env()) as (c, d):
        h = bearer(owner(d))
        d.market_checker = market = Market()
        lab = c.post("/v1/lab/agent", headers=h).json()
        def unavailable(*_):
            raise LimiterUnavailable()
        monkeypatch.setattr(SQLiteSlidingWindowLimiter, "hit", unavailable)
        # Bypass only general admission so the provider quota is the boundary exercised.
        from agentpal.platform.errors import PlatformError
        with pytest.raises(PlatformError, match="server_quota_unavailable"):
            c.app.state.platform.payments._run_market_check(
                lab["agent_id"],
                "synthetic-request", SimpleNamespace(product_query="pencil", product_url=None, amount=Decimal("1"), currency="USD", merchant_domain="example.com"))
        assert market.calls == 0


def test_errand_internal_model_and_catalogue_calls_are_admitted(tmp_path, monkeypatch):
    import httpx
    from agentpal.agents.shopper.core import Shopper
    calls = []
    original_client = httpx.Client

    def handle(request):
        calls.append(request.url.host)
        return httpx.Response(200, json={})

    def plan(self, *_):
        for _ in range(3):
            self._http("groq", "POST", "/openai/v1/chat/completions", body={})
        return payment()

    monkeypatch.setattr(Shopper, "plan", plan)
    with world(tmp_path, env(AGENTPAL_SERVER_MODEL_CALLS_DAILY="2")) as (c, d):
        h = bearer(owner(d))
        monkeypatch.setattr("agentpal.platform.lab.httpx.Client", lambda **kw: original_client(
            **kw, transport=httpx.MockTransport(handle)))
        r = c.post("/v1/lab/errands", headers=h, json={"task": "Buy pencil", "budget": "3.00"})
        assert r.status_code == 429, r.text
        assert r.json() == {"error": "server_model_calls_daily_limit"}
        assert calls == ["api.groq.com", "api.groq.com"]


def test_workspace_errands_are_exempt_but_mixed_keys_are_capped(tmp_path, monkeypatch):
    from agentpal.agents.shopper.core import Shopper, ShopperError
    verify_transport(monkeypatch)

    def failed(*_):
        raise ShopperError("synthetic failure")

    monkeypatch.setattr(Shopper, "plan", failed)
    with world(tmp_path, env(AGENTPAL_SERVER_ERRANDS_DAILY="1")) as (c, d):
        h = bearer(owner(d))
        for provider in ("channel3", "groq"):
            assert c.put("/v1/integrations/" + provider, headers=h,
                         json={"fields": {"api_key": "synthetic-own-" + provider}}).status_code == 200
        for _ in range(2):
            assert c.post("/v1/lab/errands", headers=h, json={"task": "Buy pencil", "budget": "3.00"}).status_code == 200
        assert c.delete("/v1/integrations/channel3", headers=h).status_code == 204
        assert c.post("/v1/lab/errands", headers=h, json={"task": "Buy pencil", "budget": "3.00"}).status_code == 200
        assert c.post("/v1/lab/errands", headers=h, json={"task": "Buy pencil", "budget": "3.00"}).status_code == 429


@pytest.mark.parametrize("kind", ["payment", "payout"])
def test_approved_large_request_cannot_execute_after_server_fallback(tmp_path, monkeypatch, kind):
    verify_transport(monkeypatch)
    with world(tmp_path, hosted()) as (c, d):
        h = bearer(owner(d))
        assert c.put("/v1/integrations/paypal_sandbox", headers=h, json={"fields": {
            "client_id": "synthetic-own-client", "client_secret": "synthetic-own-private"}}).status_code == 200
        a, r = agent_wallet(c, h, "1000.00", "100.00")
        assert r.status_code == 201
        ah = bearer(a["api_key"])
        body = payment("75.00") if kind == "payment" else {
            "amount": "75.00", "currency": "USD", "receiver_email": "sandbox@example.com", "purpose": "test"}
        p = c.post("/v1/" + kind + "-requests", headers={**ah, "Idempotency-Key": "fallback-request-001"}, json=body)
        assert p.status_code == 201, p.text
        path = "/v1/" + kind + "-requests/" + p.json()["request_id"]
        assert c.post(path + "/approve", headers=h, json={"terms_sha256": p.json()["terms_sha256"]}).status_code == 200
        assert c.delete("/v1/integrations/paypal_sandbox", headers=h).status_code == 204
        r = c.post(path + "/execute", headers=ah)
        assert r.status_code == 422, r.text
        assert r.json() == {"error": "hosted_amount_limit_exceeded"}
        assert not d.risk_service.store.query("SELECT * FROM provider_orders")


@pytest.mark.parametrize("key,value", [("AGENTPAL_HOSTED_MAX_AMOUNT", "NaN"),
    ("AGENTPAL_HOSTED_MAX_AMOUNT", "-1"), ("AGENTPAL_HOSTED_MAX_MONTHLY_BUDGET", "0"),
    ("AGENTPAL_SERVER_PAGE_CHECKS_DAILY", "0")])
def test_invalid_resource_limits_fail_startup_closed(tmp_path, key, value):
    with pytest.raises(ValueError):
        with world(tmp_path, {key: value}):
            pass


def test_errand_key_changes_cannot_reclassify_inflight_server_calls(tmp_path, monkeypatch):
    import httpx
    from agentpal.agents.shopper.core import Shopper
    original_client = httpx.Client
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={})

    with world(tmp_path, env(AGENTPAL_SERVER_MODEL_CALLS_DAILY="1")) as (c, d):
        h = bearer(owner(d))
        def plan(self, *_):
            # The Shopper already holds the resolved server credentials. A concurrent
            # workspace replacement must not reclassify those in-flight calls as own-key.
            monkeypatch.setattr(d.integrations, "fields", lambda *_: {"api_key": "synthetic-replacement"})
            self._http("groq", "POST", "/openai/v1/chat/completions", body={})
            self._http("groq", "POST", "/openai/v1/chat/completions", body={})
            return payment()
        monkeypatch.setattr(Shopper, "plan", plan)
        monkeypatch.setattr("agentpal.platform.lab.httpx.Client", lambda **kw: original_client(
            **kw, transport=httpx.MockTransport(handle)))
        r = c.post("/v1/lab/errands", headers=h, json={"task": "Buy pencil", "budget": "3.00"})
        assert r.status_code == 429, r.text
        assert r.json() == {"error": "server_model_calls_daily_limit"}
        assert len(calls) == 1


def test_selected_server_market_checker_remains_charged_after_key_change(tmp_path, monkeypatch):
    with world(tmp_path, env(AGENTPAL_SERVER_MARKET_CHECKS_DAILY="1")) as (c, d):
        h = bearer(owner(d))
        d.market_checker = market = Market()
        original_fields = d.integrations.fields
        def resolve(_):
            monkeypatch.setattr(d.integrations, "fields", lambda owner_id, provider: {"api_key": "synthetic-replacement"} if provider == "channel3" else original_fields(owner_id, provider))
            return market
        monkeypatch.setattr(d, "market_for", resolve)
        for i in range(2):
            r = c.post("/v1/lab/payment-requests", headers=h,
                       json={**payment("51.00"), "product_query": "pencil " + str(i)})
            assert r.status_code == (201 if i == 0 else 429), r.text
        assert market.calls == 1


def test_selected_server_paypal_remains_amount_capped_after_key_change(tmp_path, monkeypatch):
    with world(tmp_path, hosted()) as (c, d):
        h = bearer(owner(d))
        a, wallet = agent_wallet(c, h)
        assert wallet.status_code == 201
        # Isolate the execution boundary with an already-approved large request.
        monkeypatch.setattr(d.resources, "check_amount", lambda *_: None)
        with d.wallet_store.tx() as db:
            db.execute("UPDATE platform_wallets SET monthly_budget='1000',per_transaction_hard_limit='1000'")
        # Create under own-key policy, then model a replacement immediately after provider selection.
        monkeypatch.setattr(d.resources, "amount_limits", lambda *_: None)
        p = c.post("/v1/payment-requests", headers={**bearer(a["api_key"]), "Idempotency-Key": "race-cap-001"}, json=payment("75.00"))
        assert p.status_code == 201, p.text
        path = "/v1/payment-requests/" + p.json()["request_id"]
        assert c.post(path + "/approve", headers=h, json={"terms_sha256": p.json()["terms_sha256"]}).status_code == 200
        monkeypatch.undo()
        def never_submit(*_, **__):
            raise AssertionError("server provider must not be called above the hosted cap")
        monkeypatch.setattr(d.provider, "create_order", never_submit)
        original = d.paypal_for
        def resolve(owner_id):
            result = original(owner_id)
            monkeypatch.setattr(d.integrations, "fields", lambda *_: {"client_id": "synthetic-own", "client_secret": "synthetic-own-private"})
            return result
        monkeypatch.setattr(d, "paypal_for", resolve)
        r = c.post(path + "/execute", headers=bearer(a["api_key"]))
        assert r.status_code == 422, r.text
        assert r.json() == {"error": "hosted_amount_limit_exceeded"}


class _GlobalCapIntegrations:
    def __init__(self, path, own=False):
        self.env = {"AGENTPAL_SERVER_PAYPAL_EXECUTIONS_DAILY": "2"}
        self.limiter_path, self.own = path, own

    def server_fields(self, provider):
        return {"client_id": "synthetic"} if provider == "paypal_sandbox" else None

    def fields(self, owner_id, provider):
        return {"client_id": "own"} if self.own else None


def test_global_server_paypal_execution_cap_is_shared_by_all_workspaces(tmp_path):
    from agentpal.platform.errors import PlatformError
    from agentpal.platform.shared_resources import SharedResources
    res = SharedResources(_GlobalCapIntegrations(str(tmp_path / "limits.db")), hosted=True)
    res.consume_execution("alice", server_provided=True)
    res.consume_execution("bob", server_provided=True)
    with pytest.raises(PlatformError) as exc:
        res.consume_execution("carol", server_provided=True)
    assert exc.value.status == 429 and exc.value.code == "server_paypal_global_daily_limit"


def test_global_cap_ignores_own_keys_and_self_hosted(tmp_path):
    from agentpal.platform.shared_resources import SharedResources
    own = SharedResources(_GlobalCapIntegrations(str(tmp_path / "a.db"), own=True), hosted=True)
    local = SharedResources(_GlobalCapIntegrations(str(tmp_path / "b.db")), hosted=False)
    for _ in range(5):
        own.consume_execution("alice", server_provided=False)
        local.consume_execution("alice", server_provided=True)
