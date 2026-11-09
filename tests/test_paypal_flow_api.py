"""Real API, identity, approval, ledger and PayPal adapter; only PayPal HTTP is mocked."""

import httpx
import pytest

from test_infra_integration_platform import prepare, approve, execute, bearer
from test_infra_integration_platform import world as wired_world  # noqa: F401 - pytest fixture
from test_provider_paypal_support import make_provider, order_json, json_response


@pytest.fixture
def world(wired_world):  # noqa: F811 - pytest fixture
    client, deps, *_ = wired_world
    provider, fake = make_provider()
    provider._now = deps.risk_service.clock
    deps.provider = provider
    deps.lifecycle.provider = provider
    fake.routes[("POST", "/v2/checkout/orders")] = json_response(201, order_json(amount="1.00"))
    fake.routes[("GET", "/v2/checkout/orders/5O190127TN364715T")] = json_response(
        200, order_json(status="APPROVED", amount="1.00"))
    fake.routes[("POST", "/v2/checkout/orders/5O190127TN364715T/capture")] = json_response(
        201, order_json(status="COMPLETED", capture_status="COMPLETED", amount="1.00"))
    # P1: realistic order identity in success fixtures, derived from the submitted
    # request. Deliberate amount/currency/status mutations remain untouched.
    _with_purchase_identity(provider, fake)
    yield wired_world, fake


def pending(world):
    w, _ = world
    payment = prepare(w)
    approve(w, payment)
    assert execute(w, payment).status_code == 200
    return payment, f'/v1/payment-requests/{payment["request_id"]}'


def test_owner_capture_and_replay_use_real_paypal_adapter(world):
    w, fake = world
    payment, path = pending(world)
    response = w[0].post(path + "/capture", headers=bearer(w[2]))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "provider_confirmed"
    assert w[0].post(path + "/capture", headers=bearer(w[2])).status_code == 409
    captures = [r for r in fake.requests if r.url.path.endswith("/capture")]
    assert len(captures) == 1
    assert w[1].risk_service.store.approvals(payment["request_id"])[0]["consumed_at"]


@pytest.mark.parametrize("caller,status", [("none", 401), ("agent", 403), ("other", 404)])
def test_capture_is_owner_only_and_scoped_before_io(world, caller, status):
    w, fake = world
    _, path = pending(world)
    headers = {}
    if caller == "agent":
        headers = bearer(w[4])
    elif caller == "other":
        w[1].owner_authenticator.create_org("other_org", "Other organization")
        w[1].owner_authenticator.create_owner("other_owner", org_id="other_org")
        token, _ = w[1].owner_authenticator.issue_token("other_owner")
        headers = bearer(token)
    before = len(fake.requests)
    assert w[0].post(path + "/capture", headers=headers).status_code == status
    assert len(fake.requests) == before


def test_reconciliation_get_is_local_post_verifies_provider(world):
    w, fake = world
    _, path = pending(world)
    before = len(fake.requests)
    response = w[0].get(path + "/reconciliation", headers=bearer(w[2]))
    assert response.status_code == 200
    assert response.json()["provider_references"][0]["payer_action_url"].startswith(
        "https://www.sandbox.paypal.com/")
    assert len(fake.requests) == before
    fake.routes[("GET", "/v2/checkout/orders/5O190127TN364715T")] = json_response(
        200, order_json(status="COMPLETED", capture_status="COMPLETED", amount="1.00"))
    response = w[0].post(path + "/reconciliation", headers=bearer(w[2]))
    assert response.status_code == 200
    assert response.json()["status"] == "provider_confirmed"
    assert len(fake.requests) == before + 1


def test_read_timeout_keeps_allowance_held(world):
    w, fake = world
    payment, path = pending(world)
    def timeout(request):
        raise httpx.ReadTimeout("injected", request=request)
    fake.routes[("GET", "/v2/checkout/orders/5O190127TN364715T")] = timeout
    response = w[0].post(path + "/capture", headers=bearer(w[2]))
    # Lead audit decision: a failed payer READ captured nothing, so the request stays
    # pending (allowance still held) and the owner gets a retryable 503.
    assert response.status_code == 503
    assert response.json() == {"error": "provider_unavailable"}
    assert not any(r.url.path.endswith("/capture") for r in fake.requests)
    assert w[1].risk_service.store.ledger_entry(payment["request_id"])["state"] == "pending"


@pytest.mark.parametrize("change,code", [("freeze", 409), ("suspend", 403), ("revoke", 403)])
def test_capture_rechecks_current_wallet_and_agent_controls(world, change, code):
    w, fake = world
    _, path = pending(world)
    if change == "freeze":
        response = w[0].post(f'/v1/wallets/{w[6]["wallet_id"]}/freeze', headers=bearer(w[2]))
    elif change == "suspend":
        response = w[0].post(f'/v1/agents/{w[5]}/suspend', headers=bearer(w[2]))
    else:
        response = w[0].put(f'/v1/agents/{w[5]}/permissions', headers=bearer(w[2]),
                            json={"capabilities": ["wallet.read"]})
    assert response.status_code == 200
    before = len(fake.requests)
    assert w[0].post(path + "/capture", headers=bearer(w[2])).status_code == code
    assert len(fake.requests) == before


@pytest.mark.parametrize("executed,approved", [(False, False), (False, True)])
def test_capture_requires_executed_payment(world, executed, approved):
    w, fake = world
    payment = prepare(w)
    if approved:
        approve(w, payment)
    before = len(fake.requests)
    response = w[0].post(f'/v1/payment-requests/{payment["request_id"]}/capture',
                         headers=bearer(w[2]))
    assert response.status_code == 409
    assert len(fake.requests) == before


@pytest.mark.parametrize("status,amount,currency", [("CREATED", "1.00", "USD"),
    ("APPROVED", "2.00", "USD"), ("APPROVED", "1.00", "EUR")])
def test_capture_does_not_trust_browser_return_or_inexact_approval(world, status, amount, currency):
    w, fake = world
    _, path = pending(world)
    fake.routes[("GET", "/v2/checkout/orders/5O190127TN364715T")] = json_response(
        200, order_json(status=status, amount=amount, currency=currency))
    response = w[0].post(path + "/capture?PayerID=claimed&token=claimed", headers=bearer(w[2]),
                         json={"approved": True})
    assert response.status_code == 409
    assert not any(r.url.path.endswith("/capture") for r in fake.requests)


def test_capture_timeout_reconciles_once_without_resubmission(world):
    w, fake = world
    payment, path = pending(world)
    def timeout(request):
        raise httpx.ReadTimeout("after send", request=request)
    fake.routes[("POST", "/v2/checkout/orders/5O190127TN364715T/capture")] = timeout
    response = w[0].post(path + "/capture", headers=bearer(w[2]))
    assert response.json()["status"] == "reconciliation_required"
    assert w[0].post(path + "/capture", headers=bearer(w[2])).status_code == 409
    fake.routes[("GET", "/v2/checkout/orders/5O190127TN364715T")] = json_response(
        200, order_json(status="COMPLETED", capture_status="COMPLETED", amount="1.00"))
    assert w[0].post(path + "/reconciliation", headers=bearer(w[2])).json()["status"] == (
        "provider_confirmed")
    assert len([r for r in fake.requests if r.url.path.endswith("/capture")]) == 1
    assert len(w[1].risk_service.store.query("SELECT * FROM payment_outcomes")) == 1
    assert w[1].risk_service.store.ledger_entry(payment["request_id"])["state"] == "completed"


def test_reconciliation_post_is_owner_scoped_and_agent_refused(world):
    w, fake = world
    _, path = pending(world)
    w[1].owner_authenticator.create_owner("other_owner")
    token, _ = w[1].owner_authenticator.issue_token("other_owner")
    before = len(fake.requests)
    assert w[0].post(path + "/reconciliation", headers=bearer(token)).status_code == 404
    assert w[0].post(path + "/reconciliation", headers=bearer(w[4])).status_code == 403
    assert len(fake.requests) == before


def test_expired_owner_token_cannot_capture(world):
    from datetime import timedelta
    w, fake = world
    _, path = pending(world)
    identity = w[1].owner_authenticator
    identity.clock = lambda: w[1].risk_service.clock() + timedelta(days=31)
    before = len(fake.requests)
    assert w[0].post(path + "/capture", headers=bearer(w[2])).status_code == 401
    assert len(fake.requests) == before


def test_expired_approval_never_creates_provider_order(world):
    from datetime import timedelta
    w, fake = world
    payment = prepare(w)
    approve(w, payment)
    now = w[1].risk_service.clock()
    w[1].lifecycle.lifecycle.clock = lambda: now + timedelta(hours=1)
    assert execute(w, payment).status_code == 409
    assert fake.requests == []


def test_unknown_create_without_order_id_stays_held(world):
    w, fake = world
    def timeout(request):
        raise httpx.ReadTimeout("after send", request=request)
    fake.routes[("POST", "/v2/checkout/orders")] = timeout
    payment, path = pending(world)
    assert w[0].post(path + "/reconciliation", headers=bearer(w[2])).status_code == 409
    assert w[0].post(path + "/capture", headers=bearer(w[2])).status_code == 409
    assert w[1].risk_service.store.ledger_entry(payment["request_id"])["state"] == (
        "reconciliation_required")


def test_payer_url_survives_database_reopen(world):
    from agentpal.store import AssessmentStore
    w, _ = world
    payment, _ = pending(world)
    store = AssessmentStore(w[1].risk_service.store.path)
    try:
        assert store.provider_payer_url(payment["request_id"], agent_id=w[5]).startswith(
            "https://www.sandbox.paypal.com/")
    finally:
        store.close()


def test_signature_valid_unknown_order_is_audited_and_retryable(world):
    from test_provider_paypal_support import (
        make_trusted_signing_cert, trust_test_root, capture_event, signed_headers, CERT_URL,
    )
    w, fake = world
    key, pem = make_trusted_signing_cert()
    trust_test_root(w[1].provider)
    fake.routes[("GET", httpx.URL(CERT_URL).path)] = lambda r: httpx.Response(200, content=pem)
    raw = capture_event()
    sent = w[1].risk_service.clock().isoformat()
    response = w[0].post("/v1/providers/paypal/webhooks", content=raw,
                         headers=signed_headers(key, raw, transmission_time=sent))
    assert response.status_code == 503
    assert response.json() == {"error": "webhook_retry_required"}
    assert w[1].risk_service.store.provider_events()[0]["disposition"] == "unknown_order"


def test_webhook_persistence_failure_is_retryable(world, monkeypatch):
    from test_provider_paypal_support import (
        make_trusted_signing_cert, trust_test_root, capture_event, signed_headers, CERT_URL,
    )
    w, fake = world
    pending(world)
    key, pem = make_trusted_signing_cert()
    trust_test_root(w[1].provider)
    fake.routes[("GET", httpx.URL(CERT_URL).path)] = lambda r: httpx.Response(200, content=pem)
    def crash(*args, **kwargs):
        raise RuntimeError("persistence unavailable")
    monkeypatch.setattr(w[1].risk_service.store, "apply_provider_event", crash)
    raw = capture_event()
    response = w[0].post("/v1/providers/paypal/webhooks", content=raw,
        headers=signed_headers(key, raw, transmission_time=w[1].risk_service.clock().isoformat()))
    assert response.status_code == 503
    assert response.json() == {"error": "webhook_retry_required"}


def test_duplicate_signature_header_is_rejected_before_verification(world):
    w, fake = world
    before = len(fake.requests)
    response = w[0].post("/v1/providers/paypal/webhooks", content=b"{}", headers=[
        ("paypal-transmission-sig", "one"), ("paypal-transmission-sig", "two")])
    assert response.status_code == 400
    assert len(fake.requests) == before


def test_signature_valid_unknown_order_redelivery_settles_once_after_create(world):
    import json
    from test_provider_paypal_support import (
        make_trusted_signing_cert, trust_test_root, capture_event, signed_headers, CERT_URL,
    )
    w, fake = world
    key, pem = make_trusted_signing_cert()
    trust_test_root(w[1].provider)
    fake.routes[("GET", httpx.URL(CERT_URL).path)] = lambda r: httpx.Response(200, content=pem)
    payload = json.loads(capture_event())
    payload["resource"]["amount"]["value"] = "1.00"
    raw = json.dumps(payload).encode()
    headers = signed_headers(key, raw, transmission_time=w[1].risk_service.clock().isoformat())
    assert w[0].post("/v1/providers/paypal/webhooks", content=raw, headers=headers).status_code == 503
    payment, path = pending(world)
    for _ in range(2):
        response = w[0].post("/v1/providers/paypal/webhooks", content=raw, headers=headers)
        assert response.status_code == 200
        assert response.json()["status"] == "provider_confirmed"
    assert len(w[1].risk_service.store.query("SELECT * FROM payment_outcomes")) == 1
    assert len(w[1].risk_service.store.provider_events(payment["request_id"])) == 1
    assert w[0].post(path + "/capture", headers=bearer(w[2])).status_code == 409


def test_stale_signed_webhook_is_recovered_only_by_verified_read(world):
    from datetime import timedelta
    from test_provider_paypal_support import (
        make_trusted_signing_cert, trust_test_root, capture_event, signed_headers,
    )
    w, fake = world
    payment, path = pending(world)
    key, _ = make_trusted_signing_cert()
    trust_test_root(w[1].provider)
    raw = capture_event()
    sent = (w[1].risk_service.clock() - timedelta(minutes=10)).isoformat()
    response = w[0].post("/v1/providers/paypal/webhooks", content=raw,
                         headers=signed_headers(key, raw, transmission_time=sent))
    assert response.status_code == 400
    assert w[1].risk_service.store.ledger_entry(payment["request_id"])["state"] == "pending"
    fake.routes[("GET", "/v2/checkout/orders/5O190127TN364715T")] = json_response(
        200, order_json(status="COMPLETED", capture_status="COMPLETED", amount="1.00"))
    assert w[0].post(path + "/reconciliation", headers=bearer(w[2])).json()["status"] == (
        "provider_confirmed")


def _with_purchase_identity(provider, fake):
    """The inherited minimal fixtures predate payee/request binding."""
    import json
    submitted = {}
    for prior in fake.requests:
        if prior.method == 'POST' and prior.url.path == '/v2/checkout/orders':
            submitted.update(json.loads(prior.content)['purchase_units'][0])
    original_http = provider.http_client
    def handler(request):
        if request.method == "POST" and request.url.path == "/v2/checkout/orders":
            submitted.update(json.loads(request.content)["purchase_units"][0])
        response = fake.handler(request)
        if response.is_success and request.url.path.startswith("/v2/checkout/orders"):
            value = response.json()
            for unit in value.get("purchase_units", []):
                unit["reference_id"] = submitted["reference_id"]
                unit["custom_id"] = submitted["custom_id"]
                unit["payee"] = {"email_address": "app-account@business.example.com",
                                 "merchant_id": "APPACCOUNT123"}
            return httpx.Response(response.status_code, json=value)
        return response
    provider._http = httpx.Client(transport=httpx.MockTransport(handler))
    original_http.close()
