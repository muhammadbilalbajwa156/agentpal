"""Owner decision 8: verified merchant history is isolated per agent and owner by default;
cross-agent sharing only through an explicit owner policy, only for verified outcomes, and
only for merchant trust (never for another agent's spend baseline)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agentpal.analyzers import HeuristicAnalyzer
from agentpal.config import load_policy
from agentpal.schemas import PaymentRequest
from agentpal.service import RiskService
from agentpal.spending import SpendPolicy, load_spend_policy
from agentpal.store import AssessmentStore

from conftest import remember, terms, VALID

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
USD = {"USD": {"monthly_budget": "100000.00", "rolling_window_limit": "100000.00",
               "per_transaction_hard_limit": "5000.00",
               "per_transaction_review_limit": "1000.00"}}


def make(share: bool | None):
    owners = {} if share is None else {
        "owner_a": {"share_verified_merchant_history_across_agents": share}}
    sp = SpendPolicy.model_validate({
        "policy_version": "t", "owners": owners,
        "agents": {"agent_a1": {"owner_id": "owner_a", "currencies": USD},
                   "agent_a2": {"owner_id": "owner_a", "currencies": USD},
                   "agent_b1": {"owner_id": "owner_b", "currencies": USD}}})
    clock = {"t": T0}
    s = RiskService(load_policy(), AssessmentStore(":memory:"), [HeuristicAnalyzer()],
                    clock=lambda: clock["t"], spend_policy=sp)
    return s, clock


def req(rid, agent, amount="30.00"):
    return remember(PaymentRequest.model_validate({
        **VALID, "request_id": rid, "agent_id": agent, "amount": amount, "recurring": False,
        "billing_interval": None, "merchant_name": "Evil Host",
        "merchant_domain": "evil-host.net", "purpose": "VPS instance for staging"}))


def verify(s, rid, clock, verified=True):
    s.store.record_owner_decision(rid, "approved", "owner", now=clock["t"], terms=terms(rid))
    s.store.record_payment_attempt(rid, "sandbox", f"a-{rid}", now=clock["t"], terms=terms(rid))
    s.store.record_payment_outcome(rid, "completed", verified=verified,
                                   verification_method="test", provider_reference=f"c-{rid}",
                                   now=clock["t"])


def codes(a):
    return {x.code for x in a.signals}


def test_default_policy_does_not_share():
    assert all(not o.share_verified_merchant_history_across_agents
               for o in load_spend_policy().owners.values())
    s, clock = make(share=None)
    s.assess(req("req_r1", "agent_a1"))
    verify(s, "req_r1", clock)
    clock["t"] += timedelta(days=1)
    assert "UNAPPROVED_MERCHANT" in codes(s.assess(req("req_r2", "agent_a2")))


def test_explicit_owner_policy_shares_verified_history_with_same_owner_only():
    s, clock = make(share=True)
    s.assess(req("req_r1", "agent_a1"))
    verify(s, "req_r1", clock)
    clock["t"] += timedelta(days=1)
    same_owner = s.assess(req("req_r2", "agent_a2"))
    other_owner = s.assess(req("req_r3", "agent_b1"))
    assert "UNAPPROVED_MERCHANT" not in codes(same_owner)
    assert "UNAPPROVED_MERCHANT" in codes(other_owner)


def test_sharing_never_includes_unverified_or_merely_approved_history():
    s, clock = make(share=True)
    s.assess(req("req_r1", "agent_a1"))
    s.store.record_owner_decision("req_r1", "approved", "owner", now=clock["t"], terms=terms("req_r1"))  # approval only
    s.assess(req("req_r2", "agent_a1"))
    verify(s, "req_r2", clock, verified=False)                                    # unverified
    clock["t"] += timedelta(days=1)
    assert "UNAPPROVED_MERCHANT" in codes(s.assess(req("req_r3", "agent_a2")))


def test_shared_history_never_defines_another_agents_spend_baseline():
    s, clock = make(share=True)
    for i in range(3):
        clock["t"] += timedelta(hours=1)
        s.assess(req(f"req_v{i}", "agent_a1", amount="10.00"))
        verify(s, f"req_v{i}", clock)
    clock["t"] += timedelta(days=1)
    a = s.assess(req("req_big", "agent_a2", amount="500.00"))
    assert "AMOUNT_SPIKE_VS_HISTORY" not in codes(a)


def test_sharing_never_raises_another_agents_budget_or_replaces_its_checks():
    """Final owner decision 6: shared merchant history affects merchant familiarity only;
    the other agent's own budget and spending checks are unchanged."""
    outcomes = {}
    for share in (False, True):
        s, clock = make(share=share)
        s.assess(req("req_s1", "agent_a1", amount="90.00"))
        verify(s, "req_s1", clock)
        clock["t"] += timedelta(hours=1)
        over = s.assess(req("req_s2", "agent_a2", amount="6000.00"))  # hard limit 5000
        assert "AMOUNT_ABOVE_HARD_LIMIT" in over.hard_violations
        assert over.recommendation == "block"
        ok = s.assess(req("req_s3", "agent_a2", amount="20.00"))
        b = s.store.spend_breakdown("agent_a2", "USD", clock["t"])
        outcomes[share] = (ok.hard_violations, b["committed"], ok.recommendation != "block")
    assert outcomes[False] == outcomes[True]
