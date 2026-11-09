"""Real AgentPal lifecycle, mocked PayPal by default. No real money.

    python -m agentpal.demo.paypal_flow
    python -m agentpal.demo.paypal_flow --simulate-capture-timeout
    python -m agentpal.demo.paypal_flow --live

Live mode requires environment credentials AND an explicit sandbox buyer step.
All demo identities/tokens are ephemeral; plaintext credentials are never written.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import uuid

import httpx

from ..api import create_app
from ..auth import TokenRegistry
from ..config import Settings
from ..platform.wiring import build_platform
from ..providers.config import PayPalSandboxConfig, ProviderConfigError, load_config_from_env
from ..providers.paypal_sandbox import PayPalSandboxProvider
from ..providers.paypal_webhooks import signed_message

CERT_URL = "https://api.sandbox.paypal.com/v1/notifications/certs/DEMO-CERT"
ORDER_ID = "DEMO-ORDER-1"


class DemoError(RuntimeError):
    """The observed API flow did not meet the demonstration's expectations."""


class OfflinePayPal:
    """In-memory sandbox fixture. Only PayPal HTTP is replaced, never AgentPal services."""

    def __init__(self, *, capture_timeout: bool = False):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
        self.requests: list[httpx.Request] = []
        self.status = "CREATED"
        self.purchase_unit = {}
        self.amount = {"value": "1.00", "currency_code": "USD"}
        self.capture_timeout = capture_timeout
        now = datetime.now(timezone.utc)
        root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Ephemeral offline demo CA")])
        self.root = (x509.CertificateBuilder().subject_name(root_name).issuer_name(root_name)
            .public_key(root_key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
            .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(root_key.public_key()), False)
            .sign(root_key, hashes.SHA256()))
        self.signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        signing_name = "messageverificationcerts.sandbox.paypal.com"
        leaf = (x509.CertificateBuilder().subject_name(x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, signing_name)])).issuer_name(root_name)
            .public_key(self.signing_key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
            .add_extension(x509.KeyUsage(True, False, True, False, False, False, False, False, False), True)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(signing_name)]), False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()), False)
            .sign(root_key, hashes.SHA256()))
        self.pem = leaf.public_bytes(serialization.Encoding.PEM)

    def approve(self) -> None:
        self.status = "APPROVED"

    def order(self) -> dict:
        unit = {**self.purchase_unit, "amount": self.amount,
                "payee": {"email_address": "demo-app@business.example.com",
                          "merchant_id": "DEMOACCOUNT12"}}
        if self.status == "COMPLETED":
            unit["payments"] = {"captures": [{"id": "DEMO-CAPTURE-1", "status": "COMPLETED",
                                               "amount": self.amount}]}
        return {"id": ORDER_ID, "status": self.status, "purchase_units": [unit], "links": [{
            "rel": "approve", "href": f"https://www.sandbox.paypal.com/checkoutnow?token={ORDER_ID}"}]}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host not in {"api-m.sandbox.paypal.com", "api.sandbox.paypal.com"}:
            raise DemoError("offline transport received a non-sandbox request")
        route = (request.method, request.url.path)
        if route == ("POST", "/v1/oauth2/token"):
            return httpx.Response(200, json={"access_token": "offline-demo-token",
                                            "expires_in": 3600, "token_type": "Bearer"})
        if route == ("GET", httpx.URL(CERT_URL).path):
            return httpx.Response(200, content=self.pem)
        if route == ("POST", "/v2/checkout/orders"):
            self.purchase_unit = json.loads(request.content)["purchase_units"][0]
            self.amount = self.purchase_unit["amount"]
            return httpx.Response(201, json=self.order())
        if route == ("GET", f"/v2/checkout/orders/{ORDER_ID}"):
            return httpx.Response(200, json=self.order())
        if route == ("POST", f"/v2/checkout/orders/{ORDER_ID}/capture"):
            if self.status != "APPROVED":
                return httpx.Response(422, json={"name": "UNPROCESSABLE_ENTITY"})
            self.status = "COMPLETED"
            if self.capture_timeout:
                raise httpx.ReadTimeout("offline injected loss after sandbox capture", request=request)
            return httpx.Response(201, json=self.order())
        raise DemoError("unexpected offline PayPal operation")

    def delivery(self, webhook_id: str) -> tuple[bytes, dict[str, str]]:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        body = json.dumps({"id": "DEMO-WEBHOOK-1", "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "resource": {"id": "DEMO-CAPTURE-1", "status": "COMPLETED", "amount": self.amount,
                "supplementary_data": {"related_ids": {"order_id": ORDER_ID}}}}).encode()
        sent = datetime.now(timezone.utc).isoformat()
        signature = self.signing_key.sign(signed_message("DEMO-TRANSMISSION", sent, webhook_id, body),
                                          padding.PKCS1v15(), hashes.SHA256())
        return body, {"paypal-transmission-id": "DEMO-TRANSMISSION", "paypal-transmission-time": sent,
            "paypal-transmission-sig": base64.b64encode(signature).decode(),
            "paypal-cert-url": CERT_URL, "paypal-auth-algo": "SHA256withRSA"}


def require_buyer_approval(url: str) -> bool:
    print("Sandbox buyer step: open this PayPal Sandbox URL and approve the test payment:\n"
          + url + "\nType APPROVED to request a verified server-side check and capture:", file=sys.stderr)
    try:
        return input().strip() == "APPROVED"
    except EOFError:
        return False


async def run_flow(*, live: bool = False, environ: Mapping[str, str] | None = None,
                   buyer_approval: Callable[[str], bool] = require_buyer_approval,
                   simulate_capture_timeout: bool = False) -> dict:
    env = os.environ if environ is None else environ
    if live:
        load_config_from_env(env)  # fail before building anything or requesting approval
        if simulate_capture_timeout:
            raise DemoError("failure injection is available only in offline mode")
    backend = None if live else OfflinePayPal(capture_timeout=simulate_capture_timeout)
    with TemporaryDirectory(prefix="agentpal-paypal-demo-") as directory:
        settings = Settings(db_path=str(Path(directory) / "risk.db"), auth_mode="required",
            analyzer_chain=["heuristic"], allowed_hosts=["127.0.0.1"], log_level="ERROR")
        deps = build_platform(settings=settings, environ=env if live else {})
        try:
            if backend is not None:
                from cryptography.x509.verification import Store
                config = PayPalSandboxConfig(client_id="offline-demo-client",
                    client_secret="offline-demo-secret", webhook_id="DEMO-WEBHOOK-SUBSCRIPTION")
                provider = PayPalSandboxProvider(config,
                    http_client=httpx.Client(transport=httpx.MockTransport(backend.handler)))
                # Offline-only trust injection. Live verification always uses certifi.
                provider._webhook_verifier()._trust_store = Store([backend.root])
                deps.provider = provider
                deps.lifecycle.provider = provider
            identity = deps.owner_authenticator
            identity.create_owner("demo_owner")
            owner_token, _ = identity.issue_token("demo_owner")
            owner_headers = {"Authorization": "Bearer " + owner_token}
            app = create_app(service=deps.risk_service, settings=settings,
                             registry=TokenRegistry(), platform=deps)
            states = []
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                          base_url="http://127.0.0.1") as client:
                async def call(method, path, *, headers=owner_headers, expected=200, **kwargs):
                    response = await client.request(method, path, headers=headers, **kwargs)
                    if response.status_code != expected:
                        raise DemoError(f"demo API operation failed (HTTP {response.status_code})")
                    return response.json()
                registered = await call("POST", "/v1/agents", expected=201, json={
                    "name": "External sandbox demo agent", "capabilities": ["wallet.read",
                        "payment.prepare", "owner.request_decision", "payment.execute_sandbox"]})
                agent_headers = {"Authorization": "Bearer " + registered["api_key"]}
                await call("POST", "/v1/wallets", expected=201, json={
                    "agent_id": registered["agent"]["agent_id"], "currency": "USD",
                    "monthly_budget": "10.00", "per_transaction_limit": "1.00"})
                payment = await call("POST", "/v1/payment-requests", expected=201,
                    headers={**agent_headers, "Idempotency-Key": "demo-" + uuid.uuid4().hex},
                    json={"amount": "1.00", "currency": "USD", "merchant_name": "Sandbox demo",
                        "merchant_domain": "example.com", "purpose": "Sandbox only, no real money"})
                states.append(payment["status"])
                path = "/v1/payment-requests/" + payment["request_id"]
                decision = await call("POST", path + "/approve",
                                      json={"terms_sha256": payment["terms_sha256"]})
                states.append(decision["status"])
                executed = await call("POST", path + "/execute", headers=agent_headers)
                states.append(executed["status"])
                refs = (await call("GET", path + "/reconciliation"))["provider_references"]
                if not refs or not refs[0]["payer_action_url"]:
                    raise DemoError("sandbox payer URL unavailable; reconcile provider outcome")
                if backend is not None:
                    backend.approve()
                elif not buyer_approval(refs[0]["payer_action_url"]):
                    return {"mode": "live_sandbox", "status": "awaiting_explicit_buyer_step",
                            "states": states}
                captured = await call("POST", path + "/capture")
                states.append(captured["status"])
                reconciled = await call("POST", path + "/reconciliation")
                states.append(reconciled["status"])
                deliveries = []
                if backend is not None:
                    body, headers = backend.delivery(deps.provider.config.webhook_id)
                    for _ in range(2):
                        response = await client.post("/v1/providers/paypal/webhooks", content=body,
                                                     headers=headers)
                        deliveries.append(response.status_code)
                        if response.status_code != 200:
                            raise DemoError("offline signed webhook failed")
                outcomes = deps.risk_service.store.query("SELECT * FROM payment_outcomes")
                capture_posts = (len([r for r in backend.requests if r.method == "POST"
                                     and r.url.path.endswith("/capture")]) if backend else None)
                if backend and (len(outcomes) != 1 or capture_posts != 1
                                or reconciled["status"] != "provider_confirmed"):
                    raise DemoError("offline lifecycle invariant failed")
                return {"mode": "live_sandbox" if live else "offline_mocked_paypal",
                    "evidence": "LIVE SANDBOX" if live else "TESTED OFFLINE",
                    "status": reconciled["status"], "states": states,
                    "verified_outcomes": len(outcomes), "capture_posts": capture_posts,
                    "webhook_deliveries": deliveries, "is_real_money": False,
                    "live_webhooks": "UNVERIFIED: requires registered public HTTPS listener"
                        if live else "mock transport, ephemeral test CA"}
        finally:
            deps.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="explicit PayPal Sandbox network mode")
    parser.add_argument("--simulate-capture-timeout", action="store_true")
    args = parser.parse_args()
    try:
        report = asyncio.run(run_flow(live=args.live,
            simulate_capture_timeout=args.simulate_capture_timeout))
    except ProviderConfigError:
        print(json.dumps({"status": "BLOCKED BY CREDENTIALS",
                          "reason": "missing, conflicting or invalid sandbox configuration"}))
        return 2
    except Exception as exc:
        print(json.dumps({"status": "UNVERIFIED", "error_type": type(exc).__name__}))
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
