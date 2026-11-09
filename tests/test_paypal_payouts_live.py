"""LIVE, $1 USD sandbox-only checks through the actual /v1 application."""

import asyncio
import json
import os

import pytest

from agentpal.demo.payout_flow import run_flow

pytestmark = pytest.mark.skipif(not all(os.environ.get(k, "").strip() for k in (
    "PAYPAL_SANDBOX_CLIENT_ID", "PAYPAL_SANDBOX_CLIENT_SECRET")),
    reason="LIVE PayPal payouts: sandbox credentials absent")


def test_live_priya_payout_owner_approved_verified_and_idempotent():
    report = asyncio.run(run_flow(live=True, poll_timeout=120))
    print(json.dumps(report, sort_keys=True))  # identifiers are redacted by the demo
    assert report["status"] == "provider_confirmed"
    assert report["provider_item_status"] == "SUCCESS"
    assert report["verified_outcomes"] == 1
    assert report["create_invocations"] == 1
    assert report["is_real_money"] is False


def test_live_simulator_error_never_confirms():
    report = asyncio.run(run_flow(live=True, receiver="payouts-simulator-receiver@paypal.com",
                                 note="ERRPYO002", poll_timeout=60))
    print(json.dumps(report, sort_keys=True))
    assert report["status"] in {"failed", "reversed", "provider_pending", "reconciliation_required"}
    assert report["verified_outcomes"] == 0
    assert report["create_invocations"] == 1
    assert "provider_confirmed" not in report["states"]
