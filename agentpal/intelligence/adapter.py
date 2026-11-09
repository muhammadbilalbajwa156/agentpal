"""Adapter: existing rule signals + `ModelAnalysis` (+ observations) -> report.

`ModelAnalysis` (the pinned model's output contract) is consumed as-is and never changed.
The advisory is derived deterministically, and every input can only make it stricter:
  * `blocked_by_rules` comes ONLY from a deterministic rule hard violation;
  * model output can raise the advisory to `recommend_review` at most (owner decision F9);
  * the advisory is never less strict than the orchestrator's recommendation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Sequence

from ..analyzers.base import _UNVERIFIABLE_CLAIM
from ..contracts import ObservedFact, Provenance
from ..features import infer_categories
from ..schemas import (ModelAnalysis, PaymentRequest, Recommendation, RiskAssessment,
                       RiskSignal, SignalKind, SignalSource)
from .compare import claims_from_request, compare_claims
from .evidence import UNCERTAIN_COMPARISON_CODES, evidence_quality, verification_steps
from .report import (ADVISORY_ORDER, Claim, Discrepancy, FinancialIntelligenceReport,
                     IntelSignal, RequestSummary)

COMMUNICATION_RULE_CODES = frozenset({"PROMPT_INJECTION_ATTEMPT", "PRESSURE_LANGUAGE",
                                      "HIGH_RISK_PAYMENT_METHOD_LANGUAGE"})
_COMMUNICATION_MODEL = re.compile(
    r"INJECT|PRESSURE|URGEN|MANIPUL|IMPERSON|PHISH|SOCIAL|GIFT|CRYPTO|WIRE|SECRE|INSTRUCT|"
    r"BANK_DETAIL|ACCOUNT_CHANGE", re.IGNORECASE)
MODEL_NOTE = "model prediction; not a verified fact"


@dataclass(frozen=True)
class ModelView:
    """What the report uses from a model: findings, missing info and whether it flagged."""

    findings: list[tuple[str, str]] = field(default_factory=list)   # (code, description)
    missing: list[str] = field(default_factory=list)
    flagged: bool = False

    @classmethod
    def from_analysis(cls, analysis: ModelAnalysis | None) -> "ModelView":
        if analysis is None:
            return cls()
        findings = [(f.code, f.description) for f in analysis.findings
                    if not _UNVERIFIABLE_CLAIM.search(f"{f.description} {f.evidence}")]
        return cls(findings=findings, missing=list(analysis.missing_information),
                   flagged=analysis.recommendation != "allow_for_review" or bool(findings))


def build_report(req: PaymentRequest, *, rule_signals: Sequence[RiskSignal],
                 analysis: ModelAnalysis | None = None,
                 observations: Sequence[ObservedFact] = (),
                 discrepancies: Sequence[Discrepancy] | None = None,
                 orchestrator_recommendation: Recommendation | None = None,
                 generated_at: datetime | None = None) -> FinancialIntelligenceReport:
    """Report from rule signals (e.g. `RuleResult.signals` plus any combination blocks) and an
    optional `ModelAnalysis`. A fallback/heuristic analyzer's output should not be passed as
    `analysis` (it is not a model prediction)."""
    return _assemble(req, list(rule_signals), ModelView.from_analysis(analysis),
                     list(observations), discrepancies, orchestrator_recommendation,
                     generated_at, model_used=analysis is not None)


def report_from_assessment(req: PaymentRequest, assessment: RiskAssessment, *,
                           observations: Sequence[ObservedFact] = (),
                           discrepancies: Sequence[Discrepancy] | None = None,
                           generated_at: datetime | None = None) -> FinancialIntelligenceReport:
    """Report from a finished orchestrator `RiskAssessment` (rules + labelled model output)."""
    if assessment.request_id != req.request_id:
        raise ValueError("assessment and request do not match")
    rule_signals = [s for s in assessment.signals if s.source == SignalSource.rule]
    model_signals = [s for s in assessment.signals if s.source == SignalSource.model]
    findings = [(s.code.removeprefix("MODEL_"), s.message) for s in model_signals]
    missing = [m.removeprefix("(model) ") for m in assessment.missing_information
               if m.startswith("(model) ")]
    used = not assessment.model.is_fallback
    view = ModelView(findings=findings, missing=missing,
                     flagged=bool(findings) or (used and assessment.model_score is not None
                                                and assessment.model_score
                                                > assessment.rule_score)) if used else ModelView()
    return _assemble(req, rule_signals, view, list(observations), discrepancies,
                     assessment.recommendation, generated_at or assessment.assessed_at,
                     model_used=used)


# ---------------------------------------------------------------- assembly

def _rule_signal(s: RiskSignal) -> IntelSignal:
    return IntelSignal(code=s.code[:80], kind=s.kind, origin="rule", message=s.message[:500],
                       note="computed by deterministic rules from the submitted (claimed) data")


def _comparison_signal(d: Discrepancy) -> IntelSignal:
    kind = SignalKind.risk_indicator if d.severity == "review" else SignalKind.uncertainty
    return IntelSignal(code=f"OBSERVED_{d.code}", kind=kind, origin="comparison",
                       message=d.detail, note="claimed terms compared with observed facts")


def _model_signal(code: str, description: str) -> IntelSignal:
    return IntelSignal(code=f"MODEL_{code}"[:80], kind=SignalKind.risk_indicator, origin="model",
                       message=description[:500], note=MODEL_NOTE)


def _corroborated(claims: list[Claim], observations: list[ObservedFact],
                  discrepancies: list[Discrepancy], req: PaymentRequest) -> set[tuple[str, str]]:
    """Request claims consistent with an observation. Consistent is not verified."""
    codes = {d.code for d in discrepancies}
    observed = [f for f in observations if f.provenance == Provenance.observed]
    same_cur = any(f.name in ("price", "price_low") and f.unit == req.currency for f in observed)
    ok: set[tuple[str, str]] = set()
    if same_cur and not codes & {"PRICE_MISMATCH", "PRICE_OUTSIDE_RANGE", "PRICE_UNAVAILABLE"}:
        ok.add(("amount", "request"))
    if same_cur and "CURRENCY_MISMATCH" not in codes:
        ok.add(("currency", "request"))
    if (req.merchant_domain and any(f.name == "page_domain" for f in observed)
            and not codes & {"MERCHANT_DOMAIN_MISMATCH", "REDIRECTED_OFF_DOMAIN"}):
        ok.add(("merchant_domain", "request"))
    return ok


def _assemble(req: PaymentRequest, rule_signals: list[RiskSignal], model: ModelView,
              observations: list[ObservedFact], discrepancies: Sequence[Discrepancy] | None,
              orchestrator_recommendation: Recommendation | None,
              generated_at: datetime | None, model_used: bool) -> FinancialIntelligenceReport:
    rules = [s for s in rule_signals if s.source == SignalSource.rule]
    if discrepancies is None:
        discrepancies = compare_claims(req, observations) if observations else []
    discrepancies = list(discrepancies)
    has_observation = any(f.provenance == Provenance.observed for f in observations)

    claims = claims_from_request(req)
    ok = _corroborated(claims, observations, discrepancies, req)
    unverified = [c for c in claims if (c.field, c.claimed_by) not in ok]

    rule_intel = [_rule_signal(s) for s in rules]
    model_intel = [_model_signal(c, d) for c, d in model.findings][:12]
    signals = rule_intel + [_comparison_signal(d) for d in discrepancies] + model_intel
    comm = [s for s in rule_intel if s.code in COMMUNICATION_RULE_CODES]
    comm += [s for s in model_intel if _COMMUNICATION_MODEL.search(s.code)]

    quality = evidence_quality(req, observations, discrepancies)

    # ---- missing information
    missing = [s.message for s in rules if s.kind == SignalKind.uncertainty]
    if not has_observation:
        missing.append("No observation from the merchant's own website was available; price, "
                       "currency and merchant identity are claimed only.")
    missing += [d.detail for d in discrepancies if d.code in ("PRICE_UNAVAILABLE",
                                                               "CURRENCY_UNAVAILABLE")]
    missing += [f"(model) {m}"[:500] for m in model.missing]

    # ---- verification steps (triggers in priority order)
    hard = [s for s in rules if s.kind == SignalKind.hard_violation]
    triggers = (["HARD_VIOLATION"] if hard else []) + [d.code for d in discrepancies]
    triggers += [s.code for s in rules if s.kind != SignalKind.hard_violation]
    if any(_COMMUNICATION_MODEL.search(c) for c, _ in model.findings):
        triggers.append("COMMUNICATION_MODEL_FLAG")
    if not has_observation:
        triggers.append("NO_OBSERVATION")
    steps = verification_steps(triggers)

    # ---- advisory: deterministic; each input can only raise it
    reasons: list[str] = []
    level = "no_concern_found"

    def raise_to(target: str, why: str) -> None:
        nonlocal level
        if ADVISORY_ORDER[target] > ADVISORY_ORDER[level]:
            level = target
        reasons.append(why[:200])

    consistent = quality.level == "observed_consistent"
    if hard:
        raise_to("blocked_by_rules", "deterministic rule hard violation: "
                 + ", ".join(s.code for s in hard)[:150])
    elif orchestrator_recommendation == "block":
        raise ValueError("orchestrator recommendation 'block' without a rule hard violation")
    if any(d.severity == "review" for d in discrepancies):
        raise_to("recommend_review", "observed facts contradict claimed terms")
    if orchestrator_recommendation == "require_review":
        raise_to("recommend_review", "the orchestrator requires review")
    if comm:
        raise_to("recommend_review", "suspicious communication flagged")
    if model.flagged:
        raise_to("recommend_review", "the advisory model flagged the request")
    if any(s.kind == SignalKind.risk_indicator for s in rules):
        raise_to("verify_before_proceeding", "deterministic risk indicators present")
    # Lead decision (2026-10-31): a consistent page observed by our own fetcher does NOT
    # clear missing/unverified-evidence signals. It shows what was served, not that the
    # agent's invoice is genuine or the purchase justified (owner decision F2; security
    # rule 8: browser evidence is not automatically trustworthy).
    if any(s.kind == SignalKind.uncertainty for s in rules):
        raise_to("verify_before_proceeding", "rules report missing or unverifiable information")
    if not consistent:
        raise_to("verify_before_proceeding", f"evidence quality is {quality.level}")
    if any(d.code in UNCERTAIN_COMPARISON_CODES for d in discrepancies):
        raise_to("verify_before_proceeding", "observation incomplete or ambiguous")
    if model.missing:
        raise_to("verify_before_proceeding", "the advisory model lists missing information")

    summary = RequestSummary(
        direction=req.direction.value, amount=req.amount, currency=req.currency,
        merchant_name=req.merchant_name, merchant_domain=req.merchant_domain,
        recurring=req.recurring,
        billing_interval=req.billing_interval.value if req.billing_interval else None,
        purpose_categories=sorted(infer_categories(
            f"{req.purpose} {req.business_justification}"))[:20],
        claimed_evidence_types=[e.type.value for e in req.evidence][:20])

    return FinancialIntelligenceReport(
        request_id=req.request_id,
        generated_at=generated_at or datetime.now(timezone.utc),
        request_summary=summary, claims=claims,
        observations=list(observations)[:200], discrepancies=discrepancies[:40],
        evidence_quality=quality, missing_information=missing[:40],
        unverified_claims=unverified[:60], communication_flags=comm[:40],
        risk_signals=signals[:80], verification_steps=steps, advisory=level,
        advisory_reasons=reasons[:20], orchestrator_recommendation=orchestrator_recommendation,
        model_used=model_used)
