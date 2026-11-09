"""Channel3-powered MARKET CHECK and BETTER-DEAL finder for agent purchase requests.

Before an owner approves a payment that an AI agent prepared, AgentPal compares the agent's
claimed purchase (product, merchant, amount, currency) with live catalogue data from
Channel3 (100M+ products, offers from many retailers) and tells the owner:

* fraud and mistake signals: overpriced vs the market, suspiciously cheap, merchant does
  not list the product, the merchant's own listing differs from the claimed price, out of
  stock, used/refurbished at the merchant;
* better deals: the SAME product (one Channel3 product = one canonical item across
  retailers), same currency, same or better condition, in stock, cheaper elsewhere, with
  sale prices (`compare_at_price`) shown when present.

ADVISORY ONLY (rules 1, 8, 12): this never approves, unblocks, lowers risk or changes
terms. A cheaper offer is a suggestion; switching merchant means a NEW payment request
and a NEW owner approval. Channel3 data is third-party evidence, not truth. Discount
codes are NOT part of Channel3's data, and none are invented.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from statistics import median

from .. import textnorm
from ..tools.channel3 import Channel3Client, Channel3Error, Channel3Result, Offer, Product

DISCLOSURE = "We may earn a commission when you buy through links on this page."
VERIFY_NOTICE = ("Prices and stock come from Channel3's retailer catalogue and can change. "
                 "Open the retailer's site and check the price, seller, condition, shipping "
                 "and return terms yourself before buying. AI agents can make mistakes.")
SWITCH_NOTICE = ("AgentPal never switches merchants automatically: buying elsewhere needs a "
                 "new payment request and a new owner approval of its exact terms.")
NO_CODES_NOTICE = ("Channel3's data contains sale prices (compare_at_price) but no discount "
                   "or coupon codes, so none are shown.")

ABOVE_WARNING = Decimal("1.25")   # amount / market median
ABOVE_HIGH = Decimal("1.50")
BELOW_HIGH = Decimal("0.50")      # "too good to be true"
MERCHANT_PRICE_TOLERANCE = Decimal("0.02")
MIN_SAVING_ABS = Decimal("1.00")
MIN_SAVING_PCT = Decimal("0.03")
MAX_DEALS = 3
_STOP = {"the", "a", "an", "and", "or", "for", "with", "of", "to", "in", "by", "new"}
_SEVERITY_RANK = {"info": 0, "warning": 1, "high": 2}


def _q(d: Decimal | None) -> str | None:
    return None if d is None else str(d.quantize(Decimal("0.01"), ROUND_HALF_UP))


def _domain(host: str | None) -> str | None:
    if not host:
        return None
    host = host.lower().strip().removeprefix("www.")
    return textnorm.registrable_domain(host) or host


# E2E-H3-1: buy links are untrusted upstream data. Real Channel3 links go through its
# commission redirect host (hence the disclosure); otherwise the link must belong to the
# deal's own retailer. Anything else keeps the deal but drops the actionable link.
_CHANNEL3_REDIRECT_HOSTS = frozenset({"buy.trychannel3.com"})


def _safe_buy_url(url: str | None, retailer: str | None) -> str | None:
    # A strict pattern instead of a URL library (this package performs no I/O and imports
    # no networking modules): https only, a plain host (no userinfo, no port), no spaces or
    # control characters anywhere.
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) < 33 or ord(c) == 127
                                                            for c in url):
        return None
    m = re.fullmatch(r"https://([A-Za-z0-9.-]+)(?:[/?#].*)?", url)
    if m is None:
        return None
    host = m.group(1).lower().rstrip(".")
    if not host or re.fullmatch(r"[0-9.]+", host):
        return None
    if host in _CHANNEL3_REDIRECT_HOSTS:
        return url
    return url if retailer and _domain(host) == retailer else None


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOP}


def _model_tokens(text: str) -> set[str]:
    """Tokens that identify a model: any token containing a digit, single digits included
    ('8' in 'Pixel 8', '15', '3s', 'wh1000xm5')."""
    return {t for t in _tokens(text) if any(c.isdigit() for c in t)}


# Variant words change WHICH product it is (iPhone 15 Pro vs Pro Max). They must match
# in both directions. Accessory words mean the listing is a different item (a case FOR
# the product); they must not appear in the title unless the query asked for them.
_VARIANT = {"pro", "max", "mini", "plus", "ultra", "lite", "se", "xl", "xs", "air", "slim",
            "fold", "flip", "fe", "neo"}
_ACCESSORY = {"case", "cover", "sleeve", "skin", "protector", "charger", "cable", "adapter",
              "stand", "mount", "strap", "band", "replacement", "refill", "bundle", "kit",
              "pack", "compatible", "holder", "dock", "pad", "battery", "parts",
              # E2E-T3-1 (live): "AirPods Pro 2 Ear Tips" matched earbuds at $9.99.
              "tip", "tips", "eartip", "eartips", "cushion", "cushions", "earpad", "earpads",
              "foam", "hook", "hooks", "wing", "wings", "pouch", "bag", "lanyard", "keychain",
              "cord", "film", "glass", "sticker", "decal", "spare", "accessory", "accessories"}
_CAPACITY = re.compile(r"^\d+(gb|tb|mb|mm|cm|in|inch|ml|l|oz)$")


def _identity_ok(q: set[str], t: set[str], brands: Sequence[str]) -> tuple[bool, bool]:
    """(same_identity, brand_matched). Conservative: when in doubt it is NOT the same."""
    if not _model_tokens(" ".join(q)) <= t:
        return False, False
    if (q & _VARIANT) != (t & _VARIANT):
        return False, False
    if (t & _ACCESSORY) - q:
        return False, False
    if not {x for x in q if _CAPACITY.match(x)} <= t:
        return False, False
    brand_tokens = set()
    for b in brands:
        brand_tokens |= _tokens(b)
    if brand_tokens and not (brand_tokens & q):
        return False, False
    return True, bool(brand_tokens & q)


def match_product(products: Sequence[Product], query: str, merchant_domain: str | None,
                  *, exact: bool = False) -> tuple[Product | None, str]:
    """Pick the canonical product the request is about. Returns (product, confidence).

    exact: the product came from a URL lookup or product read (Channel3 resolved it).
    Otherwise a candidate must: share >= 60% of the query's tokens; contain every model
    token (so 'Pixel 8' never matches 'Pixel 9', 'MX Master 3S' never '3'); have exactly
    the query's variant words (15 Pro is not 15 Pro Max); not be an accessory the query
    did not ask for (a case for the mouse is not the mouse); contain any capacity the
    query names; and share the product's brand when Channel3 lists one.
    'high' requires >= 80% overlap, a brand match and a model token in the query; only
    exact/high matches are used for price comparisons and deal claims."""
    if exact and products:
        return products[0], "exact"
    q_tokens = _tokens(query)
    if not q_tokens:
        return None, "none"
    want = _domain(merchant_domain)
    candidates = []
    for rank, p in enumerate(products):
        t = _tokens(p.title + " " + " ".join(p.brands))
        overlap = len(q_tokens & t) / len(q_tokens)
        same, brand_hit = _identity_ok(q_tokens, t, p.brands)
        if overlap >= 0.6 and same:
            sells = want is not None and any(_domain(o.domain) == want for o in p.offers)
            strong = overlap >= 0.8 and brand_hit and bool(_model_tokens(query))
            candidates.append((not strong, not sells, -overlap, rank, p))
    if not candidates:
        return None, "none"
    candidates.sort(key=lambda c: c[:4])
    best = candidates[0]
    return best[4], ("medium" if best[0] else "high")


def _is_new(o: Offer) -> bool:
    """Only offers EXPLICITLY marked new are compared as new. Unknown condition is not
    assumed new (real data: a refurbished marketplace listed this field empty)."""
    return (o.condition or "").lower() == "new"


def _is_secondhand(o: Offer) -> bool:
    return (o.condition or "").lower() in ("used", "refurbished")


def _in_stock(o: Offer) -> bool:
    """Only offers EXPLICITLY reported InStock are compared or recommended."""
    return o.availability == "InStock"


def build_market_check(*, amount: Decimal, currency: str, merchant_domain: str | None,
                       query: str | None, product_url: str | None,
                       result: Channel3Result | None, error: Channel3Error | None,
                       checked_at: datetime, identity_from_search: bool = False) -> dict:
    """Deterministic analysis. JSON-safe dict (money as decimal strings)."""
    currency = currency.upper()
    base = {"provider": "channel3", "checked_at": checked_at.isoformat(),
            "input": {"product_query": query, "product_url": product_url,
                      "claimed_amount": _q(amount), "currency": currency,
                      "merchant_domain": merchant_domain},
            "advisory_only": True, "authoritative": False,
            "disclosure": DISCLOSURE, "verify_notice": VERIFY_NOTICE,
            "switch_notice": SWITCH_NOTICE, "discount_codes_available": False,
            "discount_codes_note": NO_CODES_NOTICE,
            "product": None, "market": None, "merchant_offer": None,
            "better_deals": [], "signals": [], "evidence": None}
    signals: list[dict] = base["signals"]

    def sig(code, severity, message, **data):
        signals.append({"code": code, "severity": severity, "message": message,
                        **({"data": data} if data else {})})

    if error is not None or result is None:
        kind = getattr(error, "kind", "unavailable")
        sig("MARKET_DATA_UNAVAILABLE", "info",
            "Market data could not be retrieved; no comparison was made. This does not "
            "affect the payment request.", reason=kind)
        base["advisory_level"] = "no_data"
        return base

    base["evidence"] = {"source_url": result.evidence.url, "endpoint": result.endpoint,
                        "retrieved_at": result.evidence.retrieved_at.isoformat(),
                        "content_sha256": result.evidence.content_sha256,
                        "method": result.evidence.method}
    product, confidence = match_product(result.products, query or "", merchant_domain,
                                        exact=result.endpoint in ("lookup", "product")
                                        and not identity_from_search)
    if product is None:
        sig("PRODUCT_NOT_FOUND", "info",
            "Channel3 found no product that clearly matches this request; similar items "
            "are not compared.", candidates=[p.title for p in result.products[:3]])
        base["advisory_level"] = "no_match"
        return base
    base["product"] = {"id": product.id, "title": product.title,
                       "brands": list(product.brands), "match_confidence": confidence,
                       "offer_count": len(product.offers)}
    if confidence == "medium":
        # never compare prices or claim "same product" deals on an approximate match
        sig("LOW_CONFIDENCE_MATCH", "info",
            "The closest Channel3 product may not be the same item (brand, model or "
            "variant uncertain), so no price comparison or deal is shown. Name the exact "
            "product or pass its page URL for a precise check.")
        base["advisory_level"] = "no_match"
        return base

    same_cur = [o for o in product.offers if o.price is not None and o.currency == currency]
    market = [o for o in same_cur if _is_new(o) and _in_stock(o)]
    want = _domain(merchant_domain)
    merchant = next((o for o in product.offers if want and _domain(o.domain) == want), None)

    if market:
        prices = sorted(o.price for o in market)
        med = Decimal(str(median(prices)))
        ratio = (amount / med) if med > 0 else None
        base["market"] = {"currency": currency, "offers_compared": len(market),
                          "low": _q(prices[0]), "median": _q(med), "high": _q(prices[-1]),
                          "claimed_vs_median": None if ratio is None else str(ratio.quantize(Decimal("0.01")))}
        if ratio is not None and ratio >= ABOVE_HIGH:
            sig("PRICE_ABOVE_MARKET", "high",
                f"The claimed price is {ratio:.2f}x the market median ({_q(med)} {currency}).",
                ratio=str(ratio.quantize(Decimal("0.01"))), median=_q(med))
        elif ratio is not None and ratio >= ABOVE_WARNING:
            sig("PRICE_ABOVE_MARKET", "warning",
                f"The claimed price is {ratio:.2f}x the market median ({_q(med)} {currency}).",
                ratio=str(ratio.quantize(Decimal("0.01"))), median=_q(med))
        elif ratio is not None and ratio <= BELOW_HIGH:
            sig("PRICE_FAR_BELOW_MARKET", "high",
                f"The claimed price is only {ratio:.2f}x the market median: too good to be "
                "true can mean counterfeit, used goods or a scam listing.",
                ratio=str(ratio.quantize(Decimal("0.01"))), median=_q(med))
    elif product.offers:
        sig("NO_COMPARABLE_OFFERS", "info",
            f"No new, in-stock offers in {currency} to compare against.")

    if want and merchant is None and product.offers:
        sig("MERCHANT_NOT_IN_MARKET", "warning",
            f"{want} is not among the retailers Channel3 lists for this product; verify the "
            "merchant before paying.", listed_retailers=sorted({_domain(o.domain) for o in
                                                                product.offers})[:10])
    if merchant is not None:
        base["merchant_offer"] = {"domain": _domain(merchant.domain), "price": _q(merchant.price),
                                  "currency": merchant.currency,
                                  "availability": merchant.availability,
                                  "condition": merchant.condition,
                                  "compare_at_price": _q(merchant.compare_at_price)}
        if merchant.availability == "OutOfStock":
            sig("OUT_OF_STOCK_AT_MERCHANT", "warning",
                f"{_domain(merchant.domain)} lists this product as out of stock.")
        if _is_secondhand(merchant):
            sig("CONDITION_NOT_NEW_AT_MERCHANT", "warning",
                f"{_domain(merchant.domain)} lists this item as {merchant.condition}.")
        if merchant.price is not None and merchant.currency == currency and merchant.price > 0:
            diff = (amount - merchant.price) / merchant.price
            if abs(diff) > MERCHANT_PRICE_TOLERANCE:
                sig("MERCHANT_PRICE_MISMATCH", "high" if diff > Decimal("0.10") else "warning",
                    f"The agent's amount ({_q(amount)}) differs from {_domain(merchant.domain)}'s "
                    f"listed price ({_q(merchant.price)} {currency}).",
                    listed=_q(merchant.price), claimed=_q(amount))

    # better deals: same product, same currency, explicitly in stock, explicitly known
    # condition: new always qualifies; a second-hand purchase may also compare with the
    # SAME second-hand condition. Unknown condition never qualifies.
    allowed = {"new"} | ({merchant.condition.lower()} if merchant is not None
                         and _is_secondhand(merchant) else set())
    floor = max(MIN_SAVING_ABS, amount * MIN_SAVING_PCT)
    deals = []
    for o in same_cur:
        if not _in_stock(o) or (o.condition or "").lower() not in allowed:
            continue
        if want and _domain(o.domain) == want:
            continue
        saving = amount - o.price
        if saving < floor:
            continue
        deal = {"retailer": _domain(o.domain), "price": _q(o.price), "currency": currency,
                "condition": o.condition.lower(), "availability": o.availability,
                "you_save": _q(saving), "you_save_pct": str((saving / amount * 100)
                                                            .quantize(Decimal("0.1"))),
                "buy_url": _safe_buy_url(o.url, _domain(o.domain)), "on_sale": False}
        if o.compare_at_price and o.compare_at_price > o.price:
            deal.update(on_sale=True, was_price=_q(o.compare_at_price),
                        sale_discount_pct=str(((o.compare_at_price - o.price)
                                               / o.compare_at_price * 100).quantize(Decimal("0.1"))))
        deals.append(deal)
    deals.sort(key=lambda d: Decimal(d["price"]))
    base["better_deals"] = deals[:MAX_DEALS]
    if deals:
        best = deals[0]
        sig("BETTER_PRICE_AVAILABLE", "info",
            f"The same product is listed for {best['price']} {currency} at {best['retailer']} "
            f"(save {best['you_save']} {currency}). Verify on the site before switching.")

    worst = max((_SEVERITY_RANK[s["severity"]] for s in signals), default=0)
    base["advisory_level"] = {0: "no_concern", 1: "review_recommended", 2: "high_risk"}[worst]
    if worst == 0 and any(s["code"] == "NO_COMPARABLE_OFFERS" for s in signals):
        # E2E-T3-3: nothing was compared (e.g. no offers in the claimed currency). That is an
        # absence of evidence, never an all-clear for the owner.
        base["advisory_level"] = "no_data"
    return base


class MarketChecker:
    """Runs the market check through a Channel3Client. Never raises: failures become a
    MARKET_DATA_UNAVAILABLE advisory so a payment request is never blocked by it."""

    def __init__(self, client: Channel3Client, clock=None):
        self.client = client
        self.clock = clock or client._clock

    def check(self, *, amount: Decimal, currency: str, merchant_domain: str | None,
              product_query: str | None = None, product_url: str | None = None,
              product_id: str | None = None) -> dict:
        result = error = None
        try:
            if product_id:
                result = self.client.get_product(product_id)          # free refresh
            elif product_url:
                result = self.client.lookup_url(product_url)
            elif product_query:
                result = self.client.search_products(product_query)
                if result.cached:
                    # Channel3: never show cached prices; re-read the matched product's
                    # live offers through the free product endpoint
                    product, conf = match_product(result.products, product_query,
                                                  merchant_domain)
                    if product is not None:
                        fresh = self.client.get_product(product.id)
                        result = Channel3Result(fresh.products, fresh.evidence, "search")
            else:
                error = Channel3Error("invalid_request", "no product_query or product_url")
        except Channel3Error as exc:
            error = exc
        except Exception:  # noqa: BLE001 - advisory must never break payment creation
            error = Channel3Error("unavailable", "unexpected market-check failure")
        kwargs = dict(amount=Decimal(str(amount)), currency=currency,
                      merchant_domain=merchant_domain, query=product_query,
                      product_url=product_url, checked_at=self.clock(),
                      # E2E-T3-5 / E2E-T1-005: a free refresh by product id re-reads offers
                      # but must not upgrade a NAME match to an "exact" identity. Only an
                      # original URL lookup is exact; otherwise the identity rules re-apply.
                      identity_from_search=bool(product_id and product_query) and not product_url)
        try:
            return build_market_check(result=result, error=error, **kwargs)
        except Exception:  # noqa: BLE001 - malformed data must not crash a caller
            return build_market_check(result=None, error=Channel3Error(
                "invalid_response", "market data could not be analysed"), **kwargs)
