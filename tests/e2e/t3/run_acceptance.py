"""Explicit live TCP acceptance run; never executes or approves a payment.

From agentpal/: python tests/e2e/t3/run_acceptance.py
Needs CHANNEL3_API_KEY. Credentials stay in memory. Maximum five paid searches.
The checked-in labels are verified before starting; a fresh /tmp/e2e-t3 is required.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "sdk/python"))

from agentpal_sdk import AgentClient, OwnerClient  # noqa: E402
from apimatic.local_server import stop, wait_ready  # noqa: E402

BASE = "http://127.0.0.1:8103"
SCRATCH = Path("/tmp/e2e-t3")


def redacted(value):
    if isinstance(value, dict):
        return {k: ("…" + str(v)[-4:] if (k.endswith("_id") or k == "id") and v
                    else redacted(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redacted(v) for v in value]
    if isinstance(value, str) and "/v1/products/" in value:
        prefix, identifier = value.rsplit("/", 1)
        return prefix + "/…" + identifier[-4:]
    return value


def checked(response, status):
    if response.status_code != status:
        # Never reflect an untrusted body or exception containing credentials.
        raise RuntimeError(f"expected HTTP {status}, got {response.status_code}")
    return response.json()


async def exercise(token, scenarios):
    rows = []
    async with OwnerClient(base_url=BASE, credential=token, timeout=40) as owner:
        async with httpx.AsyncClient(base_url=BASE, trust_env=False, timeout=40) as wire:
            for case in scenarios:
                # Respect the default 60/min owner-token limit (two setup calls
                # per case plus occasional refresh/read), without changing it.
                await asyncio.sleep(3)
                registration = await owner.register_agent(name=case["case_id"])
                aid = registration["agent"]["agent_id"]
                key = registration["api_key"]
                await owner.create_wallet(agent_id=aid, currency=case["body"]["currency"],
                                          monthly_budget="10000", per_transaction_limit="5000")
                headers = {"Authorization": "Bearer " + key,
                           "Idempotency-Key": case["case_id"]}
                started = time.perf_counter()
                response = await wire.post("/v1/payment-requests", headers=headers,
                                           json=case["body"])
                elapsed = (time.perf_counter() - started) * 1000
                result = checked(response, 201)
                # This read is through the unmodified published SDK over real TCP.
                async with AgentClient(base_url=BASE, credential=key) as agent:
                    stored = await agent.get_payment_request(result["request_id"])
                assert stored == result
                assert result["requires_owner_approval"] is True
                assert result["status"] == "awaiting_approval"
                market = result["market_check"]
                assert market["advisory_only"] and not market["authoritative"]
                assert market["disclosure"] and market["verify_notice"] and market["switch_notice"]
                assert market["discount_codes_available"] is False
                assert all(d["currency"] == case["body"]["currency"] for d in market["better_deals"])
                assert all(d["condition"] in {"new", "used", "refurbished"}
                           for d in market["better_deals"])
                replay = await wire.post("/v1/payment-requests", headers=headers, json=case["body"])
                assert checked(replay, 200) == result
                assert replay.headers["Idempotent-Replayed"] == "true"
                row = {"case_id": case["case_id"], "label": case["label"], "kind": case["kind"],
                       "http_status": response.status_code, "replay_status": replay.status_code,
                       "latency_ms": round(elapsed, 2), "result": redacted(result)}
                # Refresh the first case in every family. The free product read must
                # leave all financial authority fields untouched.
                if case["kind"] == "fair":
                    before = {f: result[f] for f in ("terms", "terms_sha256", "status",
                                                    "risk_score", "recommendation")}
                    refresh = await owner.refresh_market_check(result["request_id"])
                    after = await owner.get_payment_request(result["request_id"])
                    assert before == {f: after[f] for f in before}
                    row["refresh_advisory"] = refresh["advisory_level"]
                    row["refresh_result"] = redacted(refresh)
                    row["refresh_unchanged_authority"] = True
                rows.append(row)
                (HERE / "live_results.json").write_text(json.dumps(rows, indent=2) + "\n")
                print(case["case_id"], response.status_code, result["risk_score"],
                      result["recommendation"], market["advisory_level"], flush=True)
    return rows


def main():
    if not os.environ.get("CHANNEL3_API_KEY", "").strip():
        print("BLOCKED: CHANNEL3_API_KEY absent")
        return 2
    labels = (HERE / "scenarios.json").read_bytes()
    lock = json.loads((HERE / "labels_lock.json").read_text())
    assert hashlib.sha256(labels).hexdigest() == lock["sha256"]
    SCRATCH.mkdir(exist_ok=True)
    identity_path = SCRATCH / "identity.db"
    if identity_path.exists():
        raise RuntimeError("Use fresh /tmp/e2e-t3 identity database; never silently reuse state")
    # Documented bootstrap CLI, captured in memory instead of printed/stored tokens.
    for command in (["create-owner", "--owner-id", "owner_t3"],
                    ["issue-token", "--owner-id", "owner_t3"]):
        proc = subprocess.run([sys.executable, "-m", "agentpal.identity", "--db",
                               str(identity_path), *command], cwd=ROOT,
                              capture_output=True, text=True, check=True)
    token = proc.stdout.strip()
    env = dict(os.environ)
    env.update({"AGENTPAL_DB_PATH": str(SCRATCH / "ledger.db"),
                "AGENTPAL_IDENTITY_DB_PATH": str(identity_path),
                "AGENTPAL_WALLET_DB_PATH": str(SCRATCH / "wallet.db"),
                "AGENTPAL_RATE_LIMIT_DB_PATH": str(SCRATCH / "ratelimit.db"),
                "AGENTPAL_ALLOWED_HOSTS": "127.0.0.1,localhost,::1",
                "AGENTPAL_LOG_LEVEL": "ERROR"})
    process = subprocess.Popen([sys.executable, "-m", "agentpal", "serve", "--platform",
                                "--host", "127.0.0.1", "--port", "8103"],
                               cwd=ROOT, env=env, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    try:
        wait_ready(process, BASE)
        asyncio.run(exercise(token, json.loads(labels)))
    finally:
        stop(process)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
