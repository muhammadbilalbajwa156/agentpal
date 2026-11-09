"""Shared offline fakes for the PayPal sandbox provider tests (no test functions here).

Everything runs against ``httpx.MockTransport``; nothing touches the network."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable
from functools import lru_cache

import httpx
from pydantic import SecretStr

from agentpal.providers import PayPalSandboxConfig, PayPalSandboxProvider
from agentpal.spending import ApprovalTerms

CLIENT_ID = "test-client-id-AbCdEf"
CLIENT_SECRET = "s3cr3t-SANDBOX-value-do-not-log"
ACCESS_TOKEN = "A21AA-test-access-token-do-not-log"
WEBHOOK_ID = "8PT597110X687430LKGECATA"
NOW = datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc)
CERT_URL = "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-360caa42-fca2a594-test"


def make_terms(**kw) -> ApprovalTerms:
    base = dict(request_id="req_0001", agent_id="agent_1", amount=Decimal("12.50"),
                currency="USD", merchant_name="Acme Books", merchant_domain="acme.example",
                purpose="Buy a reference book")
    base.update(kw)
    return ApprovalTerms(**base)


def make_config(**kw) -> PayPalSandboxConfig:
    base = dict(client_id=CLIENT_ID, client_secret=SecretStr(CLIENT_SECRET),
                webhook_id=WEBHOOK_ID)
    base.update(kw)
    return PayPalSandboxConfig(**base)


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakePayPal:
    """Records every request; routes are ``(METHOD, path) -> handler(request)``."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.token_calls = 0
        self.expires_in = 32400
        self.routes: dict[tuple[str, str], Callable[[httpx.Request], httpx.Response]] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        if key == ("POST", "/v1/oauth2/token") and key not in self.routes:
            self.token_calls += 1
            return httpx.Response(200, json={
                "scope": "https://uri.paypal.com/services/payments",
                "access_token": f"{ACCESS_TOKEN}-{self.token_calls}",
                "token_type": "Bearer", "app_id": "APP-80W284485P519543T",
                "expires_in": self.expires_in, "nonce": "2026-10-31T12:00:00Zabc"})
        route = self.routes.get(key)
        if route is None:
            return httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND",
                                             "debug_id": "dbg404"})
        return route(request)

    def api_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path != "/v1/oauth2/token"]


def make_provider(fake: FakePayPal | None = None, *, clock: Clock | None = None,
                  now: datetime = NOW, **config_kw):
    fake = fake or FakePayPal()
    client = httpx.Client(transport=httpx.MockTransport(fake.handler))
    provider = PayPalSandboxProvider(make_config(**config_kw), http_client=client,
                                     monotonic=clock or Clock(), now=lambda: now)
    return provider, fake


def order_json(order_id: str = "5O190127TN364715T", status: str = "CREATED", *,
               capture_status: str | None = None, capture_id: str = "3C679366HH908993F",
               amount: str = "12.50", currency: str = "USD", refunds: list | None = None,
               links: list | None = None) -> dict:
    unit: dict = {"reference_id": "req_0001", "amount": {"currency_code": currency,
                                                         "value": amount}}
    if capture_status is not None or refunds is not None:
        unit["payments"] = {}
        if capture_status is not None:
            unit["payments"]["captures"] = [{
                "id": capture_id, "status": capture_status,
                "amount": {"currency_code": currency, "value": amount},
                "create_time": "2026-10-31T11:59:00Z", "update_time": "2026-10-31T11:59:30Z"}]
        if refunds is not None:
            unit["payments"]["refunds"] = refunds
    return {"id": order_id, "status": status, "purchase_units": [unit],
            "links": links if links is not None else [
                {"href": f"https://api-m.sandbox.paypal.com/v2/checkout/orders/{order_id}",
                 "rel": "self", "method": "GET"},
                {"href": f"https://www.sandbox.paypal.com/checkoutnow?token={order_id}",
                 "rel": "approve", "method": "GET"}]}


def json_response(status: int, body) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(status, content=json.dumps(body).encode())


# ------------------------------------------------------------------ webhook signing
def make_signing_cert(*, not_before: datetime | None = None, not_after: datetime | None = None):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,
                                         "messageverificationcerts.sandbox.paypal.test")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(not_before or NOW - timedelta(days=30))
            .not_valid_after(not_after or NOW + timedelta(days=365))
            .sign(key, hashes.SHA256()))
    return key, cert.public_bytes(serialization.Encoding.PEM)


@lru_cache(maxsize=1)
def signing_root():
    """Ephemeral test CA; private keys stay in memory."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "AgentPal ephemeral test root")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(NOW - timedelta(days=365)).not_valid_after(NOW + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=2), critical=True)
        .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False),
                       critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
        .sign(key, hashes.SHA256()))
    return key, cert


def make_trusted_signing_cert(*, hostname="messageverificationcerts.sandbox.paypal.com",
                             expired_intermediate=False, ca=True, signing_usage=True,
                             not_before=None, not_after=None):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
    root_key, root = signing_root()
    issuer_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    issuer_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test intermediate")])
    issuer = (x509.CertificateBuilder().subject_name(issuer_name).issuer_name(root.subject)
        .public_key(issuer_key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(NOW - timedelta(days=100))
        .not_valid_after(NOW + timedelta(days=-1 if expired_intermediate else 100))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=0 if ca else None), True)
        .add_extension(x509.KeyUsage(False, False, False, False, False, ca, ca, False, False), True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(issuer_key.public_key()), False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()), False)
        .sign(root_key, hashes.SHA256()))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf = (x509.CertificateBuilder().subject_name(x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, hostname)])).issuer_name(issuer.subject)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(not_before or NOW - timedelta(days=10))
        .not_valid_after(not_after or NOW + timedelta(days=10))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
        .add_extension(x509.KeyUsage(signing_usage, False, True, False, False,
                                     False, False, False, False), True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()), False)
        .sign(issuer_key, hashes.SHA256()))
    return key, (leaf.public_bytes(serialization.Encoding.PEM)
                 + issuer.public_bytes(serialization.Encoding.PEM))


def trust_test_root(provider):
    from cryptography.x509.verification import Store
    provider._webhook_verifier()._trust_store = Store([signing_root()[1]])


def sign(key, transmission_id: str, transmission_time: str, webhook_id: str,
         raw_body: bytes) -> str:
    import base64
    import zlib

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    msg = f"{transmission_id}|{transmission_time}|{webhook_id}|{zlib.crc32(raw_body)}".encode()
    return base64.b64encode(key.sign(msg, padding.PKCS1v15(), hashes.SHA256())).decode()


def capture_event(event_type: str = "PAYMENT.CAPTURE.COMPLETED", *, status: str = "COMPLETED",
                  event_id: str = "WH-58D329510W468432D-8HN650336L201105X") -> bytes:
    body = {
        "id": event_id, "event_version": "1.0", "create_time": "2026-10-31T11:59:58Z",
        "resource_type": "capture", "resource_version": "2.0", "event_type": event_type,
        "summary": "Payment completed",
        "resource": {
            "id": "3C679366HH908993F", "status": status,
            "amount": {"currency_code": "USD", "value": "12.50"},
            "supplementary_data": {"related_ids": {"order_id": "5O190127TN364715T"}},
            "create_time": "2026-10-31T11:59:57Z"}}
    # deliberately unusual spacing: verification must use the EXACT raw bytes
    return json.dumps(body, indent=1).encode()


def refund_event(amount: str = "5.00", event_type: str = "PAYMENT.CAPTURE.REFUNDED") -> bytes:
    body = {
        "id": "WH-1GE84257G0350133W-6RW800890C634293G", "event_version": "1.0",
        "create_time": "2026-10-31T11:59:58Z", "resource_type": "refund",
        "event_type": event_type,
        "resource": {"id": "1Y107995YT783435V", "status": "COMPLETED",
                     "amount": {"currency_code": "USD", "value": amount},
                     "supplementary_data": {"related_ids": {
                         "order_id": "5O190127TN364715T", "capture_id": "3C679366HH908993F"}}}}
    return json.dumps(body).encode()


def signed_headers(key, raw_body: bytes, *, webhook_id: str = WEBHOOK_ID,
                   transmission_time: str = "2026-10-31T11:59:59Z",
                   cert_url: str = CERT_URL,
                   transmission_id: str = "69cd13f0-d67a-11e5-baa3-778b53f4ae55") -> dict:
    return {"PAYPAL-TRANSMISSION-ID": transmission_id,
            "PAYPAL-TRANSMISSION-TIME": transmission_time,
            "PAYPAL-TRANSMISSION-SIG": sign(key, transmission_id, transmission_time,
                                            webhook_id, raw_body),
            "PAYPAL-CERT-URL": cert_url, "PAYPAL-AUTH-ALGO": "SHA256withRSA"}
