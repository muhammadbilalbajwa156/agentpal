"""Owner decisions 4 (separated amounts) and 6 (approval reservation validity).

6: an approval holds the reservation for a configurable window (default 15 minutes);
   expired or cancelled holds are released safely; concurrent approvals can never exceed
   the available budget; approval != payment attempt != provider-confirmed completion.
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
from agentpal.spending import InvalidLedgerTransition, LedgerState, SpendPolicy, load_spend_policy
from agentpal.store import AssessmentStore

from conftest import remember, terms, VALID

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
LIMITS = {"USD": {"monthly_budget": "100.00", "rolling_window_limit": "100.00",
                  "per_transaction_hard_limit": "5000.00",
                  "per_transaction_review_limit": "1000.00"}}


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


def make(db=":memory:", validity=15):
    sp = SpendPolicy.model_validate({
        "policy_version": "t", "approval_validity_minutes": validity,
        "agents": {"agent_demo_001": {"owner_id": "o", "currencies": LIMITS}}})
    clock = Clock()
    s = RiskService(load_policy(), AssessmentStore(db), [HeuristicAnalyzer()], clock=clock,
                    spend_policy=sp)
    return s, clock


def req(rid, amount, n=0):
    return remember(PaymentRequest.model_validate({
        **VALID, "request_id": rid, "amount": amount, "recurring": False,
        "billing_interval": None, "merchant_name": f"Vendor {n}",
        "merchant_domain": f"vendor{n}-shop.org", "expires_at": (T0 + timedelta(hours=2)).isoformat()}))


def test_shipped_default_approval_validity_is_15_minutes():
    assert load_spend_policy().approval_validity_minutes == 15


def test_approval_holds_for_validity_window_then_releases():
    s, clock = make()
    s.assess(req("req_a1", "60.00"))
    clock.t = T0 + timedelta(minutes=1)
    s.store.record_owner_decision("req_a1", "approved", "owner", now=clock.t, terms=terms("req_a1"))
    entry = s.store.ledger_entry("req_a1")
    assert datetime.fromisoformat(entry["expires_at"]) == clock.t + timedelta(minutes=15)
    # still held 1 minute before expiry
    totals = s.store.spend_breakdown("agent_demo_001", "USD", clock.t + timedelta(minutes=14))
    assert totals["approved"] == Decimal("60.00")
    # expired: released, auditable, budget available again
    later = clock.t + timedelta(minutes=15, seconds=1)
    totals = s.store.spend_breakdown("agent_demo_001", "USD", later)
    assert totals["approved"] == 0 and totals["released"] == Decimal("60.00")
    assert s.store.ledger_events("req_a1")[-1]["reason"] == "approval_expired"


def test_payment_attempt_after_approval_expiry_is_refused():
    s, clock = make()
    s.assess(req("req_a2", "30.00"))
    s.store.record_owner_decision("req_a2", "approved", "owner", now=T0, terms=terms("req_a2"))
    with pytest.raises(InvalidLedgerTransition):
        s.store.record_payment_attempt("req_a2", "sandbox", "att-1",
                                       now=T0 + timedelta(minutes=16), terms=terms("req_a2"))
    assert s.store.ledger_state("req_a2") == LedgerState.released


def test_approval_of_expired_reservation_is_refused():
    s, clock = make()
    a = s.assess(req("req_a3", "30.00"))
    with pytest.raises(InvalidLedgerTransition):
        s.store.record_owner_decision("req_a3", "approved", "owner",
                                      now=a.effective_expires_at + timedelta(seconds=1), terms=terms("req_a3"))


def test_late_approval_cannot_reclaim_budget_taken_by_others():
    s, clock = make()
    a = s.assess(req("req_a4", "60.00", 1))
    clock.t = a.effective_expires_at + timedelta(seconds=1)  # reservation lapses
    b = s.assess(req("req_a5", "60.00", 2))                  # takes the freed budget
    assert "BUDGET_EXCEEDED" not in b.hard_violations
    with pytest.raises(InvalidLedgerTransition):
        s.store.record_owner_decision("req_a4", "approved", "owner", now=clock.t, terms=terms("req_a4"))
    assert s.store.spend_totals("agent_demo_001", "USD", clock.t)["committed"] <= Decimal("100")


def test_concurrent_approvals_never_exceed_budget(tmp_path):
    s, clock = make(db=tmp_path / "appr.db")
    ids = []
    for i in range(6):  # 3 x 30 fit in 100; the rest are BUDGET_EXCEEDED (no hold)
        a = s.assess(req(f"req_c{i}", "30.00", i))
        ids.append((f"req_c{i}", "BUDGET_EXCEEDED" in a.hard_violations))
    barrier = threading.Barrier(len(ids))
    outcomes = []

    def approve(rid):
        barrier.wait()
        try:
            s.store.record_owner_decision(rid, "approved", "owner", now=T0, terms=terms(rid))
            outcomes.append("ok")
        except InvalidLedgerTransition:
            outcomes.append("refused")

    ts = [threading.Thread(target=approve, args=(rid,)) for rid, _ in ids]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert outcomes.count("ok") == 3 and outcomes.count("refused") == 3
    b = s.store.spend_breakdown("agent_demo_001", "USD", T0)
    assert b["approved"] == Decimal("90.00") and b["committed"] <= Decimal("100.00")


def test_cancelled_approval_releases_hold():
    s, clock = make()
    s.assess(req("req_a6", "40.00"))
    s.store.record_owner_decision("req_a6", "approved", "owner", now=T0, terms=terms("req_a6"))
    s.store.transition_ledger("req_a6", LedgerState.released, actor="owner", reason="cancelled",
                              now=T0 + timedelta(minutes=2))
    assert s.store.spend_totals("agent_demo_001", "USD", T0)["committed"] == 0


# ------------------------------------------------------------- decision 4: breakdown

def test_amounts_are_reported_separately_by_state():
    s, clock = make()
    for i, amt in enumerate(["10.00", "11.00", "12.00", "13.00", "14.00"]):
        s.assess(req(f"req_b{i}", amt, i))
    s.store.record_owner_decision("req_b1", "approved", "owner", now=T0, terms=terms("req_b1"))
    for rid in ("req_b2", "req_b3", "req_b4"):
        s.store.record_owner_decision(rid, "approved", "owner", now=T0, terms=terms(rid))
        s.store.record_payment_attempt(rid, "sandbox", f"att-{rid}", now=T0, terms=terms(rid))
    s.store.record_payment_outcome("req_b3", "completed", verified=True,
                                   verification_method="test", provider_reference="c3", now=T0)
    s.store.record_payment_outcome("req_b4", "failed", verified=True,
                                   verification_method="test", provider_reference="c4", now=T0)
    b = s.store.spend_breakdown("agent_demo_001", "USD", T0)
    assert b["reserved"] == Decimal("10.00")
    assert b["approved"] == Decimal("11.00")
    assert b["attempted"] == Decimal("12.00")
    assert b["completed"] == Decimal("13.00")
    assert b["failed"] == Decimal("14.00")
    assert b["committed"] == Decimal("46.00")  # reserved + approved + attempted + completed


def test_unverified_outcome_never_counts_as_completed():
    s, clock = make()
    s.assess(req("req_u1", "20.00"))
    s.store.record_owner_decision("req_u1", "approved", "owner", now=T0, terms=terms("req_u1"))
    s.store.record_payment_attempt("req_u1", "sandbox", "att", now=T0, terms=terms("req_u1"))
    s.store.record_payment_outcome("req_u1", "completed", verified=False,
                                   verification_method="agent_claim", provider_reference="x",
                                   now=T0)
    b = s.store.spend_breakdown("agent_demo_001", "USD", T0)
    assert b["completed"] == 0 and b["attempted"] == Decimal("20.00")
