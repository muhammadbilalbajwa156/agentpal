"""PayPal sandbox webhook verification (offline): self-verification with a locally
generated RSA cert served by httpx.MockTransport, optional postback mode, parsing."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest

pytest.importorskip("cryptography")

from test_provider_paypal_support import (  # noqa: E402
    CERT_URL,
    NOW,
    WEBHOOK_ID,
    FakePayPal,
    capture_event,
    make_provider,
    make_signing_cert,
    make_trusted_signing_cert,
    trust_test_root,
    refund_event,
    signed_headers,
)

from agentpal.infra_contracts import ProviderStatus  # noqa: E402
from agentpal.providers import paypal_webhooks  # noqa: E402
from agentpal.providers.paypal_webhooks import (  # noqa: E402
    VerifiedEventIgnored,
    WebhookConfigError,
    WebhookVerificationError,
    check_cert_url,
)

CERT_PATH = "/v1/notifications/certs/CERT-360caa42-fca2a594-test"


@pytest.fixture(scope="module")
def signer():
    return make_trusted_signing_cert()


def _setup(signer, **config_kw):
    key, pem = signer
    fake = FakePayPal()
    fake.routes[("GET", CERT_PATH)] = lambda r: httpx.Response(200, content=pem)
    provider, _ = make_provider(fake, **config_kw)
    trust_test_root(provider)
    return provider, fake, key


def _cert_fetches(fake):
    return [r for r in fake.requests if r.url.path.startswith("/v1/notifications/certs/")]


# ------------------------------------------------------------------ pass


def test_valid_capture_event_verified(signer):
    provider, fake, key = _setup(signer)
    raw = capture_event()
    event = provider.verify_webhook(signed_headers(key, raw), raw)
    assert event.verified is True
    assert event.provider == "paypal" and event.environment == "sandbox"
    assert event.event_id == "WH-58D329510W468432D-8HN650336L201105X"
    assert event.event_type == "PAYMENT.CAPTURE.COMPLETED"
    assert event.provider_order_id == "5O190127TN364715T"
    assert event.provider_capture_id == "3C679366HH908993F"
    assert event.status is ProviderStatus.completed
    assert event.amount == Decimal("12.50") and event.currency == "USD"
    assert event.raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert event.received_at == NOW
    assert _cert_fetches(fake)[0].url.host == "api.sandbox.paypal.com"


def test_headers_case_insensitive(signer):
    provider, _, key = _setup(signer)
    raw = capture_event()
    headers = {k.lower(): v for k, v in signed_headers(key, raw).items()}
    assert provider.verify_webhook(headers, raw).verified is True


def test_certificate_cached(signer):
    provider, fake, key = _setup(signer)
    for i in range(3):
        raw = capture_event(event_id=f"WH-{i}")
        provider.verify_webhook(signed_headers(key, raw), raw)
    assert len(_cert_fetches(fake)) == 1


@pytest.mark.parametrize("event_type,status,expected", [
    ("PAYMENT.CAPTURE.PENDING", "PENDING", ProviderStatus.pending),
    ("PAYMENT.CAPTURE.DECLINED", "DECLINED", ProviderStatus.declined),
    ("PAYMENT.CAPTURE.DENIED", "DECLINED", ProviderStatus.declined),
    ("PAYMENT.CAPTURE.COMPLETED", "PENDING", ProviderStatus.unknown),   # disagreement
])
def test_capture_event_statuses(signer, event_type, status, expected):
    provider, _, key = _setup(signer)
    raw = capture_event(event_type, status=status)
    assert provider.verify_webhook(signed_headers(key, raw), raw).status is expected


@pytest.mark.parametrize("event_type,expected", [
    ("PAYMENT.CAPTURE.REFUNDED", ProviderStatus.refunded),
    ("PAYMENT.CAPTURE.REVERSED", ProviderStatus.reversed)])
def test_refund_events_carry_refunded_amount(signer, event_type, expected):
    provider, _, key = _setup(signer)
    raw = refund_event("5.00", event_type)       # partial: captured 12.50, refunded 5.00
    event = provider.verify_webhook(signed_headers(key, raw), raw)
    assert event.status is expected
    assert event.amount == Decimal("5.00") and event.currency == "USD"
    assert event.provider_capture_id == "3C679366HH908993F"
    assert event.provider_order_id == "5O190127TN364715T"


def test_order_approved_event(signer):
    provider, _, key = _setup(signer)
    raw = json.dumps({"id": "WH-ORDER-APPROVED-1", "event_type": "CHECKOUT.ORDER.APPROVED",
                      "create_time": "2026-10-31T11:59:00Z", "resource_type": "checkout-order",
                      "resource": {"id": "5O190127TN364715T", "status": "APPROVED",
                                   "purchase_units": [{"amount": {"currency_code": "USD",
                                                                  "value": "12.50"}}]}}).encode()
    event = provider.verify_webhook(signed_headers(key, raw), raw)
    assert event.status is ProviderStatus.approved_by_payer
    assert event.provider_order_id == "5O190127TN364715T" and event.amount == Decimal("12.50")


# ------------------------------------------------------------------ fail


def test_tampered_body_rejected(signer):
    provider, _, key = _setup(signer)
    raw = capture_event()
    headers = signed_headers(key, raw)
    tampered = raw.replace(b'"12.50"', b'"99.99"')
    assert tampered != raw
    with pytest.raises(WebhookVerificationError):
        provider.verify_webhook(headers, tampered)


def test_whitespace_change_rejected_exact_bytes(signer):
    provider, _, key = _setup(signer)
    raw = capture_event()
    headers = signed_headers(key, raw)
    with pytest.raises(WebhookVerificationError):
        provider.verify_webhook(headers, json.dumps(json.loads(raw)).encode())


@pytest.mark.parametrize("field,value", [
    ("PAYPAL-TRANSMISSION-SIG", "AAAA"),
    ("PAYPAL-TRANSMISSION-SIG", "not base64 !!"),
    ("PAYPAL-TRANSMISSION-ID", "other-transmission-id"),
    ("PAYPAL-AUTH-ALGO", "SHA1withRSA"),
    ("PAYPAL-AUTH-ALGO", ""),
])
def test_tampered_headers_rejected(signer, field, value):
    provider, _, key = _setup(signer)
    raw = capture_event()
    headers = signed_headers(key, raw)
    headers[field] = value
    with pytest.raises(WebhookVerificationError):
        provider.verify_webhook(headers, raw)


@pytest.mark.parametrize("missing", ["PAYPAL-TRANSMISSION-ID", "PAYPAL-TRANSMISSION-TIME",
                                     "PAYPAL-TRANSMISSION-SIG", "PAYPAL-CERT-URL",
                                     "PAYPAL-AUTH-ALGO"])
def test_missing_header_rejected(signer, missing):
    provider, fake, key = _setup(signer)
    raw = capture_event()
    headers = signed_headers(key, raw)
    del headers[missing]
    with pytest.raises(WebhookVerificationError):
        provider.verify_webhook(headers, raw)
    assert _cert_fetches(fake) == []


def test_signed_for_other_webhook_id_rejected(signer):
    provider, _, key = _setup(signer)
    raw = capture_event()
    with pytest.raises(WebhookVerificationError):
        provider.verify_webhook(signed_headers(key, raw, webhook_id="OTHERWEBHOOK"), raw)


def test_simulator_mock_event_rejected(signer):
    """Mock events are signed for the placeholder id WEBHOOK_ID, which can never be ours."""
    provider, _, key = _setup(signer)
    raw = capture_event()
    with pytest.raises(WebhookVerificationError):
        provider.verify_webhook(signed_headers(key, raw, webhook_id="WEBHOOK_ID"), raw)


def test_wrong_signing_key_rejected(signer):
    provider, _, _ = _setup(signer)
    other_key, _ = make_signing_cert()
    raw = capture_event()
    with pytest.raises(WebhookVerificationError):
        provider.verify_webhook(signed_headers(other_key, raw), raw)


@pytest.mark.parametrize("offset", [timedelta(seconds=301), timedelta(hours=3),
                                    timedelta(days=2), -timedelta(seconds=301)])
def test_stale_or_future_but_otherwise_valid_event_rejected(signer, offset):
    provider, _, key = _setup(signer)
    raw = capture_event()
    when = (NOW - offset).strftime("%Y-%m-%dT%H:%M:%SZ")
    headers = signed_headers(key, raw, transmission_time=when)   # correctly signed
    with pytest.raises(WebhookVerificationError, match="freshness"):
        provider.verify_webhook(headers, raw)


def test_inside_window_accepted(signer):
    provider, _, key = _setup(signer)
    raw = capture_event()
    when = (NOW - timedelta(seconds=299)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert provider.verify_webhook(signed_headers(key, raw, transmission_time=when),
                                   raw).verified


@pytest.mark.parametrize("bad", ["yesterday", "2026-10-31T11:59:59"])   # no timezone
def test_bad_transmission_time_rejected(signer, bad):
    provider, _, key = _setup(signer)
    raw = capture_event()
    with pytest.raises(WebhookVerificationError):
        provider.verify_webhook(signed_headers(key, raw, transmission_time=bad), raw)


@pytest.mark.parametrize("url", [
    "https://api.paypal.com/v1/notifications/certs/CERT-1",          # production host
    "https://api-m.paypal.com/v1/notifications/certs/CERT-1",
    "https://evil.example/v1/notifications/certs/CERT-1",
    "https://api.sandbox.paypal.com.evil.example/v1/notifications/certs/CERT-1",
    "https://evilapi.sandbox.paypal.com/v1/notifications/certs/CERT-1",
    "http://api.sandbox.paypal.com/v1/notifications/certs/CERT-1",
    "https://api.sandbox.paypal.com/v1/other/CERT-1",
    "https://api.sandbox.paypal.com/v1/notifications/certs/../../oauth2/token",
    "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-1?x=https://evil.example",
    "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-1#frag",
    "https://user@api.sandbox.paypal.com/v1/notifications/certs/CERT-1",
    "https://api.sandbox.paypal.com:8443/v1/notifications/certs/CERT-1",
    "https://api.sandbox.paypal.com:notaport/v1/notifications/certs/CERT-1",
    "file:///etc/passwd",
])
def test_wrong_cert_host_rejected_without_fetch(signer, url):
    provider, fake, key = _setup(signer)
    raw = capture_event()
    with pytest.raises(WebhookVerificationError):
        provider.verify_webhook(signed_headers(key, raw, cert_url=url), raw)
    assert fake.requests == []
    with pytest.raises(WebhookVerificationError):
        check_cert_url(url)


@pytest.mark.parametrize("url", [CERT_URL,
                                 "https://api-m.sandbox.paypal.com/v1/notifications/certs/C-1",
                                 "https://api.sandbox.paypal.com:443/v1/notifications/certs/C"])
def test_sandbox_cert_urls_allowed(url):
    assert check_cert_url(url) == url


def test_expired_certificate_rejected():
    key, pem = make_signing_cert(not_before=NOW - timedelta(days=400),
                                 not_after=NOW - timedelta(days=1))
    provider, _, _ = _setup((key, pem))
    raw = capture_event()
    with pytest.raises(WebhookVerificationError, match="validity"):
        provider.verify_webhook(signed_headers(key, raw), raw)


@pytest.mark.parametrize("response", [httpx.Response(404), httpx.Response(200, content=b"junk"),
                                      httpx.Response(200, content=b"A" * (70 * 1024)),
                                      httpx.Response(302, headers={
                                          "Location": "https://evil.example/cert.pem"})])
def test_cert_fetch_failures_rejected(signer, response):
    key, _ = signer
    fake = FakePayPal()
    fake.routes[("GET", CERT_PATH)] = lambda r: response
    provider, _ = make_provider(fake)
    raw = capture_event()
    with pytest.raises(WebhookVerificationError):
        provider.verify_webhook(signed_headers(key, raw), raw)
    assert all(r.url.host == "api.sandbox.paypal.com" for r in fake.requests)


def test_verified_unsupported_event_type_is_ignored_not_applied(signer):
    # Changed by the lead 2026-11-06: live PayPal sends payout events to the same
    # webhook. A VERIFIED event of a type AgentPal does not consume is acknowledged as
    # ignored (so PayPal stops retrying) and never becomes a ProviderEvent.
    provider, _, key = _setup(signer)
    raw = capture_event("PAYMENT.PAYOUTS-ITEM.SUCCEEDED")
    with pytest.raises(VerifiedEventIgnored) as info:
        provider.verify_webhook(signed_headers(key, raw), raw)
    assert info.value.event_type == "PAYMENT.PAYOUTS-ITEM.SUCCEEDED"
    assert not isinstance(info.value, WebhookVerificationError)


def test_unsigned_unsupported_event_type_is_still_rejected(signer):
    provider, _, key = _setup(signer)
    raw = capture_event("PAYMENT.PAYOUTS-ITEM.SUCCEEDED")
    headers = signed_headers(key, capture_event())   # signature over a different body
    with pytest.raises(WebhookVerificationError, match="signature"):
        provider.verify_webhook(headers, raw)


def test_unsupported_event_type_alone_is_a_rejection():
    with pytest.raises(WebhookVerificationError, match="unsupported"):
        paypal_webhooks.parse_webhook_event(capture_event("BILLING.SUBSCRIPTION.CREATED"),
                                            received_at=NOW)


def test_ignored_event_type_is_sanitized(signer):
    provider, _, key = _setup(signer)
    raw = capture_event("X<script>|" + "A" * 100)
    with pytest.raises(VerifiedEventIgnored) as info:
        provider.verify_webhook(signed_headers(key, raw), raw)
    assert info.value.event_type == "unrecognized"


def test_event_without_order_reference_rejected(signer):
    provider, _, key = _setup(signer)
    body = json.loads(capture_event())
    del body["resource"]["supplementary_data"]
    raw = json.dumps(body).encode()
    with pytest.raises(WebhookVerificationError, match="order"):
        provider.verify_webhook(signed_headers(key, raw), raw)


def test_no_webhook_id_configured_refuses(signer):
    provider, _, key = _setup(signer, webhook_id=None)
    raw = capture_event()
    with pytest.raises(WebhookConfigError):
        provider.verify_webhook(signed_headers(key, raw), raw)


def test_missing_cryptography_never_passes(signer, monkeypatch):
    monkeypatch.setattr(paypal_webhooks, "HAVE_CRYPTOGRAPHY", False)
    provider, _, key = _setup(signer)
    raw = capture_event()
    with pytest.raises(WebhookConfigError, match="cryptography"):
        provider.verify_webhook(signed_headers(key, raw), raw)


@pytest.mark.parametrize("raw", [b"", b"x" * (600 * 1024)])
def test_empty_or_oversized_body_rejected(signer, raw):
    provider, _, key = _setup(signer)
    with pytest.raises(WebhookVerificationError):
        provider.verify_webhook(signed_headers(key, raw or b"{}"), raw)


def test_error_messages_do_not_echo_input(signer):
    provider, _, key = _setup(signer)
    raw = capture_event()
    headers = signed_headers(key, raw, cert_url="https://evil.example/SECRETPATH")
    with pytest.raises(WebhookVerificationError) as exc:
        provider.verify_webhook(headers, raw)
    assert "evil" not in str(exc.value) and "SECRETPATH" not in str(exc.value)


# ------------------------------------------------------------------ postback mode


def _postback_setup(signer, verify_handler):
    key, _ = signer
    fake = FakePayPal()
    fake.routes[("POST", "/v1/notifications/verify-webhook-signature")] = verify_handler
    provider, _ = make_provider(fake, webhook_verification="postback")
    return provider, fake, key


def test_postback_success_sends_raw_event_unchanged(signer):
    seen = {}

    def handler(request):
        seen["body"] = request.content
        seen["auth"] = request.headers["Authorization"]
        return httpx.Response(200, json={"verification_status": "SUCCESS"})

    provider, fake, key = _postback_setup(signer, handler)
    raw = capture_event()                          # indented JSON: must be posted verbatim
    headers = signed_headers(key, raw)
    event = provider.verify_webhook(headers, raw)
    assert event.verified is True and event.status is ProviderStatus.completed
    sent = seen["body"]
    assert sent.endswith(b',"webhook_event":' + raw + b"}")
    fields = json.loads(sent)
    assert fields["webhook_id"] == WEBHOOK_ID
    assert fields["transmission_sig"] == headers["PAYPAL-TRANSMISSION-SIG"]
    assert fields["cert_url"] == CERT_URL and fields["auth_algo"] == "SHA256withRSA"
    assert seen["auth"].startswith("Bearer ")
    post = [r for r in fake.requests if r.url.path.endswith("verify-webhook-signature")][0]
    assert str(post.url).startswith("https://api-m.sandbox.paypal.com/")
    assert _cert_fetches(fake) == []               # PayPal verifies; no cert download


@pytest.mark.parametrize("handler", [
    lambda r: httpx.Response(200, json={"verification_status": "FAILURE"}),
    lambda r: httpx.Response(200, json={}),
    lambda r: httpx.Response(200, content=b"nope"),
    lambda r: httpx.Response(500, json={"name": "INTERNAL_SERVER_ERROR"}),
    lambda r: httpx.Response(400, json={"name": "VALIDATION_ERROR"}),
])
def test_postback_failure_rejected(signer, handler):
    provider, _, key = _postback_setup(signer, handler)
    raw = capture_event()
    with pytest.raises(WebhookVerificationError):
        provider.verify_webhook(signed_headers(key, raw), raw)


def test_postback_timeout_rejected(signer):
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)
    provider, _, key = _postback_setup(signer, handler)
    raw = capture_event()
    with pytest.raises(WebhookVerificationError):
        provider.verify_webhook(signed_headers(key, raw), raw)


def test_postback_still_pins_cert_url_and_freshness(signer):
    calls = []
    provider, fake, key = _postback_setup(
        signer, lambda r: calls.append(r) or httpx.Response(
            200, json={"verification_status": "SUCCESS"}))
    raw = capture_event()
    with pytest.raises(WebhookVerificationError):
        provider.verify_webhook(signed_headers(
            key, raw, cert_url="https://evil.example/v1/notifications/certs/C"), raw)
    with pytest.raises(WebhookVerificationError):
        provider.verify_webhook(signed_headers(
            key, raw, transmission_time="2026-10-30T11:00:00Z"), raw)
    assert calls == [] and fake.requests == []


# ---- live-shaped refund events (lead, 2026-11-06) ---------------------------------
# Real sandbox PAYMENT.CAPTURE.REFUNDED events carry no supplementary_data; the refunded
# capture is only in links[rel=up]. The order is resolved by an authenticated capture read.

def _live_refund(up_href: str = "https://api.sandbox.paypal.com/v2/payments/captures/3C679366HH908993F"):
    body = {"id": "WH-LIVE-REFUND-1", "event_version": "1.0", "resource_type": "refund",
            "event_type": "PAYMENT.CAPTURE.REFUNDED", "create_time": "2026-10-31T11:59:58Z",
            "resource": {"id": "1Y107995YT783435V", "status": "COMPLETED",
                         "amount": {"currency_code": "USD", "value": "0.40"},
                         "links": [{"rel": "self", "href": "https://api.sandbox.paypal.com/v2/payments/refunds/1Y107995YT783435V"},
                                   {"rel": "up", "href": up_href}]}}
    return json.dumps(body).encode()


def _capture_route(fake, capture_id="3C679366HH908993F", order_id="5O190127TN364715T"):
    body = {"id": capture_id, "status": "PARTIALLY_REFUNDED",
            "supplementary_data": {"related_ids": {"order_id": order_id}}}
    fake.routes[("GET", "/v2/payments/captures/3C679366HH908993F")] = (
        lambda r: httpx.Response(200, json=body))


def _capture_reads(fake):
    return [r for r in fake.requests if r.url.path.startswith("/v2/payments/captures/")]


def test_live_refund_event_resolves_order_by_authenticated_capture_read(signer):
    provider, fake, key = _setup(signer)
    _capture_route(fake)
    raw = _live_refund()
    event = provider.verify_webhook(signed_headers(key, raw), raw)
    assert event.provider_order_id == "5O190127TN364715T"
    assert event.provider_capture_id == "3C679366HH908993F"
    assert event.status is ProviderStatus.refunded and event.amount == Decimal("0.40")
    [read] = _capture_reads(fake)
    assert read.url.host == "api-m.sandbox.paypal.com"
    assert read.headers["authorization"].startswith("Bearer ")


def test_live_refund_event_rejects_a_capture_read_for_another_capture(signer):
    provider, fake, key = _setup(signer)
    _capture_route(fake, capture_id="OTHERCAPTURE1")
    raw = _live_refund()
    with pytest.raises(WebhookVerificationError, match="capture lookup"):
        provider.verify_webhook(signed_headers(key, raw), raw)


@pytest.mark.parametrize("href", [
    "https://evil.example/v2/payments/captures/3C679366HH908993F",
    "https://api.paypal.com/v2/payments/captures/3C679366HH908993F",          # live host
    "https://api.sandbox.paypal.com/v2/payments/captures/3C67/../x",
    "https://api.sandbox.paypal.com/v2/checkout/orders/5O190127TN364715T",
])
def test_live_refund_event_with_untrusted_up_link_names_no_order(signer, href):
    provider, fake, key = _setup(signer)
    _capture_route(fake)
    raw = _live_refund(href)
    with pytest.raises(WebhookVerificationError, match="order"):
        provider.verify_webhook(signed_headers(key, raw), raw)
    assert _capture_reads(fake) == []


def test_unsigned_live_refund_event_triggers_no_capture_read(signer):
    provider, fake, key = _setup(signer)
    _capture_route(fake)
    raw = _live_refund()
    with pytest.raises(WebhookVerificationError, match="signature"):
        provider.verify_webhook(signed_headers(key, capture_event()), raw)
    assert _capture_reads(fake) == []
