"""OFFLINE ONLY TCP server; real AgentPal services, durable synthetic PayPal HTTP.

Crash gates stop the process AFTER the fake external provider has committed a
mutation but BEFORE AgentPal receives its response. No production code is patched.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sys
import threading
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from agentpal.api import create_app, uvicorn_log_config  # noqa: E402
from agentpal.config import get_settings  # noqa: E402
from agentpal.platform.wiring import build_platform  # noqa: E402
from agentpal.providers.config import PayPalSandboxConfig  # noqa: E402
from agentpal.providers.paypal_sandbox import PayPalSandboxProvider  # noqa: E402


class DurablePayPal:
    def __init__(self, directory):
        self.directory = directory
        self.path = directory / "synthetic-provider.json"
        if not self.path.exists():
            self.save({"orders": {}, "refunds": {}, "payouts": {}, "posts": []})

    def load(self):
        return json.loads(self.path.read_text())

    def save(self, data):
        temporary = self.path.with_suffix(".writing")
        temporary.write_text(json.dumps(data))
        os.replace(temporary, self.path)

    def gate(self, operation):
        if os.environ.get("T4_GATE") == operation:
            gate = self.directory / "gate-writing"
            gate.write_text(operation)
            os.replace(gate, self.directory / "gate-hit")
            os.kill(os.getpid(), signal.SIGSTOP)
            # Keep the response blocked even if an external process supervisor
            # resumes a stopped child before the tester delivers SIGKILL.
            threading.Event().wait()

    def handler(self, request):
        assert request.url.host == "api-m.sandbox.paypal.com"
        path, method = request.url.path, request.method
        if path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "synthetic-token", "expires_in": 3600, "token_type": "Bearer"})
        data = self.load()
        body = json.loads(request.content) if request.content else {}
        if method == "POST":
            data["posts"].append(path)
        if path == "/v2/checkout/orders" and method == "POST":
            oid = "T4ORDER" + str(len(data["orders"]) + 1)
            unit = body["purchase_units"][0]
            unit.setdefault("payee", {"email_address": "t4@business.example.com", "merchant_id": "T4MERCHANT12"})
            order = {"id": oid, "status": "CREATED", "purchase_units": [unit],
                     "links": [{"rel": "approve", "href": "https://www.sandbox.paypal.com/checkoutnow?token=" + oid}]}
            data["orders"][oid] = order
            self.save(data)
            self.gate("execute")
            return httpx.Response(201, json=order)
        for oid, order in data["orders"].items():
            if path == "/v2/checkout/orders/" + oid and method == "GET":
                return httpx.Response(200, json=order)
            unit = order["purchase_units"][0]
            cid = "T4CAPTURE" + oid
            if path == "/v2/checkout/orders/" + oid + "/capture" and method == "POST":
                assert order["status"] == "APPROVED"
                order["status"] = "COMPLETED"
                unit["payments"] = {"captures": [{"id": cid, "status": "COMPLETED", "amount": unit["amount"],
                    "update_time": datetime.now(timezone.utc).isoformat()}]}
                self.save(data)
                self.gate("capture")
                return httpx.Response(201, json=order)
            if path == "/v2/payments/captures/" + cid and method == "GET":
                return httpx.Response(200, json={"id": cid, "status": "COMPLETED", "amount": unit["amount"]})
            if path == "/v2/payments/captures/" + cid + "/refund" and method == "POST":
                rid = "T4REFUND" + str(len(data["refunds"]) + 1)
                refund = {"id": rid, "status": "COMPLETED", "amount": body["amount"],
                          "invoice_id": body["invoice_id"], "links": [{"rel": "up",
                          "href": "https://api.sandbox.paypal.com/v2/payments/captures/" + cid}]}
                data["refunds"][rid] = refund
                unit["payments"].setdefault("refunds", []).append(refund)
                self.save(data)
                self.gate("refund")
                return httpx.Response(201, json=refund)
        for rid, refund in data["refunds"].items():
            if path == "/v2/payments/refunds/" + rid and method == "GET":
                return httpx.Response(200, json=refund)
        if path == "/v1/payments/payouts" and method == "POST":
            bid = "T4BATCH" + str(len(data["payouts"]) + 1)
            data["payouts"][bid] = body
            self.save(data)
            self.gate("payout")
            return httpx.Response(201, json={"batch_header": {"payout_batch_id": bid}})
        for bid, payout in data["payouts"].items():
            if path == "/v1/payments/payouts/" + bid and method == "GET":
                return httpx.Response(200, json={"batch_header": {"payout_batch_id": bid,
                    "batch_status": "SUCCESS", "sender_batch_header": payout["sender_batch_header"]},
                    "items": [{"payout_batch_id": bid, "payout_item_id": "T4ITEM1234",
                    "transaction_status": "SUCCESS", "payout_item": payout["items"][0]}]})
        if path == "/v1/reporting/transactions":
            return httpx.Response(200, json={"transaction_details": [], "page": 1, "total_pages": 0, "total_items": 0})
        raise AssertionError("Unexpected synthetic PayPal operation")


def main():
    settings = get_settings()
    backend = DurablePayPal(Path(settings.db_path).parent)
    deps = build_platform(settings=settings, environ={},
        identity_db_path=os.environ["AGENTPAL_IDENTITY_DB_PATH"],
        wallet_db_path=os.environ["AGENTPAL_WALLET_DB_PATH"])
    provider = PayPalSandboxProvider(PayPalSandboxConfig(client_id="synthetic-client", client_secret="synthetic-secret"),
                                    http_client=httpx.Client(transport=httpx.MockTransport(backend.handler)))
    deps.provider = deps.lifecycle.provider = provider
    if os.environ.get("T4_CLOCK_FILE"):
        def clock():
            return datetime.fromisoformat(Path(os.environ["T4_CLOCK_FILE"]).read_text())
        deps.risk_service.clock = deps.risk_service.store.clock = clock
        deps.lifecycle.lifecycle.clock = clock
        deps.owner_authenticator.clock = clock
    app = create_app(service=deps.risk_service, settings=settings, platform=deps)
    port = int(sys.argv[sys.argv.index("--port") + 1])
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, proxy_headers=False,
                    server_header=False, log_config=uvicorn_log_config())
    finally:
        deps.close()


if __name__ == "__main__":
    main()
