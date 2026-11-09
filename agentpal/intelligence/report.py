"""Structured financial intelligence: the output schema for the central orchestrator.

A `FinancialIntelligenceReport` separates what was CLAIMED (by the agent, a request, a
document or a message) from what was OBSERVED (extracted by a tool from a retrieved source
with URL, time and content hash) and from what was INFERRED (by a model or heuristic).

It is advisory. It never authorizes, approves, executes or unblocks a payment:
  * `advisory` has no "approve" value by construction;
  * `blocked_by_rules` only reports that the deterministic rules already blocked the
    request; it can only be set when a rule hard violation is present (validator below);
  * `authoritative` and `payment_executed` are the constant False.
The single authority stays the deterministic rules plus the orchestrator's
`RiskAssessment.recommendation` (see docs/INTELLIGENCE_ARCHITECTURE.md for the mapping).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..contracts import ObservedFact, Provenance, SourceEvidence
from ..schemas import Recommendation, SignalKind

REPORT_VERSION = "intel-report-1"

Advisory = Literal["no_concern_found", "verify_before_proceeding", "recommend_review",
                   "blocked_by_rules"]
ADVISORY_ORDER: dict[str, int] = {"no_concern_found": 0, "verify_before_proceeding": 1,
                                  "recommend_review": 2, "blocked_by_rules": 3}
# Explicit mapping onto the existing (authoritative) Recommendation vocabulary. The report
# never relaxes the orchestrator: its mapped value is always at least as strict.
ADVISORY_TO_RECOMMENDATION: dict[str, Recommendation] = {
    "no_concern_found": "allow_for_review",
    "verify_before_proceeding": "require_review",
    "recommend_review": "require_review",
    "blocked_by_rules": "block",
}

# Same order as orchestrator._SEVERITY (kept local to avoid importing the orchestrator).
RECOMMENDATION_ORDER: dict[str, int] = {"allow_for_review": 0, "require_review": 1, "block": 2}

DiscrepancyCode = Literal[
    "PRICE_MISMATCH",               # no observed price (or price+shipping) within tolerance
    "PRICE_OUTSIDE_RANGE",          # claimed amount outside an observed low..high range
    "CURRENCY_MISMATCH",            # observed prices exist only in other currencies
    "CURRENCY_UNAVAILABLE",         # a price was observed but its currency was not
    "PRICE_UNAVAILABLE",            # pages were examined, no parseable price was found
    "MULTIPLE_CONFLICTING_PRICES",  # several distinct observed prices (variants, sales...)
    "MERCHANT_DOMAIN_MISMATCH",     # the page is not on the claimed merchant's domain
    "REDIRECTED_OFF_DOMAIN",        # the requested URL redirected to another domain
    "OUT_OF_STOCK",                 # observed availability says it cannot be bought
]
REVIEW_SEVERITY_CODES = frozenset({"PRICE_MISMATCH", "PRICE_OUTSIDE_RANGE", "CURRENCY_MISMATCH",
                                   "MERCHANT_DOMAIN_MISMATCH", "REDIRECTED_OFF_DOMAIN",
                                   "OUT_OF_STOCK"})

DISCLAIMER = (
    "Financial intelligence report (advisory only). Observed facts show only what a source "
    "served at the time it was retrieved; they are not verified truth and do not establish "
    "that a merchant is legitimate. Claims are unverified. Model output is a prediction. "
    "This report does not approve, authorize or execute any payment; the deterministic rules "
    "and an authenticated human decide."
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Claim(_Strict):
    """A value asserted by the request, a document or a message. Never observed."""

    field: str = Field(pattern=r"^[a-z][a-z0-9_]{1,47}$")
    value: str | None = Field(default=None, max_length=500)
    provenance: Literal[Provenance.claimed] = Provenance.claimed
    claimed_by: str = Field(pattern=r"^(request|evidence\[\d{1,2}\]|message)$")


class Discrepancy(_Strict):
    code: DiscrepancyCode
    severity: Literal["info", "review"]
    claimed: Claim | None = None
    observed: list[ObservedFact] = Field(default_factory=list, max_length=20)
    detail: str = Field(max_length=500)

    @model_validator(mode="after")
    def _severity_matches_code(self) -> "Discrepancy":
        expected = "review" if self.code in REVIEW_SEVERITY_CODES else "info"
        if self.severity != expected:
            raise ValueError(f"{self.code} has severity {expected}")
        if any(f.provenance != Provenance.observed for f in self.observed):
            raise ValueError("a discrepancy cites only observed facts")
        return self


class EvidenceQuality(_Strict):
    """How well-supported the request is. There is deliberately no "verified" level."""

    level: Literal["none", "claimed_only", "observed_inconsistent", "observed_consistent"]
    claimed_evidence_items: int = Field(ge=0, le=20)
    sources: list[SourceEvidence] = Field(default_factory=list, max_length=20)
    notes: list[str] = Field(default_factory=list, max_length=10)


class VerificationStep(_Strict):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,63}$")
    description: str = Field(max_length=400)
    requires_human: bool
    reason_codes: list[str] = Field(default_factory=list, max_length=10)


class IntelSignal(_Strict):
    """A risk signal restated for the report. Carries no points: scoring stays in rules."""

    code: str = Field(max_length=80)
    kind: SignalKind
    origin: Literal["rule", "model", "comparison"]
    message: str = Field(max_length=500)
    note: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def _only_rules_are_hard(self) -> "IntelSignal":
        if self.kind == SignalKind.hard_violation and self.origin != "rule":
            raise ValueError("only deterministic rules raise hard violations")
        return self


class RequestSummary(_Strict):
    """Structured understanding of the payment request. Every value is CLAIMED by the
    requesting agent; nothing here has been checked against an outside source."""

    provenance: Literal[Provenance.claimed] = Provenance.claimed
    direction: str
    amount: Decimal
    currency: str
    merchant_name: str = Field(max_length=200)
    merchant_domain: str | None = None
    recurring: bool
    billing_interval: str | None = None
    purpose_categories: list[str] = Field(default_factory=list, max_length=20)
    claimed_evidence_types: list[str] = Field(default_factory=list, max_length=20)


class FinancialIntelligenceReport(_Strict):
    report_version: Literal["intel-report-1"] = REPORT_VERSION
    request_id: str
    generated_at: datetime
    request_summary: RequestSummary
    claims: list[Claim] = Field(default_factory=list, max_length=60)
    observations: list[ObservedFact] = Field(default_factory=list, max_length=200)
    discrepancies: list[Discrepancy] = Field(default_factory=list, max_length=40)
    evidence_quality: EvidenceQuality
    missing_information: list[str] = Field(default_factory=list, max_length=40)
    unverified_claims: list[Claim] = Field(default_factory=list, max_length=60)
    communication_flags: list[IntelSignal] = Field(default_factory=list, max_length=40)
    risk_signals: list[IntelSignal] = Field(default_factory=list, max_length=80)
    verification_steps: list[VerificationStep] = Field(default_factory=list, max_length=20)
    advisory: Advisory
    advisory_reasons: list[str] = Field(default_factory=list, max_length=20)
    orchestrator_recommendation: Recommendation | None = None
    model_used: bool = False
    authoritative: Literal[False] = False
    payment_executed: Literal[False] = False
    disclaimer: str = DISCLAIMER

    @field_validator("generated_at")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware")
        return v.astimezone(timezone.utc)

    @property
    def advisory_as_recommendation(self) -> Recommendation:
        return ADVISORY_TO_RECOMMENDATION[self.advisory]

    @model_validator(mode="after")
    def _advisory_is_consistent(self) -> "FinancialIntelligenceReport":
        rule_hard = any(s.kind == SignalKind.hard_violation and s.origin == "rule"
                        for s in self.risk_signals)
        if self.advisory == "blocked_by_rules" and not rule_hard:
            raise ValueError("blocked_by_rules requires a deterministic rule hard violation")
        if rule_hard and self.advisory != "blocked_by_rules":
            raise ValueError("a rule hard violation must be reported as blocked_by_rules")
        if self.advisory == "no_concern_found":
            if any(d.severity == "review" for d in self.discrepancies):
                raise ValueError("no_concern_found is impossible with a review discrepancy")
            if any(d.code == "PRICE_UNAVAILABLE" for d in self.discrepancies) or any(
                    f.name == "price" and f.provenance == Provenance.unavailable
                    for f in self.observations):
                raise ValueError("no_concern_found is impossible while the price is "
                                 "unavailable")
        if self.orchestrator_recommendation is not None:
            if (RECOMMENDATION_ORDER[self.advisory_as_recommendation]
                    < RECOMMENDATION_ORDER[self.orchestrator_recommendation]):
                raise ValueError("the report may never be less strict than the orchestrator")
        return self
