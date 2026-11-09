"""Observe only provider HTTP statuses for a governed refund of T1's prior capture.

This is a diagnostic after the TCP reproduction. It uses the real wired ASGI app,
existing T1 stores, and a response hook that NEVER reads provider bodies or alters
requests. No new order is created. All financial mutation still enters the owner API.
"""
import asyncio
import json
from pathlib import Path
import sys
import uuid

import httpx

from run_acceptance import ROOT, redacted


async def diagnose(directory):
    from agentpal.api import create_app
    from agentpal.config import Settings
    from agentpal.platform.wiring import build_platform

    settings = Settings(db_path=str(directory / "ledger.db"), auth_mode="required")
    deps = build_platform(settings=settings, identity_db_path=directory / "identity.db",
                          wallet_db_path=directory / "wallet.db")
    token, _ = deps.owner_authenticator.issue_token("owner_t1")
    seen = []

    def observe(response):
        path = response.request.url.path
        if path.startswith("/v2/payments/captures/"):
            seen.append({"method": response.request.method, "path": redacted(path),
                         "http": response.status_code})

    deps.provider.http_client.event_hooks["response"].append(observe)
    app = create_app(service=deps.risk_service, settings=settings, platform=deps)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1", headers={"Authorization": "Bearer " + token}) as client:
            payments = (await client.get("/v1/payment-requests")).json()["payment_requests"]
            p = next(p for p in payments if p["terms"]["merchant_name"] == "Northwind Office Supply")
            r = await client.post(f"/v1/payment-requests/{p['request_id']}/refunds",
                headers={"Idempotency-Key": uuid.uuid4().hex},
                json={"amount": "0.40", "reason": "T1 governed provider diagnostic"})
            result = {"api_http": r.status_code, "refund_status": r.json().get("status"), "provider": seen}
            (directory / "refund-diagnostic.json").write_text(json.dumps(result, indent=2) + "\n")
            print(json.dumps(result))
    finally:
        deps.close()


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    asyncio.run(diagnose(Path(sys.argv[1])))
