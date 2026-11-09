"""Claimed request terms vs observed facts -> discrepancies.

Pure and deterministic. A discrepancy is a reason to look closer, not proof of fraud; a
match is not proof of legitimacy (the observed page may itself be fake). No currency
conversion is ever attempted, and a price whose currency was not observed is not compared.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Iterable

from ..contracts import ObservedFact, Provenance, SourceEvidence
from ..features import base_domain
from ..schemas import CURRENCY_MINOR_UNITS, PaymentRequest, normalize_domain
from .extractors import page_domain, parse_price
from .report import Claim, Discrepancy

DEFAULT_PRICE_TOLERANCE_RATIO = Decimal("0.02")   # same spirit as near-duplicate tolerance
UNAVAILABLE_STATES = frozenset({"OutOfStock", "SoldOut", "Discontinued"})
AVAILABLE_STATES = frozenset({"InStock", "LimitedAvailability", "OnlineOnly", "InStoreOnly",
                              "PreOrder", "PreSale", "BackOrder", "MadeToOrder"})


# ---------------------------------------------------------------- claims

def _claim(field: str, value: object, by: str) -> Claim:
    text = None if value is None else str(value.value if hasattr(value, "value") else value)
    return Claim(field=field, value=None if text is None else text[:500], claimed_by=by)


def claims_from_request(req: PaymentRequest) -> list[Claim]:
    """Every term the request asserts, labelled as claimed (none of it is verified)."""
    claims = [_claim("amount", req.amount, "request"), _claim("currency", req.currency, "request"),
              _claim("merchant_name", req.merchant_name, "request"),
              _claim("merchant_domain", req.merchant_domain, "request"),
              _claim("purpose", req.purpose, "request"),
              _claim("recurring", str(req.recurring).lower(), "request")]
    if req.billing_interval is not None:
        claims.append(_claim("billing_interval", req.billing_interval, "request"))
    if req.business_justification:
        claims.append(_claim("business_justification", req.business_justification, "request"))
    for i, ev in enumerate(req.evidence[:20]):
        by = f"evidence[{i}]"
        claims.append(_claim("evidence_type", ev.type, by))
        for field in ("amount", "currency", "merchant_domain", "reference"):
            value = getattr(ev, field)
            if value is not None:
                claims.append(_claim(field, value, by))
        if ev.description:
            claims.append(_claim("description", ev.description, by))
    return claims[:60]


# ---------------------------------------------------------------- helpers

def _minor_unit(currency: str) -> Decimal:
    return Decimal(1).scaleb(-CURRENCY_MINOR_UNITS.get(currency, 2))


def tolerance(observed: Decimal, currency: str, ratio: Decimal,
              min_minor_units: int = 1) -> Decimal:
    return max(abs(observed) * ratio, _minor_unit(currency) * min_minor_units)


def _source_key(src: SourceEvidence) -> tuple[str, str]:
    return (src.url, src.content_sha256)


def _host_base(url_or_host: str | None) -> str | None:
    if not url_or_host:
        return None
    try:
        return base_domain(normalize_domain(url_or_host))
    except ValueError:
        return None


def _numeric(facts: list[ObservedFact], name: str) -> list[ObservedFact]:
    """Facts of `name` with a Decimal value. A JSON round trip of the contract turns Decimal
    values into strings, so strict decimal strings are re-parsed (never anything looser)."""
    out = []
    for f in facts:
        if f.name != name or isinstance(f.value, bool):
            continue
        if isinstance(f.value, Decimal):
            out.append(f)
        elif isinstance(f.value, str) and (value := parse_price(f.value)) is not None:
            out.append(f.model_copy(update={"value": value}))
    return out


def _fmt(value: Decimal, currency: str | None) -> str:
    return f"{value} {currency or '(currency not observed)'}"


# ---------------------------------------------------------------- comparison

def compare_claims(req: PaymentRequest, facts: Iterable[ObservedFact], *,
                   price_tolerance_ratio: Decimal = DEFAULT_PRICE_TOLERANCE_RATIO,
                   min_abs_tolerance_minor_units: int = 1) -> list[Discrepancy]:
    """Compare the request's claimed amount, currency and merchant domain against observed
    facts. Only `provenance=observed` facts are used; claims are never compared to claims."""
    observed = [f for f in facts if f.provenance == Provenance.observed and f.source]
    if not observed:
        return []
    out: list[Discrepancy] = []
    out += _domain_discrepancies(req, observed)
    out += _price_discrepancies(req, observed, price_tolerance_ratio,
                                min_abs_tolerance_minor_units)
    out += _availability_discrepancies(observed)
    return out[:40]


def _domain_discrepancies(req: PaymentRequest, observed: list[ObservedFact]) -> list[Discrepancy]:
    out: list[Discrepancy] = []
    by_source: dict[tuple[str, str], list[ObservedFact]] = defaultdict(list)
    for f in observed:
        by_source[_source_key(f.source)].append(f)
    claimed_base = _host_base(req.merchant_domain)
    claim = _claim("merchant_domain", req.merchant_domain, "request")
    for facts in by_source.values():
        src = facts[0].source
        cite = [f for f in facts if f.name == "page_domain"] or facts[:1]
        requested_base, served_domain = _host_base(src.url), page_domain(src)
        served_base = _host_base(served_domain)
        if src.final_url and requested_base and served_base and requested_base != served_base:
            out.append(Discrepancy(
                code="REDIRECTED_OFF_DOMAIN", severity="review", claimed=None, observed=cite,
                detail=f"The requested URL on {requested_base} was served from {served_base} "
                       f"after redirects."))
        if claimed_base and served_base and served_base != claimed_base:
            out.append(Discrepancy(
                code="MERCHANT_DOMAIN_MISMATCH", severity="review", claimed=claim,
                observed=cite,
                detail=f"The page was served from {served_base}, not from the claimed merchant "
                       f"domain {claimed_base}."))
    return out


def _price_discrepancies(req: PaymentRequest, observed: list[ObservedFact], ratio: Decimal,
                         min_units: int) -> list[Discrepancy]:
    cur, amount = req.currency, req.amount
    amount_claim = _claim("amount", amount, "request")
    prices = _numeric(observed, "price")
    lows = _numeric(observed, "price_low")
    highs = {(f.extracted_by, _source_key(f.source), f.note.rsplit(".", 1)[0]): f
             for f in _numeric(observed, "price_high")}
    ranges = []
    for low in lows:
        high = highs.get((low.extracted_by, _source_key(low.source), low.note.rsplit(".", 1)[0]))
        if high is not None and high.unit == low.unit:
            ranges.append((low, high))
    shipping = [f for f in _numeric(observed, "shipping_price") if f.unit == cur]

    if not prices and not ranges:
        return [Discrepancy(code="PRICE_UNAVAILABLE", severity="info", claimed=amount_claim,
                            detail="Sources were examined but no parseable price was observed; "
                                   "the claimed amount could not be compared.")]
    same = [f for f in prices if f.unit == cur]
    same_ranges = [(lo, hi) for lo, hi in ranges if lo.unit == cur]
    other = [f for f in prices if f.unit and f.unit != cur] + [lo for lo, _ in ranges
                                                                if lo.unit and lo.unit != cur]
    unknown = [f for f in prices if f.unit is None] + [lo for lo, _ in ranges if lo.unit is None]
    out: list[Discrepancy] = []
    if unknown:
        out.append(Discrepancy(
            code="CURRENCY_UNAVAILABLE", severity="info", observed=unknown[:20],
            detail="A price was observed without a currency; it was not compared (currency is "
                   "never guessed)."))
    if not same and not same_ranges:
        if other:
            seen = sorted({f.unit for f in other if f.unit})
            out.append(Discrepancy(
                code="CURRENCY_MISMATCH", severity="review",
                claimed=_claim("currency", cur, "request"), observed=other[:20],
                detail=f"The request claims {cur}; observed prices are only in "
                       f"{', '.join(seen)}. No currency conversion was attempted."))
        return out

    def within(observed_value: Decimal, claimed: Decimal) -> bool:
        return abs(claimed - observed_value) <= tolerance(observed_value, cur, ratio, min_units)

    matched = any(within(p.value, amount) for p in same)
    matched = matched or any(within(p.value + s.value, amount) for p in same for s in shipping)
    matched = matched or any(
        lo.value - tolerance(lo.value, cur, ratio, min_units) <= amount
        <= hi.value + tolerance(hi.value, cur, ratio, min_units) for lo, hi in same_ranges)
    if not matched:
        if same:
            nearest = min(same, key=lambda p: abs(p.value - amount))
            pct = (abs(amount - nearest.value) / nearest.value * 100) if nearest.value else None
            pct_text = f" ({pct:.1f}% difference)" if pct is not None else ""
            out.append(Discrepancy(
                code="PRICE_MISMATCH", severity="review", claimed=amount_claim,
                observed=same[:20],
                detail=f"Claimed {_fmt(amount, cur)}; nearest observed price "
                       f"{_fmt(nearest.value, cur)}{pct_text}. The claim may include tax, "
                       f"shipping or quantity; this needs confirmation, not an assumption."))
        else:
            lo, hi = same_ranges[0]
            out.append(Discrepancy(
                code="PRICE_OUTSIDE_RANGE", severity="review", claimed=amount_claim,
                observed=[lo, hi],
                detail=f"Claimed {_fmt(amount, cur)} is outside the observed range "
                       f"{lo.value}-{hi.value} {cur}."))
    distinct = sorted({p.value for p in same})
    if len(distinct) > 1:
        out.append(Discrepancy(
            code="MULTIPLE_CONFLICTING_PRICES", severity="info", observed=same[:20],
            detail=f"Several different prices were observed in {cur}: "
                   f"{', '.join(str(v) for v in distinct[:6])} (variants, sale price or "
                   f"conflicting page data)."))
    return out


def _availability_discrepancies(observed: list[ObservedFact]) -> list[Discrepancy]:
    states = [f for f in observed if f.name == "availability" and isinstance(f.value, str)]
    if not states:
        return []
    unavailable = [f for f in states if f.value in UNAVAILABLE_STATES]
    if unavailable and not any(f.value in AVAILABLE_STATES for f in states):
        return [Discrepancy(code="OUT_OF_STOCK", severity="review", observed=unavailable[:20],
                            detail="Observed availability says the item cannot currently be "
                                   f"bought ({unavailable[0].value}).")]
    return []
