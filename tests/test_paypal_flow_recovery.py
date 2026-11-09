"""A lost capture response survives restart and never leads to another capture."""

import httpx

from agentpal.platform.wiring import build_platform
from test_paypal_flow_api import world, pending, wired_world  # noqa: F401 - pytest fixtures
from test_infra_integration_platform import settings_for
from test_provider_paypal_support import json_response, order_json, make_provider
from agentpal.spending import InvalidLedgerTransition
import pytest


def test_capture_timeout_restart_and_verified_reconciliation(world):  # noqa: F811
    w, fake = world
    payment, path = pending(world)
    def lost_response(request):
        raise httpx.ReadTimeout("accepted capture response lost", request=request)
    fake.routes[("POST", "/v2/checkout/orders/5O190127TN364715T/capture")] = lost_response
    response = w[0].post(path + "/capture", headers={"Authorization": "Bearer " + w[2]})
    assert response.json()["status"] == "reconciliation_required"
    before = w[1].risk_service.store.provider_order(payment["request_id"])
    restarted = build_platform(settings=settings_for(w[1].risk_service.store.path), environ={})
    provider, _ = make_provider(fake)
    from test_paypal_flow_api import _with_purchase_identity
    _with_purchase_identity(provider, fake)
    provider._now = restarted.risk_service.clock
    restarted.provider = provider
    restarted.lifecycle.provider = provider
    try:
        assert restarted.risk_service.store.provider_order(payment["request_id"]) == before
        with pytest.raises(InvalidLedgerTransition):
            restarted.lifecycle.capture(payment["request_id"])
        fake.routes[("GET", "/v2/checkout/orders/5O190127TN364715T")] = json_response(
            200, order_json(status="COMPLETED", capture_status="COMPLETED", amount="1.00"))
        assert restarted.lifecycle.reconcile(payment["request_id"]).value == "provider_confirmed"
        assert restarted.lifecycle.reconcile(payment["request_id"]).value == "provider_confirmed"
        assert len(restarted.risk_service.store.query("SELECT * FROM payment_outcomes")) == 1
        assert len([r for r in fake.requests if r.url.path.endswith("/capture")]) == 1
    finally:
        restarted.close()
