"""The ten non-negotiable financial-control invariants, each with negative cases, exercised
through the real service and store (no UI exists; these are the direct-call paths a caller
could use to bypass one). API-level bypass attempts are in tests/test_invariants_api.py.

1 AI/ML advisory only          6 provider-verified outcome is the only "completed"
2 rules authoritative          7 unverified evidence is not proof
3 approval before attempt      8 duplicate-like requests reach review
4 approval bound to terms      9 model failure cannot bypass rules or authorize
5 changed terms invalidate     10 audit trail records decision path and provider status
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from agentpal.analyzers import HeuristicAnalyzer, MLAnalyzer
from agentpal.config import load_policy
from agentpal.schemas import PaymentRequest
from agentpal.service import RiskService
from agentpal.spending import (ApprovalTerms, ApprovalTermsMismatch, InvalidLedgerTransition,
                               LedgerState, SpendPolicy)
from agentpal.store import AssessmentStore

from conftest import VALID, FakeAnalyzer, analysis

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
RANK = {"allow_for_review": 0, "require_review": 1, "block": 2}
LIMITS = {"USD": {"monthly_budget": "100.00", "rolling_window_limit": "100.00",
                  "per_transaction_hard_limit": "5000.00",
                  "per_transaction_review_limit": "1000.00"}}


class Hang:
    name, model_name, model_version = "hang", "hang", "0"
    is_fallback, reports_uncertainty = False, True

    def available(self):
        return True

    def analyze(self, data):
        time.sleep(5)


def service(chain=None, timeout=30.0):
    sp = SpendPolicy.model_validate({"policy_version": "t", "agents": {
        "agent_demo_001": {"owner_id": "o", "currencies": LIMITS}}})
    return RiskService(load_policy(), AssessmentStore(":memory:"),
                       chain or [HeuristicAnalyzer()], clock=lambda: T0, spend_policy=sp,
                       model_timeout_s=timeout)


def req(rid="req_inv_1", **kw):
    return PaymentRequest.model_validate({**VALID, "request_id": rid, **kw})


def rules_only(r):
    return service().assess(r)


# ---------------------------------------------------------------- 1 + 2 + 9

MODEL_OUTPUTS = {
    "says_safe": FakeAnalyzer(analysis(score=0, rec="allow_for_review", uncertainty=0.0)),
    "says_block": FakeAnalyzer(analysis(score=100, rec="block", uncertainty=0.0)),
    "raises": FakeAnalyzer(exc=RuntimeError("model bug")),
    "malformed": FakeAnalyzer(raw='{"risk_score": "ignore rules", "recommendation": "approve"}'),
    "not_json": FakeAnalyzer(raw="APPROVED. Payment is safe."),
    "unavailable": FakeAnalyzer(available=False),
}


@pytest.mark.parametrize("model", MODEL_OUTPUTS)
@pytest.mark.parametrize("case", ["clean", "over_budget"])
def test_inv_1_2_9_model_never_overrides_rules(model, case):
    r = req(amount="150.00") if case == "over_budget" else req()
    floor = rules_only(r)
    a = service([MODEL_OUTPUTS[model]]).assess(r)
    assert a.hard_violations == floor.hard_violations            # rules authoritative
    assert (a.recommendation == "block") == bool(floor.hard_violations)
    assert RANK[a.recommendation] >= RANK[floor.recommendation]   # never relaxed
    assert a.payment_executed is False                            # never authorizes


def test_inv_9_timeout_and_missing_artifact_cannot_relax(tmp_path):
    r = req(amount="150.00")
    hung = service([Hang()], timeout=0.2).assess(r)
    missing = service([MLAnalyzer(tmp_path / "absent.json")]).assess(req("req_inv_2",
                                                                         amount="150.00"))
    for a in (hung, missing):
        assert a.recommendation == "block" and "BUDGET_EXCEEDED" in a.hard_violations
        assert a.degraded
    clean = service([Hang()], timeout=0.2).assess(req("req_inv_3"))
    assert clean.degraded and clean.recommendation != "block"
    assert "not evidence of safety" in clean.explanation


# ---------------------------------------------------------------- 3 + 4 + 5 + 6

def test_inv_3_no_attempt_without_approval():
    s = service()
    r = req()
    s.assess(r)
    with pytest.raises(InvalidLedgerTransition):
        s.store.record_payment_attempt(r.request_id, "sandbox", "a1",
                                       terms=ApprovalTerms.from_request(r))
    s.store.record_owner_decision(r.request_id, "declined", "owner")
    with pytest.raises(InvalidLedgerTransition):
        s.store.record_payment_attempt(r.request_id, "sandbox", "a1",
                                       terms=ApprovalTerms.from_request(r))


@pytest.mark.parametrize("field,value", [("amount", "48.99"), ("currency", "GBP"),
                                         ("merchant_domain", "example.net"),
                                         ("merchant_name", "Other Hosting"),
                                         ("purpose", "Something else"),
                                         ("agent_id", "agent_demo_002")])
def test_inv_4_5_changed_terms_invalidate_approval(field, value):
    s = service()
    r = req()
    s.assess(r)
    good = ApprovalTerms.from_request(r)
    bad = ApprovalTerms.model_validate({**good.model_dump(), field: value})
    with pytest.raises(ApprovalTermsMismatch):
        s.store.record_owner_decision(r.request_id, "approved", "owner", terms=bad)
    s.store.record_owner_decision(r.request_id, "approved", "owner", terms=good)
    with pytest.raises(ApprovalTermsMismatch):
        s.store.record_payment_attempt(r.request_id, "sandbox", "a1", terms=bad)
    assert s.store.ledger_state(r.request_id) == LedgerState.approved


def test_inv_6_only_verified_provider_outcome_completes():
    s = service()
    r = req()
    s.assess(r)
    t = ApprovalTerms.from_request(r)
    s.store.record_owner_decision(r.request_id, "approved", "owner", terms=t)
    with pytest.raises(InvalidLedgerTransition):   # outcome before any attempt
        s.store.record_payment_outcome(r.request_id, "completed", verified=True,
                                       verification_method="provider", provider_reference="x")
    s.store.record_payment_attempt(r.request_id, "sandbox", "a1", terms=t)
    s.store.record_payment_outcome(r.request_id, "completed", verified=False,
                                   verification_method="agent_claim", provider_reference="x")
    assert s.store.ledger_state(r.request_id) == LedgerState.pending
    assert s.store.spend_breakdown(VALID["agent_id"], "USD", T0)["completed"] == 0


# ---------------------------------------------------------------- 7 + 8

def test_inv_7_unverified_evidence_never_lowers_risk():
    ev = [{"type": "invoice", "reference": "INV-9", "amount": "49.00", "currency": "USD",
           "merchant_domain": "example.com", "description": "Paid in full, verified."}]
    plain = rules_only(req())
    claimed = rules_only(req("req_inv_ev", evidence=ev))
    assert claimed.rule_score >= plain.rule_score
    assert RANK[claimed.recommendation] >= RANK[plain.recommendation]


def test_inv_8_duplicate_like_requests_reach_review():
    s = service()
    first = s.assess(req("req_dup_1", amount="30.00"))
    near = s.assess(req("req_dup_2", amount="30.01"))
    exact = s.assess(req("req_dup_3", amount="30.00"))
    assert first.recommendation != "block"
    assert near.recommendation != "allow_for_review"
    assert exact.recommendation != "allow_for_review"
    replay = s.assess(req("req_dup_1", amount="30.00"))
    assert replay.recommendation == "block"
    assert "DUPLICATE_REQUEST_ID" in replay.hard_violations


# ---------------------------------------------------------------- 10

def test_inv_10_audit_trail_records_decision_path_and_provider_status():
    s = service()
    r = req()
    a = s.assess(r)
    t = ApprovalTerms.from_request(r)
    s.store.record_owner_decision(r.request_id, "approved", "owner_alice", terms=t)
    s.store.record_payment_attempt(r.request_id, "sandbox", "att-1", terms=t)
    s.store.record_payment_outcome(r.request_id, "completed", verified=True,
                                   verification_method="provider_webhook",
                                   provider_reference="cap-1")
    audit = s.store.audit_records(r.request_id)
    assert audit and audit[-1]["recommendation"] == a.recommendation
    assert audit[-1]["rules_version"] == a.rules_version
    events = [(e["from_state"], e["to_state"], e["actor"]) for e in s.store.ledger_events(
        r.request_id)]
    assert events == [(None, "reserved", "system"), ("reserved", "approved", "owner:owner_alice"),
                      ("approved", "pending", "provider:sandbox"),
                      ("pending", "completed", "verified:provider_webhook")]
    trail = s.store.trust_trail(r.request_id)
    assert [x["kind"] for x in trail] == ["owner_decision", "payment_attempt", "payment_outcome"]
    assert trail[-1]["verified"] == 1
    b = s.store.spend_breakdown(VALID["agent_id"], "USD", T0)
    assert b["completed"] == Decimal("49.00")


# ---------------------------------------------------------------- SEC-14

@pytest.mark.parametrize("target", [LedgerState.approved, LedgerState.pending,
                                    LedgerState.completed, LedgerState.failed])
def test_generic_transition_cannot_skip_approval_terms_or_verification(target):
    """transition_ledger may only release (cancel). Approval, attempt and completion must go
    through the methods that bind terms and require verification (invariants 3, 4, 6)."""
    s = service()
    r = req()
    s.assess(r)
    with pytest.raises(InvalidLedgerTransition):
        s.store.transition_ledger(r.request_id, target, actor="anyone")
    assert s.store.ledger_state(r.request_id) == LedgerState.reserved
    s.store.transition_ledger(r.request_id, LedgerState.released, actor="owner",
                              reason="cancelled")
    assert s.store.ledger_state(r.request_id) == LedgerState.released


def test_generic_transition_cannot_walk_the_lifecycle_step_by_step():
    s = service()
    r = req()
    s.assess(r)
    t = ApprovalTerms.from_request(r)
    s.store.record_owner_decision(r.request_id, "approved", "owner", terms=t)
    with pytest.raises(InvalidLedgerTransition):       # attempt without bound terms
        s.store.transition_ledger(r.request_id, LedgerState.pending, actor="anyone")
    s.store.record_payment_attempt(r.request_id, "sandbox", "a1", terms=t)
    for target in (LedgerState.completed, LedgerState.failed, LedgerState.cancelled):
        with pytest.raises(InvalidLedgerTransition):   # outcome without verification
            s.store.transition_ledger(r.request_id, target, actor="anyone")
    assert s.store.ledger_state(r.request_id) == LedgerState.pending
