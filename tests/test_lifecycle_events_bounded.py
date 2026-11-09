"""Signature-valid unknown orders are retryable but cannot grow retention without bound."""

import pytest

from agentpal import store as store_module
from agentpal.store import UnknownProviderOrder
from test_lifecycle_execution import setup, event


def test_unknown_order_retention_is_bounded_and_capture_rows_are_pruned(monkeypatch):
    monkeypatch.setattr(store_module, "MAX_UNKNOWN_PROVIDER_EVENTS", 3, raising=False)
    store, lifecycle, _, _ = setup()
    for i in range(8):
        with pytest.raises(UnknownProviderOrder):
            lifecycle.apply_provider_event(event(event_id=f"unknown-{i}",
                provider_order_id=f"UNKNOWN-{i}", provider_capture_id=f"CAPTURE-{i}"))
    assert len(store.query("SELECT * FROM provider_events WHERE disposition='unknown_order'")) == 3
    assert len(store.query("SELECT * FROM provider_event_captures")) == 3
    assert len(store.audit_records()) >= 8


def test_pruning_unknown_events_preserves_known_financial_evidence(monkeypatch):
    monkeypatch.setattr(store_module, "MAX_UNKNOWN_PROVIDER_EVENTS", 2, raising=False)
    store, lifecycle, ticket, provider = setup()
    lifecycle.submit_to_provider(ticket, provider)
    lifecycle.apply_provider_event(event())
    for i in range(5):
        with pytest.raises(UnknownProviderOrder):
            lifecycle.apply_provider_event(event(event_id=f"unknown-{i}", provider_order_id="OTHER"))
    assert len(store.provider_events(ticket.request_id)) == 1
    assert len(store.query("SELECT * FROM payment_outcomes")) == 1
