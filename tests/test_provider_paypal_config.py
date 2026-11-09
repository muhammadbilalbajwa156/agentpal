"""PayPal sandbox config + OAuth (offline, httpx.MockTransport)."""

from __future__ import annotations

import base64
import logging

import httpx
import pytest
from pydantic import SecretStr
from test_provider_paypal_support import (
    ACCESS_TOKEN,
    CLIENT_ID,
    CLIENT_SECRET,
    Clock,
    FakePayPal,
    json_response,
    make_config,
    make_provider,
    order_json,
)

from agentpal.infra_contracts import PaymentProvider, ProviderError
from agentpal.providers import (
    SANDBOX_BASE_URL,
    PayPalSandboxConfig,
    PayPalSandboxProvider,
    ProviderConfigError,
    load_config_from_env,
)
from agentpal.providers.config import assert_sandbox_base_url

# ------------------------------------------------------------------ sandbox-only


@pytest.mark.parametrize("url", [
    "https://api-m.paypal.com",                       # production
    "https://api.paypal.com",
    "http://api-m.sandbox.paypal.com",                # not https
    "https://api-m.sandbox.paypal.com.evil.example",  # lookalike
    "https://evil.example/https://api-m.sandbox.paypal.com",
    "https://api-m.sandbox.paypal.com:8443",
    "https://user@api-m.sandbox.paypal.com",
    "https://api-m.sandbox.paypal.com/v2",
    "",
])
def test_non_sandbox_base_url_refused(url):
    with pytest.raises(ValueError):
        make_config(base_url=url)
    with pytest.raises(ProviderConfigError):
        assert_sandbox_base_url(url)


def test_sandbox_base_url_accepted():
    assert make_config().base_url == SANDBOX_BASE_URL == "https://api-m.sandbox.paypal.com"
    assert make_config(base_url=SANDBOX_BASE_URL + "/").base_url == SANDBOX_BASE_URL


def test_provider_rejects_non_config():
    with pytest.raises(TypeError):
        PayPalSandboxProvider({"base_url": "https://api-m.paypal.com"})  # type: ignore[arg-type]


def test_provider_satisfies_payment_provider_protocol():
    provider, _ = make_provider()
    assert isinstance(provider, PaymentProvider)
    assert provider.name == "paypal" and provider.environment == "sandbox"


def test_default_client_has_timeouts_and_no_redirects():
    provider = PayPalSandboxProvider(make_config(connect_timeout_s=3, read_timeout_s=7))
    assert provider.timeout.connect == 3 and provider.timeout.read == 7
    assert provider.http_client.timeout.connect == 3
    assert provider.http_client.timeout.read == 7
    assert provider.http_client.follow_redirects is False


# ------------------------------------------------------------------ env loading


def test_load_from_env():
    cfg = load_config_from_env({"PAYPAL_SANDBOX_CLIENT_ID": CLIENT_ID,
                                "PAYPAL_SANDBOX_CLIENT_SECRET": CLIENT_SECRET,
                                "PAYPAL_SANDBOX_WEBHOOK_ID": "WH123"})
    assert cfg.client_id == CLIENT_ID and cfg.webhook_id == "WH123"
    assert cfg.client_secret.get_secret_value() == CLIENT_SECRET


@pytest.mark.parametrize("primary,alias", [(None, "alias-secret"), ("", "alias-secret"),
                                         ("same-secret", "same-secret")])
def test_secret_alias_accepted(primary, alias):
    env = {"PAYPAL_SANDBOX_CLIENT_ID": CLIENT_ID, "PAYPAL_SANDBOX_SECRET": alias}
    if primary is not None:
        env["PAYPAL_SANDBOX_CLIENT_SECRET"] = primary
    assert load_config_from_env(env).client_secret.get_secret_value() == alias


def test_conflicting_secret_aliases_fail_closed_without_values():
    with pytest.raises(ProviderConfigError) as failure:
        load_config_from_env({"PAYPAL_SANDBOX_CLIENT_ID": CLIENT_ID,
                              "PAYPAL_SANDBOX_CLIENT_SECRET": "primary-private-value",
                              "PAYPAL_SANDBOX_SECRET": "alias-private-value"})
    assert "private-value" not in str(failure.value)


def test_signing_identity_and_intermediates_environment_settings():
    cfg = load_config_from_env({"PAYPAL_SANDBOX_CLIENT_ID": CLIENT_ID,
        "PAYPAL_SANDBOX_SECRET": "test-secret",
        "PAYPAL_SANDBOX_SIGNING_IDENTITY": "rotated.sandbox.paypal.com",
        "PAYPAL_SANDBOX_CERT_INTERMEDIATES_FILE": "/tmp/public-intermediates.pem"})
    assert cfg.webhook_signing_identity == "rotated.sandbox.paypal.com"
    assert cfg.webhook_intermediates_file == "/tmp/public-intermediates.pem"


@pytest.mark.parametrize("env", [{}, {"PAYPAL_SANDBOX_CLIENT_ID": "x"},
                                 {"PAYPAL_SANDBOX_CLIENT_SECRET": "y"},
                                 {"PAYPAL_SANDBOX_CLIENT_ID": " ",
                                  "PAYPAL_SANDBOX_CLIENT_SECRET": "y"}])
def test_load_from_env_missing_refused(env):
    with pytest.raises(ProviderConfigError):
        load_config_from_env(env)


def test_load_from_env_error_never_contains_secret():
    with pytest.raises(ProviderConfigError) as exc:
        load_config_from_env({"PAYPAL_SANDBOX_CLIENT_ID": CLIENT_ID,
                              "PAYPAL_SANDBOX_CLIENT_SECRET": CLIENT_SECRET,
                              "PAYPAL_SANDBOX_WEBHOOK_ID": "WEBHOOK_ID"})
    assert CLIENT_SECRET not in str(exc.value) and CLIENT_ID not in str(exc.value)


def test_simulator_webhook_id_refused():
    with pytest.raises(ValueError):
        make_config(webhook_id="WEBHOOK_ID")


def test_secrets_absent_from_repr_and_str():
    cfg = make_config()
    provider, _ = make_provider()
    for text in (repr(cfg), str(cfg), repr(provider), str(provider)):
        assert CLIENT_SECRET not in text
        assert CLIENT_ID not in text


def test_empty_secret_refused():
    with pytest.raises(ValueError):
        PayPalSandboxConfig(client_id="x", client_secret=SecretStr(""))


# ------------------------------------------------------------------ OAuth


def test_token_request_shape():
    provider, fake = make_provider()
    assert provider.access_token() == f"{ACCESS_TOKEN}-1"
    req = fake.requests[0]
    assert req.method == "POST"
    assert str(req.url) == "https://api-m.sandbox.paypal.com/v1/oauth2/token"
    expected = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    assert req.headers["Authorization"] == f"Basic {expected}"
    assert req.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert req.content == b"grant_type=client_credentials"


def test_token_cached_until_expiry_then_refreshed():
    clock = Clock(1000.0)
    fake = FakePayPal()
    fake.expires_in = 3600
    provider, _ = make_provider(fake, clock=clock)
    fake.routes[("GET", "/v2/checkout/orders/ORDER1")] = json_response(
        200, order_json("ORDER1"))
    provider.get_order_status("ORDER1")
    provider.get_order_status("ORDER1")
    assert fake.token_calls == 1
    clock.t += 3600 - 61                     # still inside the 60 s safety margin
    provider.get_order_status("ORDER1")
    assert fake.token_calls == 1
    clock.t += 2                             # past expires_in - 60
    provider.get_order_status("ORDER1")
    assert fake.token_calls == 2
    assert fake.api_requests()[-1].headers["Authorization"] == f"Bearer {ACCESS_TOKEN}-2"


@pytest.mark.parametrize("response", [
    httpx.Response(401, json={"error": "invalid_client"}),
    httpx.Response(500, text="oops"),
    httpx.Response(200, text="not json"),
    httpx.Response(200, json={"access_token": "", "expires_in": 100}),
    httpx.Response(200, json={"access_token": "t", "expires_in": 0}),
    httpx.Response(200, json={"access_token": "t", "expires_in": 100, "token_type": "MAC"}),
])
def test_token_failure_is_not_outcome_unknown(response):
    fake = FakePayPal()
    fake.routes[("POST", "/v1/oauth2/token")] = lambda r: response
    provider, _ = make_provider(fake)
    with pytest.raises(ProviderError) as exc:
        provider.access_token()
    assert exc.value.outcome_unknown is False


def test_token_network_failure_is_not_outcome_unknown():
    def boom(request):
        raise httpx.ReadTimeout("slow", request=request)
    fake = FakePayPal()
    fake.routes[("POST", "/v1/oauth2/token")] = boom
    provider, _ = make_provider(fake)
    with pytest.raises(ProviderError) as exc:
        provider.access_token()
    assert exc.value.outcome_unknown is False


def test_secrets_and_tokens_never_logged(caplog):
    caplog.set_level(logging.DEBUG)            # includes httpx/httpcore debug logs
    fake = FakePayPal()
    fake.routes[("GET", "/v2/checkout/orders/ORDER1")] = json_response(
        200, order_json("ORDER1", "COMPLETED", capture_status="COMPLETED"))
    fake.routes[("POST", "/v2/checkout/orders/ORDER2/capture")] = json_response(
        500, {"name": "INTERNAL_SERVER_ERROR", "debug_id": "abc"})
    provider, _ = make_provider(fake)
    provider.get_order_status("ORDER1")
    with pytest.raises(ProviderError) as exc:
        provider.capture_order("ORDER2", idempotency_key="cap-key-0001")
    basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    texts = [caplog.text, str(exc.value), repr(exc.value)]
    for text in texts:
        for secret in (CLIENT_SECRET, ACCESS_TOKEN, basic, "Bearer ", "Basic "):
            assert secret not in text
