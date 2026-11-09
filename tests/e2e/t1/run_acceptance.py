"""T1 real TCP acceptance journey. Run from agentpal/: python tests/e2e/t1/run_acceptance.py --live.

Secrets live only in memory/environment. Output is an allowlisted, redacted report.
Each invocation uses NEW databases and creates at most four $1 sandbox orders.
Without --live this exercises the same real server with the local simulator.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

import httpx

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "sdk/python"))
from agentpal.identity import OwnerIdentityService  # noqa: E402
from agentpal.identity.store import IdentityStore  # noqa: E402
from agentpal_sdk import AgentClient, OwnerClient  # noqa: E402

CAPS = ["wallet.read", "payment.prepare", "owner.request_decision",
        "payment.execute_sandbox", "payout.request"]


class Secret(str):
    """Prevent pytest's automatic argument repr from revealing throwaway keys."""

    def __repr__(self):
        return "[credential omitted]"


def redacted(value):
    """Never persist credentials, full resource identifiers, or buyer URLs."""
    if isinstance(value, dict):
        return {k: ("[omitted]" if any(x in k.lower() for x in (
            "key", "credential", "payer_action_url", "sha256")) else
                    "..." + v[-4:] if isinstance(v, str) and (k == "id" or k.endswith("_id")) else redacted(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [redacted(v) for v in value]
    if isinstance(value, str):
        if value.startswith(("apo_", "apk_")):
            return "[omitted]"
        if value.startswith("https://buy.trychannel3.com/"):
            return "https://buy.trychannel3.com/[redacted]"
        if value.startswith("https://api.trychannel3.com/v1/products/"):
            return "https://api.trychannel3.com/v1/products/..." + value[-4:]
        return re.sub(r"\b(?:agt_|wal_|pay_|pyo_|rfd-)[0-9a-f]{32}\b|\b[A-Z0-9]{17}\b|(?<=credentials/)[0-9a-f]{16}\b",
                      lambda m: "..." + m[0][-4:], value)
    return value


@contextmanager
def server(directory: Path, *, live=False, port=8101, resume=False):
    directory.mkdir(parents=True, exist_ok=resume)
    identity = OwnerIdentityService(IdentityStore(directory / "identity.db"))
    if not resume:
        identity.create_owner("owner_t1", "T1 acceptance owner")
    token, _ = identity.issue_token("owner_t1")
    identity.store.close()
    child = {k: v for k, v in os.environ.items() if not k.startswith("AGENTPAL_")}
    if not live:
        child = {k: v for k, v in child.items() if not k.startswith((
            "PAYPAL_", "CHANNEL3_", "KERNEL_"))}
    child.update(AGENTPAL_DB_PATH=str(directory / "ledger.db"),
                 AGENTPAL_IDENTITY_DB_PATH=str(directory / "identity.db"),
                 AGENTPAL_WALLET_DB_PATH=str(directory / "wallet.db"),
                 AGENTPAL_AUTH_MODE="required", AGENTPAL_ENV="development")
    # Keep provider and access logs private; they are never printed or committed.
    with (directory / "server.log").open("a" if resume else "w") as log:
        proc = subprocess.Popen([sys.executable, "-m", "agentpal", "serve", "--platform",
                                 "--host", "127.0.0.1", "--port", str(port)],
                                cwd=ROOT, env=child, stdout=log, stderr=log)
        try:
            base = f"http://127.0.0.1:{port}"
            with httpx.Client(base_url=base, trust_env=False, timeout=65) as client:
                for _ in range(100):
                    if proc.poll() is not None:
                        raise RuntimeError("server_start_failed; inspect private log")
                    try:
                        if client.get("/v1/health").status_code == 200:
                            yield client, Secret(token), base
                            break
                    except httpx.ConnectError:
                        pass
                    time.sleep(.1)
                else:
                    raise RuntimeError("server_start_timeout")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


class Journey:
    def __init__(self, client, owner, report):
        self.client, self.owner, self.report = client, owner, report

    def call(self, method, path, token=None, body=None, *, key=None, expected=(200, 201, 204)):
        __tracebackhide__ = True
        headers = {"Authorization": "Bearer " + (token or self.owner)}
        if key:
            headers["Idempotency-Key"] = key
        r = self.client.request(method, path, headers=headers, json=body)
        if r.status_code == 429:
            # Rejected admission is safe to retry; never retry uncertain provider mutations.
            time.sleep(min(int(r.headers.get("Retry-After", "60")), 60))
            r = self.client.request(method, path, headers=headers, json=body)
        data = r.json() if r.content else {}
        if "api_key" in data:
            data["api_key"] = Secret(data["api_key"])
        self.report["http"].append({"method": method, "path": redacted(path),
                                    "status": r.status_code, "error": data.get("error")})
        assert r.status_code in expected, f"{method} {redacted(path)} -> {r.status_code}: {data.get('error')}"
        return data

    def allowance(self, key, label):
        wallet = self.call("GET", "/v1/me/wallet", key)["wallets"][0]
        assert wallet["is_real_money"] is False
        a = wallet["allowance"]
        assert Decimal(a["committed"]) == sum(Decimal(a[k]) for k in ("reserved", "pending", "spent"))
        assert Decimal(a["available"]) + Decimal(a["committed"]) == Decimal(wallet["limits"]["monthly_budget"])
        self.report["allowances"].append({"step": label, **a})
        return a

    def onboard(self):
        agents = []
        for name, role, caps, budget, limit in [
            ("T1 integrator", "external", CAPS, "10000.00", "5000.00"),
            ("T1 researcher", "research", ["wallet.read", "research.fetch"], "50.00", "5.00"),
            ("T1 payer", "payment", CAPS, "100.00", "5.00"),
        ]:
            reg = self.call("POST", "/v1/agents", body={"name": name, "role": role, "capabilities": caps})
            aid, key = reg["agent"]["agent_id"], reg["api_key"]
            wallet = self.call("POST", "/v1/wallets", body={"agent_id": aid, "currency": "USD",
                               "monthly_budget": budget, "per_transaction_limit": limit})
            agents.append((aid, key, wallet["wallet_id"]))
            self.call("GET", "/v1/me", key)
            self.call("GET", f"/v1/agents/{aid}/permissions")
        aid, old, wid = agents[0]
        reg = self.call("POST", f"/v1/agents/{aid}/credentials", body={"overlap_seconds": 0})
        self.call("GET", "/v1/me", old, expected=(401,))
        key = reg["api_key"]
        self.call("GET", "/v1/me", key)
        self.call("DELETE", f"/v1/agents/{aid}/credentials/{reg['key_id']}")
        self.call("GET", "/v1/me", key, expected=(401,))
        reg = self.call("POST", f"/v1/agents/{aid}/credentials", body={"overlap_seconds": 0})
        agents[0] = (aid, reg["api_key"], wid)
        key = reg["api_key"]
        self.call("POST", f"/v1/wallets/{wid}/freeze")
        self.call("POST", "/v1/payment-requests", key, self.payment_body(), key=uuid.uuid4().hex, expected=(409, 403))
        self.call("POST", f"/v1/wallets/{wid}/unfreeze")
        self.call("POST", f"/v1/agents/{aid}/suspend")
        self.call("GET", "/v1/me", key, expected=(401,))
        self.call("POST", f"/v1/agents/{aid}/reactivate")
        self.call("GET", "/v1/me", key)
        self.call("POST", "/v1/payment-requests", agents[1][1], self.payment_body(),
                  key=uuid.uuid4().hex, expected=(403,))
        self.report["passes"].append("Three roles/wallets; rotation, revocation, freeze/unfreeze, suspension and capability denial")
        return agents

    @staticmethod
    def payment_body(**extra):
        return {"amount": "1.00", "currency": "USD", "merchant_name": "T1 sandbox office",
                "merchant_domain": "northwind.example", "purpose": "T1 labelled sandbox acceptance", **extra}

    def market(self, key):
        cases = [
            ("Apple MacBook Air M3 13 inch", "1099.00", "bestbuy.com", None),
            ("Lenovo ThinkPad X1 Carbon Gen 12", "2499.00", "lenovo.com", None),
            ("Logitech MX Master 3S wireless mouse", "250.00", "shady-deals.example", None),
            ("Logitech MX Anywhere 3S", "79.99", "bestbuy.com", None),
            ("Sony WH-1000XM5 headphones", "20.00", "sony.com", None),
            ("Bose QuietComfort Ultra Headphones", "429.00", "bose.com", None),
            ("Dell UltraSharp U2723QE monitor", "999.00", "dell.com", None),
            ("Samsung Odyssey G5 27 monitor", "249.99", "samsung.com", None),
            ("Apple iPhone 16 128GB", "799.00", "apple.com", None),
            ("Google Pixel 9 128GB", "40.00", "wrong-merchant.example", None),
            ("Apple iPhone 16 128GB", "799.00", "apple.com", "https://www.apple.com/shop/buy-iphone/iphone-16"),
            ("Google Pixel 9 128GB", "40.00", "wrong-merchant.example", "https://store.google.com/product/pixel_9"),
        ]
        for query, price, domain, url in cases:
            body = self.payment_body(amount=price, merchant_name=domain, merchant_domain=domain,
                                     purpose="Office purchase: " + query, product_query=query)
            if url:
                body["product_url"] = url
            idem = uuid.uuid4().hex
            p = self.call("POST", "/v1/payment-requests", key, body, key=idem)
            rid = p["request_id"]
            check = p.get("market_check")
            before = self.call("GET", f"/v1/payment-requests/{rid}")
            refreshed = self.call("POST", f"/v1/payment-requests/{rid}/market-check", expected=(200, 409))
            after = self.call("GET", f"/v1/payment-requests/{rid}")
            assert before["terms_sha256"] == after["terms_sha256"]
            assert before["status"] == after["status"] == "awaiting_approval"
            assert refreshed.get("advisory_only", True) is True
            replay = self.call("POST", "/v1/payment-requests", key, body, key=idem)
            assert replay["request_id"] == rid
            self.report["market"].append({"query": query, "claimed": price, "merchant": domain,
                                           "url_lookup": bool(url), "initial": check,
                                           "refresh": refreshed, "status_unchanged": after["status"]})
            self.call("POST", f"/v1/payment-requests/{rid}/reject")
            print("Market checked:", query, check.get("advisory_level"), flush=True)
        self.report["passes"].append("12 real-product requests, owner refresh and historical replay preserve exact terms/approval; rejected holds released")

    def purchase(self, key, persona):
        body = self.payment_body(merchant_name=persona["name"], merchant_paypal_email=persona["email"])
        p = self.call("POST", "/v1/payment-requests", key, body, key=uuid.uuid4().hex)
        rid = p["request_id"]
        self.allowance(key, persona["name"] + " reserved")
        self.call("POST", f"/v1/payment-requests/{rid}/execute", key, expected=(409,))
        self.call("POST", f"/v1/payment-requests/{rid}/approve", key,
                  {"terms_sha256": p["terms_sha256"]}, expected=(403,))
        self.call("POST", f"/v1/payment-requests/{rid}/approve", body={"terms_sha256": "0" * 64}, expected=(409,))
        self.call("POST", f"/v1/payment-requests/{rid}/approve", body={"terms_sha256": p["terms_sha256"]})
        self.call("POST", f"/v1/payment-requests/{rid}/execute", key)
        self.allowance(key, persona["name"] + " pending")
        ref = self.call("GET", f"/v1/payment-requests/{rid}/reconciliation")
        from agentpal.demo.sandbox_buyer import approve_sandbox_buyer
        references = ref["provider_references"]
        result = approve_sandbox_buyer(references[0]["payer_action_url"], timeout_s=90)
        print("Buyer harness:", persona["name"], result.status, result.reason, flush=True)
        self.report["buyer"].append({"merchant": persona["name"], "result": result.status, "reason": result.reason})
        assert result.status == "approved", result.reason
        for _ in range(10):
            response = self.client.post(f"/v1/payment-requests/{rid}/capture",
                                       headers={"Authorization": "Bearer " + self.owner})
            self.report["http"].append({"method": "POST", "path": redacted(f"/v1/payment-requests/{rid}/capture"),
                                        "status": response.status_code, "error": response.json().get("error")})
            if response.status_code == 200:
                break
            assert response.status_code == 409 and response.json().get("error") == "order_not_approved"
            time.sleep(3)
        assert response.status_code == 200
        p = self.call("GET", f"/v1/payment-requests/{rid}")
        assert p["status"] == "provider_confirmed"
        self.allowance(key, persona["name"] + " captured")
        recon = self.call("POST", f"/v1/payment-requests/{rid}/reconciliation")
        self.report["purchases"].append({"merchant": persona["name"], "request_id": rid,
                                        "status": p["status"], "reconciliation": recon})
        return rid

    def refunds(self, key, rid):
        for amount in ("0.40", "0.60"):
            body = {"amount": amount, "reason": "T1 sandbox acceptance refund"}
            idem = uuid.uuid4().hex
            r = self.call("POST", f"/v1/payment-requests/{rid}/refunds", body=body, key=idem)
            self.report["refunds"].append(r)
            assert r["status"] == "COMPLETED"
            before = self.allowance(key, "refund " + amount)
            replay = self.call("POST", f"/v1/payment-requests/{rid}/refunds", body=body, key=idem)
            assert replay["refund_id"] == r["refund_id"]
            assert before == self.allowance(key, "refund replay " + amount)
            self.call("POST", f"/v1/payment-requests/{rid}/refunds/reconciliation")
        self.call("GET", f"/v1/payment-requests/{rid}/refunds")
        assert self.call("GET", f"/v1/payment-requests/{rid}")["status"] == "reversed"

    def payout(self, key, persona):
        p = self.call("POST", "/v1/payout-requests", key, {
            "receiver_email": persona["email"], "amount": "1.00", "currency": "USD",
            "purpose": "T1 sandbox freelancer payment", "note": "T1 acceptance"}, key=uuid.uuid4().hex)
        rid = p["request_id"]
        self.allowance(key, persona["name"] + " payout reserved")
        self.call("POST", f"/v1/payout-requests/{rid}/approve", body={"terms_sha256": p["terms_sha256"]})
        p = self.call("POST", f"/v1/payout-requests/{rid}/execute", key)
        for _ in range(60):
            if p["status"] in ("provider_confirmed", "failed"):
                break
            time.sleep(3)
            p = self.call("POST", f"/v1/payout-requests/{rid}/reconciliation")
        self.report["payouts"].append({"recipient": persona["name"], "result": p})
        assert p["status"] == "provider_confirmed"
        self.allowance(key, persona["name"] + " payout SUCCESS")

    async def sdk(self, base, key, persona):
        async with OwnerClient(base_url=base, credential=self.owner, timeout=65) as owner:
            async with AgentClient(base_url=base, credential=key, timeout=65) as agent:
                p = await agent.create_payment_request(amount="1.00", currency="USD",
                    merchant_name=persona["name"], merchant_paypal_email=persona["email"],
                    merchant_domain="clouddesk.example", purpose="T1 SDK purchase")
                rid = p["request_id"]
                await owner.approve_payment_request(rid, terms_sha256=p["terms_sha256"])
                # Avoid stranding a real order when the public SDK cannot capture it.
                if not callable(getattr(owner, "capture_payment_request", None)):
                    self.report["sdk"].append({"purchase": "BLOCKED", "reason": "OwnerClient lacks capture_payment_request",
                                                "request_id": rid})
                    await owner.reject_payment_request(rid)
                else:
                    await agent.execute_payment_request(rid)
                p = await agent.create_payout_request(receiver_email=persona["payout_email"], amount="1.00",
                    currency="USD", note="T1 SDK", purpose="T1 SDK freelancer payment")
                rid = p["request_id"]
                await owner.approve_payout_request(rid, terms_sha256=p["terms_sha256"])
                p = await agent.execute_payout_request(rid)
                for _ in range(60):
                    if p["status"] in ("provider_confirmed", "failed"):
                        break
                    await asyncio.sleep(3)
                    p = await owner.reconcile_payout_request(rid)
                self.report["sdk"].append({"payout": p})
                assert p["status"] == "provider_confirmed"
                await agent.get_wallet()
                await owner.list_disputes()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--directory", type=Path, default=Path("/tmp/e2e-t1") / ("run-" + uuid.uuid4().hex[:8]))
    args = parser.parse_args()
    if args.live:
        required = ("PAYPAL_SANDBOX_CLIENT_ID", "PAYPAL_SANDBOX_CLIENT_SECRET", "CHANNEL3_API_KEY",
                    "PAYPAL_SANDBOX_BUYER_EMAIL", "PAYPAL_SANDBOX_BUYER_PASSWORD")
        if any(not os.environ.get(k) for k in required):
            print("BLOCKED: live credentials missing (values suppressed)")
            return 2
    report = {k: [] for k in ("http", "passes", "allowances", "market", "buyer", "purchases", "refunds", "payouts", "sdk")}
    report.update(live=args.live, started_at=datetime.now(timezone.utc).isoformat())
    try:
        with server(args.directory, live=args.live) as (client, token, base):
            j = Journey(client, token, report)
            agents = j.onboard()
            key = agents[0][1]
            j.allowance(key, "initial")
            if args.live:
                j.market(key)
                j.allowance(key, "market holds rejected")
                people = json.loads((ROOT / "agentpal/demo/paypal_sandbox_personas.json").read_text())["personas"]
                for name in ("Northwind", "Bluepeak", "CloudDesk"):
                    persona = next(p for p in people if p["name"].startswith(name))
                    rid = j.purchase(key, persona)
                    if name == "Northwind":
                        j.refunds(key, rid)
                for name in ("Priya", "Tom"):
                    j.payout(key, next(p for p in people if p["name"].startswith(name)))
                persona = next(p for p in people if p["name"].startswith("CloudDesk"))
                persona["payout_email"] = next(p["email"] for p in people if p["name"].startswith("Priya"))
                asyncio.run(j.sdk(base, key, persona))
                j.allowance(key, "final including SDK")
                j.call("GET", "/v1/disputes")
                now = datetime.now(timezone.utc)
                recon = j.call("GET", "/v1/paypal/reconciliation?" + str(httpx.QueryParams({
                    "start": (now - timedelta(hours=1)).isoformat(), "end": now.isoformat()})))
                report["global_reconciliation"] = recon
                j.call("GET", f"/v1/wallets/{agents[0][2]}/transactions")
            report["finished"] = True
    except Exception as exc:
        # SDK exceptions and assertion text are constant/sanitized; arbitrary exception text is not safe.
        report["failure"] = {"type": type(exc).__name__, "detail": str(exc) if isinstance(exc, AssertionError) else "suppressed"}
        raise
    finally:
        target = args.directory / "evidence.json"
        target.write_text(json.dumps(redacted(report), indent=2, default=str) + "\n")
        print("Redacted evidence:", target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
