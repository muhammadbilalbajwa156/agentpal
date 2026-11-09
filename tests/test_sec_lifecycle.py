"""Security audit (helper 1): payment-request lifecycle hardening in the store.

Each test here was written before its fix and failed on commit 685d1bb (see
docs/SECURITY_AUDIT.md for the finding IDs). The lifecycle methods are Phase-2 hooks;
none of them is reachable over HTTP.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from agentpal.analyzers import HeuristicAnalyzer
from agentpal.config import load_policy
from agentpal.schemas import PaymentRequest
from agentpal.service import RiskService
from agentpal.spending import (
    ApprovalTerms,
    ApprovalTermsMismatch,
    InvalidLedgerTransition,
    LedgerState,
    OutcomeConflict,
    OutcomeMismatch,
    RequestNotApprovable,
    SpendPolicy,
)
from agentpal.store import AssessmentStore

from conftest import VALID

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
LIMITS = {"USD": {"monthly_budget": "100.00", "rolling_window_limit": "100.00",
                  "per_transaction_hard_limit": "5000.00",
                  "per_transaction_review_limit": "1000.00"}}


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def spend_policy(validity=15):
    return SpendPolicy.model_validate({
        "policy_version": "t", "approval_validity_minutes": validity,
        "agents": {"agent_demo_001": {"owner_id": "o", "currencies": LIMITS},
                   "agent_demo_002": {"owner_id": "o", "currencies": LIMITS}}})


def make(db=":memory:", clock=None, validity=15):
    clock = clock or Clock()
    s = RiskService(load_policy(), AssessmentStore(db), [HeuristicAnalyzer()], clock=clock,
                    spend_policy=spend_policy(validity))
    return s, clock


def req(rid, amount="40.00", n=0, expires=timedelta(hours=2), agent="agent_demo_001"):
    return PaymentRequest.model_validate({
        **VALID, "request_id": rid, "agent_id": agent, "amount": amount, "recurring": False,
        "billing_interval": None, "merchant_name": f"Vendor {n}",
        "merchant_domain": f"vendor{n}-shop.org",
        "expires_at": (T0 + expires).isoformat()})


def approved_and_attempted(s, r, ref="att-1"):
    s.assess(r)
    t = ApprovalTerms.from_request(r)
    s.store.record_owner_decision(r.request_id, "approved", "owner", terms=t, now=T0)
    s.store.record_payment_attempt(r.request_id, "sandbox", ref, terms=t, now=T0)
    return t


def outcome_rows(s, rid):
    with s.store._lock:
        return [dict(x) for x in s.store._conn.execute(
            "SELECT * FROM payment_outcomes WHERE request_id=? ORDER BY id", (rid,))]


def audit_statuses(s, rid):
    return [a["status"] for a in s.store.audit_records(rid)]


# --------------------------------------------------------------- SEC-05 approval gating
def test_approval_refused_while_assessment_is_still_running():
    """A reservation exists from the claim transaction onwards, before any rule has run.
    Approving it then would survive a later `block` (only `reserved` is released)."""
    s, _ = make()
    r = req("req_inflight")
    claimed, _snap = s.store.claim_request(r, T0, s._reservation_request(r, T0))
    assert claimed and s.store.ledger_state(r.request_id) == LedgerState.reserved
    with pytest.raises(RequestNotApprovable):
        s.store.record_owner_decision(r.request_id, "approved", "owner",
                                      terms=ApprovalTerms.from_request(r), now=T0)
    assert s.store.ledger_state(r.request_id) == LedgerState.reserved
    assert s.store.trust_trail(r.request_id) == []


def test_request_not_approvable_is_still_an_invalid_transition():
    # existing callers that catch InvalidLedgerTransition keep failing closed
    assert issubclass(RequestNotApprovable, InvalidLedgerTransition)


# --------------------------------------------------------- SEC-06 approval vs request expiry
def test_approval_never_outlives_the_request_expiry():
    s, _ = make()
    r = req("req_short", expires=timedelta(minutes=5))
    s.assess(r)
    t = ApprovalTerms.from_request(r)
    s.store.record_owner_decision(r.request_id, "approved", "owner", terms=t,
                                  now=T0 + timedelta(minutes=1))
    entry = s.store.ledger_entry(r.request_id)
    assert datetime.fromisoformat(entry["expires_at"]) == T0 + timedelta(minutes=5)
    with pytest.raises(InvalidLedgerTransition):
        s.store.record_payment_attempt(r.request_id, "sandbox", "att", terms=t,
                                       now=T0 + timedelta(minutes=10))
    assert s.store.ledger_state(r.request_id) == LedgerState.released


def test_approval_inside_both_windows_still_works():
    s, _ = make()
    r = req("req_ok", expires=timedelta(hours=2))
    s.assess(r)
    t = ApprovalTerms.from_request(r)
    s.store.record_owner_decision(r.request_id, "approved", "owner", terms=t, now=T0)
    s.store.record_payment_attempt(r.request_id, "sandbox", "att", terms=t,
                                   now=T0 + timedelta(minutes=14))
    assert s.store.ledger_state(r.request_id) == LedgerState.pending


# ------------------------------------------------------------- SEC-07 timezone handling
def test_non_utc_time_cannot_extend_an_expired_approval():
    """Timestamps are compared as ISO strings in SQLite; a caller-supplied `now` with a
    negative UTC offset sorted *before* the stored UTC expiry and revived the approval."""
    s, _ = make()
    r = req("req_tz")
    s.assess(r)
    t = ApprovalTerms.from_request(r)
    s.store.record_owner_decision(r.request_id, "approved", "owner", terms=t, now=T0)
    late = (T0 + timedelta(minutes=16)).astimezone(timezone(timedelta(hours=-5)))
    with pytest.raises(InvalidLedgerTransition):
        s.store.record_payment_attempt(r.request_id, "sandbox", "att", terms=t, now=late)
    assert s.store.ledger_state(r.request_id) == LedgerState.released


def test_naive_timestamps_are_refused_by_the_ledger():
    s, _ = make()
    r = req("req_naive")
    s.assess(r)
    with pytest.raises(ValueError):
        s.store.record_owner_decision(r.request_id, "approved", "owner",
                                      terms=ApprovalTerms.from_request(r),
                                      now=datetime(2026, 10, 1, 12, 0))
    assert s.store.ledger_state(r.request_id) == LedgerState.reserved


def test_service_normalizes_a_non_utc_clock():
    s, clock = make(clock=Clock(T0.astimezone(timezone(timedelta(hours=-5)))))
    a = s.assess(req("req_clock"))
    assert a.assessed_at.utcoffset() == timedelta(0)
    assert s.store.ledger_entry("req_clock")["created_at"].endswith("+00:00")


# --------------------------------------------------------------- SEC-08 outcome handling
def test_duplicate_verified_outcome_is_idempotent_and_audited():
    """Providers redeliver webhooks (PayPal: up to 25 times over 3 days). The same verified
    outcome twice must not raise and must not be stored twice."""
    s, _ = make()
    r = req("req_dup_out")
    approved_and_attempted(s, r)
    for _ in range(2):
        s.store.record_payment_outcome(r.request_id, "completed", verified=True,
                                       verification_method="webhook",
                                       provider_reference="cap-1")
    assert s.store.ledger_state(r.request_id) == LedgerState.completed
    assert len([o for o in outcome_rows(s, r.request_id) if o["verified"]]) == 1
    assert "outcome_duplicate_ignored" in audit_statuses(s, r.request_id)
    assert s.store.spend_breakdown("agent_demo_001", "USD", T0)["completed"] == Decimal("40.00")


@pytest.mark.parametrize("second", [("failed", "cap-1"), ("completed", "cap-OTHER"),
                                    ("cancelled", "cap-2")])
def test_conflicting_outcome_after_terminal_state_is_refused_and_audited(second):
    s, _ = make()
    r = req("req_conflict")
    approved_and_attempted(s, r)
    s.store.record_payment_outcome(r.request_id, "completed", verified=True,
                                   verification_method="webhook", provider_reference="cap-1")
    with pytest.raises(OutcomeConflict):
        s.store.record_payment_outcome(r.request_id, second[0], verified=True,
                                       verification_method="webhook",
                                       provider_reference=second[1])
    assert s.store.ledger_state(r.request_id) == LedgerState.completed
    assert len(outcome_rows(s, r.request_id)) == 1
    assert any(x.startswith("outcome_rejected:") for x in audit_statuses(s, r.request_id))


def test_outcome_conflict_is_still_an_invalid_transition():
    assert issubclass(OutcomeConflict, InvalidLedgerTransition)


@pytest.mark.parametrize("verified", [True, False])
def test_outcome_for_unknown_request_is_refused(verified):
    s, _ = make()
    with pytest.raises(InvalidLedgerTransition):
        s.store.record_payment_outcome("req_never_seen", "completed", verified=verified,
                                       verification_method="webhook",
                                       provider_reference="cap-x")
    assert outcome_rows(s, "req_never_seen") == []
    assert "outcome_rejected:unknown_request" in audit_statuses(s, "req_never_seen")


def test_out_of_order_outcome_before_attempt_is_refused_and_audited():
    s, _ = make()
    r = req("req_ooo")
    s.assess(r)
    s.store.record_owner_decision(r.request_id, "approved", "owner",
                                  terms=ApprovalTerms.from_request(r), now=T0)
    with pytest.raises(InvalidLedgerTransition):
        s.store.record_payment_outcome(r.request_id, "completed", verified=True,
                                       verification_method="webhook",
                                       provider_reference="cap-1")
    assert s.store.ledger_state(r.request_id) == LedgerState.approved
    assert outcome_rows(s, r.request_id) == []
    assert any(x.startswith("outcome_rejected:") for x in audit_statuses(s, r.request_id))


@pytest.mark.parametrize("amount,currency", [("41.00", "USD"), ("40.00", "EUR"),
                                             ("39.99", "USD")])
def test_outcome_amount_or_currency_mismatch_is_refused(amount, currency):
    s, _ = make()
    r = req("req_mismatch")
    approved_and_attempted(s, r)
    with pytest.raises(OutcomeMismatch):
        s.store.record_payment_outcome(r.request_id, "completed", verified=True,
                                       verification_method="webhook",
                                       provider_reference="cap-1",
                                       amount=Decimal(amount), currency=currency)
    assert s.store.ledger_state(r.request_id) == LedgerState.pending
    assert outcome_rows(s, r.request_id) == []
    assert "outcome_rejected:amount_or_currency_mismatch" in audit_statuses(s, r.request_id)


def test_outcome_with_matching_amount_currency_and_attempt_reference_completes():
    s, _ = make()
    r = req("req_match")
    approved_and_attempted(s, r, ref="ORDER-1")
    s.store.record_payment_outcome(r.request_id, "completed", verified=True,
                                   verification_method="webhook", provider_reference="CAP-1",
                                   amount=Decimal("40.00"), currency="usd",
                                   attempt_reference="ORDER-1")
    assert s.store.ledger_state(r.request_id) == LedgerState.completed


def test_outcome_for_an_unknown_attempt_reference_is_refused():
    s, _ = make()
    r = req("req_attref")
    approved_and_attempted(s, r, ref="ORDER-1")
    with pytest.raises(OutcomeMismatch):
        s.store.record_payment_outcome(r.request_id, "completed", verified=True,
                                       verification_method="webhook",
                                       provider_reference="CAP-1",
                                       attempt_reference="ORDER-OTHER")
    assert s.store.ledger_state(r.request_id) == LedgerState.pending


def test_unverified_outcome_never_completes_even_when_repeated():
    s, _ = make()
    r = req("req_unv")
    approved_and_attempted(s, r)
    for _ in range(3):
        s.store.record_payment_outcome(r.request_id, "completed", verified=False,
                                       verification_method="agent_claim",
                                       provider_reference="cap-1")
    assert s.store.ledger_state(r.request_id) == LedgerState.pending
    assert s.store.trusted_history_for("agent_demo_001", "o") == []


# ------------------------------------------------------------- replay and tampering
def test_attempt_replay_is_refused():
    s, _ = make()
    r = req("req_att_replay")
    t = approved_and_attempted(s, r)
    with pytest.raises(InvalidLedgerTransition):
        s.store.record_payment_attempt(r.request_id, "sandbox", "att-1", terms=t, now=T0)
    with s.store._lock:
        n = s.store._conn.execute("SELECT COUNT(*) FROM payment_attempts WHERE request_id=?",
                                  (r.request_id,)).fetchone()[0]
    assert n == 1


def test_approval_of_one_request_cannot_be_presented_for_another():
    s, _ = make()
    a, b = req("req_bind_a", n=1), req("req_bind_b", n=1)
    s.assess(a)
    s.assess(b)
    ta = ApprovalTerms.from_request(a)
    s.store.record_owner_decision(a.request_id, "approved", "owner", terms=ta, now=T0)
    with pytest.raises(ApprovalTermsMismatch):
        s.store.record_owner_decision(b.request_id, "approved", "owner", terms=ta, now=T0)
    with pytest.raises(ApprovalTermsMismatch):
        s.store.record_payment_attempt(b.request_id, "sandbox", "x", terms=ta, now=T0)


@pytest.mark.parametrize("field,value", [("agent_id", "agent_demo_002"),
                                         ("merchant_name", "Vendor 0 "),
                                         ("merchant_domain", None),
                                         ("purpose", "Monthly hosting subscription!")])
def test_tampered_terms_after_approval_are_refused(field, value):
    s, _ = make()
    r = req("req_tamper")
    t = ApprovalTerms.from_request(r)
    s.assess(r)
    s.store.record_owner_decision(r.request_id, "approved", "owner", terms=t, now=T0)
    bad = ApprovalTerms.model_validate({**t.model_dump(), field: value})
    with pytest.raises(ApprovalTermsMismatch):
        s.store.record_payment_attempt(r.request_id, "sandbox", "x", terms=bad, now=T0)
    assert s.store.ledger_state(r.request_id) == LedgerState.approved


# ----------------------------------------------------------------------- concurrency
def _race(n, fn):
    barrier, results = threading.Barrier(n), []
    lock = threading.Lock()

    def run(i):
        barrier.wait()
        try:
            fn(i)
            out = "ok"
        except (InvalidLedgerTransition, sqlite3.OperationalError) as exc:
            out = type(exc).__name__
        with lock:
            results.append(out)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    return results


def test_concurrent_attempts_execute_once(tmp_path):
    s, _ = make(db=tmp_path / "a.db")
    r = req("req_race_att")
    s.assess(r)
    t = ApprovalTerms.from_request(r)
    s.store.record_owner_decision(r.request_id, "approved", "owner", terms=t, now=T0)
    # separate connections, as separate API workers would have
    stores = [AssessmentStore(tmp_path / "a.db") for _ in range(8)]
    res = _race(8, lambda i: stores[i].record_payment_attempt(
        r.request_id, "sandbox", f"att-{i}", terms=t, now=T0))
    assert res.count("ok") == 1
    assert s.store.ledger_state(r.request_id) == LedgerState.pending


def test_concurrent_conflicting_outcomes_settle_exactly_once(tmp_path):
    s, _ = make(db=tmp_path / "o.db")
    r = req("req_race_out")
    approved_and_attempted(s, r)
    stores = [AssessmentStore(tmp_path / "o.db") for _ in range(8)]
    outcomes = ["completed", "failed"] * 4
    res = _race(8, lambda i: stores[i].record_payment_outcome(
        r.request_id, outcomes[i], verified=True, verification_method="webhook",
        provider_reference=f"cap-{outcomes[i]}"))
    final = s.store.ledger_state(r.request_id)
    assert final in (LedgerState.completed, LedgerState.failed)
    winners = [o for o in outcome_rows(s, r.request_id) if o["verified"]]
    # the first verified outcome settles the entry; identical redeliveries are no-ops and
    # the other outcome is refused as a conflict
    assert len(winners) == 1 and winners[0]["outcome"] == final.value
    assert res.count("ok") == 4 and res.count("OutcomeConflict") == 4


def test_concurrent_approve_and_decline_settle_once(tmp_path):
    s, _ = make(db=tmp_path / "d.db")
    r = req("req_race_dec")
    s.assess(r)
    t = ApprovalTerms.from_request(r)
    stores = [AssessmentStore(tmp_path / "d.db") for _ in range(6)]
    res = _race(6, lambda i: stores[i].record_owner_decision(
        r.request_id, "approved" if i % 2 else "declined", "owner",
        terms=t if i % 2 else None, now=T0))
    # at most one approval wins; a decline after it is a revocation (approved -> released)
    decisions = [d["detail"] for d in s.store.trust_trail(r.request_id)]
    assert decisions.count("approved") <= 1 and 1 <= res.count("ok") <= 2
    final = s.store.ledger_state(r.request_id)
    assert final == (LedgerState.released if "declined" in decisions else LedgerState.approved)
    if decisions == ["approved", "declined"]:
        with pytest.raises(InvalidLedgerTransition):  # a revoked approval cannot be used
            s.store.record_payment_attempt(r.request_id, "sandbox", "x", terms=t, now=T0)


# ------------------------------------------------------------ SEC-09 restart recovery
def test_restart_recovers_a_stale_claim_and_releases_its_hold(tmp_path):
    """A crash between claim and complete left status `claimed` with a reservation that
    held budget until the request's own expiry (up to 48 h)."""
    db = tmp_path / "crash.db"
    s, clock = make(db=db)
    r = req("req_crashed", amount="90.00", expires=timedelta(hours=40))
    claimed, snap = s.store.claim_request(r, T0, s._reservation_request(r, T0))
    assert claimed and snap.reservation_created
    s.store.close()  # the worker dies here, before complete_request/fail_request

    later = Clock(T0 + timedelta(minutes=10))
    s2, _ = make(db=db, clock=later)  # restart
    assert s2.store.request_status(r.request_id) == "assessment_error"
    assert s2.store.ledger_state(r.request_id) == LedgerState.released
    assert s2.store.ledger_events(r.request_id)[-1]["reason"] == "stale_claim_recovered"
    assert "recovered_stale_claim" in audit_statuses(s2, r.request_id)
    assert s2.store.spend_breakdown("agent_demo_001", "USD", later.t)["committed"] == 0
    # the id stays claimed: a retry must use a new request_id (fail closed)
    assert s2.assess(r).recommendation == "block"


def test_restart_leaves_a_fresh_in_flight_claim_alone(tmp_path):
    db = tmp_path / "inflight.db"
    s, _ = make(db=db)
    r = req("req_inflight2")
    s.store.claim_request(r, T0, s._reservation_request(r, T0))
    s2, _ = make(db=db, clock=Clock(T0 + timedelta(seconds=30)))  # another worker starts
    assert s2.store.request_status(r.request_id) == "claimed"
    assert s2.store.ledger_state(r.request_id) == LedgerState.reserved


def test_restart_preserves_approval_expiry(tmp_path):
    db = tmp_path / "persist.db"
    s, _ = make(db=db)
    r = req("req_persist")
    s.assess(r)
    t = ApprovalTerms.from_request(r)
    s.store.record_owner_decision(r.request_id, "approved", "owner", terms=t, now=T0)
    s.store.close()
    s2, _ = make(db=db, clock=Clock(T0 + timedelta(minutes=20)))
    with pytest.raises(InvalidLedgerTransition):
        s2.store.record_payment_attempt(r.request_id, "sandbox", "att", terms=t)
    assert s2.store.ledger_state(r.request_id) == LedgerState.released
