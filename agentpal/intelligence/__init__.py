"""AgentPal intelligence engine: structured, provenance-aware financial intelligence.

Advisory only. Nothing in this package approves, authorizes, executes or unblocks a
payment; deterministic rules and an authenticated human remain the authority.
"""

from .adapter import build_report, report_from_assessment
from .compare import claims_from_request, compare_claims
from .extractors import (CompositeProductExtractor, JsonLdProductExtractor,
                         MicrodataProductExtractor, OpenGraphProductExtractor)
from .report import (ADVISORY_TO_RECOMMENDATION, Claim, Discrepancy, EvidenceQuality,
                     FinancialIntelligenceReport, IntelSignal, RequestSummary,
                     VerificationStep)
from .research import assess_sources

__all__ = [
    "ADVISORY_TO_RECOMMENDATION", "Claim", "CompositeProductExtractor", "Discrepancy",
    "EvidenceQuality", "FinancialIntelligenceReport", "IntelSignal", "JsonLdProductExtractor",
    "MicrodataProductExtractor", "OpenGraphProductExtractor", "RequestSummary",
    "VerificationStep", "assess_sources", "build_report", "claims_from_request",
    "compare_claims", "report_from_assessment",
]
