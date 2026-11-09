"""Read back actual PayPal payees for T1's three already-completed purchases.

No financial mutation, new order or buyer action. Full identifiers and credentials
are kept in memory; output contains only exact amounts and redacted references.
"""
import json
from pathlib import Path
import sys

from run_acceptance import Journey, ROOT, redacted, server


def verify(directory):
    from agentpal.providers.config import load_config_from_env
    from agentpal.providers.paypal_sandbox import PayPalSandboxProvider

    people = json.loads((ROOT / "agentpal/demo/paypal_sandbox_personas.json").read_text())["personas"]
    expected = {p["name"]: p["email"] for p in people if p["name"] in (
        "Northwind Office Supply", "Bluepeak Electronics", "CloudDesk Software")}
    results = []
    with server(directory, live=True, resume=True) as (client, owner, _):
        j = Journey(client, owner, {"http": []})
        payments = j.call("GET", "/v1/payment-requests")["payment_requests"]
        provider = PayPalSandboxProvider(load_config_from_env())
        try:
            for p in payments:
                name = p["terms"]["merchant_name"]
                if name not in expected or p["status"] != "provider_confirmed":
                    continue
                assert p["merchant_paypal_email"] == expected[name]
                refs = j.call("GET", f"/v1/payment-requests/{p['request_id']}/reconciliation")["provider_references"]
                order = refs[0]["provider_order_id"]
                response = provider.http_client.get("https://api-m.sandbox.paypal.com/v2/checkout/orders/" + order,
                    headers={"Authorization": "Bearer " + provider.access_token()})
                assert response.status_code == 200
                data = response.json()
                unit = data["purchase_units"][0]
                assert unit["payee"]["email_address"].lower() == expected[name].lower()
                capture = unit["payments"]["captures"][0]
                assert capture["status"] == "COMPLETED"
                assert capture["amount"] == {"value": "1.00", "currency_code": "USD"}
                results.append({"merchant": name, "request_id": p["request_id"], "order_id": order,
                    "capture_id": capture["id"], "approved_email_matches_paypal_payee": True,
                    "provider_http": 200, "capture_status": capture["status"], "amount": capture["amount"]})
            assert len(results) == 3
        finally:
            provider.http_client.close()
    (directory / "verified-payees.json").write_text(json.dumps(redacted(results), indent=2) + "\n")
    print("Northwind, Bluepeak and CloudDesk: exact approved email matched authenticated PayPal payee; all three $1 captures COMPLETED")


if __name__ == "__main__":
    verify(Path(sys.argv[1]))
