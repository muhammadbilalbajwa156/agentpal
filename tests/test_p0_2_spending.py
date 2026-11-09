"""P0.2 regression: operator-owned budgets and a concurrency-safe spending ledger
(review probes P01, P02).

Before the fix, budgets and spend came from agent-supplied request fields: omitting
`monthly_budget` removed the budget hard block, and near-limit splits were never
aggregated.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from agentpal.analyzers import HeuristicAnalyzer
from agentpal.config import load_policy
from agentpal.schemas import PaymentRequest
from agentpal.service import RiskService
from agentpal.spending import ApprovalTerms, InvalidLedgerTransition, LedgerState, SpendPolicy
from agentpal.store import AssessmentStore

from conftest import VALID

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def spend_policy(budget="100.00", window=None, default=True) -> SpendPolicy:
    limits = {"USD": {"monthly_budget": budget, "rolling_window_limit": window or budget,
                      "per_transaction_hard_limit": "5000.00",
                      "per_transaction_review_limit": "1000.00"}}
    return SpendPolicy.model_validate({
        "policy_version": "test",
        # a default budget is only valid in a demo-labelled configuration (final decision 4)
        "configuration": "demo" if default else "operator",
        "default_agent": {"owner_id": "owner_t", "currencies": limits} if default else None,
        "agents": {"agent_demo_001": {"owner_id": "owner_t", "currencies": limits}},
    })


def make_service(db=":memory:", clock=None, **kw) -> RiskService:
    return RiskService(load_policy(), AssessmentStore(db), [HeuristicAnalyzer()],
                       clock=clock or Clock(), spend_policy=spend_policy(**kw))


def req(**kw) -> PaymentRequest:
    base = {**VALID, "recurring": False, "billing_interval": None}
    return PaymentRequest.model_validate({**base, **kw})


def codes(a) -> set[str]:
    return {s.code for s in a.signals}


# ---------------------------------------------------- agent fields are untrusted

def test_omitting_budget_field_does_not_disable_budget_control():
    s = make_service()
    a = s.assess(req(request_id="req_b_1", amount=150, monthly_budget=None))
    assert "BUDGET_EXCEEDED" in a.hard_violations and a.recommendation == "block"


def test_falsely_high_budget_claim_is_ignored_and_flagged():
    s = make_service()
    a = s.assess(req(request_id="req_b_2", amount=150, monthly_budget=100000))
    assert "BUDGET_EXCEEDED" in a.hard_violations
    assert "AGENT_BUDGET_CLAIM_MISMATCH" in codes(a)


def test_agent_reported_spend_is_ignored_ledger_governs():
    s = make_service()
    first = s.assess(req(request_id="req_b_3", amount=90, merchant_domain="taskloom.app",
                         merchant_name="Taskloom", purpose="Workspace seats"))
    assert first.recommendation != "block"
    # agent claims nothing was spent; the ledger holds a 90.00 reservation
    second = s.assess(req(request_id="req_b_4", amount=20, budget_spent_this_period=0))
    assert "BUDGET_EXCEEDED" in second.hard_violations


def test_unconfigured_agent_is_blocked_not_uncontrolled():
    s = make_service(default=False)
    a = s.assess(req(request_id="req_b_5", agent_id="agent_unknown_1"))
    assert "NO_SPENDING_POLICY" in a.hard_violations and a.recommendation == "block"


# ------------------------------------------------------- split / cumulative spend

def test_three_near_limit_transactions_collectively_exceeding_hard_limit():
    s = make_service(budget="50000.00")
    results = [s.assess(req(request_id=f"req_split_{i}", amount=f"{4999 - i}.00",
                            purpose="Managed database storage upgrade"))
               for i in range(3)]
    assert not results[0].hard_violations
    for a in results[1:]:
        assert "SPLIT_TRANSACTION_EXCEEDS_HARD_LIMIT" in a.hard_violations
        assert a.recommendation == "block"
    totals = s.store.spend_totals("agent_demo_001", "USD", T0)
    assert totals["committed"] == Decimal("4999.00")  # blocked parts hold nothing


def test_splits_below_review_limit_are_flagged_for_review():
    s = make_service(budget="50000.00")
    s.assess(req(request_id="req_rs_0", amount="900.00", purpose="Managed database storage upgrade"))
    a = s.assess(req(request_id="req_rs_1", amount="800.00",
                     purpose="Managed database storage upgrade"))
    assert "POSSIBLE_SPLIT_TRANSACTION" in codes(a) and a.recommendation != "allow_for_review"


def test_rolling_window_cumulative_limit_across_merchants():
    s = make_service(budget="10000.00", window="1000.00")
    s.assess(req(request_id="req_rw_0", amount="600.00"))
    a = s.assess(req(request_id="req_rw_1", amount="600.00", merchant_name="Taskloom",
                     merchant_domain="taskloom.app", purpose="Workspace seats"))
    assert "CUMULATIVE_LIMIT_EXCEEDED" in a.hard_violations


def test_concurrent_requests_cannot_overspend_remaining_budget(tmp_path):
    s = make_service(db=tmp_path / "budget.db")
    n = 8
    barrier = threading.Barrier(n)
    out = []

    def worker(i):
        barrier.wait()
        out.append(s.assess(req(request_id=f"req_cc_{i}", amount="30.00",
                                # distinct registrable domains (P0.6 groups subdomains
                                # of one registrable domain as the same merchant)
                                merchant_domain=f"vendor{i}-shop.org",
                                merchant_name=f"Vendor{i}")))

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    reserved = [a for a in out if "BUDGET_EXCEEDED" not in a.hard_violations]
    assert len(reserved) == 3  # 3 x 30 = 90 <= 100; a 4th would exceed
    totals = s.store.spend_totals("agent_demo_001", "USD", T0)
    assert totals["committed"] == Decimal("90.00") <= Decimal("100.00")


# ------------------------------------------------------------ ledger semantics

def test_assessment_never_counts_as_completed_spending():
    s = make_service()
    a = s.assess(req(request_id="req_l_1"))
    assert a.recommendation == "allow_for_review"
    assert s.store.ledger_state("req_l_1") == LedgerState.reserved
    totals = s.store.spend_totals("agent_demo_001", "USD", T0)
    assert totals == {"committed": Decimal("49.00"), "completed": Decimal("0")}


def test_rejected_request_does_not_increase_spending():
    s = make_service()
    a = s.assess(req(request_id="req_l_2", amount=150))
    assert a.recommendation == "block"
    assert s.store.ledger_state("req_l_2") is None  # never reserved
    # a request blocked for a non-financial reason has its reservation released
    b = s.assess(req(request_id="req_l_3", merchant_domain="paypal-billing-support.com"))
    assert "BLOCKED_MERCHANT_DOMAIN" in b.hard_violations
    assert s.store.ledger_state("req_l_3") == LedgerState.released
    assert s.store.spend_totals("agent_demo_001", "USD", T0) == {
        "committed": Decimal("0"), "completed": Decimal("0")}


def _advance(s, r, state: str) -> None:
    """Drive the ledger through the bound lifecycle methods (SEC-14: the generic
    transition may only release)."""
    terms = ApprovalTerms.from_request(r)
    if state == "approved":
        s.store.record_owner_decision(r.request_id, "approved", "owner", terms=terms)
    elif state == "pending":
        s.store.record_payment_attempt(r.request_id, "sandbox", "att-1", terms=terms)
    elif state in ("completed", "failed", "cancelled"):
        s.store.record_payment_outcome(r.request_id, state, verified=True,
                                       verification_method="test-fixture",
                                       provider_reference="cap-1")
    else:
        s.store.transition_ledger(r.request_id, LedgerState(state), actor="test",
                                  reason=state)


@pytest.mark.parametrize("path,final", [
    (["approved", "pending", "failed"], LedgerState.failed),
    (["approved", "pending", "cancelled"], LedgerState.cancelled),
    (["approved", "released"], LedgerState.released),
    (["released"], LedgerState.released),
])
def test_failed_or_cancelled_transactions_release_hold(path, final):
    s = make_service()
    r = req(request_id="req_l_4")
    s.assess(r)
    for state in path:
        _advance(s, r, state)
    assert s.store.ledger_state("req_l_4") == final
    assert s.store.spend_totals("agent_demo_001", "USD", T0)["committed"] == Decimal("0")
    events = s.store.ledger_events("req_l_4")
    assert [e["to_state"] for e in events] == ["reserved", *path]


def test_completed_counts_as_spending_and_is_terminal():
    s = make_service()
    r = req(request_id="req_l_5")
    s.assess(r)
    for state in ("approved", "pending", "completed"):
        _advance(s, r, state)
    assert s.store.spend_totals("agent_demo_001", "USD", T0) == {
        "committed": Decimal("49.00"), "completed": Decimal("49.00")}
    with pytest.raises(InvalidLedgerTransition):
        s.store.transition_ledger("req_l_5", LedgerState.released, actor="test")


def test_unapproved_reservation_expires_and_releases():
    clock = Clock()
    s = make_service(clock=clock)
    a = s.assess(req(request_id="req_l_6", amount="90.00"))
    clock.t = a.effective_expires_at + timedelta(seconds=1)
    # after expiry the hold no longer counts, and the next request is not blocked by it
    b = s.assess(req(request_id="req_l_7", amount="90.00", merchant_name="Taskloom",
                     merchant_domain="taskloom.app", purpose="Workspace seats"))
    assert "BUDGET_EXCEEDED" not in b.hard_violations
    assert s.store.ledger_state("req_l_6") == LedgerState.released
    assert s.store.ledger_events("req_l_6")[-1]["reason"] == "reservation_expired"


def test_ledger_records_currency_and_exact_amount():
    s = make_service()
    s.assess(req(request_id="req_l_8", amount="12.30"))
    entry = s.store.ledger_entry("req_l_8")
    assert entry["currency"] == "USD" and entry["amount"] == "12.30"


def test_pure_assessment_without_trusted_spend_policy_fails_closed():
    from agentpal.features import AssessmentContext
    from agentpal.orchestrator import assess

    a = assess(req(), AssessmentContext(now=T0), load_policy(), [])
    assert "NO_SPENDING_POLICY" in a.hard_violations and a.recommendation == "block"
