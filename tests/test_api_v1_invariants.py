"""Structural invariants of the /v1 surface (companion to test_invariants_api.py, which
keeps guarding the legacy app built without `platform`)."""

from __future__ import annotations

import re

from fastapi.routing import APIRoute

from test_invariants_api import EXPECTED_ROUTES
from test_platform_fixtures import ALL_PAY, OWNER_A, build_world

V1_ROUTES = {
    ("/v1/paypal/reconciliation", "GET"),
    ("/v1/payout-requests", "POST"), ("/v1/payout-requests", "GET"),
    ("/v1/payout-requests/{request_id}", "GET"),
    ("/v1/payout-requests/{request_id}/timeline", "GET"),
    ("/v1/setup/status", "GET"),
    ("/v1/setup/owner", "POST"),
    ("/v1/integrations", "GET"),
    ("/v1/integrations/{provider}", "PUT"),
    ("/v1/integrations/{provider}", "DELETE"),
    ("/v1/integrations/{provider}/verify", "POST"),
    ("/v1/lab/agent", "POST"),
    ("/v1/lab/payment-requests", "POST"),
    ("/v1/lab/payment-requests/{request_id}/execute", "POST"),
    ("/v1/lab/errands", "POST"),
    ("/v1/payout-requests/{request_id}/approve", "POST"),
    ("/v1/payout-requests/{request_id}/reject", "POST"),
    ("/v1/payout-requests/{request_id}/execute", "POST"),
    ("/v1/payout-requests/{request_id}/reconciliation", "POST"),
    ("/v1/health", "GET"), ("/v1/agents", "POST"), ("/v1/agents", "GET"),
    ("/v1/agents/{agent_id}", "GET"), ("/v1/agents/{agent_id}/credentials", "POST"),
    ("/v1/agents/{agent_id}/credentials/{key_id}", "DELETE"),
    ("/v1/agents/{agent_id}/suspend", "POST"), ("/v1/agents/{agent_id}/reactivate", "POST"),
    ("/v1/agents/{agent_id}/permissions", "GET"), ("/v1/agents/{agent_id}/permissions", "PUT"),
    ("/v1/wallets", "POST"), ("/v1/wallets", "GET"), ("/v1/wallets/{wallet_id}", "GET"),
    ("/v1/wallets/{wallet_id}", "PATCH"), ("/v1/wallets/{wallet_id}/freeze", "POST"),
    ("/v1/wallets/{wallet_id}/unfreeze", "POST"),
    ("/v1/wallets/{wallet_id}/transactions", "GET"),
    ("/v1/me", "GET"), ("/v1/me/wallet", "GET"), ("/v1/me/wallet/transactions", "GET"),
    ("/v1/payment-requests", "POST"), ("/v1/payment-requests", "GET"),
    ("/v1/payment-requests/{request_id}", "GET"),
    ("/v1/payment-requests/{request_id}/timeline", "GET"),
    ("/v1/payment-requests/{request_id}/request-decision", "POST"),
    ("/v1/approvals", "GET"),
    ("/v1/payment-requests/{request_id}/approve", "POST"),
    ("/v1/payment-requests/{request_id}/reject", "POST"),
    ("/v1/payment-requests/{request_id}/execute", "POST"),
    ("/v1/payment-requests/{request_id}/capture", "POST"),
    ("/v1/payment-requests/{request_id}/reconciliation", "POST"),
    ("/v1/payment-requests/{request_id}/market-check", "POST"),  # Channel3, advisory
    ("/v1/payment-requests/{request_id}/reconciliation", "GET"),
    ("/v1/payment-requests/{request_id}/page-check", "POST"),
    ("/v1/research/browse", "POST"),
    ("/v1/providers/paypal/webhooks", "POST"),
    ("/v1/payment-requests/{request_id}/refunds", "POST"),
    ("/v1/payment-requests/{request_id}/refunds", "GET"),
    ("/v1/payment-requests/{request_id}/refunds/reconciliation", "POST"),
    ("/v1/disputes", "GET"),
}
OPEN = {"/v1/health", "/v1/providers/paypal/webhooks", "/v1/setup/status", "/v1/setup/owner"}


def _routes(app):
    return {(r.path, m) for r in app.routes if isinstance(r, APIRoute) for m in r.methods}


def test_route_table_is_exactly_the_documented_one():
    w = build_world()
    routes = _routes(w.app)
    assert {r for r in routes if r[0].startswith("/v1")} == V1_ROUTES
    assert EXPECTED_ROUTES <= routes                   # legacy surface unchanged


def test_no_route_completes_captures_or_records_outcomes():
    w = build_world()
    for path, method in _routes(w.app):
        if (path, method) == ("/v1/payment-requests/{request_id}/capture", "POST"):
            continue  # Mission 3 approved owner-only, verified-payer capture gateway
        if (path, method) in {
                ("/v1/payment-requests/{request_id}/refunds", "POST"),
                ("/v1/payment-requests/{request_id}/refunds", "GET"),
                ("/v1/payment-requests/{request_id}/refunds/reconciliation", "POST")}:
            # Lead-approved 2026-11-03: owner-only verified PayPal refunds (P2)
            continue
        assert not re.search(r"complete|capture|outcome|settle|ledger|refund", path), path


def test_every_non_open_v1_route_requires_a_credential():
    w = build_world()
    for path, method in V1_ROUTES:
        if path in OPEN:
            continue
        concrete = (path.replace("{agent_id}", "agt_" + "0" * 32)
                    .replace("{wallet_id}", "wal_" + "0" * 32)
                    .replace("{key_id}", "0" * 16).replace("{request_id}", "pay_x123"))
        r = w.client.request(method, concrete, json={})
        assert r.status_code == 401 and r.json() == {"error": "authentication_required"}, \
            (method, path)


def test_approval_and_execution_are_split_between_owner_and_agent():
    w = build_world()
    _, key, _ = w.funded_agent(caps=ALL_PAY)
    p = w.pay(key).json()
    rid = p["request_id"]
    assert w.client.post(f"/v1/payment-requests/{rid}/approve",
                         json={"terms_sha256": p["terms_sha256"]},
                         headers=w.h(key)).status_code == 403
    assert w.client.post(f"/v1/payment-requests/{rid}/execute",
                         headers=w.h(OWNER_A)).status_code == 403
    assert w.risk.store.ledger_state(rid).value == "reserved"


def test_platform_never_calls_record_payment_attempt_or_outcome():
    """Execution goes through the lifecycle gateway only (S1 sets strict_execution)."""
    import inspect

    import agentpal.platform as platform_pkg
    from agentpal.platform import agents, credentials, payments, router, runtime, wallets

    for mod in (agents, credentials, payments, router, runtime, wallets, platform_pkg):
        src = inspect.getsource(mod)
        assert "record_payment_attempt(" not in src.replace(
            "never calls `record_payment_attempt`", "")
        assert "record_payment_outcome(" not in src
        assert "record_owner_decision(" not in src
        assert "transition_ledger(" not in src
