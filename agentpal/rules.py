"""Deterministic, transparent risk rules.

Each triggered rule yields a RiskSignal with a stable reason code, a kind
(hard violation / risk indicator / uncertainty), the configured points and the
evidence that triggered it. Point values come from the policy file.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import RiskPolicy
from .features import Features
from .schemas import RiskSignal, SignalKind, SignalSource

HARD_VIOLATION_CODES = {
    "DUPLICATE_REQUEST_ID",
    "CURRENCY_NOT_ALLOWED",
    "AMOUNT_ABOVE_HARD_LIMIT",
    "BUDGET_EXCEEDED",
    "REQUEST_EXPIRED",
    "REQUEST_LIFETIME_EXCEEDS_MAX",
    "BLOCKED_MERCHANT_DOMAIN",
    "NO_SPENDING_POLICY",
    "CUMULATIVE_LIMIT_EXCEEDED",
    "SPLIT_TRANSACTION_EXCEEDS_HARD_LIMIT",
}


@dataclass
class RuleResult:
    signals: list[RiskSignal]
    score: int

    @property
    def hard_violations(self) -> list[str]:
        return [s.code for s in self.signals if s.kind == SignalKind.hard_violation]


def evaluate_rules(f: Features, policy: RiskPolicy) -> RuleResult:
    signals: list[RiskSignal] = []
    lim = policy.limits

    def add(code: str, kind: SignalKind, message: str, **evidence: object) -> None:
        points = 100 if kind == SignalKind.hard_violation else policy.points(code)
        signals.append(RiskSignal(code=code, kind=kind, source=SignalSource.rule,
                                  points=points, message=message, evidence=evidence))

    hard, ind, unc = SignalKind.hard_violation, SignalKind.risk_indicator, SignalKind.uncertainty

    # ---- hard security / policy violations (never overridable by a model)
    if f.duplicate_request_id:
        add("DUPLICATE_REQUEST_ID", hard,
            "This request_id has already been assessed; replays are rejected.")
    if not f.currency_allowed:
        add("CURRENCY_NOT_ALLOWED", hard, "Currency is not in the allowed list.",
            currency=f.currency, allowed=lim.allowed_currencies)
    if f.amount_over_hard_limit:
        add("AMOUNT_ABOVE_HARD_LIMIT", hard,
            "Amount exceeds the per-transaction hard limit.",
            amount=f.amount, currency=f.currency, limit=f.per_transaction_hard_limit)
    if f.no_spending_policy:
        add("NO_SPENDING_POLICY", hard,
            "No trusted operator spending policy is configured for this agent and currency; "
            "financial controls cannot be evaluated, so the request is blocked.",
            currency=f.currency)
    if f.exceeds_remaining_budget and lim.block_on_budget_exceeded:
        add("BUDGET_EXCEEDED", hard,
            "Amount exceeds the remaining operator-configured budget for this period "
            "(including active reservations).",
            amount=f.amount, budget_remaining=f.budget_remaining)
    if f.cumulative_limit_exceeded:
        add("CUMULATIVE_LIMIT_EXCEEDED", hard,
            "This request would exceed the operator's rolling-window spending limit.")
    if f.split_exceeds_hard_limit:
        add("SPLIT_TRANSACTION_EXCEEDS_HARD_LIMIT", hard,
            "Recent requests to the same merchant plus this one exceed the per-transaction "
            "hard limit (possible split transaction).",
            merchant_window_committed=f.merchant_window_committed,
            currency=f.currency, limit=f.per_transaction_hard_limit)
    if f.expired:
        add("REQUEST_EXPIRED", hard, "The request expired before it was received.")
    if f.lifetime_too_long:
        add("REQUEST_LIFETIME_EXCEEDS_MAX", hard,
            "expires_at is further in the future than the maximum allowed request lifetime.",
            max_lifetime_hours=policy.time_policy.max_lifetime_hours)
    if f.blocked_domain:
        add("BLOCKED_MERCHANT_DOMAIN", hard, "Merchant domain is on the blocklist.",
            domain=f.merchant_domain)

    # ---- probabilistic risk indicators
    if f.duplicate_intent_of:
        add("DUPLICATE_PAYMENT_INTENT", ind,
            "Same agent, merchant, amount and currency were requested recently.",
            previous_request_id=f.duplicate_intent_of,
            window_hours=lim.duplicate_intent_window_hours)
    if f.near_duplicate_of:
        add("NEAR_DUPLICATE_PAYMENT_INTENT", ind,
            "Same agent, merchant and currency requested an almost identical amount recently "
            "(possible duplicate with an altered amount).",
            previous_request_id=f.near_duplicate_of,
            tolerance_ratio=lim.near_duplicate_tolerance_ratio)
    if f.lookalike_target:
        add("LOOKALIKE_DOMAIN", ind, "Domain closely imitates an approved merchant domain.",
            domain=f.merchant_domain, imitates=f.lookalike_target)
    if f.domain_changed_from:
        add("MERCHANT_DOMAIN_CHANGED", ind,
            "Merchant name was previously associated with a different domain.",
            previous_domain=f.domain_changed_from, requested_domain=f.merchant_domain)
    if f.confusable_merchant_name:
        add("CONFUSABLE_MERCHANT_NAME", ind,
            "Merchant name is visually confusable with an approved merchant's name but uses "
            "different characters (UTS #39 skeleton match). Similarity is not proof of fraud.")
    if f.mixed_script_merchant:
        add("MIXED_SCRIPT_MERCHANT", ind,
            "Merchant name or domain mixes writing systems (e.g. Latin and Cyrillic).")
    if f.subdomain_of_approved:
        add("SUBDOMAIN_OF_APPROVED_MERCHANT", ind,
            "Domain is a subdomain of an approved merchant domain but is not itself approved.",
            domain=f.merchant_domain)
    if f.suspicious_tld:
        add("SUSPICIOUS_TLD", ind, "Domain uses a top-level domain often abused in fraud.",
            domain=f.merchant_domain)
    if f.punycode_or_ip:
        add("PUNYCODE_OR_IP_DOMAIN", ind,
            "Merchant 'domain' is an IP address or internationalized (punycode) name.",
            domain=f.merchant_domain)
    if f.name_domain_mismatch:
        add("MERCHANT_NAME_DOMAIN_MISMATCH", ind,
            "Merchant name does not appear to correspond to the merchant domain.",
            domain=f.merchant_domain)
    if not f.approved_merchant and not f.known_from_history and not f.blocked_domain:
        add("UNAPPROVED_MERCHANT", ind,
            "Merchant is not on the approved list and has no prior assessed requests. "
            "This alone does not indicate fraud.", domain=f.merchant_domain)
    if f.amount_over_review_limit and not f.amount_over_hard_limit:
        add("AMOUNT_ABOVE_REVIEW_LIMIT", ind, "Amount exceeds the per-transaction review limit.",
            amount=f.amount, currency=f.currency, limit=f.per_transaction_review_limit)
    if (f.budget_ratio is not None and f.budget_ratio >= lim.budget_share_review_ratio
            and not f.exceeds_remaining_budget):
        add("LARGE_SHARE_OF_BUDGET", ind, "Amount is a large share of the monthly budget.",
            budget_ratio=f.budget_ratio, threshold=lim.budget_share_review_ratio)
    if f.exceeds_remaining_budget and not lim.block_on_budget_exceeded:
        add("LARGE_SHARE_OF_BUDGET", ind, "Amount exceeds the remaining budget.",
            amount=f.amount, budget_remaining=f.budget_remaining)
    if f.possible_split_transaction:
        add("POSSIBLE_SPLIT_TRANSACTION", ind,
            "Recent requests to the same merchant plus this one exceed the review limit "
            "although each is below it.",
            merchant_window_committed=f.merchant_window_committed,
            currency=f.currency, review_limit=f.per_transaction_review_limit)
    if f.agent_budget_claim_mismatch:
        add("AGENT_BUDGET_CLAIM_MISMATCH", ind,
            "The agent-supplied monthly_budget differs from the operator-configured budget; "
            "the operator value was used.")
    if f.amount_spike_ratio and f.amount_spike_ratio >= lim.amount_spike_ratio_vs_history:
        add("AMOUNT_SPIKE_VS_HISTORY", ind,
            "Amount is far above this agent's typical request amount.",
            ratio=f.amount_spike_ratio, agent_median=f.agent_median_amount)
    if f.recurring_unexplained or f.recurring_but_one_time_language:
        add("RECURRING_NOT_EXPLAINED", ind,
            "Request sets up recurring billing but the purpose does not describe a "
            "subscription (or describes a one-time purchase).",
            one_time_language=f.recurring_but_one_time_language)
    if f.annualized_exceeds_annual_budget:
        add("ANNUALIZED_RECURRING_EXCEEDS_BUDGET", ind,
            "Annualized cost of this recurring charge exceeds 12x the monthly budget.",
            annualized=f.annualized_recurring_cost)
    if f.unusual_interval:
        add("UNUSUAL_BILLING_INTERVAL", ind,
            "Daily/weekly billing is unusual for this kind of merchant.")
    if f.purpose_merchant_mismatch:
        add("PURPOSE_MERCHANT_MISMATCH", ind,
            "Stated purpose does not match the merchant's category, or describes a "
            "category that is not a normal business expense.",
            purpose_categories=f.purpose_categories, merchant_category=f.merchant_category)
    if f.injection_hits:
        add("PROMPT_INJECTION_ATTEMPT", ind,
            "Agent-supplied text contains instructions aimed at the risk assessor. "
            "They were ignored and treated as data.", matches=f.injection_hits)
    if f.pressure_hits:
        add("PRESSURE_LANGUAGE", ind, "Request uses urgency or pressure language.",
            matches=f.pressure_hits)
    if f.high_risk_method_hits:
        add("HIGH_RISK_PAYMENT_METHOD_LANGUAGE", ind,
            "Text references payment methods commonly used in scams.",
            matches=f.high_risk_method_hits)
    if f.evidence_amount_mismatch:
        add("EVIDENCE_AMOUNT_MISMATCH", ind,
            "Amount in claimed evidence differs from the requested amount.",
            details=f.evidence_amount_mismatch)
    if f.evidence_domain_mismatch:
        add("EVIDENCE_DOMAIN_MISMATCH", ind,
            "Domain in claimed evidence differs from the merchant domain.",
            details=f.evidence_domain_mismatch)
    if f.created_in_future:
        add("TIMESTAMP_IN_FUTURE", ind,
            "Client created_at is ahead of server time beyond the allowed clock skew.")
    if f.stale and not f.expired:
        add("STALE_REQUEST", ind, "Request is older than the configured maximum age.")

    # ---- uncertainty: missing / unverifiable information (NOT fraud evidence)
    if f.missing_justification:
        add("MISSING_JUSTIFICATION", unc,
            "No meaningful business justification was provided; more information needed.")
    if f.missing_evidence:
        add("MISSING_EVIDENCE", unc,
            "No supporting evidence (invoice, quote, contract) was provided.")
    if f.evidence_unverified:
        add("EVIDENCE_UNVERIFIED", unc,
            "Supporting evidence was claimed by the agent but has not been verified, so it "
            "does not reduce risk.")
    if f.demo_default_budget:
        add("DEMO_DEFAULT_BUDGET_APPLIED", unc,
            "This agent has no owner-configured budget; placeholder limits from a DEMO "
            "configuration were applied. An owner must configure a budget, so this "
            "request requires review.")
    if f.missing_domain:
        add("MISSING_MERCHANT_DOMAIN", unc,
            "No merchant domain was provided, so merchant checks are limited.")
    if f.recurring_missing_interval:
        add("RECURRING_MISSING_INTERVAL", unc, "Recurring request has no billing interval.")
    if f.previous_reference_unknown:
        add("UNKNOWN_PREVIOUS_PAYMENT_REFERENCE", unc,
            "previous_payment_reference does not match any assessed request.")
    if f.direction_incoming:
        add("INCOMING_DIRECTION_LIMITED_SUPPORT", unc,
            "Incoming-payment assessment is not fully supported in Phase 1.")

    # Missing information alone must not read as high risk: its total is capped.
    uncertainty_pts = sum(s.points for s in signals if s.kind == unc)
    score = min(100, sum(s.points for s in signals if s.kind != unc)
                + min(uncertainty_pts, lim.max_uncertainty_points))
    if any(s.kind == hard for s in signals):
        score = max(score, policy.score_bands.critical)
    return RuleResult(signals=signals, score=score)
