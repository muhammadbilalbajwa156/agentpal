"""Real /v1 payout flow; offline PayPal HTTP by default, explicit --live for sandbox."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import uuid

import httpx

from ..api import create_app
from ..auth import TokenRegistry
from ..config import Settings
from ..lifecycle import check_consistency
from ..platform.wiring import build_platform
from ..providers.config import PayPalSandboxConfig, ProviderConfigError, load_config_from_env
from ..providers.paypal_sandbox import PayPalSandboxProvider

PRIYA = "agentpal.priya.cbb860@personal.example.com"


class OfflinePayouts:
    def __init__(self):
        self.body = None
        self.posts = 0

    def handler(self, request):
        if request.url.host != "api-m.sandbox.paypal.com":
            raise RuntimeError("sandbox origin required")
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "fake-offline-payout-token",
                                           "token_type": "Bearer", "expires_in": 3600})
        if request.method == "POST" and request.url.path == "/v1/payments/payouts":
            self.posts += 1
            self.body = json.loads(request.content)
            return httpx.Response(201, json={"batch_header": {"payout_batch_id": "OFFLINEBATCH1234"}})
        if request.method == "GET" and request.url.path == "/v1/payments/payouts/OFFLINEBATCH1234":
            return httpx.Response(200, json={"batch_header": {"payout_batch_id": "OFFLINEBATCH1234",
                "batch_status": "SUCCESS", "sender_batch_header": self.body["sender_batch_header"]},
                "items": [{"payout_batch_id": "OFFLINEBATCH1234", "payout_item_id": "OFFLINEITEM5678",
                    "transaction_status": "SUCCESS", "payout_item": self.body["items"][0]}]})
        raise RuntimeError("unexpected offline payout operation")


async def run_flow(*, live=False, receiver=PRIYA, note="AgentPal sandbox freelancer payout",
                   poll_timeout=120, poll_interval=3):
    """A fresh $1 sandbox request with ephemeral app identities; no credentials in report."""
    if not 0 <= poll_timeout <= 300 or not 0.1 <= poll_interval <= 10:
        raise ValueError("bounded polling required")
    if live:
        load_config_from_env()
    backend = None if live else OfflinePayouts()
    with TemporaryDirectory(prefix="agentpal-payout-demo-") as directory:
        settings = Settings(db_path=str(Path(directory) / "risk.db"), auth_mode="required",
            analyzer_chain=["heuristic"], allowed_hosts=["127.0.0.1"], log_level="ERROR")
        deps = build_platform(settings=settings, environ=os.environ if live else {})
        try:
            if backend is not None:
                deps.provider = PayPalSandboxProvider(PayPalSandboxConfig(client_id="fake-client",
                    client_secret="fake-secret"), http_client=httpx.Client(
                        transport=httpx.MockTransport(backend.handler)))
            identity = deps.owner_authenticator
            identity.create_owner("payout_demo_owner")
            owner_token, _ = identity.issue_token("payout_demo_owner")
            owner_headers = {"Authorization": "Bearer " + owner_token}
            app = create_app(service=deps.risk_service, settings=settings,
                             registry=TokenRegistry(), platform=deps)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                          base_url="http://127.0.0.1") as client:
                async def call(method, path, *, headers=owner_headers, expected=200, **kwargs):
                    response = await client.request(method, path, headers=headers, **kwargs)
                    if response.status_code != expected:
                        raise RuntimeError("payout demo API refused: HTTP " + str(response.status_code))
                    return response.json()
                registered = await call("POST", "/v1/agents", expected=201, json={
                    "name": "External freelancer payments agent", "capabilities": [
                        "wallet.read", "payout.request", "payment.execute_sandbox"]})
                agent_headers = {"Authorization": "Bearer " + registered["api_key"]}
                await call("POST", "/v1/wallets", expected=201, json={
                    "agent_id": registered["agent"]["agent_id"], "currency": "USD",
                    "monthly_budget": "5.00", "per_transaction_limit": "1.00"})
                payout = await call("POST", "/v1/payout-requests", expected=201,
                    headers={**agent_headers, "Idempotency-Key": "payout-demo-" + uuid.uuid4().hex},
                    json={"receiver_email": receiver, "amount": "1.00", "currency": "USD",
                          "note": note, "purpose": "Sandbox freelancer demo; no real money"})
                path = "/v1/payout-requests/" + payout["request_id"]
                states = [payout["status"]]
                approved = await call("POST", path + "/approve",
                    json={"terms_sha256": payout["terms_sha256"]})
                states.append(approved["status"])
                outcome = await call("POST", path + "/execute", headers=agent_headers)
                states.append(outcome["status"])
                replay = await call("POST", path + "/execute", headers=agent_headers)
                if replay["provider_batch_id"] != outcome["provider_batch_id"]:
                    raise RuntimeError("idempotent execution returned a different batch")
                deadline = time.monotonic() + poll_timeout
                while (outcome["status"] in {"provider_pending", "reconciliation_required"}
                       and outcome["provider_batch_id"] and time.monotonic() < deadline):
                    await asyncio.sleep(min(poll_interval, max(0, deadline - time.monotonic())))
                    outcome = await call("POST", path + "/reconciliation")
                    if states[-1] != outcome["status"]:
                        states.append(outcome["status"])
                operations = deps.risk_service.store.query("SELECT * FROM provider_operations")
                outcomes = deps.risk_service.store.query("SELECT * FROM payment_outcomes WHERE verified=1")
                if len(operations) != 1 or (backend is not None and backend.posts != 1):
                    raise RuntimeError("payout execution was not single-use")
                if check_consistency(deps.risk_service.store, deps.wallet_store):
                    raise RuntimeError("payout financial consistency check failed")
                return {"mode": "live_sandbox" if live else "offline_mocked_paypal",
                    "states": states, "status": outcome["status"], "is_real_money": False,
                    "provider_item_status": outcome["provider_item_status"],
                    "batch_id_redacted": ("..." + outcome["provider_batch_id"][-4:]
                                          if outcome["provider_batch_id"] else None),
                    "request_id_redacted": "..." + payout["request_id"][-4:],
                    "create_invocations": len(operations), "verified_outcomes": len(outcomes),
                    "unclaimed": outcome["unclaimed"]}
        finally:
            deps.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="explicit PayPal sandbox mode ($1 USD)")
    args = parser.parse_args()
    try:
        report = asyncio.run(run_flow(live=args.live))
    except ProviderConfigError:
        print(json.dumps({"status": "BLOCKED", "reason": "sandbox credentials missing or invalid"}))
        return 2
    except Exception:
        print(json.dumps({"status": "UNVERIFIED", "reason": "payout demo could not complete"}))
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "provider_confirmed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
