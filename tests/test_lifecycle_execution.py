"""Execution boundaries, run fail-first against the inherited WIP."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal

import pytest

from agentpal.infra_contracts import (
    PaymentLifecycle as L, ProviderError, ProviderEvent, ProviderOrderRef, ProviderStatus,
)
from agentpal.lifecycle import PaymentLifecycleService
from agentpal.spending import ApprovalTerms, InvalidLedgerTransition, LedgerState
from agentpal.store import AssessmentStore, UnknownProviderOrder
from test_sec_v2_isolation import T0, make, req


def event(**changes):
    return ProviderEvent.model_validate({"provider": "fake", "environment": "sandbox",
        "provider_order_id": "ORDER-1", "event_id": "event-1", "event_type": "capture",
        "status": "completed", "amount": "40.00", "currency": "USD", "verified": True,
        "received_at": T0, "raw_sha256": "a" * 64, **changes})


class FakeProvider:
    name = "fake"
    environment = "sandbox"

    def __init__(self, store, ticket):
        self.store, self.ticket = store, ticket
        self.creates = self.captures = 0
        self.error = None
        self.capture_result = event()

    def create_order(self, terms, *, idempotency_key, return_url=None):
        self.creates += 1
        persisted = self.store.provider_order(terms.request_id, agent_id=terms.agent_id)
        assert persisted["create_key"] == idempotency_key == self.ticket.create_key
        assert self.store.approvals(terms.request_id)[0]["consumed_at"] is not None
        if self.error:
            raise self.error
        return ProviderOrderRef(provider=self.name, environment=self.environment,
            provider_order_id="ORDER-1", idempotency_key=idempotency_key, status="created")

    def capture_order(self, provider_order_id, *, idempotency_key):
        self.captures += 1
        assert idempotency_key == self.ticket.capture_key != self.ticket.create_key
        if self.error:
            raise self.error
        return self.capture_result

    def get_order_status(self, provider_order_id):
        if self.error:
            raise self.error
        status = ("completed" if self.store.ledger_state(self.ticket.request_id)
                  == LedgerState.reconciliation_required else "approved_by_payer")
        return event(event_id="query-1", event_type="ORDER_STATUS_READ", status=status)


def setup(db=":memory:"):
    risk = make(db)
    r = req("agent_a", amount="40.00")
    risk.assess(r)
    terms = ApprovalTerms.from_request(r)
    risk.store.approve_authenticated(r.request_id, terms, agent_id=r.agent_id,
        owner_id="owner_a", session_id="session_123", auth_method="owner_api_token", now=T0)
    lifecycle = PaymentLifecycleService(risk.store)
    ticket = lifecycle.begin_execution(r.agent_id, r.request_id, terms, "fake")
    return risk.store, lifecycle, ticket, FakeProvider(risk.store, ticket)


def test_begin_execution_consumes_once_and_persists_operation_keys():
    store, lifecycle, ticket, _ = setup()
    assert store.ledger_state(ticket.request_id) == LedgerState.pending
    assert store.approvals(ticket.request_id)[0]["consumed_by"] == ticket.create_key
    assert ticket.create_key != ticket.capture_key
    with pytest.raises(InvalidLedgerTransition):
        lifecycle.begin_execution(ticket.agent_id, ticket.request_id, ticket.terms, "fake")


@pytest.mark.parametrize("change", [{"create_key": "forged-key"},
    {"capture_key": "forged-key"}, {"approval_id": 900}, {"terms_sha256": "b" * 64},
    {"terms": None}])
def test_forged_execution_ticket_never_calls_provider(change):
    store, lifecycle, ticket, provider = setup()
    if change.get("terms", 1) is None:
        change = {"terms": ticket.terms.model_copy(update={"amount": Decimal("1.00")})}
    with pytest.raises(InvalidLedgerTransition):
        lifecycle.submit_to_provider(ticket.model_copy(update=change), provider)
    assert provider.creates == 0


def test_submission_and_capture_are_single_call_even_on_replay():
    store, lifecycle, ticket, provider = setup()
    assert lifecycle.submit_to_provider(ticket, provider) == L.provider_pending
    with pytest.raises(InvalidLedgerTransition):
        lifecycle.submit_to_provider(ticket, provider)
    provider.capture_result = event(status="pending", event_id="capture-pending")
    assert lifecycle.capture(ticket.agent_id, ticket.request_id, provider) == L.provider_pending
    with pytest.raises(InvalidLedgerTransition):
        lifecycle.capture(ticket.agent_id, ticket.request_id, provider)
    assert provider.creates == provider.captures == 1


@pytest.mark.parametrize("operation", ["submit", "capture"])
def test_reconciliation_is_never_retried(operation):
    store, lifecycle, ticket, provider = setup()
    if operation == "capture":
        lifecycle.submit_to_provider(ticket, provider)
    store.mark_reconciliation_required(ticket.request_id, "test", agent_id=ticket.agent_id)
    with pytest.raises(InvalidLedgerTransition):
        if operation == "submit":
            lifecycle.submit_to_provider(ticket, provider)
        else:
            lifecycle.capture(ticket.agent_id, ticket.request_id, provider)
    assert provider.captures == 0
    assert provider.creates == (operation == "capture")


@pytest.mark.parametrize("unknown,expected", [(True, L.reconciliation_required), (False, L.failed)])
def test_create_error_distinguishes_unknown_from_definite_refusal(unknown, expected):
    store, lifecycle, ticket, provider = setup()
    provider.error = ProviderError("failure", outcome_unknown=unknown)
    assert lifecycle.submit_to_provider(ticket, provider) == expected
    with pytest.raises(InvalidLedgerTransition):
        lifecycle.submit_to_provider(ticket, provider)
    assert provider.creates == 1


@pytest.mark.parametrize("step", ["create", "capture", "record_order", "apply_event"])
def test_crash_after_attempt_keeps_hold_and_blocks_retry(step, monkeypatch):
    store, lifecycle, ticket, provider = setup()
    def crash(*args, **kwargs):
        raise RuntimeError("injected crash")
    if step in ("capture", "apply_event"):
        lifecycle.submit_to_provider(ticket, provider)
    if step == "record_order":
        monkeypatch.setattr(store, "record_provider_order", crash)
    elif step == "capture":
        # inject into the capture CALL itself (since Mission 3 a payer read precedes it)
        monkeypatch.setattr(provider, "capture_order", crash)
    elif step == "apply_event":
        monkeypatch.setattr(store, "apply_provider_event", crash)
    else:
        provider.error = RuntimeError("injected crash")
    with pytest.raises(RuntimeError, match="injected crash"):
        if step in ("capture", "apply_event"):
            lifecycle.capture(ticket.agent_id, ticket.request_id, provider)
        else:
            lifecycle.submit_to_provider(ticket, provider)
    assert store.ledger_state(ticket.request_id) == LedgerState.reconciliation_required
    assert store.spend_breakdown(ticket.agent_id, "USD", T0)["committed"] == Decimal("40.00")


def test_execution_transaction_crash_rolls_back_every_step(monkeypatch):
    risk = make()
    r = req("agent_a")
    risk.assess(r)
    terms = ApprovalTerms.from_request(r)
    risk.store.approve_authenticated(r.request_id, terms, agent_id=r.agent_id,
        owner_id="owner_a", session_id="session_123", auth_method="owner_api_token", now=T0)
    original = risk.store._to_pending
    def crash(*args, **kw):
        original(*args, **kw)
        raise RuntimeError("after approval consumption and state update")
    monkeypatch.setattr(risk.store, "_to_pending", crash)
    with pytest.raises(RuntimeError):
        PaymentLifecycleService(risk.store).begin_execution(r.agent_id, r.request_id, terms, "fake")
    assert risk.store.approvals(r.request_id)[0]["consumed_at"] is None
    assert risk.store.ledger_state(r.request_id) == LedgerState.approved
    assert risk.store.provider_order(r.request_id) is None
    assert risk.store.query("SELECT * FROM payment_attempts") == []


def test_deadline_keeps_hold_and_does_not_touch_reconciliation_again():
    store, lifecycle, ticket, _ = setup()
    assert lifecycle.expire_and_reconcile(T0 + timedelta(days=1)) == [(ticket.agent_id, ticket.request_id)]
    before = store.ledger_events(ticket.request_id)
    assert lifecycle.expire_and_reconcile(T0 + timedelta(days=100)) == []
    assert store.ledger_events(ticket.request_id) == before


@pytest.mark.parametrize("changes,disposition", [({"verified": False}, "unverified_recorded"),
    ({"amount": "41.00"}, "mismatch_refused"), ({"currency": "EUR"}, "mismatch_refused"),
    ({"amount": None}, "mismatch_refused"), ({"currency": None}, "mismatch_refused")])
def test_unverified_or_mismatched_events_never_settle(changes, disposition):
    store, lifecycle, ticket, provider = setup()
    lifecycle.submit_to_provider(ticket, provider)
    assert lifecycle.apply_provider_event(event(**changes)) == L.provider_pending
    assert store.provider_events()[0]["disposition"] == disposition
    assert any(disposition in a["status"] for a in store.audit_records())


def test_verified_out_of_order_and_refund():
    store, lifecycle, ticket, provider = setup()
    lifecycle.submit_to_provider(ticket, provider)
    assert lifecycle.apply_provider_event(event()) == L.provider_confirmed
    assert lifecycle.apply_provider_event(event(status="pending", event_id="old")) == L.provider_confirmed
    assert store.provider_events()[-1]["disposition"] == "out_of_order_ignored"
    assert lifecycle.apply_provider_event(event(status="refunded", event_id="refund")) == L.reversed


def test_verified_conflict_requires_reconciliation():
    store, lifecycle, ticket, provider = setup()
    lifecycle.submit_to_provider(ticket, provider)
    lifecycle.apply_provider_event(event())
    assert lifecycle.apply_provider_event(event(status="declined", event_id="conflict")) == L.reconciliation_required
    assert lifecycle.reconcile(ticket.agent_id, ticket.request_id, provider) == L.provider_confirmed


def test_concurrent_duplicate_events_settle_once(tmp_path):
    store, lifecycle, ticket, provider = setup(tmp_path / "ledger.db")
    lifecycle.submit_to_provider(ticket, provider)
    stores = [AssessmentStore(tmp_path / "ledger.db") for _ in range(8)]
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda s: PaymentLifecycleService(s, clock=lambda: T0).apply_provider_event(event()), stores))
    assert results == [L.provider_confirmed] * 8
    assert len(store.provider_events()) == 1
    assert len(store.query("SELECT * FROM payment_outcomes")) == 1
    for connection in stores:
        connection.close()


def test_unknown_order_audited_without_settlement():
    store, lifecycle, _, _ = setup()
    with pytest.raises(UnknownProviderOrder):
        lifecycle.apply_provider_event(event())
    assert store.provider_events()[0]["disposition"] == "unknown_order"


def test_provider_environment_checked_before_capture():
    store, lifecycle, ticket, provider = setup()
    lifecycle.submit_to_provider(ticket, provider)
    provider.environment = "production"
    with pytest.raises(InvalidLedgerTransition):
        lifecycle.capture(ticket.agent_id, ticket.request_id, provider)
    assert provider.captures == 0


@pytest.mark.parametrize("status", ["declined", "refunded", "reversed"])
@pytest.mark.parametrize("missing", ["amount", "currency"])
def test_every_final_event_requires_exact_amount_and_currency(status, missing):
    store, lifecycle, ticket, provider = setup()
    lifecycle.submit_to_provider(ticket, provider)
    if status != "declined":
        lifecycle.apply_provider_event(event(event_id="complete-first"))
    before = store.ledger_state(ticket.request_id)
    lifecycle.apply_provider_event(event(status=status, **{missing: None}))
    assert store.ledger_state(ticket.request_id) == before
    assert store.provider_events()[-1]["disposition"] == "mismatch_refused"


def test_event_id_collision_cannot_replace_unknown_event_with_other_order():
    store, lifecycle, ticket, provider = setup()
    with pytest.raises(UnknownProviderOrder):
        lifecycle.apply_provider_event(event(provider_order_id="UNKNOWN"))
    lifecycle.submit_to_provider(ticket, provider)
    with pytest.raises(UnknownProviderOrder):
        lifecycle.apply_provider_event(event())
    assert store.ledger_state(ticket.request_id) == LedgerState.pending
    assert store.provider_events()[0]["provider_order_id"] == "UNKNOWN"
    assert any("collision" in a["status"] for a in store.audit_records())


@pytest.mark.parametrize("field,value", [("approval_id", 800), ("org_id", "other-org"),
    ("auth_method", "owner_session"), ("mfa_satisfied", 1),
    ("approved_at", "2099-01-01T00:00:00+00:00")])
def test_approval_identity_fields_cannot_change_during_consumption(field, value):
    import sqlite3
    risk = make()
    r = req("agent_a")
    risk.assess(r)
    terms = ApprovalTerms.from_request(r)
    risk.store.approve_authenticated(r.request_id, terms, agent_id=r.agent_id,
        owner_id="owner_a", session_id="session_123", auth_method="owner_api_token", now=T0)
    with pytest.raises(sqlite3.IntegrityError):
        with risk.store._tx() as c:
            c.execute(f"UPDATE approvals SET {field}=?, consumed_at=?, consumed_by='key'",
                      (value, T0.isoformat()))


def test_approval_consumption_requires_operation_reference():
    import sqlite3
    risk = make()
    r = req("agent_a")
    risk.assess(r)
    risk.store.approve_authenticated(r.request_id, ApprovalTerms.from_request(r),
        agent_id=r.agent_id, owner_id="owner_a", session_id="session_123",
        auth_method="owner_api_token", now=T0)
    with pytest.raises(sqlite3.IntegrityError):
        with risk.store._tx() as c:
            c.execute("UPDATE approvals SET consumed_at=?", (T0.isoformat(),))


def test_verified_delivery_can_replace_unverified_event_id_poisoning():
    store, lifecycle, ticket, provider = setup()
    lifecycle.submit_to_provider(ticket, provider)
    lifecycle.apply_provider_event(event(verified=False, amount="1.00"))
    assert lifecycle.apply_provider_event(event()) == L.provider_confirmed
    assert len(store.provider_events()) == 1
    assert store.provider_events()[0]["verified"] == 1


def test_different_verified_capture_reference_is_a_conflict():
    store, lifecycle, ticket, provider = setup()
    lifecycle.submit_to_provider(ticket, provider)
    lifecycle.apply_provider_event(event(provider_capture_id="CAPTURE-1"))
    assert lifecycle.apply_provider_event(event(event_id="second", provider_capture_id="CAPTURE-2")) == L.reconciliation_required


def test_unknown_capture_result_requires_reconciliation():
    store, lifecycle, ticket, provider = setup()
    lifecycle.submit_to_provider(ticket, provider)
    provider.capture_result = event(status="unknown")
    assert lifecycle.capture(ticket.agent_id, ticket.request_id, provider) == L.reconciliation_required


def test_strict_mode_cannot_settle_through_legacy_verified_boolean():
    from agentpal.spending import LegacyPathDisabled
    store, lifecycle, ticket, provider = setup()
    store.strict_execution = True
    lifecycle.submit_to_provider(ticket, provider)
    with pytest.raises(LegacyPathDisabled):
        store.record_payment_outcome(ticket.request_id, "completed", True,
            "untrusted_boolean", "CAPTURE-X", agent_id=ticket.agent_id)
    assert store.ledger_state(ticket.request_id) == LedgerState.pending


@pytest.mark.parametrize("step", ["approvals", "ledger_entries", "payment_attempts", "provider_orders"])
def test_sql_crash_at_each_begin_step_rolls_back(step):
    import sqlite3
    risk = make()
    r = req("agent_a")
    risk.assess(r)
    terms = ApprovalTerms.from_request(r)
    risk.store.approve_authenticated(r.request_id, terms, agent_id=r.agent_id,
        owner_id="owner_a", session_id="session_123", auth_method="owner_api_token", now=T0)
    action = "UPDATE" if step in ("approvals", "ledger_entries") else "INSERT"
    with risk.store._tx() as c:
        c.execute(f"CREATE TRIGGER crash_step BEFORE {action} ON {step} "
                  "BEGIN SELECT RAISE(ABORT, 'injected sql crash'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected sql crash"):
        PaymentLifecycleService(risk.store).begin_execution(r.agent_id, r.request_id, terms, "fake")
    assert risk.store.approvals(r.request_id)[0]["consumed_at"] is None
    assert risk.store.ledger_state(r.request_id) == LedgerState.approved
    assert risk.store.provider_order(r.request_id) is None
    assert risk.store.query("SELECT * FROM payment_attempts") == []


@pytest.mark.parametrize("step", ["begin", "create_claim", "order_recorded", "capture_claim"])
def test_restart_after_each_execution_commit_never_retries(step, tmp_path):
    db = tmp_path / "crash.db"
    store, lifecycle, ticket, provider = setup(db)
    if step == "create_claim":
        store.claim_provider_operation(ticket.request_id, agent_id=ticket.agent_id,
            provider="fake", operation="create_order",
            ticket={**ticket.model_dump(), "terms": ticket.terms}, now=T0)
    if step in ("order_recorded", "capture_claim"):
        lifecycle.submit_to_provider(ticket, provider)
    if step == "capture_claim":
        store.claim_provider_operation(ticket.request_id, agent_id=ticket.agent_id,
            provider="fake", operation="capture", now=T0,
            payer_approval=provider.get_order_status("ORDER-1"))
    before = store.provider_order(ticket.request_id)
    store.close()
    risk = make(db)
    recovered = risk.store.provider_order(ticket.request_id)
    assert recovered == before
    assert risk.store.ledger_state(ticket.request_id) == LedgerState.reconciliation_required
    assert risk.store.approvals(ticket.request_id)[0]["consumed_at"] is not None
    with pytest.raises(InvalidLedgerTransition):
        PaymentLifecycleService(risk.store).submit_to_provider(ticket, provider)
    assert provider.creates == (step in ("order_recorded", "capture_claim"))


@pytest.mark.parametrize("operation", ["create", "capture"])
def test_concurrent_provider_operations_call_once(operation, tmp_path):
    store, lifecycle, ticket, provider = setup(tmp_path / "concurrent.db")
    if operation == "capture":
        lifecycle.submit_to_provider(ticket, provider)
        provider.capture_result = event(status="pending")
    stores = [AssessmentStore(tmp_path / "concurrent.db") for _ in range(8)]
    def invoke(s):
        service = PaymentLifecycleService(s, clock=lambda: T0)
        try:
            if operation == "create":
                service.submit_to_provider(ticket, provider)
            else:
                service.capture(ticket.agent_id, ticket.request_id, provider)
            return "ok"
        except InvalidLedgerTransition:
            return "refused"
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(invoke, stores))
    assert results.count("ok") == 1
    assert provider.creates == 1
    assert provider.captures == (operation == "capture")
    for connection in stores:
        connection.close()


def test_same_event_id_with_conflicting_verified_body_quarantines():
    store, lifecycle, ticket, provider = setup()
    lifecycle.submit_to_provider(ticket, provider)
    lifecycle.apply_provider_event(event())
    assert lifecycle.apply_provider_event(event(status="declined")) == L.reconciliation_required


def test_same_event_id_with_different_capture_quarantines():
    store, lifecycle, ticket, provider = setup()
    lifecycle.submit_to_provider(ticket, provider)
    lifecycle.apply_provider_event(event(provider_capture_id="CAPTURE-1"))
    assert lifecycle.apply_provider_event(event(provider_capture_id="CAPTURE-2")) == L.reconciliation_required


def test_upgraded_approval_trigger_protects_existing_v4_database(tmp_path):
    import sqlite3
    db = tmp_path / "old-v4.db"
    store, _, ticket, _ = setup(db)
    with store._tx() as c:
        c.execute("DROP TRIGGER trg_approvals_single_use")
        c.execute("UPDATE approvals SET consumed_at=NULL, consumed_by=NULL")
        c.execute("CREATE TRIGGER trg_approvals_single_use BEFORE UPDATE ON approvals "
                  "WHEN OLD.consumed_at IS NOT NULL BEGIN SELECT RAISE(ABORT,'used'); END")
    store.close()
    reopened = AssessmentStore(db)
    # Reopening replaces the old trigger even for an unconsumed v4 approval.
    with pytest.raises(sqlite3.IntegrityError):
        with reopened._tx() as c:
            c.execute("UPDATE approvals SET org_id='changed', consumed_at=?, consumed_by='key'",
                      (T0.isoformat(),))
    assert reopened.approvals(ticket.request_id)[0]["org_id"] is None


def test_verified_refund_resolves_conflict_after_known_completion():
    store, lifecycle, ticket, provider = setup()
    lifecycle.submit_to_provider(ticket, provider)
    lifecycle.apply_provider_event(event())
    lifecycle.apply_provider_event(event(status="declined", event_id="conflict"))
    assert lifecycle.apply_provider_event(event(status="refunded", event_id="refund")) == L.reversed


@pytest.mark.parametrize("operation", ["create", "capture"])
def test_operation_claim_crash_prevents_any_provider_call(operation):
    import sqlite3
    store, lifecycle, ticket, provider = setup()
    if operation == "capture":
        lifecycle.submit_to_provider(ticket, provider)
    before = (provider.creates, provider.captures)
    with store._tx() as c:
        c.execute("CREATE TRIGGER crash_claim BEFORE INSERT ON provider_operations "
                  "BEGIN SELECT RAISE(ABORT,'claim crash'); END")
    with pytest.raises(sqlite3.IntegrityError, match="claim crash"):
        if operation == "create":
            lifecycle.submit_to_provider(ticket, provider)
        else:
            lifecycle.capture(ticket.agent_id, ticket.request_id, provider)
    assert (provider.creates, provider.captures) == before
    assert store.ledger_state(ticket.request_id) == LedgerState.pending


def test_event_insert_crash_rolls_back_settlement_and_quarantines():
    import sqlite3
    store, lifecycle, ticket, provider = setup()
    lifecycle.submit_to_provider(ticket, provider)
    with store._tx() as c:
        c.execute("CREATE TRIGGER crash_event BEFORE INSERT ON provider_events "
                  "BEGIN SELECT RAISE(ABORT,'event crash'); END")
    with pytest.raises(sqlite3.IntegrityError, match="event crash"):
        lifecycle.capture(ticket.agent_id, ticket.request_id, provider)
    assert store.ledger_state(ticket.request_id) == LedgerState.reconciliation_required
    assert store.query("SELECT * FROM payment_outcomes") == []
    assert store.query("SELECT * FROM provider_event_captures") == []


def test_strict_execution_refuses_legacy_attempt_path():
    from agentpal.spending import LegacyPathDisabled
    risk = make()
    r = req("agent_a")
    risk.assess(r)
    terms = ApprovalTerms.from_request(r)
    risk.store.record_owner_decision(r.request_id, "approved", "owner_a", terms=terms, now=T0)
    risk.store.strict_execution = True
    with pytest.raises(LegacyPathDisabled):
        risk.store.record_payment_attempt(r.request_id, "fake", "ORDER", terms=terms)
    assert risk.store.ledger_state(r.request_id) == LedgerState.approved
    assert risk.store.approvals(r.request_id) == []


def test_structured_approval_cannot_outlive_request():
    risk = make()
    r = req("agent_a", expires_at=(T0 + timedelta(minutes=2)).isoformat())
    risk.assess(r)
    record = risk.store.approve_authenticated(r.request_id, ApprovalTerms.from_request(r),
        agent_id=r.agent_id, owner_id="owner_a", session_id="session_123",
        auth_method="owner_api_token", now=T0)
    assert record["expires_at"] == (T0 + timedelta(minutes=2)).isoformat()


def test_status_applies_deadlines_without_writing():
    store, lifecycle, ticket, _ = setup()
    before = store.ledger_events(ticket.request_id)
    lifecycle.clock = lambda: T0 + timedelta(days=1)
    assert lifecycle.status(ticket.request_id) == L.reconciliation_required
    assert store.ledger_state(ticket.request_id) == LedgerState.pending
    assert store.ledger_events(ticket.request_id) == before


def test_unknown_create_response_keeps_order_for_reconciliation():
    store, lifecycle, ticket, provider = setup()
    original = provider.create_order
    def unknown(*args, **kw):
        return original(*args, **kw).model_copy(update={"status": ProviderStatus.unknown})
    provider.create_order = unknown
    assert lifecycle.submit_to_provider(ticket, provider) == L.reconciliation_required
    assert store.provider_order(ticket.request_id)["provider_order_id"] == "ORDER-1"
