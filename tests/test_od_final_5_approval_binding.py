"""Final owner decision 5: an approval is bound to the exact request, agent, amount,
currency, recipient (merchant name and domain) and purpose. A payment attempt must present
the same terms. Approval != attempt != provider-confirmed completion."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from agentpal.analyzers import HeuristicAnalyzer
from agentpal.config import load_policy
from agentpal.schemas import PaymentRequest
from agentpal.service import RiskService
from agentpal.spending import (ApprovalTerms, ApprovalTermsMismatch,
                               InvalidLedgerTransition, LedgerState, SpendPolicy)
from agentpal.store import AssessmentStore

from conftest import VALID

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def make():
    sp = SpendPolicy.model_validate({
        "policy_version": "t", "configuration": "operator",
        "agents": {"agent_demo_001": {"owner_id": "o", "currencies": {"USD": {
            "monthly_budget": "100.00", "rolling_window_limit": "100.00",
            "per_transaction_hard_limit": "5000.00", "per_transaction_review_limit": "1000.00"}}}}})
    s = RiskService(load_policy(), AssessmentStore(":memory:"), [HeuristicAnalyzer()],
                    clock=lambda: T0, spend_policy=sp)
    req = PaymentRequest.model_validate({**VALID, "request_id": "req_bind_1", "amount": "49.00"})
    s.assess(req)
    assert s.store.ledger_state("req_bind_1") == LedgerState.reserved
    return s, req


def terms(req, **change):
    return ApprovalTerms.model_validate({**ApprovalTerms.from_request(req).model_dump(), **change})


def test_approval_requires_bound_terms():
    s, _ = make()
    with pytest.raises(ValueError, match="terms"):
        s.store.record_owner_decision("req_bind_1", "approved", "owner")
    assert s.store.ledger_state("req_bind_1") == LedgerState.reserved


def test_exact_terms_approve_then_attempt_but_never_complete():
    s, req = make()
    s.store.record_owner_decision("req_bind_1", "approved", "owner", terms=terms(req))
    s.store.record_payment_attempt("req_bind_1", "sandbox", "att-1", terms=terms(req))
    assert s.store.ledger_state("req_bind_1") == LedgerState.pending
    b = s.store.spend_breakdown(VALID["agent_id"], "USD", T0)
    assert b["attempted"] == Decimal("49.00") and b["completed"] == 0


@pytest.mark.parametrize("change", [
    {"amount": "49.01"}, {"amount": "4900"}, {"currency": "GBP"},
    {"merchant_name": "Example Hosting Ltd"}, {"merchant_domain": "examp1e.com"},
    {"purpose": "Annual hosting subscription"}, {"agent_id": "agent_demo_002"},
    {"request_id": "req_bind_2"},
])
def test_approval_with_different_terms_is_refused(change):
    s, req = make()
    with pytest.raises(ApprovalTermsMismatch):
        s.store.record_owner_decision("req_bind_1", "approved", "owner",
                                      terms=terms(req, **change))
    assert s.store.ledger_state("req_bind_1") == LedgerState.reserved


@pytest.mark.parametrize("change", [{"amount": "50.00"}, {"merchant_domain": "evil.example"},
                                    {"purpose": "Gift cards"}])
def test_attempt_with_terms_other_than_the_approved_ones_is_refused(change):
    s, req = make()
    s.store.record_owner_decision("req_bind_1", "approved", "owner", terms=terms(req))
    with pytest.raises(ApprovalTermsMismatch):
        s.store.record_payment_attempt("req_bind_1", "sandbox", "att-1",
                                       terms=terms(req, **change))
    assert s.store.ledger_state("req_bind_1") == LedgerState.approved


def test_attempt_requires_terms_and_an_approval():
    s, req = make()
    with pytest.raises(TypeError):
        s.store.record_payment_attempt("req_bind_1", "sandbox", "att-1")  # no terms
    with pytest.raises(InvalidLedgerTransition):  # reserved -> pending is not allowed
        s.store.record_payment_attempt("req_bind_1", "sandbox", "att-1", terms=terms(req))
    assert s.store.ledger_state("req_bind_1") == LedgerState.reserved


def test_decline_needs_no_terms():
    s, _ = make()
    s.store.record_owner_decision("req_bind_1", "declined", "owner")
    assert s.store.ledger_state("req_bind_1") == LedgerState.released


def test_amount_is_canonical_per_currency():
    req = PaymentRequest.model_validate({**VALID, "amount": "49"})
    assert ApprovalTerms.from_request(req).digest() == terms(req, amount="49.00").digest()
    with pytest.raises(ValueError):
        terms(req, amount="49.001")  # more decimals than USD allows: never rounded


def test_store_keeps_only_a_digest_of_the_terms():
    s, req = make()
    rows = s.store._conn.execute("SELECT * FROM request_terms").fetchall()
    assert len(rows) == 1 and rows[0]["terms_sha256"] == ApprovalTerms.from_request(req).digest()
    assert VALID["purpose"] not in str(tuple(rows[0]))


def test_expired_approval_cannot_be_attempted_even_with_exact_terms():
    clock = {"t": T0}
    s, req = make()
    s.clock = s.store.clock = lambda: clock["t"]
    s.store.record_owner_decision("req_bind_1", "approved", "owner", terms=terms(req),
                                  now=T0)
    with pytest.raises(InvalidLedgerTransition):  # approval expired -> released
        s.store.record_payment_attempt("req_bind_1", "sandbox", "att-1", terms=terms(req),
                                       now=T0 + timedelta(minutes=16))
    assert s.store.ledger_state("req_bind_1") == LedgerState.released
