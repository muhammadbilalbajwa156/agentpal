"""Explicit live accessory reproduction, one paid search, no payment execution.

Run after run_acceptance.py, with its databases retained in /tmp/e2e-t3.
"""
import asyncio
import json
import os
import subprocess
import sys

from run_acceptance import BASE, HERE, ROOT, SCRATCH, OwnerClient, redacted, stop, wait_ready
import httpx


async def probe(token):
    async with OwnerClient(base_url=BASE, credential=token) as owner:
        registration = await owner.register_agent(name="T3 accessory defect probe")
        await owner.create_wallet(agent_id=registration["agent"]["agent_id"], currency="USD",
                                  monthly_budget="1000", per_transaction_limit="500")
        async with httpx.AsyncClient(base_url=BASE, trust_env=False, timeout=40) as c:
            body = {"amount": "250.00", "currency": "USD", "merchant_name": "Apple",
                    "merchant_domain": "apple.com", "purpose": "Purchase AirPods Pro 2 earbuds",
                    "product_query": "Apple AirPods Pro 2"}
            response = await c.post("/v1/payment-requests", headers={
                "Authorization": "Bearer " + registration["api_key"],
                "Idempotency-Key": "t3-accessory-probe"}, json=body)
            assert response.status_code == 201
            payment = response.json()
            snapshot = payment["market_check"]
            (HERE / "live_accessory_probe.json").write_text(json.dumps({
                "http_status": response.status_code, "request": body,
                "result": redacted(payment)}, indent=2) + "\n")
            print("HTTP", response.status_code, "matched", snapshot["product"]["title"],
                  "confidence", snapshot["product"]["match_confidence"],
                  "median", snapshot["market"]["median"], snapshot["advisory_level"])


def main():
    if not os.environ.get("CHANNEL3_API_KEY", "").strip():
        print("BLOCKED: CHANNEL3_API_KEY absent")
        return 2
    issued = subprocess.run([sys.executable, "-m", "agentpal.identity", "--db",
                             str(SCRATCH / "identity.db"), "issue-token", "--owner-id", "owner_t3"],
                            cwd=ROOT, capture_output=True, text=True, check=True)
    env = dict(os.environ)
    env.update({"AGENTPAL_DB_PATH": str(SCRATCH / "ledger.db"),
                "AGENTPAL_IDENTITY_DB_PATH": str(SCRATCH / "identity.db"),
                "AGENTPAL_WALLET_DB_PATH": str(SCRATCH / "wallet.db"),
                "AGENTPAL_RATE_LIMIT_DB_PATH": str(SCRATCH / "ratelimit.db"),
                "AGENTPAL_ALLOWED_HOSTS": "127.0.0.1,localhost,::1",
                "AGENTPAL_LOG_LEVEL": "ERROR"})
    process = subprocess.Popen([sys.executable, "-m", "agentpal", "serve", "--platform",
                                "--host", "127.0.0.1", "--port", "8103"], cwd=ROOT,
                               env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_ready(process, BASE)
        asyncio.run(probe(issued.stdout.strip()))
    finally:
        stop(process)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
