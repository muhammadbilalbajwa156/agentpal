"""Market check and better-deal logic on REAL (sanitized) Channel3 data. Advisory only."""

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from agentpal.intelligence.market_check import (
    DISCLOSURE,
    MarketChecker,
    build_market_check,
    match_product,
)
from agentpal.tools.channel3 import Channel3Client, Channel3Error, Channel3Result, Offer, Product

RAW = (Path(__file__).parent / "fixtures" / "channel3" / "search_mx_master_3s.json").read_bytes()
NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
QUERY = "Logitech MX Master 3S wireless mouse"


def live_like_result():
    c = Channel3Client("k" * 20, client=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=RAW))))
    return c.search_products(QUERY)


def check(amount, merchant="shady-deals.example", query=QUERY, result=None, error=None,
          currency="USD"):
    return build_market_check(amount=Decimal(amount), currency=currency,
                              merchant_domain=merchant, query=query, product_url=None,
                              result=live_like_result() if result is None and error is None
                              else result, error=error, checked_at=NOW)


def codes(c):
    return {(s["code"], s["severity"]) for s in c["signals"]}


def test_overpriced_unknown_merchant_is_high_risk_with_cheaper_same_product_deals():
    c = check("250.00")
    assert c["advisory_level"] == "high_risk"
    assert c["product"]["id"] == "nLZKmNT" and c["product"]["match_confidence"] == "high"
    # new + in-stock only: backmarket.com has no condition and is excluded
    assert c["market"] == {"currency": "USD", "offers_compared": 3, "low": "99.99",
                           "median": "108.62", "high": "113.99", "claimed_vs_median": "2.30"}
    assert ("PRICE_ABOVE_MARKET", "high") in codes(c)
    assert ("MERCHANT_NOT_IN_MARKET", "warning") in codes(c)
    assert [d["retailer"] for d in c["better_deals"]] == ["officedepot.com", "bestbuy.com",
                                                          "6ave.com"]
    assert c["better_deals"][0]["you_save"] == "150.01"
    assert all(d["condition"] == "new" for d in c["better_deals"])


@pytest.mark.parametrize("amount,expected", [("108.62", None), ("135.00", None),
                                             ("135.78", "warning"), ("162.92", "warning"),
                                             ("162.93", "high"), ("54.31", "high"),
                                             ("54.32", None)])
def test_price_ratio_tiers_and_boundaries(amount, expected):
    c = check(amount, merchant=None)
    above = {s["severity"] for s in c["signals"] if s["code"] == "PRICE_ABOVE_MARKET"}
    below = {s["severity"] for s in c["signals"] if s["code"] == "PRICE_FAR_BELOW_MARKET"}
    assert (above | below) == ({expected} if expected else set())


def test_too_good_to_be_true_price_is_flagged():
    c = check("40.00", merchant=None)
    assert ("PRICE_FAR_BELOW_MARKET", "high") in codes(c) and c["better_deals"] == []


def test_listed_merchant_with_padded_price_is_a_mismatch_and_own_offer_is_not_a_deal():
    c = check("130.00", merchant="bestbuy.com")
    assert c["merchant_offer"]["domain"] == "bestbuy.com" and c["merchant_offer"]["price"] == "108.62"
    assert ("MERCHANT_PRICE_MISMATCH", "high") in codes(c)
    assert "bestbuy.com" not in [d["retailer"] for d in c["better_deals"]]
    assert ("MERCHANT_NOT_IN_MARKET", "warning") not in codes(c)


def test_fair_price_at_cheapest_listed_merchant_has_no_concern():
    c = check("99.99", merchant="officedepot.com")
    assert c["advisory_level"] == "no_concern" and c["better_deals"] == []
    assert codes(c) == set()


def test_subdomains_match_by_registrable_domain():
    c = check("99.99", merchant="www.officedepot.com")
    assert c["merchant_offer"]["domain"] == "officedepot.com"


def test_model_tokens_must_match_so_3s_never_matches_plain_master():
    p3s = Product("p1", "Logitech MX Master 3S Mouse", ("Logitech",), ())
    p3 = Product("p2", "Logitech MX Master 3 Mouse", ("Logitech",), ())
    plain = Product("p3", "Logitech MX Master Wireless Mouse", ("Logitech",), ())
    assert match_product([p3, plain], "Logitech MX Master 3S", None) == (None, "none")
    assert match_product([p3, plain, p3s], "Logitech MX Master 3S", None)[0] is p3s


def test_prefers_candidate_the_claimed_merchant_sells():
    a = Product("a", "Sony WH-1000XM5 Headphones", ("Sony",), (Offer("x.com", Decimal(300), "USD", "InStock", "new", None, None, None),))
    b = Product("b", "Sony WH-1000XM5 Headphones Black", ("Sony",), (Offer("target.com", Decimal(330), "USD", "InStock", "new", None, None, None),))
    assert match_product([a, b], "Sony WH-1000XM5 headphones", "target.com")[0] is b


def deal_world(merchant_condition):
    offers = (
        Offer("agent-shop.com", Decimal("200"), "USD", "InStock", merchant_condition, None, "https://buy.trychannel3.com/1", None),
        Offer("cheap-used.com", Decimal("90"), "USD", "InStock", "used", None, "https://buy.trychannel3.com/2", None),
        Offer("unknown.com", Decimal("95"), "USD", "InStock", None, None, "https://buy.trychannel3.com/3", None),
        Offer("oos.com", Decimal("100"), "USD", "OutOfStock", "new", None, "https://buy.trychannel3.com/4", None),
        Offer("eur.com", Decimal("80"), "EUR", "InStock", "new", None, "https://buy.trychannel3.com/5", None),
        Offer("tiny.com", Decimal("199"), "USD", "InStock", "new", None, "https://buy.trychannel3.com/6", None),
        Offer("sale.com", Decimal("150"), "USD", "InStock", "new", Decimal("190"), "https://buy.trychannel3.com/7", None),
        Offer("good.com", Decimal("160"), "USD", "InStock", "new", None, "https://buy.trychannel3.com/8", None),
    )
    from agentpal.contracts import SourceEvidence
    ev = SourceEvidence(url="https://api.trychannel3.com/v1/products/p", retrieved_at=NOW,
                        content_sha256="0" * 64, method="search_result")
    return Channel3Result((Product("p", "Gadget X100", (), offers),), ev, "product")


def test_deals_are_same_currency_in_stock_new_meaningful_and_show_sales():
    c = check("200.00", merchant="agent-shop.com", query="Gadget X100", result=deal_world("new"))
    deals = c["better_deals"]
    assert [d["retailer"] for d in deals] == ["sale.com", "good.com"]   # used/unknown/oos/EUR/tiny excluded
    assert deals[0] == {"retailer": "sale.com", "price": "150.00", "currency": "USD",
                        "condition": "new", "availability": "InStock", "you_save": "50.00",
                        "you_save_pct": "25.0", "buy_url": "https://buy.trychannel3.com/7", "on_sale": True,
                        "was_price": "190.00", "sale_discount_pct": "21.1"}


def test_secondhand_purchase_may_compare_with_secondhand_offers_and_is_flagged():
    c = check("200.00", merchant="agent-shop.com", query="Gadget X100", result=deal_world("used"))
    assert ("CONDITION_NOT_NEW_AT_MERCHANT", "warning") in codes(c)
    assert "cheap-used.com" in [d["retailer"] for d in c["better_deals"]]


def test_unavailable_market_data_never_blocks_and_is_labelled():
    c = check("250.00", error=Channel3Error("credits_exhausted", "402"))
    assert c["advisory_level"] == "no_data"
    assert c["signals"][0]["code"] == "MARKET_DATA_UNAVAILABLE"
    assert c["signals"][0]["data"] == {"reason": "credits_exhausted"}


def test_no_match_reports_candidates_without_comparing():
    c = check("250.00", query="Garmin Fenix 8 watch")
    assert c["advisory_level"] == "no_match" and c["product"] is None
    assert c["better_deals"] == [] and c["market"] is None


def test_every_check_is_advisory_and_carries_disclosure_and_honesty_notes():
    for c in (check("250.00"), check("99.99", merchant="officedepot.com"),
              check("1.00", error=Channel3Error("auth", "401"))):
        assert c["advisory_only"] is True and c["authoritative"] is False
        assert c["disclosure"] == DISCLOSURE
        assert c["discount_codes_available"] is False
        assert "new payment request" in c["switch_notice"]
        assert "AI agents can make mistakes" in c["verify_notice"]
        assert not ({"approve", "approved", "recommendation", "risk_score"} & set(c))


def test_checker_never_raises_even_on_unexpected_errors():
    class Broken:
        _clock = staticmethod(lambda: NOW)
        def search_products(self, q):
            raise ZeroDivisionError
    c = MarketChecker(Broken()).check(amount=Decimal("10"), currency="USD",
                                      merchant_domain=None, product_query="x y")
    assert c["advisory_level"] == "no_data"


def test_checker_routes_url_to_lookup_and_id_to_free_refresh():
    seen = []
    def handler(request):
        seen.append((request.method, request.url.path))
        product = json.loads(RAW)["products"][0]
        return httpx.Response(200, json={"product": product} if request.url.path.endswith(
            "lookup") else product)
    client = Channel3Client("k" * 20, client=httpx.Client(transport=httpx.MockTransport(handler)))
    m = MarketChecker(client, clock=lambda: NOW)
    a = m.check(amount=Decimal("99.99"), currency="USD", merchant_domain="officedepot.com",
                product_url="https://www.officedepot.com/a/products/1/")
    b = m.check(amount=Decimal("99.99"), currency="USD", merchant_domain="officedepot.com",
                product_id="nLZKmNT")
    assert seen == [("POST", "/v1/lookup"), ("GET", "/v1/products/nLZKmNT")]
    assert a["product"]["match_confidence"] == b["product"]["match_confidence"] == "exact"
    assert json.dumps(json.loads(json.dumps(a)))  # JSON-safe


# --------------------------------------------------- Codex review fixes (2026-11-01)
def _p(pid, title, brands=(), offers=()):
    return Product(pid, title, tuple(brands), tuple(offers))


@pytest.mark.parametrize("query,titles,brand", [
    ("Google Pixel 8 phone", ["Google Pixel 9 phone"], "Google"),
    ("Logitech MX Master 3S wireless mouse",
     ["Logitech MX Master 3S Wireless Mouse Protective Case"], "Logitech"),
    ("Apple iPhone 15 Pro Max 256GB", ["Apple iPhone 15 Pro 256GB"], "Apple"),
    ("Apple iPhone 15 Pro", ["Apple iPhone 15 Pro Max"], "Apple"),
    ("Apple wireless keyboard", ["Dell wireless keyboard"], "Dell"),
    ("Samsung Galaxy S24 512GB", ["Samsung Galaxy S24 256GB"], "Samsung"),
])
def test_different_products_are_never_treated_as_the_same(query, titles, brand):
    products = [_p(f"p{i}", t, (brand,)) for i, t in enumerate(titles)]
    assert match_product(products, query, None) == (None, "none")


def test_correct_product_still_matches_with_high_confidence():
    real = _p("ok", "Apple iPhone 15 Pro Max 256GB Natural Titanium", ("Apple",))
    decoys = [_p("a", "Apple iPhone 15 Pro 256GB", ("Apple",)),
              _p("b", "Apple iPhone 15 Pro Max Silicone Case", ("Apple",))]
    assert match_product(decoys + [real], "Apple iPhone 15 Pro Max 256GB", None) == (real, "high")


def test_approximate_match_never_makes_price_or_deal_claims():
    from agentpal.contracts import SourceEvidence
    ev = SourceEvidence(url="https://api.trychannel3.com/v1/search", retrieved_at=NOW,
                        content_sha256="0" * 64, method="search_result")
    offers = (Offer("cheap.com", Decimal("10"), "USD", "InStock", "new", None, "u", None),)
    # generic query (no model token) -> at most medium confidence
    result = Channel3Result((_p("g", "Wireless Mouse Black", ("Acme",), offers),), ev, "search")
    c = check("250.00", query="Acme wireless mouse", result=result)
    assert c["advisory_level"] == "no_match"
    assert {s["code"] for s in c["signals"]} == {"LOW_CONFIDENCE_MATCH"}
    assert c["better_deals"] == [] and c["market"] is None


def test_unknown_availability_is_neither_compared_nor_recommended():
    world = deal_world("new")
    product = world.products[0]
    offers = tuple(o if o.domain != "good.com" else Offer("good.com", Decimal("160"), "USD",
                   None, "new", None, "https://buy.trychannel3.com/8", None) for o in product.offers)
    result = Channel3Result((Product("p", product.title, (), offers),), world.evidence, "product")
    c = check("200.00", merchant="agent-shop.com", query="Gadget X100", result=result)
    assert "good.com" not in [d["retailer"] for d in c["better_deals"]]
    # explicit new + InStock + USD: agent-shop, tiny, sale (good.com would make it 4)
    assert c["market"]["offers_compared"] == 3


def test_secondhand_purchase_never_invents_new_condition_for_unknown_offers():
    world = deal_world("refurbished")
    product = world.products[0]
    extra = (Offer("refurb.com", Decimal("120"), "USD", "InStock", "refurbished", None, "https://buy.trychannel3.com/9", None),)
    result = Channel3Result((Product("p", product.title, (), product.offers + extra),),
                            world.evidence, "product")
    c = check("200.00", merchant="agent-shop.com", query="Gadget X100", result=result)
    deals = {d["retailer"]: d["condition"] for d in c["better_deals"]}
    assert "unknown.com" not in deals and "cheap-used.com" not in deals   # unknown / used
    assert deals.get("refurb.com") == "refurbished"
    assert all(cond in ("new", "refurbished") for cond in deals.values())


def test_cached_search_refreshes_live_offers_through_free_product_endpoint():
    product = json.loads(RAW)["products"][0]
    state = {"price": 99.99}
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        if request.url.path == "/v1/search":
            return httpx.Response(200, content=RAW)
        fresh = json.loads(json.dumps(product))
        fresh["offers"][0]["price"]["price"] = state["price"]
        return httpx.Response(200, json=fresh)
    m = MarketChecker(Channel3Client("k" * 20, client=httpx.Client(
        transport=httpx.MockTransport(handler))), clock=lambda: NOW)
    m.check(amount=Decimal("250"), currency="USD", merchant_domain=None, product_query=QUERY)
    state["price"] = 300.00                     # the retailer changed its price
    c = m.check(amount=Decimal("250"), currency="USD", merchant_domain=None,
                product_query=QUERY)
    assert seen == [("POST", "/v1/search"), ("GET", "/v1/products/nLZKmNT")]
    assert "officedepot.com" not in [d["retailer"] for d in c["better_deals"]]


def test_analysis_failure_degrades_to_no_data(monkeypatch):
    import agentpal.intelligence.market_check as mc
    real = mc.build_market_check

    def flaky(*, result, error, **kw):
        if result is not None:
            raise ArithmeticError("malformed data")
        return real(result=result, error=error, **kw)
    monkeypatch.setattr(mc, "build_market_check", flaky)
    m = MarketChecker(Channel3Client("k" * 20, client=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=RAW)))), clock=lambda: NOW)
    c = m.check(amount=Decimal("10"), currency="USD", merchant_domain=None, product_query=QUERY)
    assert c["advisory_level"] == "no_data"


def test_deal_link_must_be_channel3_redirect_or_the_retailers_own_domain():
    """E2E-H3-1 (lead 2026-11-05): links are untrusted upstream data."""
    from agentpal.intelligence.market_check import _safe_buy_url
    assert _safe_buy_url("https://buy.trychannel3.com/abc", "bestbuy.com")
    assert _safe_buy_url("https://www.bestbuy.com/site/x", "bestbuy.com")
    for bad in ("http://www.bestbuy.com/x", "https://bestbuy.com.evil.example/x",
                "https://evil.example/x", "https://u:p@bestbuy.com/x", "https://bestbuy.com:8443/x",
                "https://127.0.0.1/x", "javascript:alert(1)", "https://bestbuy.com/\nx", None):
        assert _safe_buy_url(bad, "bestbuy.com") is None, bad
