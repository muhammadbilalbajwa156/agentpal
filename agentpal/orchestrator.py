"""Assessment orchestrator: rules + model -> final, auditable risk assessment.

Policy (conservative by design):
  * Hard violations always yield `block` and a critical score; no model can change that.
  * A model may move the rule score only within the configured blending bounds.
  * A model can escalate the recommendation, never relax it below what the rules
    alone would recommend. By default a model can escalate only to human review:
    `block` requires deterministic evidence (also limits injected "block" DoS).
  * Model output is labelled as prediction; rule evidence is labelled as computed
    from the submitted data. Nothing is presented as externally verified.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

from .analyzers import (
    AnalyzerInput,
    AnalyzerUnavailable,
    HeuristicAnalyzer,
    MalformedModelOutput,
    RiskAnalyzer,
)
from .config import RiskPolicy
from .features import AssessmentContext, extract_features
from .rules import evaluate_rules
from .schemas import (
    ModelAnalysis,
    ModelTrace,
    PaymentRequest,
    RiskAssessment,
    RiskSignal,
    SignalKind,
    SignalSource,
)

log = logging.getLogger("agentpal.orchestrator")

_SEVERITY = {"allow_for_review": 0, "require_review": 1, "block": 2}
_INCONSISTENCY_CODES = {
    "EVIDENCE_AMOUNT_MISMATCH", "EVIDENCE_DOMAIN_MISMATCH", "PURPOSE_MERCHANT_MISMATCH",
    "MERCHANT_NAME_DOMAIN_MISMATCH", "RECURRING_NOT_EXPLAINED", "MERCHANT_DOMAIN_CHANGED",
    "TIMESTAMP_IN_FUTURE",
}


def risk_category(score: int, policy: RiskPolicy) -> str:
    b = policy.score_bands
    if score >= b.critical:
        return "critical"
    if score >= b.high:
        return "high"
    if score >= b.medium:
        return "medium"
    return "low"


def recommendation_for(score: int, policy: RiskPolicy, hard_violation: bool = False) -> str:
    """Owner decision 2: `block` only for deterministic policy violations (hard rules or an
    enabled combination policy). A score - however high - means human review."""
    if hard_violation:
        return "block"
    if score >= policy.recommendation.review_at_or_above:
        return "require_review"
    return "allow_for_review"


def matched_combinations(rule_signals: list[RiskSignal], policy: RiskPolicy) -> list:
    """Enabled combination blocks whose codes were ALL raised by deterministic rules."""
    rule_codes = {s.code for s in rule_signals if s.source == SignalSource.rule}
    return [c for c in policy.recommendation.combination_blocks
            if c.enabled and set(c.all_of) <= rule_codes]


def _stricter(a: str, b: str) -> str:
    return a if _SEVERITY[a] >= _SEVERITY[b] else b


DEFAULT_MODEL_TIMEOUT_S = 30.0
# Model calls run on worker threads so a hung backend cannot block the request thread.
# A thread that ignores cancellation keeps running in the background (Python cannot kill
# threads); the pool size bounds how many such threads can exist, and queued calls still
# time out, so request latency stays bounded (P0.7a).
_MODEL_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="agentpal-model")


def _bounded(fn, deadline: float):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise FutureTimeout()
    return _MODEL_POOL.submit(fn).result(timeout=remaining)


def run_analyzers(chain: list[RiskAnalyzer], data: AnalyzerInput,
                  timeout_s: float = DEFAULT_MODEL_TIMEOUT_S
                  ) -> tuple[ModelAnalysis, RiskAnalyzer, list[str], list[str], bool]:
    """Try analyzers in order, each bounded by `timeout_s`; return the first valid
    analysis. The last element reports whether a configured backend actually failed
    (timeout, crash or malformed output) as opposed to simply being unavailable."""
    attempted, errors, failed = [], [], False
    for analyzer in chain:
        attempted.append(analyzer.name)
        deadline = time.monotonic() + timeout_s
        bounded_input = AnalyzerInput(request=data.request, features=data.features,
                                      rules=data.rules, deadline=deadline)
        try:
            if not _bounded(analyzer.available, deadline):
                errors.append(f"{analyzer.name}: unavailable")
                continue
            # bind this iteration's input: a call queued behind a saturated pool must not
            # pick up a later iteration's input (deadline)
            result = _bounded(lambda a=analyzer, inp=bounded_input: a.analyze(inp), deadline)
            return result, analyzer, attempted, errors, failed
        except FutureTimeout:
            failed = True
            errors.append(f"{analyzer.name}: timeout after {timeout_s:g}s")
        except MalformedModelOutput as exc:
            failed = True
            errors.append(f"{analyzer.name}: malformed output ({exc})")
        except AnalyzerUnavailable as exc:
            errors.append(f"{analyzer.name}: unavailable ({exc})")
        except Exception as exc:  # a backend bug must never break assessment
            failed = True
            log.exception("analyzer %s failed", analyzer.name)
            errors.append(f"{analyzer.name}: error ({type(exc).__name__})")
    fallback = HeuristicAnalyzer()
    attempted.append(fallback.name)
    return fallback.analyze(data), fallback, attempted, errors, failed


def assess(req: PaymentRequest, ctx: AssessmentContext, policy: RiskPolicy,
           chain: list[RiskAnalyzer],
           model_timeout_s: float = DEFAULT_MODEL_TIMEOUT_S) -> RiskAssessment:
    started = time.perf_counter()
    features = extract_features(req, ctx, policy)
    rules = evaluate_rules(features, policy)
    hard = rules.hard_violations

    analysis, analyzer, attempted, errors, _model_failed = run_analyzers(
        chain, AnalyzerInput(request=req, features=features, rules=rules), model_timeout_s)
    # degraded = a model backend was configured but the deterministic fallback had to be used
    degraded = analyzer.is_fallback and any(not a.is_fallback for a in chain)
    degraded_reason = ("; ".join(errors) or "model backend unavailable") if degraded else None
    latency_ms = (time.perf_counter() - started) * 1000

    # ---- deterministic blocks: hard rules + explicit, enabled combination policies
    combo_signals = [
        RiskSignal(code=f"COMBINATION_BLOCK:{c.name}", kind=SignalKind.hard_violation,
                   source=SignalSource.rule, points=0,
                   message=f"Owner policy '{c.name}' blocks this combination of rule "
                           f"signals: {c.rationale}",
                   evidence={"all_of": c.all_of, "rationale": c.rationale})
        for c in matched_combinations(rules.signals, policy)]
    hard = hard + [s.code for s in combo_signals]

    # ---- combine scores within policy bounds
    blend = policy.model_blending
    delta = analysis.risk_score - rules.score
    delta = max(-blend.max_downward_adjustment, min(blend.max_upward_adjustment, delta))
    final_score = max(0, min(100, rules.score + delta))
    if hard:
        final_score = max(final_score, policy.score_bands.critical)

    rp = policy.recommendation
    # rules decide the floor; nothing below can relax it, and only rules can block
    recommendation = recommendation_for(rules.score, policy, bool(hard))
    # model influence is the bounded score (owner decision 9: score-only escalation)
    recommendation = _stricter(recommendation, recommendation_for(final_score, policy))
    if rp.model_escalation == "label_or_score" and analysis.recommendation != "allow_for_review":
        recommendation = _stricter(recommendation, "require_review")
    if (analyzer.reports_uncertainty and analysis.uncertainty
            >= rp.review_if_model_uncertainty_at_or_above):
        recommendation = _stricter(recommendation, "require_review")
    if rp.review_on_any_uncertainty and any(
            s.kind == SignalKind.uncertainty for s in rules.signals):
        recommendation = _stricter(recommendation, "require_review")
    # owner decision 5: a degraded assessment with material unresolved risk needs a human;
    # model failure is never evidence that a request is safe
    if degraded and rules.score >= rp.degraded_mode.review_if_rule_score_at_or_above:
        recommendation = _stricter(recommendation, "require_review")
    category = risk_category(final_score, policy)
    if category in ("high", "critical"):  # high-risk is never allow_for_review
        recommendation = _stricter(recommendation, "require_review")

    # ---- assemble signals: rule evidence + (labelled) model predictions
    signals: list[RiskSignal] = list(rules.signals) + combo_signals
    if degraded:
        signals.append(RiskSignal(
            code="MODEL_UNAVAILABLE_OR_TIMED_OUT", kind=SignalKind.uncertainty,
            source=SignalSource.rule, points=0,
            message="Degraded assessment: a configured model backend was unavailable, timed "
                    "out or failed, so only deterministic rules were applied (see "
                    "model.errors). This is not evidence that the request is safe.",
            evidence={"errors": errors[-3:]}))
    if not analyzer.is_fallback:
        for f in analysis.findings:
            signals.append(RiskSignal(
                code=f"MODEL_{f.code}"[:64], kind=SignalKind.risk_indicator,
                source=SignalSource.model, points=0, message=f.description,
                evidence={"model_evidence": f.evidence,
                          "note": "model prediction; not a verified fact"}))

    missing = [s.message for s in rules.signals if s.kind == SignalKind.uncertainty]
    if not analyzer.is_fallback:
        missing += [f"(model) {m}" for m in analysis.missing_information]
    inconsistencies = [s.message for s in rules.signals if s.code in _INCONSISTENCY_CODES]

    return RiskAssessment(
        request_id=req.request_id,
        agent_id=req.agent_id,
        assessed_at=ctx.now,
        effective_expires_at=features.effective_expires_at,
        risk_score=final_score,
        risk_category=category,
        recommendation=recommendation,
        explanation=_explain(final_score, category, recommendation, rules.signals, hard,
                             analyzer, analysis, degraded, combo_signals),
        signals=signals,
        hard_violations=hard,
        missing_information=missing,
        inconsistencies=inconsistencies,
        rule_score=rules.score,
        model_score=None if analyzer.is_fallback else analysis.risk_score,
        model_uncertainty=None if analyzer.is_fallback else analysis.uncertainty,
        model_summary=analysis.summary or None,
        model=ModelTrace(backend=analyzer.name, model_name=analyzer.model_name,
                         model_version=analyzer.model_version,
                         is_fallback=analyzer.is_fallback, attempted_backends=attempted,
                         errors=errors, latency_ms=round(latency_ms, 2)),
        rules_version=policy.rules_version,
        degraded=degraded,
        degraded_reason=degraded_reason,
    )


def _explain(score: int, category: str, rec: str, signals: list[RiskSignal],
             hard: list[str], analyzer: RiskAnalyzer, analysis: ModelAnalysis,
             degraded: bool = False, combos: list[RiskSignal] | None = None) -> str:
    parts = [f"Risk score {score}/100 ({category}); recommendation: {rec}."]
    if hard:
        parts.append("Mandatory policy violation(s): " + ", ".join(hard) + ".")
    for c in combos or []:
        parts.append(c.message)
    if rec == "require_review" and category == "critical" and not hard:
        parts.append("Only soft risk signals were found: they require human review but do "
                     "not block on their own (owner policy).")
    if degraded:
        parts.append("DEGRADED: the configured model was not used (unavailable, timed out or "
                     "failed); deterministic rules only. Model failure is not evidence of "
                     "safety.")
    indicators = [s for s in signals if s.kind == SignalKind.risk_indicator]
    if indicators:
        top = sorted(indicators, key=lambda s: -s.points)[:4]
        parts.append("Risk indicators: " + "; ".join(f"{s.code} — {s.message}" for s in top))
    uncertain = [s.code for s in signals if s.kind == SignalKind.uncertainty]
    if uncertain:
        parts.append("Information gaps (not evidence of fraud): " + ", ".join(uncertain) + ".")
    if not indicators and not hard:
        parts.append("No configured risk rule was triggered. This does not establish that "
                     "the merchant or payment is legitimate.")
    if analyzer.is_fallback and not degraded:
        parts.append("No AI model configured; deterministic rules used.")
    else:
        parts.append(f"Model ({analyzer.model_name}) estimated {analysis.risk_score}/100; "
                     "model output is a prediction, not verified fact.")
    if rec == "allow_for_review":
        parts.append("allow_for_review means no blocking condition was found; it does not "
                     "authorize or execute a payment.")
    return " ".join(parts)
