"""Research seam between the agent workforce (Agent 2's fetch/browser/search tools) and the
intelligence engine. I/O-free by design: the workforce retrieves pages under its own
capability checks and sandbox, then hands over `(SourceEvidence, bytes)` pairs (with
`final_url` set after redirects). This module only extracts and compares.

Flow (see docs/INTELLIGENCE_ARCHITECTURE.md):
  search (workforce) -> fetch allowed origins (workforce) -> assess_sources (here)
  -> build_report / report_from_assessment (here) -> orchestrator / owner dashboard.
"""

from __future__ import annotations

from typing import Sequence

from ..contracts import Extractor, ObservedFact, SourceEvidence
from ..schemas import PaymentRequest
from .compare import compare_claims
from .extractors import CompositeProductExtractor
from .report import Discrepancy

MAX_PAGES = 10


def assess_sources(req: PaymentRequest, pages: Sequence[tuple[SourceEvidence, bytes]],
                   extractor: Extractor | None = None,
                   ) -> tuple[list[ObservedFact], list[Discrepancy]]:
    """Extract facts from each retrieved page and compare them with the request's claims.

    Pages beyond MAX_PAGES are ignored. A page whose bytes do not match its sha256 yields
    only `unavailable` facts. No pages -> no facts and no discrepancies (the report then
    says the terms are claimed only)."""
    ex = extractor or CompositeProductExtractor()
    if not isinstance(ex, Extractor):
        raise TypeError("extractor must implement contracts.Extractor")
    facts: list[ObservedFact] = []
    for source, content in list(pages)[:MAX_PAGES]:
        if not isinstance(source, SourceEvidence):
            raise TypeError("pages must be (SourceEvidence, bytes) pairs")
        facts.extend(ex.extract(source, content))
    return facts, compare_claims(req, facts)
