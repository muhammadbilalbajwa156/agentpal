"""Regression tests for the two findings of the independent Codex review (2026-10-31),
both verified by the lead before fixing:

1. an injected redirect-following httpx client could be redirected from the sandbox to
   the production host; every API call now passes follow_redirects=False;
2. the create/capture key separation (SEC-20) lived only in an in-memory cache; the
   `PayPal-Request-Id` sent is now derived from (operation, key), so a capture header
   can never equal a create header on any instance or after a restart."""

from __future__ import annotations

import httpx
import pytest
from test_provider_paypal_support import (
    FakePayPal,
    json_response,
    make_config,
    make_provider,
    make_terms,
    order_json,
)

from agentpal.infra_contracts import ProviderError
from agentpal.providers import PayPalSandboxProvider
from agentpal.providers.paypal_sandbox import paypal_request_id

OID = "5O190127TN364715T"
CREATE = ("POST", "/v2/checkout/orders")
CAPTURE = ("POST", f"/v2/checkout/orders/{OID}/capture")
PROD = "https://api-m.paypal.com"


def _redirect(path: str):
    return lambda request: httpx.Response(307, headers={"Location": PROD + path})


def _following_provider(fake: FakePayPal) -> PayPalSandboxProvider:
    client = httpx.Client(transport=httpx.MockTransport(fake.handler), follow_redirects=True)
    return PayPalSandboxProvider(make_config(), http_client=client)


def _hosts(fake: FakePayPal) -> set[str]:
    return {r.url.host for r in fake.requests}


def test_oauth_redirect_to_production_is_not_followed():
    fake = FakePayPal()
    fake.routes[("POST", "/v1/oauth2/token")] = _redirect("/v1/oauth2/token")
    with pytest.raises(ProviderError) as exc:
        _following_provider(fake).access_token()
    assert exc.value.outcome_unknown is False
    assert _hosts(fake) == {"api-m.sandbox.paypal.com"}


@pytest.mark.parametrize("route,call", [
    (CREATE, lambda p: p.create_order(make_terms(), idempotency_key="create-key-0001")),
    (CAPTURE, lambda p: p.capture_order(OID, idempotency_key="capture-key-0001")),
])
def test_order_redirect_to_production_is_not_followed(route, call):
    fake = FakePayPal()
    fake.routes[route] = _redirect(route[1])
    with pytest.raises(ProviderError) as exc:
        call(_following_provider(fake))
    # a mutating call answered with an unexpected 3xx: outcome unknown -> reconcile
    assert exc.value.outcome_unknown is True
    assert _hosts(fake) == {"api-m.sandbox.paypal.com"}


def test_create_and_capture_headers_never_collide_across_instances():
    key = "same-key-0001"
    fake = FakePayPal()
    fake.routes[CREATE] = json_response(201, order_json(OID))
    fake.routes[CAPTURE] = json_response(201, order_json(OID, "COMPLETED",
                                                         capture_status="COMPLETED"))
    first, _ = make_provider(fake)
    first.create_order(make_terms(), idempotency_key=key)
    second, _ = make_provider(fake)            # restart / other worker: empty key cache
    second.capture_order(OID, idempotency_key=key)
    create_hdr, capture_hdr = [r.headers["PayPal-Request-Id"] for r in fake.api_requests()]
    assert create_hdr != capture_hdr
    assert create_hdr == paypal_request_id("create", key)
    assert capture_hdr == paypal_request_id("capture", key)


def test_request_id_is_deterministic_per_operation_and_bounded():
    a = paypal_request_id("create", "k" * 128)
    assert a == paypal_request_id("create", "k" * 128)          # retries stay idempotent
    assert a != paypal_request_id("capture", "k" * 128)
    assert len(a) <= 64 and a.isascii()
    with pytest.raises(ValueError):
        paypal_request_id("refund", "key-00001")
