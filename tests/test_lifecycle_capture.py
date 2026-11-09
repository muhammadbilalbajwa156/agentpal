"""A capture needs an exact, verified provider READ, including at the durable claim."""

from datetime import timedelta

import pytest

from agentpal.infra_contracts import PaymentLifecycle as L, ProviderError
from agentpal.spending import InvalidLedgerTransition, LedgerState
from test_lifecycle_execution import setup, event, T0


@pytest.mark.parametrize("changes", [{"verified": False}, {"status": "created"},
    {"status": "payer_action_required"}, {"status": "unknown"}, {"amount": "41.00"},
    {"amount": None}, {"currency": "EUR"}, {"currency": None},
    {"provider_order_id": "OTHER-ORDER"}, {"provider": "other"},
    {"event_type": "CHECKOUT.ORDER.APPROVED"},
    {"received_at": T0 - timedelta(minutes=1)}])
def test_capture_refuses_invalid_payer_read_without_capture_io(changes):
    store, lifecycle, ticket, provider = setup()
    lifecycle.submit_to_provider(ticket, provider)
    provider.get_order_status = lambda _: event(status="approved_by_payer",
        event_type="ORDER_STATUS_READ").model_copy(update=changes)
    with pytest.raises(InvalidLedgerTransition):
        lifecycle.capture(ticket.agent_id, ticket.request_id, provider)
    assert provider.captures == 0
    assert store.query("SELECT * FROM provider_operations WHERE operation='capture'") == []


def test_capture_operation_cannot_be_claimed_without_verified_read():
    store, lifecycle, ticket, provider = setup()
    lifecycle.submit_to_provider(ticket, provider)
    with pytest.raises(InvalidLedgerTransition):
        store.claim_provider_operation(ticket.request_id, agent_id=ticket.agent_id,
            provider=provider.name, operation="capture", now=T0)


def test_claim_rechecks_terms_after_provider_read():
    store, lifecycle, ticket, provider = setup()
    lifecycle.submit_to_provider(ticket, provider)
    def race(_):
        with store._tx() as c:
            c.execute("UPDATE ledger_entries SET amount='41.00'")
        return event(status="approved_by_payer", event_type="ORDER_STATUS_READ")
    provider.get_order_status = race
    with pytest.raises(InvalidLedgerTransition):
        lifecycle.capture(ticket.agent_id, ticket.request_id, provider)
    assert provider.captures == 0


def test_claim_rechecks_deadline_after_provider_read():
    store, lifecycle, ticket, provider = setup()
    lifecycle.submit_to_provider(ticket, provider)
    def race(_):
        lifecycle.clock = lambda: T0 + timedelta(days=2)
        return event(status="approved_by_payer", event_type="ORDER_STATUS_READ")
    provider.get_order_status = race
    with pytest.raises(InvalidLedgerTransition):
        lifecycle.capture(ticket.agent_id, ticket.request_id, provider)
    assert provider.captures == 0
    assert store.ledger_state(ticket.request_id) == LedgerState.reconciliation_required


def test_preflight_timeout_keeps_hold_without_capture():
    store, lifecycle, ticket, provider = setup()
    lifecycle.submit_to_provider(ticket, provider)
    provider.get_order_status = lambda _: (_ for _ in ()).throw(
        ProviderError("read timeout", outcome_unknown=True))
    # Lead audit decision: a failed READ (no capture sent) keeps the entry pending and
    # capturable, never reconciliation_required; the hold and no-capture guarantees stay.
    with pytest.raises(InvalidLedgerTransition, match="PAYER_READ_UNAVAILABLE"):
        lifecycle.capture(ticket.agent_id, ticket.request_id, provider)
    assert store.ledger_state(ticket.request_id) == LedgerState.pending
    assert provider.captures == 0
    assert store.spend_breakdown(ticket.agent_id, "USD", T0)["committed"] == ticket.terms.amount


def test_verified_read_of_completed_order_reconciles_without_capture():
    store, lifecycle, ticket, provider = setup()
    lifecycle.submit_to_provider(ticket, provider)
    provider.get_order_status = lambda _: event(event_type="ORDER_STATUS_READ")
    assert lifecycle.capture(ticket.agent_id, ticket.request_id, provider) == L.provider_confirmed
    assert provider.captures == 0


def test_capture_compares_read_with_assessed_amount_even_if_ledger_corrupt():
    store, lifecycle, ticket, provider = setup()
    lifecycle.submit_to_provider(ticket, provider)
    with store._tx() as c:
        c.execute("UPDATE ledger_entries SET amount='41.00'")
    provider.get_order_status = lambda _: event(status="approved_by_payer", amount="41.00",
                                               event_type="ORDER_STATUS_READ")
    with pytest.raises(InvalidLedgerTransition):
        lifecycle.capture(ticket.agent_id, ticket.request_id, provider)
    assert provider.captures == 0


@pytest.mark.parametrize("changes", [{"verified": False}, {"amount": "41.00"},
    {"currency": "EUR"}, {"amount": None}, {"currency": None}])
def test_unverified_or_inexact_capture_response_requires_reconciliation(changes):
    store, lifecycle, ticket, provider = setup()
    lifecycle.submit_to_provider(ticket, provider)
    provider.capture_result = event().model_copy(update=changes)
    assert lifecycle.capture(ticket.agent_id, ticket.request_id, provider) == L.reconciliation_required
    assert len(store.query("SELECT * FROM payment_outcomes")) == 0
    with pytest.raises(InvalidLedgerTransition):
        lifecycle.capture(ticket.agent_id, ticket.request_id, provider)
    assert provider.captures == 1


@pytest.mark.parametrize("failure", [ProviderError("read timeout", outcome_unknown=True),
                                     RuntimeError("read crashed")])
def test_failed_payer_read_before_capture_keeps_the_payment_capturable(failure):
    """Lead audit (Mission 3): a failed READ happens before any capture request, so no
    money outcome is uncertain. It must not freeze the payment in reconciliation_required
    (which can never be captured again); the owner simply retries the capture."""
    store, lifecycle, ticket, provider = setup()
    lifecycle.submit_to_provider(ticket, provider)
    approved = provider.get_order_status

    def failing(_):
        raise failure
    provider.get_order_status = failing
    with pytest.raises((InvalidLedgerTransition, RuntimeError)):
        lifecycle.capture(ticket.agent_id, ticket.request_id, provider)
    assert provider.captures == 0
    assert store.ledger_state(ticket.request_id) == LedgerState.pending
    assert store.query("SELECT * FROM provider_operations WHERE operation='capture'") == []

    provider.get_order_status = lambda _: event(status="approved_by_payer",
                                                event_type="ORDER_STATUS_READ")
    lifecycle.capture(ticket.agent_id, ticket.request_id, provider)
    assert provider.captures == 1
    assert store.ledger_state(ticket.request_id) != LedgerState.reconciliation_required
    del approved
