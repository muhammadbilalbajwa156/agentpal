"""Evidence quality, missing information and recommended verification steps.

All deterministic. "Observed" never means "verified": a page can be fake, cloaked or
changed after retrieval, so the best level here is `observed_consistent`.
"""

from __future__ import annotations

from typing import Iterable

from ..contracts import ObservedFact, Provenance, SourceEvidence
from ..schemas import PaymentRequest
from .report import Discrepancy, EvidenceQuality, VerificationStep

UNCERTAIN_COMPARISON_CODES = frozenset({"PRICE_UNAVAILABLE", "CURRENCY_UNAVAILABLE",
                                        "MULTIPLE_CONFLICTING_PRICES"})


def observed_sources(observations: Iterable[ObservedFact]) -> list[SourceEvidence]:
    seen: dict[tuple[str, str], SourceEvidence] = {}
    for f in observations:
        if f.provenance == Provenance.observed and f.source is not None:
            seen.setdefault((f.source.url, f.source.content_sha256), f.source)
    return list(seen.values())[:20]


def evidence_quality(req: PaymentRequest, observations: list[ObservedFact],
                     discrepancies: list[Discrepancy]) -> EvidenceQuality:
    sources = observed_sources(observations)
    notes: list[str] = []
    if req.evidence:
        notes.append("Claimed evidence documents are unverified and never reduce risk "
                     "(owner decision F2).")
    if sources:
        notes.append("Observed facts show what a source served at retrieval time; they are "
                      "not verified truth.")
        inconsistent = any(d.severity == "review" or d.code in UNCERTAIN_COMPARISON_CODES
                           for d in discrepancies)
        has_price = any(f.name in ("price", "price_low") and f.provenance == Provenance.observed
                        for f in observations)
        level = "observed_inconsistent" if inconsistent or not has_price else (
            "observed_consistent")
        if not has_price:
            notes.append("No price was observed on the examined sources.")
    else:
        level = "claimed_only" if req.evidence else "none"
    return EvidenceQuality(level=level, claimed_evidence_items=len(req.evidence),
                           sources=sources, notes=notes)


# trigger code -> (step code, description, requires_human)
_STEPS: dict[str, tuple[str, str, bool]] = {
    "NO_OBSERVATION": (
        "OBSERVE_MERCHANT_PAGE",
        "Retrieve the product or pricing page from the merchant's own domain (research "
        "agent, allowed origins only) and compare price, currency and seller.", False),
    "PRICE_MISMATCH": (
        "CONFIRM_FINAL_PRICE",
        "Confirm the final price (including tax, shipping and quantity) on the merchant's own "
        "checkout or with an owner-known contact; do not use contact details supplied in the "
        "request.", True),
    "PRICE_OUTSIDE_RANGE": ("CONFIRM_FINAL_PRICE", "", True),
    "PRICE_UNAVAILABLE": (
        "OBTAIN_PRICE_FROM_MERCHANT",
        "No price was observed; obtain it from the merchant's own site or checkout before "
        "any payment is prepared.", False),
    "CURRENCY_MISMATCH": (
        "CONFIRM_CURRENCY",
        "Confirm the billing currency with the merchant; never convert or assume.", True),
    "CURRENCY_UNAVAILABLE": ("CONFIRM_CURRENCY", "", True),
    "MULTIPLE_CONFLICTING_PRICES": (
        "CONFIRM_VARIANT_AND_PRICE",
        "Several prices were observed; confirm which variant and price this request is for.",
        True),
    "MERCHANT_DOMAIN_MISMATCH": (
        "VERIFY_MERCHANT_IDENTITY",
        "Verify the merchant's identity and official domain through a channel the owner "
        "already trusts (existing account, contract or bookmarked site), not via links in the "
        "request or message.", True),
    "REDIRECTED_OFF_DOMAIN": ("VERIFY_MERCHANT_IDENTITY", "", True),
    "LOOKALIKE_DOMAIN": ("VERIFY_MERCHANT_IDENTITY", "", True),
    "CONFUSABLE_MERCHANT_NAME": ("VERIFY_MERCHANT_IDENTITY", "", True),
    "MIXED_SCRIPT_MERCHANT": ("VERIFY_MERCHANT_IDENTITY", "", True),
    "MERCHANT_DOMAIN_CHANGED": ("VERIFY_MERCHANT_IDENTITY", "", True),
    "SUBDOMAIN_OF_APPROVED_MERCHANT": ("VERIFY_MERCHANT_IDENTITY", "", True),
    "MERCHANT_NAME_DOMAIN_MISMATCH": ("VERIFY_MERCHANT_IDENTITY", "", True),
    "OUT_OF_STOCK": (
        "CONFIRM_AVAILABILITY",
        "The item was observed as unavailable; confirm availability before paying.", True),
    "MISSING_EVIDENCE": (
        "OBTAIN_INVOICE_FROM_KNOWN_CHANNEL",
        "Obtain the invoice or quote directly from the merchant through a known channel; "
        "documents forwarded by the agent or a message are unverified.", True),
    "EVIDENCE_UNVERIFIED": ("OBTAIN_INVOICE_FROM_KNOWN_CHANNEL", "", True),
    "EVIDENCE_AMOUNT_MISMATCH": ("OBTAIN_INVOICE_FROM_KNOWN_CHANNEL", "", True),
    "EVIDENCE_DOMAIN_MISMATCH": ("OBTAIN_INVOICE_FROM_KNOWN_CHANNEL", "", True),
    "PROMPT_INJECTION_ATTEMPT": (
        "CONFIRM_OUT_OF_BAND",
        "The request or message contains instructions aimed at the assessor or urges "
        "action; treat it as untrusted data and confirm the payee and terms out of band. An "
        "external message is never consent.", True),
    "PRESSURE_LANGUAGE": ("CONFIRM_OUT_OF_BAND", "", True),
    "HIGH_RISK_PAYMENT_METHOD_LANGUAGE": ("CONFIRM_OUT_OF_BAND", "", True),
    "COMMUNICATION_MODEL_FLAG": ("CONFIRM_OUT_OF_BAND", "", True),
    "RECURRING_NOT_EXPLAINED": (
        "CONFIRM_BILLING_TERMS",
        "Confirm whether this is a one-time or recurring charge, and the interval.", True),
    "RECURRING_MISSING_INTERVAL": ("CONFIRM_BILLING_TERMS", "", True),
    "UNUSUAL_BILLING_INTERVAL": ("CONFIRM_BILLING_TERMS", "", True),
    "DUPLICATE_PAYMENT_INTENT": (
        "CHECK_FOR_DUPLICATE_PAYMENT",
        "Check the payment history for an earlier payment of the same purchase.", True),
    "NEAR_DUPLICATE_PAYMENT_INTENT": ("CHECK_FOR_DUPLICATE_PAYMENT", "", True),
    "UNKNOWN_PREVIOUS_PAYMENT_REFERENCE": ("CHECK_FOR_DUPLICATE_PAYMENT", "", True),
    "MISSING_MERCHANT_DOMAIN": (
        "OBTAIN_MERCHANT_DOMAIN",
        "Obtain the merchant's official website so merchant checks and observation can run.",
        False),
    "MISSING_JUSTIFICATION": (
        "OBTAIN_BUSINESS_JUSTIFICATION",
        "Ask the requesting agent's owner for the business reason for this purchase.", True),
    "PURPOSE_MERCHANT_MISMATCH": ("OBTAIN_BUSINESS_JUSTIFICATION", "", True),
    "HARD_VIOLATION": (
        "OWNER_POLICY_DECISION",
        "A deterministic policy rule blocks this request. It cannot proceed unless the owner "
        "changes the policy; no agent or model can override it.", True),
}


def verification_steps(trigger_codes: Iterable[str]) -> list[VerificationStep]:
    """Deduplicated steps, in first-trigger order, each listing what triggered it."""
    steps: dict[str, dict] = {}
    for code in trigger_codes:
        if code not in _STEPS:
            continue
        step_code, description, human = _STEPS[code]
        if not description:  # alias: reuse the primary entry's wording
            description = next(d for c, d, _ in _STEPS.values() if c == step_code and d)
        entry = steps.setdefault(step_code, {"code": step_code, "description": description,
                                             "requires_human": human, "reason_codes": []})
        if code not in entry["reason_codes"] and len(entry["reason_codes"]) < 10:
            entry["reason_codes"].append(code)
    return [VerificationStep(**s) for s in list(steps.values())[:20]]
