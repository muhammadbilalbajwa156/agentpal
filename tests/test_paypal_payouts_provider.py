"""Provider boundaries: sandbox origin, no redirects, bounded parsing and exact reads."""

from copy import deepcopy
import json

import httpx
import pytest

from agentpal.infra_contracts import ProviderError
from agentpal.providers.config import PayPalSandboxConfig, ProviderConfigError
from agentpal.providers.paypal_payouts import (
    MAX_RESPONSE_BYTES, PayPalPayoutsProvider, payout_request_id, sender_batch_id,
)
from test_paypal_payouts import BODY, PayoutHTTP

TERMS = {**BODY, "request_id": "payout_" + "a" * 32, "agent_id": "agt_" + "b" * 32,
         "owner_id": "owner_fake", "wallet_id": "wal_" + "c" * 32}


def provider(handler, **client_options):
    return PayPalPayoutsProvider(PayPalSandboxConfig(client_id="fake-client",
        client_secret="fake-secret"), http_client=httpx.Client(
            transport=httpx.MockTransport(handler), **client_options))


def test_sandbox_origin_check_survives_pydantic_construction_bypass():
    with pytest.raises(ProviderConfigError):
        PayPalPayoutsProvider(PayPalSandboxConfig.model_construct(base_url="https://api-m.paypal.com",
            client_id="fake-client", client_secret="fake-secret"), http_client=httpx.Client())


def test_operation_ids_are_distinct_bounded_and_stable():
    from agentpal.providers.paypal_sandbox import paypal_request_id
    rid = TERMS["request_id"]
    assert sender_batch_id(rid) == sender_batch_id(rid)
    assert payout_request_id(rid) == payout_request_id(rid)
    assert len(sender_batch_id(rid)) <= 30
    assert len(payout_request_id(rid)) <= 38
    assert payout_request_id(rid) not in {paypal_request_id("create", rid), paypal_request_id("capture", rid)}
    assert sender_batch_id(rid) != sender_batch_id(rid + "b")
    assert payout_request_id(rid) != payout_request_id(rid + "b")


@pytest.mark.parametrize("phase", ["oauth", "create", "read"])
def test_oversized_response_is_refused_for_every_operation(phase):
    fake = PayoutHTTP()
    fake.body = {"sender_batch_header": {"sender_batch_id": sender_batch_id(TERMS["request_id"])},
                 "items": []}
    def handler(request):
        current = ("oauth" if request.url.path.endswith("/token") else
                   "create" if request.method == "POST" else "read")
        if phase == current:
            return httpx.Response(200, content=b" " * (MAX_RESPONSE_BYTES + 1))
        return fake.handler(request)
    adapter = provider(handler)
    with pytest.raises(ProviderError, match="payout_response_too_large"):
        if phase == "read":
            adapter.read("BATCH123456789", TERMS)
        else:
            adapter.create(TERMS)


@pytest.mark.parametrize("phase", ["oauth", "create", "read"])
def test_redirects_are_never_followed_even_with_redirecting_injected_client(phase):
    fake = PayoutHTTP()
    requests = []
    def handler(request):
        requests.append(request)
        current = ("oauth" if request.url.path.endswith("/token") else
                   "create" if request.method == "POST" else "read")
        if phase == current:
            return httpx.Response(302, headers={"Location": "https://api-m.paypal.com/unsafe"})
        return fake.handler(request)
    adapter = provider(handler, follow_redirects=True)
    with pytest.raises(ProviderError):
        if phase == "read":
            adapter.read("BATCH123456789", TERMS)
        else:
            adapter.create(TERMS)
    assert all(r.url.host == "api-m.sandbox.paypal.com" for r in requests)


@pytest.mark.parametrize("field,value", [("total_items", 2), ("total_pages", 2)])
def test_truncated_or_paginated_batch_never_verifies(field, value):
    fake = PayoutHTTP()
    adapter = provider(fake.handler)
    adapter.create(TERMS)
    fake.change = lambda data: {**data, field: value}
    with pytest.raises(ProviderError, match="payout_response_mismatch"):
        adapter.read("BATCH123456789", TERMS)


@pytest.mark.parametrize("field,value", [("recipient_type", "PHONE"),
    ("receiver", None), ("amount", {"value": 1.0, "currency": "USD"}),
    ("amount", {"value": "NaN", "currency": "USD"}),
    ("amount", {"value": "1E0", "currency": "USD"}),
    ("amount", {"value": "10000000000000000000000", "currency": "USD"})])
def test_malformed_or_inexact_item_never_verifies(field, value):
    fake = PayoutHTTP()
    adapter = provider(fake.handler)
    adapter.create(TERMS)
    def change(data):
        data = deepcopy(data)
        data["items"][0]["payout_item"][field] = value
        return data
    fake.change = change
    with pytest.raises(ProviderError, match="payout_response_mismatch"):
        adapter.read("BATCH123456789", TERMS)


@pytest.mark.parametrize("bad_id", ["../other", "x?leak=1", "https://api-m.paypal.com", "x" * 31])
def test_batch_identifiers_are_bounded_path_components(bad_id):
    fake = PayoutHTTP()
    with pytest.raises(ProviderError):
        provider(fake.handler).read(bad_id, TERMS)
    assert fake.requests == []


def test_unknown_status_is_not_a_success_and_timeouts_are_bounded():
    fake = PayoutHTTP()
    adapter = provider(fake.handler)
    adapter.create(TERMS)
    fake.status = "COMPLETED"  # an Orders status is not a successful payout item
    with pytest.raises(ProviderError):
        adapter.read("BATCH123456789", TERMS)
    assert all(0 < r.extensions["timeout"]["read"] <= 120 for r in fake.requests)


def test_errors_and_repr_do_not_contain_credentials_or_raw_provider_bodies():
    def handler(request):
        return httpx.Response(401, json={"name": "FAKE_ERROR", "message": "private-raw-error"})
    adapter = provider(handler)
    with pytest.raises(ProviderError) as error:
        adapter.create(TERMS)
    for text in (repr(adapter), str(error.value)):
        assert "fake-client" not in text and "fake-secret" not in text
        assert "private-raw-error" not in text


def test_create_success_without_batch_id_is_unknown():
    fake = PayoutHTTP()
    def handler(request):
        if request.url.path.endswith("/payouts"):
            return httpx.Response(201, json={"batch_header": {"batch_status": "SUCCESS"}})
        return fake.handler(request)
    with pytest.raises(ProviderError) as error:
        provider(handler).create(TERMS)
    assert error.value.outcome_unknown is True


def test_json_is_sent_as_one_email_item_with_a_persistently_derived_sender_id():
    fake = PayoutHTTP()
    provider(fake.handler).create(TERMS)
    body = json.loads(fake.posts[0].content)
    assert body["sender_batch_header"]["sender_batch_id"] == sender_batch_id(TERMS["request_id"])
    assert body["items"][0]["sender_item_id"] == sender_batch_id(TERMS["request_id"])
    assert body["items"][0]["amount"] == {"value": "1.00", "currency": "USD"}
    assert fake.posts[0].headers["PayPal-Request-Id"] == payout_request_id(TERMS["request_id"])


def test_slow_stream_cannot_extend_the_response_deadline(monkeypatch):
    from types import SimpleNamespace
    import agentpal.providers.paypal_payouts as module
    ticks = iter((0, 21))
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    with pytest.raises(ProviderError, match="payout_response_deadline"):
        provider(PayoutHTTP().handler).create(TERMS)


def test_origin_is_rechecked_immediately_before_io():
    fake = PayoutHTTP()
    adapter = provider(fake.handler)
    adapter._base = "https://api-m.paypal.com"
    with pytest.raises(ProviderError, match="payout_sandbox_required"):
        adapter.create(TERMS)
    assert fake.requests == []


@pytest.mark.parametrize("payload", [{"access_token": "bad token", "token_type": "Bearer"},
    {"access_token": "fake-token", "token_type": "Basic"}, {"access_token": "x" * 8193},
    {"access_token": None, "token_type": "Bearer"}])
def test_oauth_tokens_are_bounded_header_values(payload):
    with pytest.raises(ProviderError):
        provider(lambda r: httpx.Response(200, json=payload))._token()
