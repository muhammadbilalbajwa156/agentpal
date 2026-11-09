"""SEC-13: request ids are scoped per agent (schema v4, PK (agent_id, request_id)).

Fail-first: on 4c0a65d (schema v3, global request_id PK) the squatting tests fail: the
victim's first submission is blocked with DUPLICATE_REQUEST_ID and gets no reservation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agentpal.analyzers import HeuristicAnalyzer
from agentpal.config import load_policy
from agentpal.schemas import PaymentRequest
from agentpal.service import RiskService
from agentpal.spending import (
    AmbiguousRequestId,
    ApprovalTerms,
    ApprovalTermsMismatch,
    LedgerState,
    SpendPolicy,
)
from agentpal.store import AssessmentStore

from conftest import VALID

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
LIMITS = {"USD": {"monthly_budget": "100.00", "rolling_window_limit": "100.00",
                  "per_transaction_hard_limit": "5000.00",
                  "per_transaction_review_limit": "1000.00"}}


def make(db=":memory:"):
    sp = SpendPolicy.model_validate({"policy_version": "t", "agents": {
        "agent_a": {"owner_id": "owner_a", "currencies": LIMITS},
        "agent_b": {"owner_id": "owner_b", "currencies": LIMITS}}})
    return RiskService(load_policy(), AssessmentStore(db), [HeuristicAnalyzer()],
                       clock=lambda: T0, spend_policy=sp)


def req(agent, rid="req_shared_1", **kw):
    return PaymentRequest.model_validate({**VALID, "agent_id": agent, "request_id": rid,
                                          "expires_at": (T0 + timedelta(hours=1)).isoformat(),
                                          **kw})


def test_another_agent_cannot_squat_a_request_id():
    s = make()
    attacker = s.assess(req("agent_b"))
    victim = s.assess(req("agent_a"))
    assert "DUPLICATE_REQUEST_ID" not in attacker.hard_violations
    assert "DUPLICATE_REQUEST_ID" not in victim.hard_violations
    assert s.store.ledger_state("req_shared_1", agent_id="agent_a") == LedgerState.reserved
    assert s.store.ledger_state("req_shared_1", agent_id="agent_b") == LedgerState.reserved


def test_the_same_agent_replaying_an_id_is_still_blocked():
    s = make()
    s.assess(req("agent_a"))
    replay = s.assess(req("agent_a"))
    assert "DUPLICATE_REQUEST_ID" in replay.hard_violations
    s.assess(req("agent_b"))  # an unrelated agent is unaffected


def test_unscoped_access_to_a_shared_id_fails_closed():
    s = make()
    s.assess(req("agent_a"))
    s.assess(req("agent_b"))
    with pytest.raises(AmbiguousRequestId):
        s.store.ledger_state("req_shared_1")
    with pytest.raises(AmbiguousRequestId):
        s.store.record_owner_decision("req_shared_1", "declined", "x")
    with pytest.raises(AmbiguousRequestId):
        s.store.transition_ledger("req_shared_1", LedgerState.released, actor="x")
    assert s.store.get_assessment("req_shared_1") is None          # C3: no guessing
    assert s.store.get_assessment("req_shared_1", "agent_a").agent_id == "agent_a"
    assert s.store.get_assessment("req_shared_1", "agent_b").agent_id == "agent_b"


def test_one_agents_terms_cannot_approve_the_other_agents_entry():
    s = make()
    a, b = req("agent_a"), req("agent_b")
    s.assess(a)
    s.assess(b)
    with pytest.raises(ApprovalTermsMismatch):
        s.store.record_owner_decision("req_shared_1", "approved", "owner_b",
                                      terms=ApprovalTerms.from_request(a), agent_id="agent_b")
    with pytest.raises(ApprovalTermsMismatch):
        s.store.approve_authenticated("req_shared_1", ApprovalTerms.from_request(b),
                                      agent_id="agent_a", owner_id="owner_a",
                                      session_id="0" * 24, auth_method="owner_api_token")
    assert s.store.ledger_state("req_shared_1", agent_id="agent_a") == LedgerState.reserved
    assert s.store.ledger_state("req_shared_1", agent_id="agent_b") == LedgerState.reserved


def test_decisions_on_one_agent_do_not_touch_the_other():
    s = make()
    s.assess(req("agent_a"))
    s.assess(req("agent_b"))
    s.store.record_owner_decision("req_shared_1", "declined", "owner_a", agent_id="agent_a")
    assert s.store.ledger_state("req_shared_1", agent_id="agent_a") == LedgerState.released
    assert s.store.ledger_state("req_shared_1", agent_id="agent_b") == LedgerState.reserved
    assert len(s.store.ledger_events("req_shared_1", agent_id="agent_b")) == 1


def test_cross_owner_authenticated_approval_is_refused():
    s = make()
    a = req("agent_a")
    s.assess(a)
    from agentpal.spending import RequestNotApprovable
    with pytest.raises(RequestNotApprovable):
        s.store.approve_authenticated("req_shared_1", ApprovalTerms.from_request(a),
                                      agent_id="agent_a", owner_id="owner_b",
                                      session_id="0" * 24, auth_method="owner_api_token")
    assert s.store.ledger_state("req_shared_1", agent_id="agent_a") == LedgerState.reserved
    assert s.store.approvals("req_shared_1", agent_id="agent_a") == []


def test_isolation_survives_a_restart(tmp_path):
    db = tmp_path / "v4.db"
    s = make(db)
    s.assess(req("agent_a"))
    s.store.close()
    s2 = make(db)
    assert "DUPLICATE_REQUEST_ID" not in s2.assess(req("agent_b")).hard_violations
    assert "DUPLICATE_REQUEST_ID" in s2.assess(req("agent_a")).hard_violations
