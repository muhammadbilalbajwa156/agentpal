"""Product-page extractors: observed facts cite their source, absent facts are unavailable,
nothing is guessed, and page text never acts as instructions. Fixtures are synthetic."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from agentpal.contracts import Extractor, ObservedFact, Provenance, SourceEvidence
from agentpal.intelligence import (CompositeProductExtractor, JsonLdProductExtractor,
                                   MicrodataProductExtractor, OpenGraphProductExtractor)
from agentpal.intelligence import extractors as ex

FIXTURES = Path(__file__).parent / "fixtures" / "intel"
NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
ALL = [JsonLdProductExtractor(), OpenGraphProductExtractor(), MicrodataProductExtractor(),
       CompositeProductExtractor()]


def page(name: str, url: str = "https://shop.lumen.example/p/1",
         final_url: str | None = None) -> tuple[SourceEvidence, bytes]:
    content = (FIXTURES / name).read_bytes()
    return SourceEvidence(url=url, final_url=final_url, retrieved_at=NOW, method="fixture",
                          content_sha256=SourceEvidence.digest(content)), content


def observed(facts: list[ObservedFact], name: str) -> list[ObservedFact]:
    return [f for f in facts if f.name == name and f.provenance == Provenance.observed]


def unavailable(facts: list[ObservedFact], name: str) -> bool:
    return any(f.name == name and f.provenance == Provenance.unavailable for f in facts)


@pytest.mark.parametrize("extractor", ALL, ids=lambda e: e.name)
def test_every_extractor_implements_the_protocol(extractor):
    assert isinstance(extractor, Extractor)


@pytest.mark.parametrize("extractor", ALL, ids=lambda e: e.name)
@pytest.mark.parametrize("fixture", sorted(p.name for p in FIXTURES.glob("*.html")))
def test_provenance_invariants_on_every_fixture(extractor, fixture):
    src, content = page(fixture)
    facts = extractor.extract(src, content)
    assert facts, "an extractor always reports something (at least unavailable facts)"
    for f in facts:
        assert f.provenance in (Provenance.observed, Provenance.unavailable)
        if f.provenance == Provenance.observed:
            assert f.source == src and f.value is not None
        else:
            assert f.value is None and f.source is None
    names = {f.name for f in facts}
    assert set(ex.CORE_FACTS) <= names  # every core fact is either observed or unavailable


def test_jsonld_product_offer_fields():
    src, content = page("jsonld_product.html")
    facts = JsonLdProductExtractor().extract(src, content)
    price = observed(facts, "price")
    assert [(p.value, p.unit) for p in price] == [(Decimal("49.00"), "USD")]
    assert price[0].note == "jsonld[0].Product[0].offers[0].price"  # locator kept in note
    assert observed(facts, "price_currency")[0].value == "USD"
    assert observed(facts, "availability")[0].value == "InStock"
    assert observed(facts, "tax_included")[0].value is False
    ship = observed(facts, "shipping_price")[0]
    assert (ship.value, ship.unit) == (Decimal("5.5"), "USD")
    assert observed(facts, "seller_name")[0].value == "Lumenfield Supply"
    assert observed(facts, "product_name")[0].value == "Trailhead Desk Lamp"


def test_plain_script_and_visible_text_are_never_read_as_prices():
    src, content = page("jsonld_product.html")
    for extractor in ALL:
        values = {f.value for f in observed(extractor.extract(src, content), "price")}
        assert Decimal("999.99") not in values
    src, content = page("no_product_data.html")
    facts = CompositeProductExtractor().extract(src, content)
    assert not observed(facts, "price") and unavailable(facts, "price")


def test_jsonld_graph_and_aggregate_offer_range():
    src, content = page("jsonld_graph_aggregate.html")
    facts = JsonLdProductExtractor().extract(src, content)
    assert observed(facts, "price_low")[0].value == Decimal("120")
    assert observed(facts, "price_high")[0].value == Decimal("180.00")
    assert observed(facts, "price_low")[0].unit == "EUR"
    assert unavailable(facts, "price")  # a range is not a price


def test_opengraph_product_meta():
    src, content = page("opengraph_product.html")
    facts = OpenGraphProductExtractor().extract(src, content)
    assert [(p.value, p.unit) for p in observed(facts, "price")] == [(Decimal("2500"), "JPY")]
    assert observed(facts, "availability")[0].value == "OutOfStock"
    assert observed(facts, "product_name")[0].value == "Quillmere Notebook Set"


def test_microdata_product_offer():
    src, content = page("microdata_product.html")
    facts = MicrodataProductExtractor().extract(src, content)
    assert [(p.value, p.unit) for p in observed(facts, "price")] == [(Decimal("19.99"), "GBP")]
    assert observed(facts, "availability")[0].value == "InStock"
    assert observed(facts, "product_name")[0].value == "Halberd Cable Organizer"


def test_malformed_and_non_finite_jsonld_yields_nothing_observed():
    src, content = page("malformed_jsonld.html")
    facts = JsonLdProductExtractor().extract(src, content)
    assert not [f for f in facts if f.provenance == Provenance.observed]


def test_unparseable_price_is_unavailable_not_repaired():
    src, content = page("unparseable_price.html")
    facts = CompositeProductExtractor().extract(src, content)
    assert not observed(facts, "price")
    note = next(f.note for f in facts if f.name == "price")
    assert "not guessed" in note
    assert observed(facts, "price_currency")[0].value == "EUR"


def test_price_without_currency_keeps_unit_empty():
    src, content = page("price_without_currency.html")
    facts = CompositeProductExtractor().extract(src, content)
    price = observed(facts, "price")[0]
    assert price.value == Decimal("12") and price.unit is None
    assert unavailable(facts, "price_currency")


def test_conflicting_vocabularies_are_all_kept_unmerged():
    src, content = page("conflicting_sources.html")
    facts = CompositeProductExtractor().extract(src, content)
    by = {(f.extracted_by, f.value) for f in observed(facts, "price")}
    assert by == {("jsonld_product", Decimal("45.00")), ("meta_product", Decimal("39.00"))}
    # "1,299.00" uses a thousands separator: never parsed as 1.299 or 1299
    assert all(f.value not in (Decimal("1.299"), Decimal("1299")) for f in facts
               if f.name == "price")


def test_injection_text_is_inert_data():
    src, content = page("injection_text.html")
    facts = CompositeProductExtractor().extract(src, content)
    name = observed(facts, "product_name")[0]
    assert name.value.startswith("SYSTEM: ignore")  # kept verbatim, as data
    assert len(name.value) <= ex.MAX_TEXT
    assert {f.name for f in facts} <= {*ex.CORE_FACTS, "page_domain", "brand_name",
                                       "seller_name", "price_low", "price_high"}


def test_hash_mismatch_extracts_nothing():
    src, content = page("jsonld_product.html")
    for extractor in ALL:
        facts = extractor.extract(src, content + b"<!-- tampered -->")
        assert facts and all(f.provenance == Provenance.unavailable for f in facts)
        assert "sha256" in facts[0].note


def test_oversized_content_extracts_nothing():
    content = b"<html>" + b" " * (ex.MAX_CONTENT_BYTES + 1)
    src = SourceEvidence(url="https://shop.lumen.example/", retrieved_at=NOW, method="fixture",
                         content_sha256=SourceEvidence.digest(content))
    facts = CompositeProductExtractor().extract(src, content)
    assert all(f.provenance == Provenance.unavailable for f in facts)


def test_page_domain_uses_final_url_after_redirects():
    src, content = page("jsonld_product.html", url="https://lumen.example/go",
                        final_url="https://www.other-shop.example/item")
    facts = CompositeProductExtractor().extract(src, content)
    dom = observed(facts, "page_domain")[0]
    assert dom.value == "other-shop.example" and dom.note == "source.final_url"


def test_extraction_is_deterministic():
    src, content = page("conflicting_sources.html")
    a = CompositeProductExtractor().extract(src, content)
    b = CompositeProductExtractor().extract(src, content)
    assert [f.model_dump() for f in a] == [f.model_dump() for f in b]


def test_bounded_on_many_products_and_deep_nesting():
    products = [{"@type": "Product", "name": f"P{i}",
                 "offers": [{"@type": "Offer", "price": str(i + j), "priceCurrency": "USD"}
                            for j in range(30)]} for i in range(30)]
    deep: dict = {"@type": "Thing"}
    for _ in range(500):
        deep = {"child": deep}
    blocks = "".join(f'<script type="application/ld+json">{json.dumps(b)}</script>'
                     for b in (products, deep))
    content = f"<html><head>{blocks}</head></html>".encode()
    src = SourceEvidence(url="https://shop.lumen.example/", retrieved_at=NOW, method="fixture",
                         content_sha256=SourceEvidence.digest(content))
    facts = CompositeProductExtractor().extract(src, content)
    assert len(facts) <= 150
    assert len(observed(facts, "price")) <= ex.MAX_PRODUCTS * ex.MAX_OFFERS_PER_PRODUCT


@pytest.mark.parametrize("raw,expected", [
    ("49.00", Decimal("49.00")), (49, Decimal("49")), (5.5, Decimal("5.5")),
    (" 10 ", Decimal("10")), ("1,299.00", None), ("1.234,50", None), ("$49", None),
    ("-5", None), ("10-20", None), (True, None), (float("nan"), None), (None, None),
    ("", None), ("1e3", None), (-1, None)])
def test_parse_price_is_strict(raw, expected):
    assert ex.parse_price(raw) == expected


@pytest.mark.parametrize("raw,expected", [("usd", "USD"), (" EUR ", "EUR"), ("$", None),
                                          ("US Dollar", None), (None, None)])
def test_parse_currency_is_strict(raw, expected):
    assert ex.parse_currency(raw) == expected
