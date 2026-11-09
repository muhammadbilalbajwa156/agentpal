"""Channel3 market check through the real /v1 HTTP API (mocked Channel3 transport)."""

import json
from pathlib import Path

import httpx
import pytest

from agentpal.intelligence.market_check import DISCLOSURE, MarketChecker
from agentpal.tools.channel3 import Channel3Client
from test_platform_fixtures import ALL_PAY, OWNER_A, OWNER_B, build_world

RAW = (Path(__file__).parent / "fixtures" / "channel3" / "search_mx_master_3s.json").read_bytes()
PRODUCT = json.loads(RAW)["products"][0]
OVERPRICED = dict(amount="250.00", merchant_name="Shady Deals",
                  merchant_domain="shady-deals.example", purpose="Office mouse",
                  product_query="Logitech MX Master 3S wireless mouse")


class FakeChannel3:
    def __init__(self, status=200):
        self.status, self.calls = status, []

    def __call__(self, request):
        self.calls.append((request.method, request.url.path))
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": "x"})
        if request.url.path == "/v1/search":
            return httpx.Response(200, content=RAW)
        if request.url.path == "/v1/lookup":
            return httpx.Response(200, json={"product": PRODUCT})
        return httpx.Response(200, json=PRODUCT)


def world(status=200, enabled=True):
    fake = FakeChannel3(status)
    checker = MarketChecker(Channel3Client("k" * 20, client=httpx.Client(
        transport=httpx.MockTransport(fake)))) if enabled else None
    w = build_world(market_checker=checker)
    agent_id, key, _ = w.funded_agent(caps=ALL_PAY, monthly_budget="1000.00",
                                      per_transaction_limit="500.00")
    return w, fake, agent_id, key


def test_agent_request_carries_market_check_and_owner_sees_it_before_approving():
    w, fake, _, key = world()
    r = w.pay(key, **OVERPRICED)
    assert r.status_code == 201, r.text
    mc = r.json()["market_check"]
    assert mc["advisory_level"] == "high_risk" and mc["product"]["id"] == "nLZKmNT"
    assert {s["code"] for s in mc["signals"]} >= {"PRICE_ABOVE_MARKET", "MERCHANT_NOT_IN_MARKET",
                                                    "BETTER_PRICE_AVAILABLE"}
    assert mc["better_deals"][0]["retailer"] == "officedepot.com"
    assert mc["disclosure"] == DISCLOSURE and mc["advisory_only"] is True
    rid = r.json()["request_id"]
    owner_view = w.client.get(f"/v1/payment-requests/{rid}", headers=w.h(OWNER_A)).json()
    approvals = w.client.get("/v1/approvals", headers=w.h(OWNER_A)).json()
    (listed,) = [p for p in approvals["payment_requests"] if p["request_id"] == rid]
    assert owner_view["market_check"] == mc
    assert listed["market_check"] == mc          # visible in the owner's approval queue
    assert fake.calls == [("POST", "/v1/search")]


def test_market_check_changes_nothing_about_risk_terms_or_approval_state():
    # identical first request in two fresh worlds (a second request in the SAME world
    # would trip the risk engine's repeat/velocity rules, which is unrelated)
    w, _, _, key = world()
    with_check = w.pay(key, **OVERPRICED).json()
    w2, _, _, key2 = world(enabled=False)
    without = w2.pay(key2, **OVERPRICED).json()
    assert with_check["market_check"]["advisory_level"] == "high_risk"
    assert without["market_check"] is None
    for field in ("recommendation", "risk_score", "status", "requires_owner_approval",
                  "is_real_money"):
        assert with_check[field] == without[field], field
    assert with_check["terms"]["amount"] == without["terms"]["amount"]
    assert "product_query" not in with_check["terms"]


def test_idempotent_replay_returns_same_check_without_spending_a_credit():
    w, fake, _, key = world()
    first = w.pay(key, idem="mc-replay-0001", **OVERPRICED)
    again = w.pay(key, idem="mc-replay-0001", **OVERPRICED)
    assert first.status_code == 201 and again.status_code == 200
    assert again.json()["market_check"] == first.json()["market_check"]
    assert len(fake.calls) == 1


@pytest.mark.parametrize("status", [401, 402, 429, 500])
def test_channel3_failure_never_blocks_the_payment(status):
    w, _, _, key = world(status=status)
    r = w.pay(key, **OVERPRICED)
    assert r.status_code == 201
    mc = r.json()["market_check"]
    assert mc["advisory_level"] == "no_data"
    assert mc["signals"][0]["code"] == "MARKET_DATA_UNAVAILABLE"


def test_disabled_or_unrequested_check_makes_no_call():
    w, fake, _, key = world(enabled=False)
    assert w.pay(key, **OVERPRICED).json()["market_check"] is None
    w, fake, _, key = world()
    assert w.pay(key).json()["market_check"] is None and fake.calls == []


def test_product_url_uses_exact_lookup():
    w, fake, _, key = world()
    body = {**OVERPRICED, "product_query": None,
            "product_url": "https://www.officedepot.com/a/products/123/"}
    mc = w.pay(key, **body).json()["market_check"]
    assert fake.calls == [("POST", "/v1/lookup")]
    assert mc["product"]["match_confidence"] == "exact"


@pytest.mark.parametrize("change", [{"product_query": "x" * 201}, {"product_query": "a"},
                                    {"product_url": "ftp://shop.example/x"},
                                    {"product_url": "javascript:alert(1)"}])
def test_invalid_product_fields_are_refused(change):
    w, fake, _, key = world()
    assert w.pay(key, **{**OVERPRICED, **change}).status_code == 422
    assert fake.calls == []


def test_owner_refresh_uses_the_free_product_endpoint_and_is_owner_scoped():
    w, fake, _, key = world()
    rid = w.pay(key, **OVERPRICED).json()["request_id"]
    path = f"/v1/payment-requests/{rid}/market-check"
    assert w.client.post(path, headers=w.h(key)).status_code in (401, 403)
    assert w.client.post(path, headers=w.h(OWNER_B)).status_code == 404
    r = w.client.post(path, headers=w.h(OWNER_A))
    assert r.status_code == 200, r.text
    # Lead decision 2026-11-05 (E2E-T1-005 / E2E-T3-5): refreshing a NAME match re-reads
    # live offers but keeps the name-match confidence; only a URL lookup is "exact".
    assert r.json()["product"]["match_confidence"] == "high"
    assert fake.calls[-1] == ("GET", "/v1/products/nLZKmNT")
    view = w.client.get(f"/v1/payment-requests/{rid}", headers=w.h(OWNER_A)).json()
    assert view["market_check"]["evidence"]["endpoint"] == "product"


def test_refresh_without_a_matched_product_or_checker_is_refused():
    w, _, _, key = world()
    rid = w.pay(key).json()["request_id"]      # no product named: nothing to refresh
    assert w.client.post(f"/v1/payment-requests/{rid}/market-check",
                         headers=w.h(OWNER_A)).json() == {"error": "market_check_unavailable"}
    w, _, _, key = world(enabled=False)
    rid = w.pay(key).json()["request_id"]
    assert w.client.post(f"/v1/payment-requests/{rid}/market-check",
                         headers=w.h(OWNER_A)).status_code == 503


def test_approval_still_requires_the_owner_and_exact_terms():
    w, _, _, key = world()
    p = w.pay(key, **OVERPRICED).json()
    assert p["status"] in ("awaiting_approval", "prepared")
    bad = w.client.post(f"/v1/payment-requests/{p['request_id']}/approve",
                        json={"terms_sha256": "0" * 64}, headers=w.h(OWNER_A))
    assert bad.status_code == 409


def test_a_crashing_checker_never_blocks_the_payment():
    class Crashing:
        def check(self, **kw):
            raise RuntimeError("checker bug")
    w = build_world(market_checker=Crashing())
    _, key, _ = w.funded_agent(caps=ALL_PAY, monthly_budget="1000.00",
                               per_transaction_limit="500.00")
    r = w.pay(key, **OVERPRICED)
    assert r.status_code == 201 and r.json()["market_check"] is None


def test_crash_recovery_retry_of_the_same_claim_reuses_the_first_check():
    """A worker that dies after the market check but before preparation is retried with
    the SAME request id; it must not spend another credit or replace the evidence."""
    from agentpal.platform.models import PaymentCreate
    w, fake, agent_id, _ = world()
    desk = w.platform.payments
    body = PaymentCreate(**{"currency": "USD", **OVERPRICED})
    desk._run_market_check(agent_id, "pay_" + "1" * 32, body)
    first = desk.market_check("pay_" + "1" * 32)
    desk._run_market_check(agent_id, "pay_" + "1" * 32, body)
    assert len(fake.calls) == 1
    assert desk.market_check("pay_" + "1" * 32) == first


def test_python_sdk_sends_product_query_and_owner_refreshes():
    import asyncio
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk" / "python"))
    from agentpal_sdk import AgentClient, OwnerClient
    w, fake, _, key = world()

    async def run():
        transport = httpx.ASGITransport(app=w.app)
        async with AgentClient(base_url="http://127.0.0.1", credential=key,
                               transport=transport) as agent:
            created = await agent.create_payment_request(
                amount="250.00", currency="USD", merchant_name="Shady Deals",
                purpose="Office mouse", merchant_domain="shady-deals.example",
                product_query="Logitech MX Master 3S wireless mouse")
        async with OwnerClient(base_url="http://127.0.0.1", credential=OWNER_A,
                               transport=transport) as owner:
            refreshed = await owner.refresh_market_check(created["request_id"])
        return created, refreshed
    created, refreshed = asyncio.run(run())
    assert created["market_check"]["advisory_level"] == "high_risk"
    assert refreshed["evidence"]["endpoint"] == "product"
    assert fake.calls == [("POST", "/v1/search"), ("GET", "/v1/products/nLZKmNT")]


def test_retry_after_a_failed_preparation_reuses_the_check_without_a_new_credit():
    """Codex review finding 7: preparation fails AFTER the market check, the claim is
    released, and the agent retries the same idempotency key (new request id)."""
    w, fake, _, key = world()
    desk = w.platform.payments
    real = desk.wallet_service
    state = {"fail": True}

    class Flaky:
        def __init__(self, inner):
            self.inner = inner

        def prepare_payment(self, ctx, draft):
            if state.pop("fail", False):
                raise RuntimeError("worker died mid-preparation")
            return self.inner.prepare_payment(ctx, draft)

        def __getattr__(self, name):
            return getattr(self.inner, name)
    desk.wallet_service = lambda: Flaky(real())
    with pytest.raises(RuntimeError):          # TestClient re-raises the simulated crash
        w.pay(key, idem="mc-crash-0001", **OVERPRICED)
    again = w.pay(key, idem="mc-crash-0001", **OVERPRICED)
    assert again.status_code == 201, again.text
    assert again.json()["market_check"]["advisory_level"] == "high_risk"
    assert len(fake.calls) == 1
    other = w.pay(key, idem="mc-other-0002", **OVERPRICED)   # different request: fresh check
    assert other.json()["market_check"] is not None
