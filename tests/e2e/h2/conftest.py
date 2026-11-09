"""Real financial services; provider HTTP alone is replaced in offline tests."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
import pytest

from agentpal.api import create_app
from agentpal.config import Settings
from agentpal.platform.wiring import build_platform
from agentpal.providers.config import PayPalSandboxConfig
from agentpal.providers.paypal_sandbox import PayPalSandboxProvider

PROJECT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT / "sdk/python"))
CAPS = ["wallet.read", "payment.prepare", "owner.request_decision",
        "payment.execute_sandbox", "payout.request"]
PAYMENT = dict(amount="1.00", currency="USD", merchant_name="H2 office supplier",
               merchant_domain="example.com", purpose="Office supplies")
PAYOUT = dict(amount="1.00", currency="USD", receiver_email="freelancer@example.com",
              purpose="Freelancer invoice", note="H2 sandbox verification")


class ProviderHTTP:
    """Independent bounded Orders, Refunds and Payouts fixtures, no external I/O."""

    def __init__(self):
        self.calls = []
        self.orders = {}
        self.refunds = {}
        self.batches = {}
        self.timeout_operation = None
        self.payout_status = "SUCCESS"

    def posts(self, suffix):
        return [r for r in self.calls if r.method == "POST" and r.url.path.endswith(suffix)]

    def approve(self):
        for order in self.orders.values():
            order["status"] = "APPROVED"

    def handler(self, req):
        assert req.url.host == "api-m.sandbox.paypal.com"
        self.calls.append(req)
        path = req.url.path
        body = json.loads(req.content) if req.content and path != "/v1/oauth2/token" else {}
        if path == "/v1/oauth2/token":
            return httpx.Response(200, json=dict(access_token="h2-offline-token", expires_in=3600,
                                                 token_type="Bearer"))
        if path == "/v2/checkout/orders" and req.method == "POST":
            oid = "H2ORDER" + str(len(self.orders) + 1).zfill(10)
            unit = body["purchase_units"][0]
            unit.setdefault("payee", {"email_address": "app@business.example.com",
                                       "merchant_id": "H2MERCHANT123"})
            self.orders[oid] = dict(id=oid, status="CREATED", purchase_units=[unit], links=[{
                "rel": "approve", "href": "https://www.sandbox.paypal.com/checkoutnow?token=" + oid}])
            return httpx.Response(201, json=self.orders[oid])
        if path.startswith("/v2/checkout/orders/"):
            oid = path.split("/")[4]
            order = self.orders[oid]
            if path.endswith("/capture"):
                assert order["status"] == "APPROVED"
                order["status"] = "COMPLETED"
                unit = order["purchase_units"][0]
                unit["payments"] = {"captures": [dict(id="CAPTURE" + oid, status="COMPLETED",
                                                       amount=unit["amount"])]}
                if self.timeout_operation == "capture":
                    raise httpx.ReadTimeout("offline injected lost capture", request=req)
            return httpx.Response(201 if req.method == "POST" else 200, json=order)
        if path.startswith("/v2/payments/captures/"):
            cid = path.split("/")[4]
            unit = self.orders[cid.removeprefix("CAPTURE")]["purchase_units"][0]
            if req.method == "GET":
                return httpx.Response(200, json=dict(id=cid, status="COMPLETED", amount=unit["amount"]))
            rid = "H2REFUND" + str(len(self.refunds) + 1)
            out = dict(id=rid, status="COMPLETED", amount=body["amount"], invoice_id=body["invoice_id"],
                       links=[{"rel": "up", "href": "https://api-m.sandbox.paypal.com/v2/payments/captures/" + cid}])
            self.refunds[rid] = out
            if self.timeout_operation == "refund":
                raise httpx.ReadTimeout("offline injected lost refund", request=req)
            return httpx.Response(201, json=out)
        if path.startswith("/v2/payments/refunds/"):
            return httpx.Response(200, json=self.refunds[path.rsplit("/", 1)[1]])
        if path == "/v1/payments/payouts" and req.method == "POST":
            bid = "H2BATCH" + str(len(self.batches) + 1).zfill(6)
            self.batches[bid] = body
            if self.timeout_operation == "payout":
                raise httpx.ReadTimeout("offline injected lost payout", request=req)
            return httpx.Response(201, json={"batch_header": {"payout_batch_id": bid}})
        if path.startswith("/v1/payments/payouts/"):
            bid = path.rsplit("/", 1)[1]
            batch = self.batches[bid]
            return httpx.Response(200, json={"batch_header": {"payout_batch_id": bid,
                "batch_status": "SUCCESS", "sender_batch_header": batch["sender_batch_header"]},
                "items": [{"payout_batch_id": bid, "payout_item_id": "ITEM" + bid,
                           "transaction_status": self.payout_status, "payout_item": batch["items"][0]}]})
        raise AssertionError("Unexpected offline provider operation")


class World:
    def __init__(self, directory):
        self.now = datetime.now(timezone.utc)
        settings = Settings(db_path=str(directory / "risk.db"), auth_mode="required",
            allowed_hosts=["127.0.0.1"], analyzer_chain=["heuristic"],
            rate_limit_db_path=str(directory / "rate.db"), require_mfa_for_approval=False,
            rate_limit_per_token_per_minute=1000, rate_limit_per_peer_per_minute=2000)
        self.settings = settings
        self.deps = build_platform(settings=settings, environ={})
        self.deps.risk_service.clock = lambda: self.now
        self.deps.owner_authenticator.clock = lambda: self.now
        self.deps.lifecycle.lifecycle.clock = lambda: self.now
        self.backend = ProviderHTTP()
        self.deps.provider = PayPalSandboxProvider(
            PayPalSandboxConfig(client_id="h2-offline-client", client_secret="h2-offline-secret"),
            http_client=httpx.Client(transport=httpx.MockTransport(self.backend.handler)))
        self.deps.lifecycle.provider = self.deps.provider
        self.app = create_app(settings=settings, service=self.deps.risk_service, platform=self.deps)
        self.client = TestClient(self.app, base_url="http://127.0.0.1")
        self.platform = self.app.state.platform
        self.tokens, self.agents, self.wallets, self.keys = {}, {}, {}, {}
        for name in ("owner_a", "owner_b"):
            self.deps.owner_authenticator.create_owner("h2_" + name)
            self.tokens[name] = self.deps.owner_authenticator.issue_token("h2_" + name)[0]
        self.register("a")
        self.register("sibling")
        self.register("b", owner="owner_b")

    def call(self, method, path, actor="owner_a", *, headers=None, **kwargs):
        hs = dict(headers or {})
        if actor:
            hs["Authorization"] = "Bearer " + self.tokens[actor]
        return self.client.request(method, path, headers=hs, **kwargs)

    def register(self, name, owner="owner_a", *, budget="10.00", role="external", metadata=None,
                 capabilities=None):
        body = dict(name=name, role=role, metadata=metadata or {})
        body["capabilities"] = CAPS if capabilities is None else capabilities
        r = self.call("POST", "/v1/agents", owner, json=body)
        assert r.status_code == 201, r.json().get("error")
        data = r.json()
        self.tokens[name], self.agents[name], self.keys[name] = (
            data["api_key"], data["agent"]["agent_id"], data["key_id"])
        self.wallet(name, owner=owner, budget=budget)
        return data

    def wallet(self, name, *, owner="owner_a", budget="10.00", currency="USD"):
        r = self.call("POST", "/v1/wallets", owner, json=dict(agent_id=self.agents[name],
            currency=currency, monthly_budget=budget, per_transaction_limit=budget,
            rolling_window_limit=budget))
        assert r.status_code == 201, r.json().get("error")
        self.wallets[name, currency] = r.json()["wallet_id"]
        return r.json()

    def create(self, kind="payment", actor="a", *, key=None, **changes):
        return self.call("POST", f"/v1/{kind}-requests", actor,
            headers={"Idempotency-Key": key or "h2-" + uuid4().hex},
            json={**(PAYMENT if kind == "payment" else PAYOUT), **changes})

    def prepared(self, kind="payment", actor="a", **changes):
        r = self.create(kind, actor, **changes)
        assert r.status_code == 201, r.json().get("error")
        assert r.json()["status"] == "awaiting_approval"
        return r.json(), f"/v1/{kind}-requests/" + r.json()["request_id"]

    def approved(self, kind="payment", actor="a", **changes):
        data, path = self.prepared(kind, actor, **changes)
        r = self.call("POST", path + "/approve", json={"terms_sha256": data["terms_sha256"]})
        assert r.status_code == 200, r.json().get("error")
        return data, path

    def captured(self):
        data, path = self.approved()
        assert self.call("POST", path + "/execute", "a").status_code == 200
        self.backend.approve()
        r = self.call("POST", path + "/capture")
        assert r.status_code == 200 and r.json()["status"] == "provider_confirmed"
        return data, path

    def allowance(self, name="a", currency="USD"):
        r = self.call("GET", "/v1/me/wallet", name)
        assert r.status_code == 200
        return next(w["allowance"] for w in r.json()["wallets"] if w["currency"] == currency)

    def close(self):
        self.client.close()
        self.deps.close()


@pytest.fixture
def world(tmp_path):
    instance = World(tmp_path)
    try:
        yield instance
    finally:
        instance.close()
