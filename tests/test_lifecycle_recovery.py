"""SEC-22: startup recovery never guesses a provider outcome or trusts wallet copies."""

from datetime import timedelta

import pytest

from agentpal.lifecycle.recovery import check_consistency, startup_recovery
from agentpal.spending import ApprovalTerms, LedgerState
from agentpal.wallet.store import WalletStore
from test_sec_lifecycle import T0, make, req


def ledger_request(request_id="pay_recovery"):
    risk, clock = make()
    request = req(request_id)
    risk.assess(request)
    return risk, clock, request, ApprovalTerms.from_request(request)


def wallet_copy(request, terms):
    wallet = WalletStore()
    with wallet.tx() as c:
        c.execute("INSERT INTO wallet_owners VALUES ('o','Owner',?)", (T0.isoformat(),))
        c.execute("INSERT INTO wallet_agents VALUES (?, 'o', 'external', 'active', ?)",
                  (request.agent_id, T0.isoformat()))
        c.execute("INSERT INTO wallet_payment_requests VALUES (?,?,?,?,?,?,?,?)",
                  (request.request_id, request.agent_id, "o", terms.model_dump_json(),
                   terms.digest(), "approve", 0, T0.isoformat()))
    return wallet


def attempt(risk, request, terms):
    risk.store.approve_authenticated(request.request_id, terms, agent_id=request.agent_id,
                                    owner_id="o", session_id="session_recovery",
                                    auth_method="owner_api_token", now=T0)
    risk.store.begin_execution(request.request_id, terms, "fake_sandbox",
                               agent_id=request.agent_id, now=T0)


def test_restart_quarantines_fresh_pending_before_reconcile_deadline():
    risk, clock, request, terms = ledger_request()
    attempt(risk, request, terms)
    report = startup_recovery(risk.store, now=T0 + timedelta(seconds=1))
    assert risk.store.ledger_state(request.request_id) == LedgerState.reconciliation_required
    assert report.moved_to_reconciliation == [(request.agent_id, request.request_id)]
    assert report.clean
    events = risk.store.ledger_events(request.request_id)
    startup_recovery(risk.store, now=T0 + timedelta(days=1))
    assert risk.store.ledger_events(request.request_id) == events


def test_cross_database_mismatch_refuses_startup_instead_of_only_reporting():
    risk, clock, request, terms = ledger_request()
    wallet = wallet_copy(request, terms)
    with wallet.tx() as c:
        c.execute("UPDATE wallet_payment_requests SET terms_sha256=?", ("0" * 64,))
    with pytest.raises(RuntimeError, match="consistency") as failure:
        startup_recovery(risk.store, wallet, now=T0)
    assert failure.value.report.issues[0].kind == "terms_digest_differs"
    assert risk.store.ledger_state(request.request_id) == LedgerState.reserved


def test_canonical_wallet_terms_are_checked_not_only_stored_digest():
    risk, clock, request, terms = ledger_request()
    wallet = wallet_copy(request, terms)
    with wallet.tx() as c:
        c.execute("UPDATE wallet_payment_requests SET terms_json='{}'")
    assert "wallet_terms_invalid" in {i.kind for i in check_consistency(risk.store, wallet)}


def test_registry_owner_mismatch_fails_closed():
    risk, clock, request, terms = ledger_request()
    wallet = wallet_copy(request, terms)
    with wallet.tx() as c:
        c.execute("INSERT INTO wallet_owners VALUES ('other','Other',?)", (T0.isoformat(),))
        c.execute("UPDATE wallet_agents SET owner_id='other'")
    assert "registry_owner_differs" in {i.kind for i in check_consistency(risk.store, wallet)}


def test_strict_execution_requires_provider_attempt_and_consumed_approval():
    risk, clock, request, terms = ledger_request()
    attempt(risk, request, terms)
    risk.store.strict_execution = True
    with risk.store._tx() as c:
        c.execute("DELETE FROM provider_orders")
    assert "attempt_without_provider_order" in {i.kind for i in check_consistency(risk.store)}


def test_startup_recovers_stale_assessment_claims_itself():
    risk, clock = make()
    request = req("pay_stale_recovery")
    claimed, _ = risk.store.claim_request(request, T0, risk._reservation_request(request, T0))
    assert claimed
    report = startup_recovery(risk.store, now=T0 + timedelta(minutes=6))
    assert report.stale_claims_recovered == [request.request_id]
    assert risk.store.ledger_state(request.request_id) == LedgerState.released


def test_clean_wallet_and_ledger_remain_unchanged():
    risk, clock, request, terms = ledger_request()
    wallet = wallet_copy(request, terms)
    assert check_consistency(risk.store, wallet) == []
    assert startup_recovery(risk.store, wallet, now=T0).clean


def test_wallet_initialization_can_check_without_quarantining_live_attempt():
    risk, clock, request, terms = ledger_request()
    wallet = wallet_copy(request, terms)
    attempt(risk, request, terms)
    report = startup_recovery(risk.store, wallet, now=T0, recover_inflight=False)
    assert report.clean
    assert risk.store.ledger_state(request.request_id) == LedgerState.pending


def test_simulation_reference_must_match_the_ledger_order():
    risk, clock, request, terms = ledger_request()
    wallet = wallet_copy(request, terms)
    attempt(risk, request, terms)
    order = risk.store.provider_order(request.request_id)
    risk.store.record_provider_order(request.request_id, agent_id=request.agent_id,
                                     provider="fake_sandbox", provider_order_id="sim-right",
                                     idempotency_key=order["create_key"], status="created", now=T0)
    with wallet.tx() as c:
        c.execute("INSERT INTO wallet_sandbox_simulations "
                  "(request_id, agent_id, simulation_reference, terms_sha256, at) "
                  "VALUES (?,?,?,?,?)", (request.request_id, request.agent_id,
                                         "sim-wrong", terms.digest(), T0.isoformat()))
    assert "simulation_reference_differs" in {
        i.kind for i in check_consistency(risk.store, wallet)}


def test_provider_order_must_match_the_persisted_attempt_key():
    risk, clock, request, terms = ledger_request()
    attempt(risk, request, terms)
    with risk.store._tx() as c:
        c.execute("UPDATE payment_attempts SET provider_reference='wrong_key'")
    assert "provider_attempt_differs" in {i.kind for i in check_consistency(risk.store)}


@pytest.mark.parametrize("column,value", [
    ("owner_id", "other_owner"),
    ("terms_sha256", "0" * 64),
    ("consumed_at", (T0 + timedelta(minutes=15)).isoformat()),
    ("consumed_at", (T0 - timedelta(seconds=1)).isoformat()),
])
def test_consumed_approval_must_match_authoritative_owner_terms_and_window(column, value):
    risk, clock, request, terms = ledger_request()
    attempt(risk, request, terms)
    # Simulate a corrupted/old database, bypassing the current immutable-row trigger.
    with risk.store._tx() as c:
        c.execute("DROP TRIGGER trg_approvals_single_use")
        c.execute(f"UPDATE approvals SET {column}=?", (value,))
    assert "provider_approval_differs" in {i.kind for i in check_consistency(risk.store)}
    with pytest.raises(RuntimeError, match="consistency"):
        startup_recovery(risk.store, now=T0, recover_inflight=False)


@pytest.mark.parametrize("column,value", [("amount", "41.00"), ("currency", "EUR")])
def test_ledger_amount_and_currency_must_match_assessed_request(column, value):
    risk, clock, request, terms = ledger_request()
    with risk.store._tx() as c:
        c.execute(f"UPDATE ledger_entries SET {column}=?", (value,))
    assert "ledger_terms_differ" in {i.kind for i in check_consistency(risk.store)}


def test_equivalent_exact_decimal_representations_are_consistent():
    risk, clock, request, terms = ledger_request()
    with risk.store._tx() as c:
        c.execute("UPDATE ledger_entries SET amount='40.0000'")
    assert check_consistency(risk.store) == []


@pytest.mark.parametrize("operation", ["create_order", "capture"])
def test_provider_operation_key_must_match_persisted_order_key(operation):
    risk, clock, request, terms = ledger_request()
    attempt(risk, request, terms)
    with risk.store._tx() as c:
        c.execute("INSERT INTO provider_operations VALUES (?,?,?,?,?)",
                  (request.agent_id, request.request_id, operation,
                   "incorrect_operation_key", T0.isoformat()))
    assert "provider_operation_differs" in {i.kind for i in check_consistency(risk.store)}


def test_orphan_provider_operation_is_inconsistent():
    risk, clock = make()
    with risk.store._tx() as c:
        c.execute("INSERT INTO provider_operations VALUES (?,?,?,?,?)",
                  ("agent_demo_001", "missing", "create_order", "orphan_operation_key",
                   T0.isoformat()))
    assert "provider_operation_differs" in {i.kind for i in check_consistency(risk.store)}
