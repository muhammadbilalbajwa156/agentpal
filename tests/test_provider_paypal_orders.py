"""PayPal sandbox Orders v2: create / capture / status, idempotency, error classes
(offline, httpx.MockTransport)."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal

import httpx
import pytest
from test_provider_paypal_support import (
    ACCESS_TOKEN,
    NOW,
    FakePayPal,
    json_response,
    make_provider,
    make_terms,
    order_json,
)

from agentpal.infra_contracts import ProviderError, ProviderStatus
from agentpal.providers import map_order_status
from agentpal.providers.paypal_sandbox import paypal_request_id

OID = "5O190127TN364715T"
CREATE = ("POST", "/v2/checkout/orders")
CAPTURE = ("POST", f"/v2/checkout/orders/{OID}/capture")
STATUS = ("GET", f"/v2/checkout/orders/{OID}")


def _provider_with(route, handler):
    fake = FakePayPal()
    fake.routes[route] = handler
    provider, _ = make_provider(fake)
    return provider, fake


# ------------------------------------------------------------------ create


def test_create_order_request_body_and_headers():
    provider, fake = _provider_with(CREATE, json_response(201, order_json(OID)))
    ref = provider.create_order(make_terms(), idempotency_key="create-req_0001-a1")
    req = fake.api_requests()[0]
    assert str(req.url) == "https://api-m.sandbox.paypal.com/v2/checkout/orders"
    assert req.headers["PayPal-Request-Id"] == paypal_request_id("create", "create-req_0001-a1")
    assert req.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}-1"
    assert req.headers["Content-Type"] == "application/json"
    assert req.headers["Prefer"] == "return=representation"
    body = json.loads(req.content)
    unit = body["purchase_units"][0]
    assert body["intent"] == "CAPTURE"
    assert unit["amount"] == {"currency_code": "USD", "value": "12.50"}
    assert unit["reference_id"] == "req_0001" and unit["custom_id"] == "req_0001"
    assert unit["description"] == "Acme Books: Buy a reference book"
    assert "invoice_id" not in unit and "payment_source" not in body
    assert ref.provider == "paypal" and ref.environment == "sandbox"
    assert ref.provider_order_id == OID and ref.status is ProviderStatus.created
    assert ref.idempotency_key == "create-req_0001-a1"
    assert ref.payer_action_url == f"https://www.sandbox.paypal.com/checkoutnow?token={OID}"


@pytest.mark.parametrize("amount,currency,expected", [
    (Decimal("1000"), "JPY", "1000"), (Decimal("0.10"), "USD", "0.10"),
    (Decimal("7"), "EUR", "7.00"), (Decimal("1E+3"), "JPY", "1000")])
def test_amount_is_plain_decimal_string_in_terms_currency(amount, currency, expected):
    provider, fake = _provider_with(CREATE, json_response(201, order_json(OID)))
    provider.create_order(make_terms(amount=amount, currency=currency),
                          idempotency_key="create-key-0001")
    unit = json.loads(fake.api_requests()[0].content)["purchase_units"][0]
    assert unit["amount"] == {"currency_code": currency, "value": expected}


def test_description_truncated_and_cleaned():
    provider, fake = _provider_with(CREATE, json_response(201, order_json(OID)))
    provider.create_order(make_terms(purpose="x\n" * 200), idempotency_key="create-key-0001")
    desc = json.loads(fake.api_requests()[0].content)["purchase_units"][0]["description"]
    assert len(desc) == 127 and "\n" not in desc


def test_return_url_goes_to_experience_context_and_payer_action_link():
    links = [{"rel": "payer-action",
              "href": f"https://www.sandbox.paypal.com/checkoutnow?token={OID}"}]
    provider, fake = _provider_with(CREATE, json_response(
        200, order_json(OID, "PAYER_ACTION_REQUIRED", links=links)))
    ref = provider.create_order(make_terms(), idempotency_key="create-key-0001",
                                return_url="https://owner.example/return")
    body = json.loads(fake.api_requests()[0].content)
    assert body["payment_source"]["paypal"]["experience_context"]["return_url"] == \
        "https://owner.example/return"
    assert ref.status is ProviderStatus.payer_action_required
    assert ref.payer_action_url == links[0]["href"]


def test_non_sandbox_payer_link_dropped():
    links = [{"rel": "approve", "href": f"https://www.paypal.com/checkoutnow?token={OID}"}]
    provider, _ = _provider_with(CREATE, json_response(201, order_json(OID, links=links)))
    ref = provider.create_order(make_terms(), idempotency_key="create-key-0001")
    assert ref.payer_action_url is None


@pytest.mark.parametrize("url", ["javascript:alert(1)", "ftp://x.example/", "https://"])
def test_bad_return_url_refused_before_sending(url):
    provider, fake = _provider_with(CREATE, json_response(201, order_json(OID)))
    with pytest.raises(ProviderError) as exc:
        provider.create_order(make_terms(), idempotency_key="create-key-0001", return_url=url)
    assert exc.value.outcome_unknown is False and fake.requests == []


@pytest.mark.parametrize("key", ["", "short", "1234567", "has space x", "x" * 129, None,
                                 "tab\tkey-0001", "ключ-ключ-ключ"])
def test_bad_idempotency_key_refused_before_sending(key):
    provider, fake = _provider_with(CREATE, json_response(201, order_json(OID)))
    with pytest.raises(ProviderError) as exc:
        provider.create_order(make_terms(), idempotency_key=key)
    assert exc.value.outcome_unknown is False
    with pytest.raises(ProviderError):
        provider.capture_order(OID, idempotency_key=key)
    assert fake.requests == []


def test_create_response_without_order_id_is_unknown():
    provider, _ = _provider_with(CREATE, json_response(201, {"status": "CREATED"}))
    with pytest.raises(ProviderError) as exc:
        provider.create_order(make_terms(), idempotency_key="create-key-0001")
    assert exc.value.outcome_unknown is True


# ------------------------------------------------------------------ capture


def test_capture_sends_request_id_and_returns_verified_event():
    body = order_json(OID, "COMPLETED", capture_status="COMPLETED")
    provider, fake = _provider_with(CAPTURE, json_response(201, body))
    event = provider.capture_order(OID, idempotency_key="capture-req_0001-a1")
    req = fake.api_requests()[0]
    assert req.headers["PayPal-Request-Id"] == paypal_request_id("capture",
                                                                 "capture-req_0001-a1")
    assert req.content == b"{}"
    assert event.verified is True and event.status is ProviderStatus.completed
    assert event.provider_order_id == OID and event.provider_capture_id == "3C679366HH908993F"
    assert event.amount == Decimal("12.50") and event.currency == "USD"
    assert event.event_type == "ORDER_CAPTURE_RESPONSE"
    assert event.raw_sha256 == hashlib.sha256(json.dumps(body).encode()).hexdigest()
    assert event.received_at == NOW


def test_every_mutating_call_carries_the_request_id():
    fake = FakePayPal()
    fake.routes[CREATE] = json_response(201, order_json(OID))
    fake.routes[CAPTURE] = json_response(201, order_json(OID, "COMPLETED",
                                                         capture_status="COMPLETED"))
    fake.routes[STATUS] = json_response(200, order_json(OID))
    provider, _ = make_provider(fake)
    provider.create_order(make_terms(), idempotency_key="create-key-0001")
    provider.capture_order(OID, idempotency_key="capture-key-0001")
    provider.get_order_status(OID)
    for req in fake.api_requests():
        if req.method == "POST":
            assert req.headers.get("PayPal-Request-Id") in (
                paypal_request_id("create", "create-key-0001"),
                paypal_request_id("capture", "capture-key-0001"))
        else:
            assert "PayPal-Request-Id" not in req.headers


def test_capture_with_create_key_refused_sec20():
    fake = FakePayPal()
    fake.routes[CREATE] = json_response(201, order_json(OID))
    fake.routes[CAPTURE] = json_response(201, order_json(OID, "COMPLETED",
                                                         capture_status="COMPLETED"))
    provider, _ = make_provider(fake)
    provider.create_order(make_terms(), idempotency_key="req_0001-shared")
    with pytest.raises(ProviderError) as exc:
        provider.capture_order(OID, idempotency_key="req_0001-shared")
    assert exc.value.outcome_unknown is False
    assert [r.url.path for r in fake.api_requests()] == ["/v2/checkout/orders"]


def test_create_key_remembered_even_when_create_outcome_unknown():
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)
    provider, _ = _provider_with(CREATE, timeout)
    with pytest.raises(ProviderError):
        provider.create_order(make_terms(), idempotency_key="req_0001-shared")
    with pytest.raises(ProviderError, match="SEC-20"):
        provider.capture_order(OID, idempotency_key="req_0001-shared")


@pytest.mark.parametrize("order_id", ["", "../../v1/oauth2/token", "A B", "x" * 129, "id?x=1",
                                      None])
def test_malformed_order_id_refused_before_sending(order_id):
    provider, fake = make_provider()
    with pytest.raises(ProviderError) as exc:
        provider.capture_order(order_id, idempotency_key="capture-key-0001")
    assert exc.value.outcome_unknown is False
    with pytest.raises(ProviderError):
        provider.get_order_status(order_id)
    assert fake.requests == []


def test_response_for_other_order_is_unknown():
    provider, _ = _provider_with(CAPTURE, json_response(
        201, order_json("OTHERORDER", "COMPLETED", capture_status="COMPLETED")))
    with pytest.raises(ProviderError) as exc:
        provider.capture_order(OID, idempotency_key="capture-key-0001")
    assert exc.value.outcome_unknown is True


# ------------------------------------------------------------------ error classification


def _raiser(exc_type):
    def handler(request):
        raise exc_type("boom", request=request)
    return handler


@pytest.mark.parametrize("exc_type", [httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError,
                                      httpx.RemoteProtocolError, httpx.WriteError])
@pytest.mark.parametrize("route,call", [
    (CREATE, lambda p: p.create_order(make_terms(), idempotency_key="create-key-0001")),
    (CAPTURE, lambda p: p.capture_order(OID, idempotency_key="capture-key-0001"))])
def test_after_send_failures_are_outcome_unknown(exc_type, route, call):
    provider, fake = _provider_with(route, _raiser(exc_type))
    with pytest.raises(ProviderError) as exc:
        call(provider)
    assert exc.value.outcome_unknown is True
    assert len(fake.api_requests()) == 1               # never retried


@pytest.mark.parametrize("exc_type", [httpx.ConnectError, httpx.ConnectTimeout])
def test_connect_failures_are_not_unknown(exc_type):
    provider, fake = _provider_with(CAPTURE, _raiser(exc_type))
    with pytest.raises(ProviderError) as exc:
        provider.capture_order(OID, idempotency_key="capture-key-0001")
    assert exc.value.outcome_unknown is False
    assert len(fake.api_requests()) == 1


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_5xx_is_outcome_unknown_and_not_retried(status):
    provider, fake = _provider_with(CAPTURE, json_response(
        status, {"name": "INTERNAL_SERVER_ERROR", "debug_id": "d1"}))
    with pytest.raises(ProviderError) as exc:
        provider.capture_order(OID, idempotency_key="capture-key-0001")
    assert exc.value.outcome_unknown is True
    assert len(fake.api_requests()) == 1
    # Lead-approved 2026-11-03: the message is now an allowlisted code (no PayPal text);
    # the debug_id is preserved as a sanitized attribute instead of inside the message.
    assert exc.value.debug_id == "d1"
    assert str(exc.value) == "provider_error" and exc.value.code == "PROVIDER_ERROR"


@pytest.mark.parametrize("body", [b"not json", b"[1,2]", b""])
def test_unparseable_success_is_outcome_unknown(body):
    provider, _ = _provider_with(CAPTURE, lambda r: httpx.Response(201, content=body))
    with pytest.raises(ProviderError) as exc:
        provider.capture_order(OID, idempotency_key="capture-key-0001")
    assert exc.value.outcome_unknown is True


@pytest.mark.parametrize("status,name", [(400, "INVALID_REQUEST"), (403, "NOT_AUTHORIZED"),
                                         (404, "RESOURCE_NOT_FOUND"),
                                         (422, "UNPROCESSABLE_ENTITY")])
@pytest.mark.parametrize("route,call", [
    (CREATE, lambda p: p.create_order(make_terms(), idempotency_key="create-key-0001")),
    (CAPTURE, lambda p: p.capture_order(OID, idempotency_key="capture-key-0001"))])
def test_4xx_is_not_outcome_unknown(status, name, route, call):
    provider, fake = _provider_with(route, json_response(status, {
        "name": name, "debug_id": "dbg1",
        "details": [{"issue": "ORDER_NOT_APPROVED", "description": "x"}]}))
    with pytest.raises(ProviderError) as exc:
        call(provider)
    assert exc.value.outcome_unknown is False
    assert len(fake.api_requests()) == 1


def test_401_invalidates_token_no_retry_not_unknown():
    fake = FakePayPal()
    fake.routes[CREATE] = json_response(201, order_json(OID))
    fake.routes[CAPTURE] = json_response(401, {"error": "invalid_token"})
    provider, _ = make_provider(fake)
    provider.create_order(make_terms(), idempotency_key="create-key-0001")
    with pytest.raises(ProviderError) as exc:
        provider.capture_order(OID, idempotency_key="capture-key-0001")
    # PayPal rejected the call before processing it, even after a prior create.
    assert exc.value.outcome_unknown is False
    assert len(fake.api_requests()) == 2               # create + one capture, no retry
    fake.routes[CAPTURE] = json_response(201, order_json(OID, "COMPLETED",
                                                         capture_status="COMPLETED"))
    provider.capture_order(OID, idempotency_key="capture-key-0001")
    assert fake.token_calls == 2                       # a fresh token was fetched


def test_already_captured_is_outcome_unknown():
    provider, _ = _provider_with(CAPTURE, json_response(422, {
        "name": "UNPROCESSABLE_ENTITY",
        "details": [{"issue": "ORDER_ALREADY_CAPTURED"}], "debug_id": "x"}))
    with pytest.raises(ProviderError) as exc:
        provider.capture_order(OID, idempotency_key="capture-key-0001")
    assert exc.value.outcome_unknown is True


def test_409_concurrent_duplicate_is_outcome_unknown():
    provider, _ = _provider_with(CAPTURE, json_response(409, {"name": "RESOURCE_CONFLICT"}))
    with pytest.raises(ProviderError) as exc:
        provider.capture_order(OID, idempotency_key="capture-key-0001")
    assert exc.value.outcome_unknown is True


def test_error_message_sanitizes_provider_text():
    provider, _ = _provider_with(CAPTURE, json_response(400, {
        "name": "BAD<script>\nINJECT", "debug_id": "a b\r\nc"}))
    with pytest.raises(ProviderError) as exc:
        provider.capture_order(OID, idempotency_key="capture-key-0001")
    assert "<" not in str(exc.value) and "\n" not in str(exc.value)


# ------------------------------------------------------------------ status mapping


@pytest.mark.parametrize("order_status,capture_status,expected", [
    ("CREATED", None, ProviderStatus.created),
    ("SAVED", None, ProviderStatus.pending),
    ("PAYER_ACTION_REQUIRED", None, ProviderStatus.payer_action_required),
    ("APPROVED", None, ProviderStatus.approved_by_payer),
    ("VOIDED", None, ProviderStatus.declined),
    ("COMPLETED", None, ProviderStatus.unknown),
    ("COMPLETED", "COMPLETED", ProviderStatus.completed),
    ("COMPLETED", "PENDING", ProviderStatus.pending),
    ("COMPLETED", "DECLINED", ProviderStatus.declined),
    ("COMPLETED", "FAILED", ProviderStatus.declined),
    ("COMPLETED", "REFUNDED", ProviderStatus.refunded),
    ("COMPLETED", "PARTIALLY_REFUNDED", ProviderStatus.refunded),
    ("COMPLETED", "SOMETHING_NEW", ProviderStatus.unknown),
    ("WEIRD", None, ProviderStatus.unknown),
    (None, None, ProviderStatus.unknown),
])
def test_status_mapping(order_status, capture_status, expected):
    assert map_order_status(order_status, capture_status) is expected
    body = order_json(OID, order_status or "", capture_status=capture_status)
    if order_status is None:
        body.pop("status")
    provider, _ = _provider_with(STATUS, json_response(200, body))
    event = provider.get_order_status(OID)
    assert event.status is expected and event.verified is True


def test_completed_order_with_declined_capture_is_declined_not_completed():
    provider, _ = _provider_with(STATUS, json_response(
        200, order_json(OID, "COMPLETED", capture_status="DECLINED")))
    assert provider.get_order_status(OID).status is ProviderStatus.declined


def test_multiple_captures_are_unknown():
    body = order_json(OID, "COMPLETED", capture_status="COMPLETED")
    caps = body["purchase_units"][0]["payments"]["captures"]
    caps.append(dict(caps[0], id="SECONDCAPTURE"))
    provider, _ = _provider_with(STATUS, json_response(200, body))
    assert provider.get_order_status(OID).status is ProviderStatus.unknown


def test_refund_status_reports_refunded_amount_not_captured_amount():
    refunds = [{"id": "R1", "status": "COMPLETED",
                "amount": {"currency_code": "USD", "value": "2.00"}},
               {"id": "R2", "status": "COMPLETED",
                "amount": {"currency_code": "USD", "value": "1.50"}},
               {"id": "R3", "status": "PENDING",
                "amount": {"currency_code": "USD", "value": "9.00"}}]
    provider, _ = _provider_with(STATUS, json_response(200, order_json(
        OID, "COMPLETED", capture_status="PARTIALLY_REFUNDED", refunds=refunds)))
    event = provider.get_order_status(OID)
    assert event.status is ProviderStatus.refunded
    assert event.amount == Decimal("3.50") and event.currency == "USD"


def test_refund_status_without_refund_details_has_no_amount():
    provider, _ = _provider_with(STATUS, json_response(200, order_json(
        OID, "COMPLETED", capture_status="REFUNDED")))
    event = provider.get_order_status(OID)
    assert event.status is ProviderStatus.refunded and event.amount is None


# ------------------------------------------------------------------ reconciliation ids


def test_status_event_id_deterministic_and_changes_with_state():
    fake = FakePayPal()
    fake.routes[STATUS] = json_response(200, order_json(OID, "APPROVED"))
    provider, _ = make_provider(fake)
    a, b = provider.get_order_status(OID), provider.get_order_status(OID)
    assert a.event_id == b.event_id and a.event_id.startswith("q_")
    assert a.event_type == "ORDER_STATUS_READ" and a.verified is True
    fake.routes[STATUS] = json_response(200, order_json(OID, "COMPLETED",
                                                        capture_status="COMPLETED"))
    c = provider.get_order_status(OID)
    assert c.event_id != a.event_id and c.status is ProviderStatus.completed
    # another provider instance computes the same id for the same observed state
    other, _ = make_provider(fake)
    assert other.get_order_status(OID).event_id == c.event_id


def test_status_read_404_not_unknown():
    provider, _ = make_provider()
    with pytest.raises(ProviderError) as exc:
        provider.get_order_status("NOSUCHORDER")
    assert exc.value.outcome_unknown is False


@pytest.mark.parametrize("configured,expected", [
    ("https://agentpal.example/app/paypal-return.html", "https://agentpal.example/app/paypal-return.html"),
    ("http://insecure.example/return", None), ("", None)])
def test_operator_return_url_setting_is_https_only(monkeypatch, configured, expected):
    monkeypatch.setenv("AGENTPAL_PAYPAL_RETURN_URL", configured)
    provider, fake = _provider_with(CREATE, json_response(201, order_json(OID)))
    provider.create_order(make_terms(), idempotency_key="create-key-0001")
    body = json.loads(fake.api_requests()[0].content)
    got = body.get("payment_source", {}).get("paypal", {}).get("experience_context", {}).get("return_url")
    assert got == expected
