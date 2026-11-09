"""Trust-path regressions: a correct message signature alone is insufficient."""

import httpx
import pytest
from datetime import timedelta

from test_provider_paypal_support import (
    CERT_URL, NOW, capture_event, make_provider, make_signing_cert, signed_headers,
    make_trusted_signing_cert, trust_test_root, signing_root,
)
from agentpal.providers.paypal_webhooks import WebhookVerificationError


def test_self_signed_certificate_cannot_verify_even_a_valid_message():
    key, pem = make_signing_cert()
    provider, fake = make_provider()
    fake.routes[("GET", httpx.URL(CERT_URL).path)] = lambda r: httpx.Response(200, content=pem)
    raw = capture_event()
    with pytest.raises(WebhookVerificationError):
        provider.verify_webhook(signed_headers(key, raw), raw)


def setup_chain(**options):
    key, pem = make_trusted_signing_cert(**options)
    provider, fake = make_provider()
    trust_test_root(provider)
    fake.routes[("GET", httpx.URL(CERT_URL).path)] = lambda r: httpx.Response(200, content=pem)
    return provider, fake, key, pem


def test_trusted_chain_and_sandbox_signing_identity_accepted():
    provider, _, key, _ = setup_chain()
    raw = capture_event()
    assert provider.verify_webhook(signed_headers(key, raw), raw).verified


@pytest.mark.parametrize("options", [{"hostname": "attacker.example"},
    {"expired_intermediate": True}, {"ca": False}, {"signing_usage": False},
    {"not_before": NOW + timedelta(days=1)}, {"not_after": NOW - timedelta(days=1)}])
def test_invalid_chain_or_identity_fails_closed(options):
    provider, _, key, _ = setup_chain(**options)
    raw = capture_event()
    with pytest.raises(WebhookVerificationError):
        provider.verify_webhook(signed_headers(key, raw), raw)


def test_root_in_download_is_not_a_trust_anchor():
    from cryptography.hazmat.primitives.serialization import Encoding
    provider, fake, key, pem = setup_chain()
    provider._webhook_verifier()._trust_store = None
    fake.routes[("GET", httpx.URL(CERT_URL).path)] = lambda r: httpx.Response(
        200, content=pem + signing_root()[1].public_bytes(Encoding.PEM))
    raw = capture_event()
    with pytest.raises(WebhookVerificationError):
        provider.verify_webhook(signed_headers(key, raw), raw)


def test_missing_intermediate_cannot_build_path():
    provider, fake, key, pem = setup_chain()
    leaf = pem.split(b"-----END CERTIFICATE-----")[0] + b"-----END CERTIFICATE-----\n"
    fake.routes[("GET", httpx.URL(CERT_URL).path)] = lambda r: httpx.Response(200, content=leaf)
    raw = capture_event()
    with pytest.raises(WebhookVerificationError):
        provider.verify_webhook(signed_headers(key, raw), raw)


def test_wrong_issuer_signature_cannot_build_path():
    provider, fake, key, pem = setup_chain()
    _, other = make_trusted_signing_cert()
    leaf = pem.split(b"-----END CERTIFICATE-----")[0] + b"-----END CERTIFICATE-----\n"
    wrong_issuer = other.split(b"-----END CERTIFICATE-----", 1)[1]
    fake.routes[("GET", httpx.URL(CERT_URL).path)] = lambda r: httpx.Response(
        200, content=leaf + wrong_issuer)
    raw = capture_event()
    with pytest.raises(WebhookVerificationError):
        provider.verify_webhook(signed_headers(key, raw), raw)


def test_cached_chain_revalidated_when_intermediate_expires():
    provider, fake, key, _ = setup_chain(not_after=NOW + timedelta(days=200))
    raw = capture_event()
    headers = signed_headers(key, raw)
    assert provider.verify_webhook(headers, raw).verified
    verifier = provider._webhook_verifier()
    verifier._now = lambda: NOW + timedelta(days=101)
    with pytest.raises(WebhookVerificationError):
        verifier._certificate(CERT_URL)
    assert len(fake.requests) == 1


def test_signing_identity_pin_requires_the_literal_san():
    provider, _, key, _ = setup_chain(hostname="*.sandbox.paypal.com")
    raw = capture_event()
    with pytest.raises(WebhookVerificationError):
        provider.verify_webhook(signed_headers(key, raw), raw)


def test_intermediate_file_never_promotes_root_to_trusted(tmp_path):
    from cryptography.hazmat.primitives.serialization import Encoding
    provider, _, key, _ = setup_chain()
    path = tmp_path / "public-root.pem"
    path.write_bytes(signing_root()[1].public_bytes(Encoding.PEM))
    provider._config = provider.config.model_copy(update={"webhook_intermediates_file": str(path)})
    verifier = provider._webhook_verifier()
    verifier._config = provider.config
    verifier._trust_store = None
    raw = capture_event()
    with pytest.raises(WebhookVerificationError):
        provider.verify_webhook(signed_headers(key, raw), raw)


def test_identity_override_is_explicit_and_still_validated():
    key, pem = make_trusted_signing_cert(hostname="rotation.sandbox.paypal.com")
    provider, fake = make_provider(webhook_signing_identity="rotation.sandbox.paypal.com")
    trust_test_root(provider)
    fake.routes[("GET", httpx.URL(CERT_URL).path)] = lambda r: httpx.Response(200, content=pem)
    raw = capture_event()
    assert provider.verify_webhook(signed_headers(key, raw), raw).verified


def test_live_sandbox_signing_certificate_2026_validates_with_bundled_intermediates():
    """Regression (lead, 2026-11-06): PayPal rotated the sandbox webhook signing cert on
    2026-10-21 to one issued by DigiCert EV RSA CA G2. With only the old EV intermediate
    bundled, every real delivery to the hosted service failed chain validation. The leaf
    is PayPal's public cert from CERT-360caa42-fca2a594-9eaebfdf."""
    from datetime import datetime, timezone
    from pathlib import Path

    from agentpal.providers.paypal_certificates import validate_signing_chain

    leaf = (Path(__file__).parent / "fixtures" / "paypal"
            / "messageverificationcerts_sandbox_2026-10.pem").read_bytes()
    when = datetime(2026, 11, 6, tzinfo=timezone.utc)
    cert = validate_signing_chain(leaf, identity="messageverificationcerts.sandbox.paypal.com",
                                  now=when)
    assert cert.not_valid_after_utc.year == 2027
    from cryptography.x509.verification import VerificationError
    with pytest.raises(VerificationError):   # a different signing identity is still refused
        validate_signing_chain(leaf, identity="messageverificationcerts.paypal.com", now=when)
