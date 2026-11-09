"""Research seam: (SourceEvidence, bytes) in, facts + discrepancies out. No I/O."""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path

import pytest
from conftest import make_request

import agentpal.intelligence as intel
from agentpal.contracts import ObservedFact, Provenance, SourceEvidence
from agentpal.intelligence import assess_sources, build_report
from agentpal.intelligence.research import MAX_PAGES

FIXTURES = Path(__file__).parent / "fixtures" / "intel"
NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
PACKAGE = Path(intel.__file__).parent


def page(name: str, url: str = "https://shop.example.com/lamp"):
    content = (FIXTURES / name).read_bytes()
    return SourceEvidence(url=url, retrieved_at=NOW, method="fixture",
                          content_sha256=SourceEvidence.digest(content)), content


def test_intelligence_package_performs_no_io():
    banned = {"httpx", "requests", "urllib", "socket", "http", "subprocess", "aiohttp",
              "sqlite3", "os"}
    for path in PACKAGE.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                roots = {(node.module or "").split(".")[0]}
            else:
                continue
            assert not roots & banned, f"{path.name} imports {roots & banned}"
        assert "open(" not in path.read_text(), path.name


def test_no_pages_means_no_facts():
    assert assess_sources(make_request(), []) == ([], [])


def test_tampered_page_yields_no_observation_and_report_says_so():
    src, content = page("jsonld_product.html")
    facts, ds = assess_sources(make_request(), [(src, content + b" ")])
    assert facts and all(f.provenance == Provenance.unavailable for f in facts)
    assert ds == []
    r = build_report(make_request(), rule_signals=[], observations=facts, discrepancies=ds,
                     generated_at=NOW)
    assert r.evidence_quality.level == "none"
    assert r.advisory == "verify_before_proceeding"


def test_pages_are_bounded():
    pages = [page("jsonld_product.html", url=f"https://shop.example.com/p{i}")
             for i in range(MAX_PAGES + 5)]
    facts, _ = assess_sources(make_request(), pages)
    assert len({f.source.url for f in facts if f.source}) == MAX_PAGES


def test_inputs_are_type_checked():
    with pytest.raises(TypeError):
        assess_sources(make_request(), [({"url": "https://x.example"}, b"")])

    class NotAnExtractor:
        pass

    with pytest.raises(TypeError):
        assess_sources(make_request(), [page("jsonld_product.html")], NotAnExtractor())


def test_any_protocol_extractor_can_be_plugged_in():
    class Fixed:
        name = "fixed"

        def extract(self, source, content):
            return [ObservedFact(name="price", value="12.00", unit="USD",
                                 provenance=Provenance.observed, source=source,
                                 extracted_by=self.name)]

    _, ds = assess_sources(make_request(), [page("no_product_data.html")], Fixed())
    assert [d.code for d in ds] == ["PRICE_MISMATCH"]  # claimed 49.00 vs observed 12.00


def test_multiple_pages_and_vocabularies_are_combined():
    req = make_request(amount=19.99, currency="GBP")
    facts, ds = assess_sources(req, [page("microdata_product.html"),
                                     page("jsonld_product.html",
                                          url="https://shop.example.com/other")])
    assert {f.source.url for f in facts if f.source} == {"https://shop.example.com/lamp",
                                                         "https://shop.example.com/other"}
    assert ds == []  # a GBP 19.99 offer was observed; the USD page does not contradict it
