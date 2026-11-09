"""Explicit, bounded LIVE sandbox reliability session (3 orders, one payout max).

Run from agentpal/: python tests/e2e/t4/run_live.py --live
Secrets remain in memory. Only final-four identifier suffixes enter the report.
"""
from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "sdk/python"), str(Path(__file__).parent)]

import httpx  # noqa: E402
from support import Server  # noqa: E402
from agentpal.demo.sandbox_buyer import approve_sandbox_buyer  # noqa: E402
from agentpal_sdk import OwnerClient, AgentClient  # noqa: E402


def emit(stage, **data):
    print(json.dumps({"stage": stage, **data}), flush=True)


def kill_at_claim(server, path, predicate, *, token=None, delay=.2, key=None, body=None):
    pool = ThreadPoolExecutor(max_workers=1)
    headers = {"Authorization": "Bearer " + (token or server.owner)}
    if key:
        headers["Idempotency-Key"] = key
    future = pool.submit(server.client.post, path, headers=headers, json=body)
    deadline = time.monotonic() + 15
    while not predicate():
        if future.done() or time.monotonic() > deadline:
            pool.shutdown()
            raise AssertionError("Live operation did not reach its durable claim")
        time.sleep(.005)
    time.sleep(delay)
    completed_before_kill = future.done()
    server.stop(kill=True)
    try:
        future.result(timeout=4)
    except httpx.HTTPError:
        pass
    finally:
        pool.shutdown()
    server.start()
    return {"claim_observed": True, "response_finished_before_kill": completed_before_kill,
            "delay_after_claim_s": delay}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", required=True, action="store_true")
    parser.parse_args()
    required = ("PAYPAL_SANDBOX_CLIENT_ID", "PAYPAL_SANDBOX_CLIENT_SECRET",
                "PAYPAL_SANDBOX_BUYER_EMAIL", "PAYPAL_SANDBOX_BUYER_PASSWORD")
    if any(not os.environ.get(k) for k in required):
        emit("session", status="BLOCKED", reason="sandbox credentials absent")
        return 2
    root = Path("/tmp/e2e-t4")
    root.mkdir(exist_ok=True, mode=0o700)
    directory = Path(tempfile.mkdtemp(prefix="live-", dir=root))
    server = Server(directory, live=True).start()
    start = datetime.now(timezone.utc) - timedelta(minutes=1)
    credentials = [server.owner]
    try:
        async def sdk_setup():
            async with OwnerClient(base_url=server.base, credential=server.owner) as owner:
                reg = await owner.register_agent(name="T4 SDK live recovery agent", capabilities=[
                    "wallet.read", "payment.prepare", "owner.request_decision", "payment.execute_sandbox", "payout.request"])
                await owner.create_wallet(agent_id=reg["agent"]["agent_id"], currency="USD",
                                          monthly_budget="10.00", per_transaction_limit="5.00")
            async with AgentClient(base_url=server.base, credential=reg["api_key"]) as agent:
                payment = await agent.create_payment_request(amount="1.00", currency="USD",
                    merchant_name="Sandbox Test Store", merchant_domain="example.com",
                    purpose="T4 sandbox crash after owner approval", idempotency_key="t4-live-approved",
                    product_query="Logitech MX Master 3S wireless mouse")
            return reg["agent"]["agent_id"], reg["api_key"], payment
        aid, key, payment = asyncio.run(sdk_setup())
        credentials.append(key)
        path = "/v1/payment-requests/" + payment["request_id"]
        server.approve(payment)
        held = server.allowance(key)
        server.restart()
        assert server.allowance(key) == held
        assert server.call("GET", path).json()["status"] == "approved"
        server.call("POST", path + "/execute", token=key)
        ref = server.call("GET", path + "/reconciliation").json()["provider_references"][0]
        buyer = approve_sandbox_buyer(ref["payer_action_url"])
        assert buyer.status == "approved", buyer.reason
        for _ in range(8):
            result = server.call("POST", path + "/capture", expected=(200, 409))
            if result.status_code == 200 and result.json()["status"] == "provider_confirmed":
                break
            time.sleep(2)
        assert result.json()["status"] == "provider_confirmed"
        emit("live_crash_after_approval", order="…" + ref["provider_order_id"][-4:],
             status="provider_confirmed", amount="1.00", sdk_tcp=True,
             market_level=payment.get("market_check", {}).get("advisory_level"),
             verified_outcomes=len(server.ledger("SELECT * FROM payment_outcomes WHERE verified=1")))

        # Kill a live refund after its durable claim. A remote system cannot expose
        # our precise provider-acceptance instant: deterministic offline gates do.
        crash = kill_at_claim(server, path + "/refunds",
            lambda: bool(server.wallet_rows("SELECT * FROM platform_paypal_refunds")),
            delay=.6, key="t4-live-refund", body={"amount": "0.40"})
        for _ in range(3):
            refunds = server.call("POST", path + "/refunds/reconciliation").json()
            if Decimal(refunds["refunded_total"]) == Decimal("0.40"):
                break
            time.sleep(2)
        emit("live_crash_during_refund", **crash, refunded=refunds["refunded_total"],
             reserved=refunds["reserved_total"], spent=server.allowance(key)["spent"],
             refund_statuses=[r["status"] for r in refunds["refunds"]])
        server.restart()
        assert Decimal(server.allowance(key)["committed"]) in (Decimal("1"), Decimal("0.60"))

        second = server.payment(key, key="t4-live-before-capture")
        p2 = "/v1/payment-requests/" + second["request_id"]
        server.approve(second)
        server.call("POST", p2 + "/execute", token=key)
        ref2 = server.call("GET", p2 + "/reconciliation").json()["provider_references"][0]
        buyer = approve_sandbox_buyer(ref2["payer_action_url"])
        assert buyer.status == "approved", buyer.reason
        time.sleep(3)
        server.restart()
        recon = server.call("POST", p2 + "/reconciliation").json()
        capture = server.call("POST", p2 + "/capture", expected=(409,))
        emit("live_crash_after_buyer_before_capture", order="…" + ref2["provider_order_id"][-4:],
             status=recon["status"], capture_http=capture.status_code,
             committed=server.allowance(key)["committed"],
             capture_operations=server.ledger("SELECT count(*) AS n FROM provider_operations WHERE request_id=? AND operation='capture'",
                                              (second["request_id"],))[0]["n"])

        third = server.payment(key, key="t4-live-during-execute")
        p3 = "/v1/payment-requests/" + third["request_id"]
        server.approve(third)
        crash = kill_at_claim(server, p3 + "/execute", token=key, delay=.2,
            predicate=lambda: bool(server.ledger("SELECT * FROM provider_operations WHERE request_id=?", (third["request_id"],))))
        server.call("POST", p3 + "/execute", token=key, expected=(409,))
        emit("live_crash_during_execute", **crash,
             status=server.call("GET", p3).json()["status"],
             create_operations=server.ledger("SELECT count(*) AS n FROM provider_operations WHERE request_id=?", (third["request_id"],))[0]["n"],
             committed=server.allowance(key)["committed"])

        payout = server.call("POST", "/v1/payout-requests", token=key, expected=(201,), key="t4-live-payout",
            json={"amount": "1.00", "currency": "USD", "receiver_email": "agentpal.priya.cbb860@personal.example.com",
                  "purpose": "T4 sandbox crash during payout", "note": "T4 sandbox"}).json()
        server.approve(payout, payout=True)
        pp = "/v1/payout-requests/" + payout["request_id"]
        crash = kill_at_claim(server, pp + "/execute", token=key, delay=.5,
            predicate=lambda: bool(server.ledger("SELECT * FROM provider_operations WHERE request_id=?", (payout["request_id"],))))
        replay = server.call("POST", pp + "/execute", token=key).json()
        recovery = server.call("POST", pp + "/reconciliation", expected=(200, 409))
        emit("live_crash_during_payout", **crash, status=replay["status"],
             recovery_http=recovery.status_code, committed=server.allowance(key)["committed"])

        report = server.call("GET", "/v1/paypal/reconciliation", params={"start": start.isoformat(),
            "end": datetime.now(timezone.utc).isoformat()}).json()
        emit("busy_session_report", rows=[{"lifecycle": r["lifecycle"], "result": r["result"], "reason": r["reason"]}
                                        for r in report["rows"]], read_only=report["read_only"])
        leaked_logs = any(c.encode() in server.logs for c in credentials)
        secret_values = [os.environ[k] for k in required if os.environ.get(k)]
        leaked_logs |= any(c.encode() in server.logs for c in secret_values)
        leaked_database = any(c.encode() in p.read_bytes() for c in credentials for p in directory.glob("*.db"))
        emit("integrity", logs_contain_credentials=leaked_logs, db_contains_plaintext_api_credentials=leaked_database,
             database_checks={p.name: server.ledger("PRAGMA integrity_check")[0] if p.name == "ledger.db" else "not_checked"
                              for p in directory.glob("*.db")},
             database_directory=str(directory), maximum_orders_requested=3, channel3_paid_searches_maximum=1)
        assert not leaked_logs and not leaked_database
        return 0
    finally:
        server.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # No traceback: an SDK registration response can contain one-time keys.
        emit("session", status="IMPLEMENTED — UNVERIFIED", error_type=type(exc).__name__)
        raise SystemExit(1) from None
