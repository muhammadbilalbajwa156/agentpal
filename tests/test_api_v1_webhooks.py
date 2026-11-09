"""POST /v1/providers/paypal/webhooks: raw body + headers go to the provider's verifier;
only a VERIFIED event from the configured sandbox provider reaches the event sink."""

from __future__ import annotations

import json

import pytest

from test_platform_fixtures import build_world, sign

PATH = "/v1/providers/paypal/webhooks"


@pytest.fixture
def w():
    return build_world()


def _post(w, payload: dict, signature: str | None = None, raw: bytes | None = None):
    body = raw if raw is not None else json.dumps(payload).encode()
    sig = signature if signature is not None else sign(body)
    return w.client.post(PATH, content=body, headers={"x-fake-signature": sig,
                                                      "content-type": "application/json"})


def test_verified_event_reaches_the_sink(w):
    r = _post(w, {"id": "WH-1", "order": "ORDER-0001"})
    assert r.status_code == 200 and r.json() == {"received": True,
                                                 "status": "provider_confirmed"}
    [event] = w.sink.events
    assert event.verified is True and event.provider_order_id == "ORDER-0001"


def test_raw_body_is_passed_byte_for_byte(w):
    raw = b'{"id":"WH-2",   "order":"ORDER-0002"}'
    assert _post(w, {}, raw=raw).status_code == 200
    import hashlib
    assert w.sink.events[0].raw_sha256 == hashlib.sha256(raw).hexdigest()


def test_bad_signature_is_never_forwarded(w):
    r = _post(w, {"id": "WH-3", "order": "ORDER-0003"}, signature="0" * 64)
    assert r.status_code == 400 and r.json() == {"error": "invalid_webhook"}
    assert w.sink.events == []
    assert "rejected:v1_webhook_unverified" in [
        a["status"] for a in w.risk.store.audit_records()]


def test_unverified_event_is_never_forwarded(w):
    r = _post(w, {"id": "WH-4", "order": "ORDER-0004", "verified": False})
    assert r.status_code == 400 and w.sink.events == []


def test_event_from_another_provider_is_refused(w):
    r = _post(w, {"id": "WH-5", "order": "ORDER-0005", "provider": "stripe"})
    assert r.status_code == 400 and w.sink.events == []


def test_response_never_reflects_the_body(w):
    marker = "<script>alert(1)</script>"
    r = _post(w, {"id": marker, "order": marker}, signature="bad")
    assert marker not in r.text
    r = _post(w, {"id": "WH-6", "order": marker})
    assert marker not in r.text


def test_no_bearer_needed_and_a_bearer_grants_nothing(w):
    _, key = w.register()
    body = json.dumps({"id": "WH-7", "order": "ORDER-7"}).encode()
    r = w.client.post(PATH, content=body, headers={"Authorization": f"Bearer {key}",
                                                   "x-fake-signature": "nope"})
    assert r.status_code == 400 and w.sink.events == []


def test_verified_but_unconsumed_event_is_acknowledged_and_not_applied(w, monkeypatch):
    from agentpal.providers.paypal_webhooks import VerifiedEventIgnored

    def ignored(headers, raw):
        raise VerifiedEventIgnored("PAYMENT.PAYOUTS-ITEM.SUCCEEDED")
    monkeypatch.setattr(w.provider, "verify_webhook", ignored)
    r = _post(w, {"id": "WH-9", "order": "ORDER-9"})
    assert r.status_code == 200 and r.json() == {"received": True, "status": "ignored"}
    assert w.sink.events == []
