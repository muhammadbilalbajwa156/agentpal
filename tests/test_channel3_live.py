"""LIVE Channel3 checks (skipped unless CHANNEL3_API_KEY is set; ~1 search credit + 1 free
product read). They assert real response structure, never specific prices."""

import os
from decimal import Decimal

import pytest

from agentpal.intelligence.market_check import MarketChecker
from agentpal.tools.channel3 import Channel3Client

pytestmark = pytest.mark.skipif(not os.environ.get("CHANNEL3_API_KEY", "").strip(),
                                reason="LIVE Channel3 test: CHANNEL3_API_KEY not set")


def test_live_search_and_free_refresh_return_real_offers():
    client = Channel3Client()
    result = client.search_products("Logitech MX Master 3S wireless mouse")
    assert result.products and result.evidence.http_status == 200
    product = result.products[0]
    assert product.id and product.title and product.offers
    assert any(o.price is not None and o.currency == "USD" for o in product.offers)
    fresh = client.get_product(product.id)
    assert fresh.products[0].id == product.id


def test_live_market_check_flags_an_overpriced_unknown_merchant():
    check = MarketChecker(Channel3Client()).check(
        amount=Decimal("999.00"), currency="USD", merchant_domain="shady-deals.example",
        product_query="Logitech MX Master 3S wireless mouse")
    assert check["product"] is not None
    assert {s["code"] for s in check["signals"]} >= {"PRICE_ABOVE_MARKET",
                                                       "MERCHANT_NOT_IN_MARKET"}
    assert check["advisory_only"] is True
