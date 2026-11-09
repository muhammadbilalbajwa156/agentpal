"""Claimed request terms vs observed facts. Synthetic values and `.example` domains only."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from conftest import make_request

from agentpal.contracts import ObservedFact, Provenance, SourceEvidence
from agentpal.intelligence import claims_from_request, compare_claims
from agentpal.intelligence.compare import tolerance

NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)


def src(url="https://shop.lumen.example/p/1", final_url=None, body=b"x"):
    return SourceEvidence(url=url, final_url=final_url, retrieved_at=NOW, method="fixture",
                          content_sha256=SourceEvidence.digest(body))


def fact(name, value, unit=None, source=None, note="jsonld[0].Product[0].offers[0].x",
         by="jsonld_product"):
    return ObservedFact(name=name, value=value, unit=unit, provenance=Provenance.observed,
                        source=source or src(), extracted_by=by, note=note)


def req(amount="49.00", currency="USD", domain="lumen.example", **kw):
    return make_request(amount=amount, currency=currency, merchant_domain=domain,
                        merchant_name="Lumen Supply", **kw)


def codes(ds):
    return sorted(d.code for d in ds)


def test_matching_price_currency_and_domain_has_no_discrepancy():
    facts = [fact("price", Decimal("49.00"), "USD"), fact("page_domain", "shop.lumen.example")]
    assert compare_claims(req(), facts) == []


def test_no_observed_facts_means_no_comparison():
    assert compare_claims(req(), []) == []
    unavailable = ObservedFact(name="price", value=None, provenance=Provenance.unavailable,
                               extracted_by="x")
    assert compare_claims(req(), [unavailable]) == []


@pytest.mark.parametrize("claimed,mismatch", [
    ("49.98", False),   # +0.98 within 2% of 49.00
    ("50.00", True),    # +1.00 > 0.98
    ("48.02", False),   # inclusive boundary
    ("48.01", True),
])
def test_price_tolerance_boundaries(claimed, mismatch):
    ds = compare_claims(req(amount=claimed), [fact("price", Decimal("49.00"), "USD")])
    assert ("PRICE_MISMATCH" in codes(ds)) is mismatch


def test_minimum_tolerance_is_one_minor_unit_and_jpy_has_no_decimals():
    assert tolerance(Decimal("1.00"), "USD", Decimal("0.02")) == Decimal("0.02")
    assert tolerance(Decimal("0.10"), "USD", Decimal("0.02")) == Decimal("0.01")
    assert tolerance(Decimal("10"), "JPY", Decimal("0.02")) == Decimal("1")
    jpy = [fact("price", Decimal("2500"), "JPY")]
    assert compare_claims(req(amount="2550", currency="JPY"), jpy) == []
    assert codes(compare_claims(req(amount="2551", currency="JPY"), jpy)) == ["PRICE_MISMATCH"]


def test_price_plus_observed_shipping_is_accepted():
    facts = [fact("price", Decimal("49.00"), "USD"), fact("shipping_price", Decimal("5.50"), "USD")]
    assert compare_claims(req(amount="54.50"), facts) == []
    assert codes(compare_claims(req(amount="60.00"), facts)) == ["PRICE_MISMATCH"]


def test_mismatch_detail_cites_both_values_and_does_not_assume():
    [d] = compare_claims(req(amount="89.00"), [fact("price", Decimal("49.00"), "USD")])
    assert d.severity == "review" and d.claimed.value == "89.00"
    assert "49.00 USD" in d.detail and "confirmation" in d.detail
    assert d.observed[0].provenance == Provenance.observed


def test_currency_mismatch_never_converts():
    ds = compare_claims(req(amount="49.00", currency="USD"), [fact("price", Decimal("49.00"), "EUR")])
    assert codes(ds) == ["CURRENCY_MISMATCH"]
    assert "No currency conversion" in ds[0].detail


def test_price_without_currency_is_not_compared():
    ds = compare_claims(req(), [fact("price", Decimal("49.00"), None)])
    assert codes(ds) == ["CURRENCY_UNAVAILABLE"]
    assert all(d.severity == "info" for d in ds)


def test_no_price_on_examined_pages_is_price_unavailable():
    ds = compare_claims(req(), [fact("page_domain", "shop.lumen.example")])
    assert codes(ds) == ["PRICE_UNAVAILABLE"]


def test_aggregate_range():
    lo = fact("price_low", Decimal("120"), "EUR", note="jsonld[0].Product[0].offers[0].lowPrice")
    hi = fact("price_high", Decimal("180"), "EUR", note="jsonld[0].Product[0].offers[0].highPrice")
    assert compare_claims(req(amount="150.00", currency="EUR"), [lo, hi]) == []
    ds = compare_claims(req(amount="250.00", currency="EUR"), [lo, hi])
    assert codes(ds) == ["PRICE_OUTSIDE_RANGE"]


def test_conflicting_prices_are_reported_even_when_one_matches():
    facts = [fact("price", Decimal("45.00"), "USD"), fact("price", Decimal("39.00"), "USD")]
    ds = compare_claims(req(amount="45.00"), facts)
    assert codes(ds) == ["MULTIPLE_CONFLICTING_PRICES"] and ds[0].severity == "info"


def test_page_on_other_domain_is_a_merchant_domain_mismatch():
    other = src(url="https://lumen-deals.example/p")
    facts = [fact("price", Decimal("49.00"), "USD", source=other),
             fact("page_domain", "lumen-deals.example", source=other)]
    ds = compare_claims(req(), facts)
    assert codes(ds) == ["MERCHANT_DOMAIN_MISMATCH"]
    assert ds[0].observed[0].name == "page_domain"


def test_subdomain_of_claimed_merchant_is_not_a_mismatch():
    sub = src(url="https://store.eu.lumen.example/p")
    assert compare_claims(req(), [fact("price", Decimal("49.00"), "USD", source=sub)]) == []


def test_redirect_off_domain_is_flagged():
    redirected = src(url="https://lumen.example/buy", final_url="https://pay-lumen.example/x")
    ds = compare_claims(req(), [fact("price", Decimal("49.00"), "USD", source=redirected)])
    assert codes(ds) == ["MERCHANT_DOMAIN_MISMATCH", "REDIRECTED_OFF_DOMAIN"]


def test_no_claimed_domain_skips_domain_check():
    ds = compare_claims(req(domain=None), [fact("price", Decimal("49.00"), "USD")])
    assert ds == []


def test_out_of_stock_only_when_nothing_says_in_stock():
    out = fact("availability", "OutOfStock")
    assert codes(compare_claims(req(), [fact("price", Decimal("49"), "USD"), out])) == [
        "OUT_OF_STOCK"]
    both = [fact("price", Decimal("49"), "USD"), out, fact("availability", "InStock")]
    assert compare_claims(req(), both) == []


def test_claims_are_all_labelled_claimed():
    r = req(evidence=[{"type": "invoice", "amount": 49.0, "currency": "USD",
                       "merchant_domain": "lumen.example", "reference": "INV-7"}])
    claims = claims_from_request(r)
    assert all(c.provenance == Provenance.claimed for c in claims)
    assert {(c.field, c.claimed_by) for c in claims} >= {
        ("amount", "request"), ("currency", "request"), ("merchant_domain", "request"),
        ("evidence_type", "evidence[0]"), ("amount", "evidence[0]"), ("reference", "evidence[0]")}


def test_facts_survive_a_json_round_trip_of_the_contract():
    """ObservedFact.value re-reads a Decimal from JSON as str; comparison still works and
    only strict decimal strings are accepted."""
    facts = [fact("price", Decimal("49.00"), "USD")]
    again = [ObservedFact.model_validate_json(f.model_dump_json()) for f in facts]
    assert isinstance(again[0].value, str)
    assert compare_claims(req(amount="49.00"), again) == []
    assert codes(compare_claims(req(amount="60.00"), again)) == ["PRICE_MISMATCH"]
    sloppy = [fact("price", "1,299.00", "USD")]
    assert codes(compare_claims(req(), sloppy)) == ["PRICE_UNAVAILABLE"]
