"""Continue a T1 run after a provider refusal; never re-execute an existing order.

Usage from agentpal/: python tests/e2e/t1/continue_live.py /tmp/e2e-t1/run-...
Creates at most four new $1 sandbox orders (two personas, Test Store, Newman).
"""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import uuid

import httpx

from run_acceptance import Journey, ROOT, redacted, server
from run_postman import run as run_postman


def capture_read_diagnostic(j, rid):
    """Authenticated read only; disclose allowlisted error names, never bodies/IDs."""
    from agentpal.providers.config import load_config_from_env
    from agentpal.providers.paypal_sandbox import PayPalSandboxProvider
    provider = PayPalSandboxProvider(load_config_from_env())
    try:
        refs = j.call("GET", f"/v1/payment-requests/{rid}/reconciliation")["provider_references"]
        order = refs[0]["provider_order_id"]
        headers = {"Authorization": "Bearer " + provider.access_token()}
        base = "https://api-m.sandbox.paypal.com"
        r = provider.http_client.get(base + "/v2/checkout/orders/" + order, headers=headers)
        data = r.json()
        capture = data["purchase_units"][0]["payments"]["captures"][0]["id"]
        r = provider.http_client.get(base + "/v2/payments/captures/" + capture, headers=headers)
        error = r.json().get("name")
        return {"order_id": order, "capture_id": capture, "capture_read_http": r.status_code,
                "error": error if error in ("NOT_AUTHORIZED", "RESOURCE_NOT_FOUND", "AUTHENTICATION_FAILURE") else None}
    finally:
        provider.http_client.close()


def main(directory):
    report = json.loads((directory / "evidence.json").read_text())
    report.pop("failure", None)
    try:
        with server(directory, live=True, resume=True) as (client, owner, base):
            j = Journey(client, owner, report)
            agents = j.call("GET", "/v1/agents")["agents"]
            aid = next(x["agent_id"] for x in agents if x["name"] == "T1 integrator")
            rotated = j.call("POST", f"/v1/agents/{aid}/credentials", body={"overlap_seconds": 0})
            key = rotated["api_key"]
            payments = j.call("GET", "/v1/payment-requests")["payment_requests"]
            northwind = next(x for x in payments if x["terms"]["merchant_name"] == "Northwind Office Supply")
            report["refund_diagnostic"] = capture_read_diagnostic(j, northwind["request_id"])
            report["refund_blocks"] = ["Northwind $0.40 refund FAILED, spent remains 1.00; no credit invented"]
            people = json.loads((ROOT / "agentpal/demo/paypal_sandbox_personas.json").read_text())["personas"]
            for name in ("Bluepeak", "CloudDesk"):
                persona = next(p for p in people if p["name"].startswith(name))
                rid = j.purchase(key, persona)
                if name == "Bluepeak":
                    refund = j.call("POST", f"/v1/payment-requests/{rid}/refunds",
                                    body={"reason": "T1 full sandbox refund"}, key=uuid.uuid4().hex)
                    report["refunds"].append(refund)
                    j.allowance(key, "Bluepeak full refund " + refund["status"])
                    report["bluepeak_refund_diagnostic"] = capture_read_diagnostic(j, rid)
            rid = j.purchase(key, {"name": "App-linked Test Store", "email": None})
            j.refunds(key, rid)
            for name in ("Priya", "Tom"):
                j.payout(key, next(p for p in people if p["name"].startswith(name)))
                print("Payout:", name, "provider_confirmed", flush=True)
            persona = dict(next(p for p in people if p["name"].startswith("CloudDesk")))
            persona["payout_email"] = next(p["email"] for p in people if p["name"].startswith("Priya"))
            asyncio.run(j.sdk(base, key, persona))
            j.allowance(key, "final including SDK")
            report["disputes"] = j.call("GET", "/v1/disputes")
            now = datetime.now(timezone.utc)
            report["global_reconciliation"] = j.call("GET", "/v1/paypal/reconciliation?" + str(httpx.QueryParams({
                "start": (now - timedelta(hours=1)).isoformat(), "end": now.isoformat()})))
            report["postman"] = run_postman(j, base)
            report["finished"] = True
    except Exception as exc:
        report["failure"] = {"type": type(exc).__name__, "detail": str(exc) if isinstance(exc, AssertionError) else "suppressed"}
        raise
    finally:
        target = directory / "continuation.json"
        target.write_text(json.dumps(redacted(report), indent=2, default=str) + "\n")
        print("Redacted continuation evidence:", target)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
