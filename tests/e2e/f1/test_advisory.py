"""Offline injected providers through the real API and deterministic analyzers."""
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json

from fastapi.testclient import TestClient
import httpx
import pytest

from agentpal.api import create_app
from agentpal.config import Settings
from agentpal.intelligence.market_check import MarketChecker
from agentpal.intelligence.page_check import KernelPageChecker
from agentpal.platform.wiring import build_platform
from agentpal.tools.browser_contracts import BrowserCapture
from agentpal.tools.channel3 import Channel3Client


@pytest.mark.parametrize("offers,price", [
    ({"@type": "AggregateOffer", "lowPrice": "89.99", "highPrice": "99.99", "priceCurrency": "USD"}, "99.99"),
    ([{"@type": "Offer", "price": "89.99", "priceCurrency": "USD"},
      {"@type": "Offer", "price": "99.99", "priceCurrency": "USD"}], "99.99"),
])
def test_offline_page_range_variant_and_owner_only_authority(tmp_path, monkeypatch, offers, price):
    monkeypatch.setattr("agentpal.tools.netguard.socket.getaddrinfo", lambda *a, **kw: [
        (2, 1, 6, "", ("93.184.216.34", 443))])
    url = "https://shop.example/product"
    html = ('<script type="application/ld+json">' + json.dumps({"@context": "https://schema.org",
        "@type": "Product", "name": "Office mouse", "offers": offers}) + '</script>').encode()
    capture = BrowserCapture("kernel", "f1-offline", url, url, (url,), 200, "Office mouse",
        html, False, hashlib.sha256(html).hexdigest(), "", None, None, datetime.now(timezone.utc))
    class Browser:
        name = "kernel"
        def configured(self):
            return True
        def capture(self, requested, *, timeout_s=30):
            assert requested == url
            return capture
    settings = Settings(db_path=str(tmp_path / "ledger.db"), auth_mode="required",
        allowed_hosts=["testserver"], analyzer_chain=["heuristic"], rate_limit_backend="memory")
    deps = build_platform(settings=settings, environ={})
    try:
        deps.owner_authenticator.create_owner("f1_page_owner")
        token, _ = deps.owner_authenticator.issue_token("f1_page_owner")
        owner = {"Authorization": "Bearer " + token}
        deps.page_checker = KernelPageChecker(Browser())
        with TestClient(create_app(service=deps.risk_service, settings=settings, platform=deps)) as client:
            reg = client.post("/v1/agents", headers=owner, json={"name": "F1 page agent"}).json()
            agent = {"Authorization": "Bearer " + reg["api_key"]}
            assert client.post("/v1/wallets", headers=owner, json={"agent_id": reg["agent"]["agent_id"],
                "currency": "USD", "monthly_budget": "200", "per_transaction_limit": "100"}).status_code == 201
            p = client.post("/v1/payment-requests", headers={**agent, "Idempotency-Key": "f1-page-variants"},
                json={"amount": price, "currency": "USD", "merchant_name": "Office shop",
                      "merchant_domain": "shop.example", "purpose": "Office mouse", "product_url": url}).json()
            path = "/v1/payment-requests/" + p["request_id"]
            assert client.post(path + "/page-check", headers=agent).status_code == 403
            check = client.post(path + "/page-check", headers=owner)
            assert check.status_code == 200
            result = check.json()
            assert "PAGE_PRICE_MISMATCH" not in {s["code"] for s in result["signals"]}
            assert result["advisory_level"] != "high_risk"
            assert result["advisory_only"] is True and result["authoritative"] is False
            after = client.get(path, headers=owner).json()
            for field in ("terms", "terms_sha256", "status", "risk_score", "recommendation"):
                assert after[field] == p[field]
            assert client.post(path + "/page-check", headers=owner,
                               json={"url": "https://shop.example.attacker.test/product"}).status_code == 422
    finally:
        deps.close()


def product(title, *, currency="USD"):
    return {"id": "f1-offline-product", "title": title, "brands": [{"name": "Apple"}], "offers": [{
        "domain": "apple.com", "url": "https://apple.com/product", "availability": "InStock",
        "condition": "new", "price": {"price": "9.99", "currency": currency}}]}


@pytest.mark.parametrize("title", ["Apple AirPods Pro 2 Ear Tips", "Apple iPhone 16 Pro Max 256GB"])
def test_accessory_and_variant_cannot_support_wrong_product_deal(title):
    payload = product(title)
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"products": [payload]}))) as http:
        checker = MarketChecker(Channel3Client("offline-placeholder", client=http))
        query = "Apple AirPods Pro 2" if "Tips" in title else "Apple iPhone 16 Pro 128GB"
        check = checker.check(amount=Decimal("300"), currency="USD", merchant_domain="apple.com", product_query=query)
    assert check["market"] is None and check["better_deals"] == []
    assert "PRICE_ABOVE_MARKET" not in {s["code"] for s in check["signals"]}


def test_currency_no_data_and_free_refresh_never_upgrade_name_identity():
    payload = product("AirPods Pro 2")
    paths = []
    def respond(request):
        paths.append(request.url.path)
        return httpx.Response(200, json={"products": [payload]} if request.url.path.endswith("search") else payload)
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        checker = MarketChecker(Channel3Client("offline-placeholder", client=http))
        initial = checker.check(amount=Decimal("300"), currency="EUR", merchant_domain="apple.com",
                                product_query="Apple AirPods Pro 2")
        refreshed = checker.check(amount=Decimal("300"), currency="EUR", merchant_domain="apple.com",
                                  product_query="Apple AirPods Pro 2", product_id="f1-offline-product")
    assert paths == ["/v1/search", "/v1/products/f1-offline-product"]
    assert refreshed["product"]["match_confidence"] == initial["product"]["match_confidence"]
    assert refreshed["product"]["match_confidence"] != "exact"
    assert refreshed["market"] is None and not refreshed["better_deals"]
    assert refreshed["advisory_level"] != "no_concern"
