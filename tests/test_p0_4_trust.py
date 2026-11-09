"""P0.4 regression: explicit trust states and baseline isolation (review probes P07, P08).

Before the fix, "trusted" meant "our own recommendation was allow_for_review", and it
was global: one agent's unverified request made a domain "known" for every agent, and
flagged requests counted toward an agent's spend baseline.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agentpal.analyzers import HeuristicAnalyzer
from agentpal.config import load_policy
from agentpal.schemas import PaymentRequest
from agentpal.service import RiskService
from agentpal.spending import InvalidLedgerTransition, LedgerState, SpendPolicy
from agentpal.store import AssessmentStore

from conftest import remember, terms, VALID

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
USD = {"USD": {"monthly_budget": "100000.00", "rolling_window_limit": "100000.00",
               "per_transaction_hard_limit": "5000.00",
               "per_transaction_review_limit": "1000.00"}}


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


def make_service():
    sp = SpendPolicy.model_validate({
        "policy_version": "t",
        "agents": {a: {"owner_id": "owner_a" if a != "agent_b" else "owner_b",
                       "currencies": USD} for a in ("agent_a", "agent_a2", "agent_b")}})
    clock = Clock()
    s = RiskService(load_policy(), AssessmentStore(":memory:"), [HeuristicAnalyzer()],
                    clock=clock, spend_policy=sp)
    return s, clock


EVIL = {"merchant_name": "Evil Host", "merchant_domain": "evil-host.net",
        "purpose": "VPS instance for staging", "recurring": False, "billing_interval": None}


def req(rid, agent, **kw):
    ev = [{"type": "invoice", "amount": kw.get("amount", 30), "currency": "USD",
           "merchant_domain": kw.get("merchant_domain", EVIL["merchant_domain"])}]
    return remember(PaymentRequest.model_validate({**VALID, **EVIL, "request_id": rid, "agent_id": agent,
                                          "amount": 30, "evidence": ev, **kw}))


def codes(a):
    return {s.code for s in a.signals}


def complete_verified(s, rid):
    s.store.record_owner_decision(rid, "approved", decided_by="owner", terms=terms(rid))
    s.store.record_payment_attempt(rid, provider="sandbox-test", provider_reference=f"att-{rid}", terms=terms(rid))
    s.store.record_payment_outcome(rid, "completed", verified=True,
                                   verification_method="test-fixture",
                                   provider_reference=f"cap-{rid}")


# ----------------------------------------------------------- merchant trust

def test_allow_recommendation_alone_does_not_make_merchant_known():
    s, clock = make_service()
    first = s.assess(req("req_t_1", "agent_a"))
    # Owner decision 9 makes claimed evidence non-reducing, so an unapproved merchant now
    # reaches the review threshold (12 + 13). The property under test is unchanged: an
    # assessment outcome (allow OR review) never establishes merchant trust.
    assert first.recommendation != "block" and "UNAPPROVED_MERCHANT" in codes(first)
    clock.t += timedelta(days=5)
    again = s.assess(req("req_t_2", "agent_a"))
    assert "UNAPPROVED_MERCHANT" in codes(again)


def test_owner_approval_alone_does_not_make_merchant_known():
    s, clock = make_service()
    s.assess(req("req_t_3", "agent_a"))
    s.store.record_owner_decision("req_t_3", "approved", decided_by="owner", terms=terms("req_t_3"))
    clock.t += timedelta(days=5)
    assert "UNAPPROVED_MERCHANT" in codes(s.assess(req("req_t_4", "agent_a")))


def test_unverified_payment_outcome_is_not_trusted():
    s, clock = make_service()
    s.assess(req("req_t_5", "agent_a"))
    s.store.record_owner_decision("req_t_5", "approved", decided_by="owner", terms=terms("req_t_5"))
    s.store.record_payment_attempt("req_t_5", provider="sandbox-test", provider_reference="x", terms=terms("req_t_5"))
    s.store.record_payment_outcome("req_t_5", "completed", verified=False,
                                   verification_method="agent_claim", provider_reference="x")
    assert s.store.ledger_state("req_t_5") == LedgerState.pending  # unchanged
    clock.t += timedelta(days=5)
    assert "UNAPPROVED_MERCHANT" in codes(s.assess(req("req_t_6", "agent_a")))


def test_verified_completed_payment_establishes_trust_for_that_agent_only():
    s, clock = make_service()
    s.assess(req("req_t_7", "agent_a"))
    complete_verified(s, "req_t_7")
    clock.t += timedelta(days=5)
    same_agent = s.assess(req("req_t_8", "agent_a"))
    assert "UNAPPROVED_MERCHANT" not in codes(same_agent)
    other_agent_same_owner = s.assess(req("req_t_9", "agent_a2"))
    other_owner = s.assess(req("req_t_10", "agent_b"))
    assert "UNAPPROVED_MERCHANT" in codes(other_agent_same_owner)  # P07: no cross-agent leak
    assert "UNAPPROVED_MERCHANT" in codes(other_owner)


def test_rejected_request_can_never_become_trusted():
    s, _ = make_service()
    a = s.assess(req("req_t_11", "agent_a", amount=150000))
    assert a.recommendation == "block"
    with pytest.raises(InvalidLedgerTransition):
        s.store.record_owner_decision("req_t_11", "approved", decided_by="owner", terms=terms("req_t_11"))


def test_flagged_request_does_not_redefine_merchant_domain():
    s, clock = make_service()
    bad = s.assess(req("req_t_12", "agent_a", merchant_name="Taskloom",
                       merchant_domain="taskloom-billing.net", purpose="Workspace seats"))
    assert bad.recommendation != "allow_for_review"
    clock.t += timedelta(days=5)
    good = s.assess(req("req_t_13", "agent_a", merchant_name="Taskloom",
                        merchant_domain="taskloom.app", purpose="Workspace seats"))
    assert "MERCHANT_DOMAIN_CHANGED" not in codes(good)


# ------------------------------------------------------------- spend baselines

def test_flagged_and_unverified_requests_do_not_inflate_spend_baseline():
    s, clock = make_service()
    for i in range(4):  # P08: flagged requests (gift cards) at ~900
        clock.t += timedelta(hours=1)
        a = s.assess(req(f"req_f_{i}", "agent_a", amount=900 + i, purpose="Gift cards for team",
                         merchant_name="Taskloom", merchant_domain="taskloom.app"))
        assert a.recommendation != "allow_for_review"
    for i in range(3):  # verified, completed small payments define the baseline
        clock.t += timedelta(days=1)
        s.assess(req(f"req_v_{i}", "agent_a", amount=50 + i, merchant_name="Taskloom",
                     merchant_domain="taskloom.app", purpose="Workspace seats"))
        complete_verified(s, f"req_v_{i}")
    clock.t += timedelta(days=1)
    spike = s.assess(req("req_spike", "agent_a", amount=900, merchant_name="Taskloom",
                         merchant_domain="taskloom.app", purpose="Workspace seats"))
    ev = next(x for x in spike.signals if x.code == "AMOUNT_SPIKE_VS_HISTORY").evidence
    assert ev["agent_median"] == 51.0


def test_spend_baseline_isolated_by_currency_and_agent():
    s, clock = make_service()
    for i in range(3):
        clock.t += timedelta(days=1)
        s.assess(req(f"req_cb_{i}", "agent_b", amount=10, merchant_name="Taskloom",
                     merchant_domain="taskloom.app", purpose="Workspace seats"))
        complete_verified(s, f"req_cb_{i}")
    clock.t += timedelta(days=1)
    other = s.assess(req("req_cb_x", "agent_a", amount=500, merchant_name="Taskloom",
                         merchant_domain="taskloom.app", purpose="Workspace seats"))
    assert "AMOUNT_SPIKE_VS_HISTORY" not in codes(other)  # agent_b's history is not agent_a's


# ------------------------------------------------------------------ audit trail

def test_status_changes_are_auditable():
    s, _ = make_service()
    s.assess(req("req_au_1", "agent_a"))
    complete_verified(s, "req_au_1")
    trail = s.store.trust_trail("req_au_1")
    assert [e["kind"] for e in trail] == ["owner_decision", "payment_attempt", "payment_outcome"]
    assert trail[2]["verified"] == 1
    assert [e["to_state"] for e in s.store.ledger_events("req_au_1")] == [
        "reserved", "approved", "pending", "completed"]
