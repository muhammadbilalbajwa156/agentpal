"""Owner decision 9: near-duplicate amounts (review P04) and fabricated-evidence influence
(review P13/P16).

Before this change, a repeat with amount +0.01 was not a duplicate, and any agent-claimed
(unverifiable) invoice removed MISSING_EVIDENCE points and lowered the ML's estimate.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from agentpal.analyzers import HeuristicAnalyzer, MLAnalyzer
from agentpal.config import PACKAGE_ROOT
from agentpal.features import HistoricalPayment, extract_features, ml_vector, ML_FEATURE_NAMES
from agentpal.integrity import load_manifest
from agentpal.orchestrator import assess

from conftest import NOW, VALID, ctx, make_request

ORDER = {"allow_for_review": 0, "require_review": 1, "block": 2}


def prior(amount="49.00", hours_ago=2, domain="example.com", agent=VALID["agent_id"],
          name="Example Hosting"):
    return HistoricalPayment(request_id="req_prior_1", agent_id=agent,
                             merchant_name=name, merchant_domain=domain,
                             amount=Decimal(amount), currency="USD",
                             created_at=NOW - timedelta(hours=hours_ago))


def codes(req, history, policy):
    a = assess(req, ctx(history), policy, [HeuristicAnalyzer()])
    return {s.code for s in a.signals}, a


# ------------------------------------------------------------ near-duplicates (P04)

@pytest.mark.parametrize("amount", ["49.01", "48.99", "49.98"])  # +-1 cent, ~+2%
def test_near_duplicate_amount_is_flagged(policy, amount):
    found, a = codes(make_request(amount=amount), [prior()], policy)
    assert "NEAR_DUPLICATE_PAYMENT_INTENT" in found
    assert a.recommendation != "allow_for_review"


def test_exact_duplicate_keeps_exact_rule(policy):
    found, _ = codes(make_request(amount="49.00"), [prior()], policy)
    assert "DUPLICATE_PAYMENT_INTENT" in found and "NEAR_DUPLICATE_PAYMENT_INTENT" not in found


@pytest.mark.parametrize("history", [
    [prior(amount="52.00")],                        # ~6% apart: outside tolerance
    [prior(hours_ago=24 * 10)],                     # outside the window
    [prior(domain="taskloom.app", name="Taskloom")],  # different merchant (name + domain)
    [prior(agent="agent_other_1")],                 # different agent
])
def test_near_duplicate_not_flagged_outside_policy(policy, history):
    found, _ = codes(make_request(amount="49.01"), history, policy)
    assert "NEAR_DUPLICATE_PAYMENT_INTENT" not in found


def test_near_duplicate_tolerance_is_configurable(policy):
    p = policy.model_copy(deep=True)
    p.limits.near_duplicate_tolerance_ratio = 0
    found, _ = codes(make_request(amount="49.98"), [prior()], p)
    assert "NEAR_DUPLICATE_PAYMENT_INTENT" not in found


# ------------------------------------------------------- fabricated evidence (P13/P16)

FAKE_INVOICE = [{"type": "invoice", "amount": "49.00", "currency": "USD",
                 "merchant_domain": "example.com", "description": "totally real invoice"}]


@pytest.mark.parametrize("overrides", [
    {}, {"merchant_name": "Hostwise", "merchant_domain": "hostwise.net"},
    {"amount": 90}, {"purpose": "Gift cards for the team", "recurring": False,
                     "billing_interval": None},
])
def test_claimed_evidence_never_lowers_rules_outcome(policy, overrides):
    without = assess(make_request(**overrides), ctx(), policy, [HeuristicAnalyzer()])
    with_ev = assess(make_request(evidence=FAKE_INVOICE, **overrides), ctx(), policy,
                     [HeuristicAnalyzer()])
    assert with_ev.rule_score >= without.rule_score
    assert ORDER[with_ev.recommendation] >= ORDER[without.recommendation]
    assert "EVIDENCE_UNVERIFIED" in {s.code for s in with_ev.signals}


def test_evidence_mismatch_still_raises_risk(policy):
    bad = [{"type": "invoice", "amount": "10.00", "currency": "USD"}]
    found, _ = codes(make_request(evidence=bad), [], policy)
    assert "EVIDENCE_AMOUNT_MISMATCH" in found


def test_ml_input_treats_claimed_evidence_as_missing(policy):
    i = ML_FEATURE_NAMES.index("missing_evidence")
    v = ml_vector(extract_features(make_request(evidence=FAKE_INVOICE), ctx(), policy))
    assert v[i] == 1.0
    p = policy.model_copy(deep=True)
    p.evidence_policy.claimed_evidence_reduces_risk = True  # explicit, documented opt-in
    v2 = ml_vector(extract_features(make_request(evidence=FAKE_INVOICE), ctx(), p))
    assert v2[i] == 0.0


def test_claimed_evidence_does_not_avoid_ml_escalation(policy):
    path = PACKAGE_ROOT / "artifacts" / "baseline_classifier.json"
    ml = MLAnalyzer(path, load_manifest(PACKAGE_ROOT / "config" / "model_manifest.json")
                    .digest_for("ml"))
    if not ml.available():
        pytest.skip("pinned ML artifact not available")
    without = assess(make_request(), ctx(), policy, [ml])
    with_ev = assess(make_request(evidence=FAKE_INVOICE), ctx(), policy, [ml])
    assert ORDER[with_ev.recommendation] >= ORDER[without.recommendation]
