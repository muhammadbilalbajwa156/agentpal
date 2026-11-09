"""Feature extraction: turn a validated request + context into risk indicators.

Features are *observations*, not verdicts. In particular, a missing field only
produces a "missing" flag; the rules layer decides that it means "needs more
information", never "fraud".
"""

from __future__ import annotations

import ipaddress
import math
import re
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from pydantic import BaseModel, Field

from . import textnorm
from .config import RiskPolicy, TimePolicy
from .spending import SpendSnapshot
from .schemas import CURRENCY_MINOR_UNITS, BillingInterval, Direction, PaymentRequest


class HistoricalPayment(BaseModel):
    """An earlier request. In `AssessmentContext.history` it is merely *observed* (any
    outcome, possibly malicious); in `AssessmentContext.trusted_history` it is a payment
    with a VERIFIED completed outcome for the same agent (P0.4)."""

    request_id: str
    agent_id: str
    merchant_name: str
    merchant_domain: str | None = None
    amount: Decimal
    currency: str
    recurring: bool = False
    created_at: datetime
    # recommendation the earlier request received. Informational only: a recommendation
    # is our own output and is never treated as evidence of trust.
    recommendation: str | None = None


class AssessmentContext(BaseModel):
    now: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # Observed requests (same agent or same merchant), any status. Used only to RAISE
    # risk (duplicates). Never used to establish trust.
    history: list[HistoricalPayment] = Field(default_factory=list)
    # Verified completed payments of THIS agent only. The only history allowed to lower
    # risk (known merchant) or define baselines (merchant domain, spend). See
    # docs/TRUST_AND_LEDGER.md.
    trusted_history: list[HistoricalPayment] = Field(default_factory=list)
    # Decision 8: verified completed payments of the same owner's OTHER agents, present only
    # when the owner explicitly enabled sharing. Used for merchant trust only, never for the
    # spend baseline.
    shared_trusted_history: list[HistoricalPayment] = Field(default_factory=list)
    # Set by the service when the persistence layer refused to claim the request_id
    # because another submission already owns it (atomic replay protection).
    request_id_already_claimed: bool = False
    # Trusted operator budget + ledger state for (agent, currency). None means no trusted
    # spending policy is available, which is treated as NO_SPENDING_POLICY (block).
    spend: SpendSnapshot | None = None


# ------------------------------------------------------------ text detectors

CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "hosting": ("hosting", "server", "vps", "cloud", "compute", "instance", "cdn",
                "domain", "dns", "bandwidth", "storage", "kubernetes", "deploy"),
    "api": ("api", "tokens", "requests", "geocoding", "email delivery", "sms",
            "inference", "embedding", "credits", "quota", "transactional email"),
    "software": ("license", "licence", "seat", "seats", "software", "saas", "ide",
                 "project management", "design tool", "plugin", "workspace"),
    "contractor": ("contractor", "freelance", "developer hours", "consulting",
                   "milestone", "copywriting", "translation", "design work"),
    "training": ("course", "training", "certification", "workshop", "learning"),
    "data": ("dataset", "data feed", "market data", "records", "data licence",
             "data license", "scraping"),
    "office": ("office", "supplies", "printer", "paper", "chairs", "stationery"),
    "travel": ("flight", "hotel", "travel", "airfare"),
    "gaming": ("game", "gaming", "skins", "loot"),
    "gift_cards": ("gift card", "gift cards", "voucher", "prepaid card"),
    "crypto": ("crypto", "bitcoin", "usdt", "wallet top-up", "token sale"),
    "personal": ("personal", "vacation", "jewelry", "jewellery", "luxury", "fashion"),
}

RECURRING_WORDS = ("subscription", "monthly", "recurring", "renewal", "per month",
                   "annual", "yearly", "quarterly", "weekly", "plan", "retainer")
ONE_TIME_WORDS = ("one-time", "one time", "one-off", "single purchase", "once")

INJECTION_PATTERNS = [
    r"ignore (all |any )?(previous|prior|above|earlier) (instructions|rules|prompts?)",
    r"disregard (the |all |any )?(rules|instructions|policy|policies)",
    r"\b(system|assistant|developer)\s*(prompt|message)?\s*:",
    r"<\|?(im_start|im_end|system|endoftext)\|?>",
    r"\byou are now\b",
    r"\b(set|mark|return|output)\b.{0,30}\b(risk|score)\b.{0,20}\b(0|zero|low)\b",
    r"\b(auto[- ]?approve|pre[- ]?approved|already approved|approve (this|immediately))\b",
    r"\bdo not (flag|review|escalate|block)\b",
    r"\b(override|bypass)\b.{0,30}\b(policy|rules?|checks?|limits?|review)\b",
    r"\brecommendation\b.{0,20}\ballow",
    r"\bas (the|an) (admin|administrator|owner)\b",
    r"\bjailbreak\b",
    # text addressed to the assessor itself, e.g. "note for the automated reviewer"
    r"\b(note|message|instructions?)\s+(for|to)\s+(the\s+)?(automated|ai|llm)\s+"
    r"(reviewer|assessor|scorer|checker)\b",
    r"\b(keep|treat|rate|score)\b.{0,30}\b(as\s+)?(minimal|low|no)\s+risk\b",
]
_INJECTION_RE = [re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS]

PRESSURE_RE = re.compile(
    r"\b(urgent(ly)?|immediately|asap|right now|within (the )?(hour|\d+ ?min)|"
    r"final notice|account (will be )?(suspended|closed|terminated)|last chance|"
    r"act now|before it'?s too late)\b",
    re.IGNORECASE,
)
HIGH_RISK_METHOD_RE = re.compile(
    r"\b(gift ?cards?|wire transfer|western union|moneygram|crypto(currency)?|"
    r"bitcoin|usdt|btc|eth wallet|send to (this|my) personal|friends and family)\b",
    re.IGNORECASE,
)

INTERVAL_MONTHS = {
    BillingInterval.daily: 1 / 30,
    BillingInterval.weekly: 12 / 52,
    BillingInterval.monthly: 1.0,
    BillingInterval.quarterly: 3.0,
    BillingInterval.annually: 12.0,
}

GENERIC_NAME_TOKENS = {"inc", "llc", "ltd", "co", "corp", "the", "and", "company",
                       "gmbh", "services", "service", "group", "api", "io", "app"}


def effective_expiry(req: PaymentRequest, received_at: datetime, tp: TimePolicy) -> datetime:
    """Server-computed expiry: the client value can only shorten the policy bounds."""
    max_expiry = received_at + timedelta(hours=tp.max_lifetime_hours)
    return min(req.expires_at or received_at + timedelta(minutes=tp.default_ttl_minutes),
               max_expiry)


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def injection_matches(text: str) -> list[str]:
    return [m.group(0)[:80] for rx in _INJECTION_RE if (m := rx.search(text))]


def infer_categories(text: str) -> set[str]:
    lowered = text.lower()
    found = set()
    for category, words in CATEGORY_KEYWORDS.items():
        if any(re.search(rf"\b{re.escape(w)}\b", lowered) for w in words):
            found.add(category)
    return found


def registrable_label(domain: str) -> str:
    """'billing.examplehosting.co.uk' -> 'examplehosting' (Public Suffix List)."""
    return textnorm.registrable_label(domain)


def base_domain(domain: str) -> str:
    """Registrable domain (eTLD+1) via the Public Suffix List."""
    return textnorm.registrable_domain(domain) or domain


_HOMOGLYPHS = str.maketrans({"0": "o", "1": "l", "3": "e", "5": "s", "7": "t", "@": "a",
                             "4": "a", "8": "b", "9": "g", "|": "l", "$": "s"})


def _skeleton(label: str) -> str:
    """ASCII look-alike folding for domain labels (digits, rn/m, vv/w)."""
    s = label.lower().translate(_HOMOGLYPHS).replace("rn", "m").replace("vv", "w")
    return s.replace("-", "").replace("_", "")


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _folded_host(domain: str) -> str:
    """Punycode -> Unicode -> non-ASCII confusables folded to ASCII (analysis only)."""
    return textnorm.detection_text(textnorm.unicode_host(domain))


def lookalike_of(domain: str, approved: list[str]) -> str | None:
    """Return the approved domain this one imitates, if any. A similarity signal for
    review, never proof of fraud. Exact hosts and subdomains of the same registrable
    domain are not lookalikes (subdomains are reported separately)."""
    if domain in approved:
        return None
    reg = textnorm.registrable_domain(domain)
    folded = _folded_host(domain)
    folded_reg = textnorm.registrable_domain(folded)
    label_skel = _skeleton(textnorm.registrable_label(folded))
    tokens = {_skeleton(t) for t in re.split(r"[.\-_]+", folded) if t}
    for good in approved:
        good_reg = textnorm.registrable_domain(good)
        if reg is not None and reg == good_reg:
            continue
        if folded_reg == good_reg:          # homograph: differs only by confusables
            return good
        good_label = textnorm.registrable_label(good)
        good_skel = _skeleton(good_label)
        if len(good_label) < 4:
            continue
        if label_skel == good_skel:
            return good
        if len(good_label) >= 6 and levenshtein(label_skel, good_skel) <= 2:
            return good
        # brand as a whole delimited token inside another registrable domain
        # (example-billing.com, secure-example.com.zip, brand.app.billing-center.net);
        # not a naive substring test, so 'exampleplumbing.org' is not matched.
        if len(good_skel) >= 6 and good_skel in tokens:
            return good
    return None


def _name_tokens(name: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+", textnorm.detection_text(name))
    return [t for t in tokens if len(t) >= 3 and t not in GENERIC_NAME_TOKENS]


def name_matches_domain(name: str, domain: str) -> bool:
    tokens = _name_tokens(name)
    if not tokens:
        return True  # nothing to compare; do not penalize
    squashed = _folded_host(domain).replace("-", "").replace(".", "")
    joined = "".join(tokens)
    if joined in squashed:
        return True
    return any(t in squashed for t in tokens if len(t) >= 4)


# ------------------------------------------------------------------ features


@dataclass
class Features:
    amount: float
    currency: str
    currency_allowed: bool
    direction_incoming: bool
    amount_over_hard_limit: bool
    amount_over_review_limit: bool
    per_transaction_hard_limit: str | None
    per_transaction_review_limit: str | None
    monthly_budget: float | None
    budget_ratio: float | None            # amount / monthly_budget
    budget_remaining: float | None
    exceeds_remaining_budget: bool
    no_spending_policy: bool
    cumulative_limit_exceeded: bool
    split_exceeds_hard_limit: bool
    possible_split_transaction: bool
    agent_budget_claim_mismatch: bool
    merchant_window_committed: float
    annualized_recurring_cost: float | None
    annualized_exceeds_annual_budget: bool
    merchant_domain: str | None
    missing_domain: bool
    missing_justification: bool
    missing_evidence: bool
    approved_merchant: bool
    approved_category: str | None
    known_from_history: bool
    blocked_domain: bool
    subdomain_of_approved: bool
    confusable_merchant_name: bool
    mixed_script_merchant: bool
    lookalike_target: str | None
    suspicious_tld: bool
    punycode_or_ip: bool
    name_domain_mismatch: bool
    domain_changed_from: str | None
    duplicate_request_id: bool
    duplicate_intent_of: str | None
    near_duplicate_of: str | None
    evidence_unverified: bool
    demo_default_budget: bool
    previous_reference_unknown: bool
    agent_history_count: int
    agent_median_amount: float | None
    amount_spike_ratio: float | None
    recurring: bool
    recurring_missing_interval: bool
    recurring_unexplained: bool
    recurring_but_one_time_language: bool
    unusual_interval: bool
    purpose_categories: list[str]
    merchant_category: str | None
    purpose_merchant_mismatch: bool
    injection_hits: list[str]
    pressure_hits: list[str]
    high_risk_method_hits: list[str]
    evidence_amount_mismatch: list[str]
    evidence_domain_mismatch: list[str]
    expired: bool
    lifetime_too_long: bool
    effective_expires_at: datetime
    stale: bool
    created_in_future: bool
    extra: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def extract_features(
    req: PaymentRequest, ctx: AssessmentContext, policy: RiskPolicy
) -> Features:
    limits = policy.limits
    approved_list = [m.domain.lower() for m in policy.approved_merchants]
    amount = float(req.amount)
    domain = req.merchant_domain
    name_key = textnorm.name_key(req.merchant_name)

    # --- merchant & domain (P0.6: exact host match only; subdomains need explicit opt-in)
    approved_entry = None
    if domain:
        approved_entry = next((m for m in policy.approved_merchants
                               if textnorm.host_matches(domain, m.domain, m.include_subdomains)),
                              None)
    subdomain_of_approved = bool(domain and approved_entry is None and any(
        textnorm.is_subdomain_of(domain, m.domain) for m in policy.approved_merchants))
    same_name_approved = [m for m in policy.approved_merchants
                          if textnorm.name_key(m.name) == name_key]
    confusable_name = any(
        textnorm.canonical_text(m.name) != textnorm.canonical_text(req.merchant_name)
        for m in same_name_approved)
    mixed_script = textnorm.is_mixed_script(req.merchant_name) or bool(
        domain and textnorm.is_mixed_script(textnorm.unicode_host(domain)))
    is_ip = bool(domain and _is_ip(domain))
    punycode_or_ip = bool(domain and ("xn--" in domain or is_ip))
    tld = domain.rsplit(".", 1)[-1] if domain and not is_ip else None
    lookalike = lookalike_of(domain, approved_list) if domain and not is_ip else None
    name_mismatch = bool(
        domain and not is_ip and approved_entry is None
        and not name_matches_domain(req.merchant_name, domain)
    )

    # --- history
    agent_hist = [h for h in ctx.history if h.agent_id == req.agent_id]
    # defence in depth: trusted history must belong to this agent even if a caller errs
    trusted = [h for h in ctx.trusted_history if h.agent_id == req.agent_id]
    merchant_trust = trusted + list(ctx.shared_trusted_history)
    known_domains = {h.merchant_domain for h in merchant_trust if h.merchant_domain}
    duplicate_id = ctx.request_id_already_claimed or any(
        h.request_id == req.request_id for h in ctx.history)
    window = timedelta(hours=limits.duplicate_intent_window_hours)
    ref_time = ctx.now  # server receipt time; client created_at is untrusted (P0.3)
    duplicate_of = None
    near_duplicate_of = None
    for h in agent_hist:
        if h.request_id == req.request_id:
            continue
        same_merchant = (  # normalized identity (P0.6)
            (domain and h.merchant_domain
             and base_domain(h.merchant_domain) == base_domain(domain))
            or textnorm.name_key(h.merchant_name) == name_key
        )
        if not (same_merchant and h.currency == req.currency
                and abs(ref_time - h.created_at) <= window):
            continue
        if h.amount == req.amount:
            duplicate_of = h.request_id
            break
        # near-duplicate (owner decision 9 / P04): within tolerance or one minor unit
        minor_unit = Decimal(1).scaleb(-CURRENCY_MINOR_UNITS[req.currency])
        tolerance = max(Decimal(str(limits.near_duplicate_tolerance_ratio))
                        * max(h.amount, req.amount), minor_unit)
        if (near_duplicate_of is None and limits.near_duplicate_tolerance_ratio > 0
                and abs(h.amount - req.amount) <= tolerance):
            near_duplicate_of = h.request_id
    domain_changed_from = None
    if domain and approved_entry is None:  # the approved domain itself is never a "change"
        for h in reversed(merchant_trust):  # unverified requests never redefine a domain
            if (textnorm.name_key(h.merchant_name) == name_key
                    and h.merchant_domain and base_domain(h.merchant_domain) != base_domain(domain)):
                domain_changed_from = h.merchant_domain
                break
        if domain_changed_from is None and same_name_approved:
            domain_changed_from = same_name_approved[0].domain
    prev_unknown = bool(
        req.previous_payment_reference
        and not any(h.request_id == req.previous_payment_reference for h in ctx.history)
    )
    # spend baseline: verified completed payments of this agent in this currency only
    same_ccy = [float(h.amount) for h in trusted if h.currency == req.currency]
    median_amt = statistics.median(same_ccy) if len(same_ccy) >= 3 else None
    spike = amount / median_amt if median_amt else None

    # --- budget: operator-owned snapshot only; agent budget fields are untrusted (P0.2)
    spend = ctx.spend
    no_policy = spend is None or not spend.policy_found
    exact = req.amount
    budget_dec = None if no_policy else spend.monthly_budget
    budget = float(budget_dec) if budget_dec is not None else None
    budget_ratio = amount / budget if budget else None
    remaining_dec = None if budget_dec is None else budget_dec - spend.committed_in_period
    remaining = float(remaining_dec) if remaining_dec is not None else None
    exceeds_remaining = bool(remaining_dec is not None and exact > remaining_dec)
    cumulative_exceeded = bool(
        not no_policy and spend.rolling_window_limit is not None
        and spend.rolling_window_committed + exact > spend.rolling_window_limit)
    merchant_window = Decimal(0) if no_policy else spend.merchant_window_committed
    # per-currency limits from trusted config; unknown when no policy (-> blocked anyway)
    hard_lim = None if no_policy else spend.per_transaction_hard_limit
    review_lim = None if no_policy else spend.per_transaction_review_limit
    over_hard = bool(hard_lim is not None and exact > hard_lim)
    over_review = bool(review_lim is not None and exact > review_lim)
    split_exceeds_hard = bool(hard_lim is not None and merchant_window > 0
                              and exact <= hard_lim and merchant_window + exact > hard_lim)
    possible_split = bool(review_lim is not None and merchant_window > 0 and exact <= review_lim
                          and merchant_window + exact > review_lim and not split_exceeds_hard)
    claimed_budget_mismatch = bool(
        req.monthly_budget is not None and budget_dec is not None
        and req.monthly_budget != budget_dec)
    annualized = None
    annual_exceeds = False
    if req.recurring and req.billing_interval:
        annualized = amount * 12 / INTERVAL_MONTHS[req.billing_interval]
        annual_exceeds = bool(budget and annualized > budget * 12 + 1e-9)

    # --- text
    text_fields = [req.purpose, req.business_justification, req.merchant_name]
    text_fields += [e.description for e in req.evidence]
    text_fields += [e.reference or "" for e in req.evidence]
    # P0.6: every detector sees NFKC/ignorable-stripped/casefolded/confusable-folded text
    all_text = textnorm.detection_text("\n".join(t for t in text_fields if t))
    purpose_lower = textnorm.detection_text(f"{req.purpose}\n{req.business_justification}")
    # what is being bought comes from `purpose`; the justification is only a fallback
    purpose_cats = (infer_categories(textnorm.detection_text(req.purpose))
                    or infer_categories(textnorm.detection_text(req.business_justification)))
    merchant_cat = approved_entry.category if approved_entry else None
    if merchant_cat is None:
        name_cats = infer_categories(textnorm.detection_text(
            f"{req.merchant_name} {domain or ''}".replace(".", " ")))
        merchant_cat = sorted(name_cats)[0] if len(name_cats) == 1 else None
    mismatch = bool(merchant_cat and purpose_cats and merchant_cat not in purpose_cats)
    # risky purchase categories are a mismatch for a business agent regardless of merchant
    if purpose_cats & {"gift_cards", "crypto", "personal", "gaming"}:
        mismatch = True

    mentions_recurring = any(w in purpose_lower for w in RECURRING_WORDS)
    mentions_one_time = any(w in purpose_lower for w in ONE_TIME_WORDS)
    unusual_interval = bool(
        req.recurring and req.billing_interval in (BillingInterval.daily, BillingInterval.weekly)
        and (merchant_cat in {"software", "hosting", "training", "data", "office"}
             or not merchant_cat)
    )

    # --- evidence consistency (claims only; nothing is verified)
    ev_amount_mismatch, ev_domain_mismatch = [], []
    for i, ev in enumerate(req.evidence):
        if ev.amount is not None and (ev.currency in (None, req.currency)):
            if abs(float(ev.amount) - amount) > max(0.01, 0.01 * amount):
                ev_amount_mismatch.append(f"evidence[{i}] amount {ev.amount} != {req.amount}")
        if ev.merchant_domain and domain and base_domain(ev.merchant_domain) != base_domain(domain):
            ev_domain_mismatch.append(f"evidence[{i}] domain {ev.merchant_domain} != {domain}")

    # --- time (ctx.now is the trusted server receipt time)
    tp = policy.time_policy
    max_expiry = ctx.now + timedelta(hours=tp.max_lifetime_hours)
    expired = bool(req.expires_at and req.expires_at <= ctx.now)
    lifetime_too_long = bool(req.expires_at and req.expires_at > max_expiry)
    effective_expires_at = effective_expiry(req, ctx.now, tp)
    stale = bool(req.created_at and ctx.now - req.created_at
                 > timedelta(hours=tp.max_client_age_hours))
    future = bool(req.created_at and req.created_at - ctx.now
                  > timedelta(minutes=tp.max_client_clock_skew_minutes))

    return Features(
        amount=amount,
        currency=req.currency,
        currency_allowed=req.currency in limits.allowed_currencies,
        direction_incoming=req.direction == Direction.incoming,
        amount_over_hard_limit=over_hard,
        amount_over_review_limit=over_review,
        per_transaction_hard_limit=str(hard_lim) if hard_lim is not None else None,
        per_transaction_review_limit=str(review_lim) if review_lim is not None else None,
        monthly_budget=budget,
        budget_ratio=round(budget_ratio, 4) if budget_ratio is not None else None,
        budget_remaining=remaining,
        exceeds_remaining_budget=exceeds_remaining,
        no_spending_policy=no_policy,
        cumulative_limit_exceeded=cumulative_exceeded,
        split_exceeds_hard_limit=split_exceeds_hard,
        possible_split_transaction=possible_split,
        agent_budget_claim_mismatch=claimed_budget_mismatch,
        merchant_window_committed=float(merchant_window),
        annualized_recurring_cost=round(annualized, 2) if annualized else None,
        annualized_exceeds_annual_budget=annual_exceeds,
        merchant_domain=domain,
        missing_domain=domain is None,
        missing_justification=len(req.business_justification) < 10,
        missing_evidence=len(req.evidence) == 0,
        approved_merchant=approved_entry is not None,
        approved_category=approved_entry.category if approved_entry else None,
        known_from_history=bool(domain and domain in known_domains),
        blocked_domain=bool(domain and any(
            domain == b or textnorm.is_subdomain_of(domain, b) for b in policy.blocked_domains)),
        subdomain_of_approved=subdomain_of_approved,
        confusable_merchant_name=confusable_name,
        mixed_script_merchant=mixed_script,
        lookalike_target=lookalike,
        suspicious_tld=bool(tld and tld in policy.suspicious_tlds),
        punycode_or_ip=punycode_or_ip,
        name_domain_mismatch=name_mismatch,
        domain_changed_from=domain_changed_from,
        duplicate_request_id=duplicate_id,
        duplicate_intent_of=duplicate_of,
        near_duplicate_of=None if duplicate_of else near_duplicate_of,
        demo_default_budget=bool(spend is not None and spend.demo_default_budget),
        evidence_unverified=bool(req.evidence)
        and not policy.evidence_policy.claimed_evidence_reduces_risk,
        previous_reference_unknown=prev_unknown,
        agent_history_count=len(agent_hist),
        agent_median_amount=median_amt,
        amount_spike_ratio=round(spike, 3) if spike else None,
        recurring=req.recurring,
        recurring_missing_interval=req.recurring and req.billing_interval is None,
        recurring_unexplained=req.recurring and not mentions_recurring,
        recurring_but_one_time_language=req.recurring and mentions_one_time,
        unusual_interval=unusual_interval,
        purpose_categories=sorted(purpose_cats),
        merchant_category=merchant_cat,
        purpose_merchant_mismatch=mismatch,
        injection_hits=injection_matches(all_text),
        pressure_hits=[m.group(0) for m in PRESSURE_RE.finditer(all_text)][:5],
        high_risk_method_hits=[m.group(0) for m in HIGH_RISK_METHOD_RE.finditer(all_text)][:5],
        evidence_amount_mismatch=ev_amount_mismatch,
        evidence_domain_mismatch=ev_domain_mismatch,
        expired=expired,
        lifetime_too_long=lifetime_too_long,
        effective_expires_at=effective_expires_at,
        stale=stale,
        created_in_future=future,
    )


# Numeric vector used by the ML baseline. Only engineered features — no raw
# text, request IDs, labels or scenario names — to avoid trivial leakage.
ML_FEATURE_NAMES: list[str] = [
    "log_amount", "budget_ratio", "budget_missing", "exceeds_remaining_budget",
    "annualized_exceeds_annual_budget", "over_review_limit", "over_hard_limit",
    "missing_domain", "missing_justification", "missing_evidence",
    "approved_merchant", "known_from_history", "lookalike", "suspicious_tld",
    "punycode_or_ip", "name_domain_mismatch", "domain_changed", "duplicate_intent",
    "duplicate_request_id", "previous_reference_unknown", "log_spike_ratio",
    "recurring", "recurring_missing_interval", "recurring_unexplained",
    "recurring_one_time_language", "unusual_interval", "purpose_merchant_mismatch",
    "injection_hits", "pressure_hits", "high_risk_method_hits",
    "evidence_amount_mismatch", "evidence_domain_mismatch", "expired_or_stale",
    "no_history",
]


def ml_vector(f: Features) -> list[float]:
    return [
        math.log10(1 + f.amount),
        min(f.budget_ratio, 5.0) if f.budget_ratio is not None else 0.0,
        float(f.monthly_budget is None),
        float(f.exceeds_remaining_budget),
        float(f.annualized_exceeds_annual_budget),
        float(f.amount_over_review_limit),
        float(f.amount_over_hard_limit),
        float(f.missing_domain),
        float(f.missing_justification),
        # unverifiable claimed evidence is presented to the ML as missing (decision 9)
        float(f.missing_evidence or f.evidence_unverified),
        float(f.approved_merchant),
        float(f.known_from_history),
        float(f.lookalike_target is not None),
        float(f.suspicious_tld),
        float(f.punycode_or_ip),
        float(f.name_domain_mismatch),
        float(f.domain_changed_from is not None),
        float(f.duplicate_intent_of is not None),
        float(f.duplicate_request_id),
        float(f.previous_reference_unknown),
        math.log(max(f.amount_spike_ratio, 1e-3)) if f.amount_spike_ratio else 0.0,
        float(f.recurring),
        float(f.recurring_missing_interval),
        float(f.recurring_unexplained),
        float(f.recurring_but_one_time_language),
        float(f.unusual_interval),
        float(f.purpose_merchant_mismatch),
        float(min(len(f.injection_hits), 3)),
        float(min(len(f.pressure_hits), 3)),
        float(min(len(f.high_risk_method_hits), 3)),
        float(bool(f.evidence_amount_mismatch)),
        float(bool(f.evidence_domain_mismatch)),
        float(f.expired or f.stale),
        float(f.agent_history_count == 0),
    ]
