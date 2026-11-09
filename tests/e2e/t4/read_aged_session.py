"""Read-only PayPal SANDBOX comparison for a preserved T4 session.

python tests/e2e/t4/read_aged_session.py --directory /tmp/e2e-t4/live-7jyfeett
Only authenticated GETs and OAuth; no order/capture/refund/payout mutation.
Identifiers are redacted; unrelated account transactions are never printed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from agentpal.providers.config import load_config_from_env, ProviderConfigError  # noqa: E402
from agentpal.providers.paypal_reporting import PayPalReporting, ReportingError  # noqa: E402
from agentpal.providers.paypal_sandbox import PayPalSandboxProvider  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    args = parser.parse_args()
    with sqlite3.connect((args.directory / "wallet.db").resolve().as_uri() + "?mode=ro", uri=True) as conn:
        dates = conn.execute("SELECT min(created_at),max(created_at) FROM platform_payment_meta").fetchone()
        claims = conn.execute("SELECT request_id,capture_id,provider_refund_id FROM platform_paypal_refunds").fetchall()
    owned_ids = {identifier for _request, capture, refund in claims for identifier in (capture, refund) if identifier}
    request_ids = {request for request, _capture, _refund in claims}
    with sqlite3.connect((args.directory / "ledger.db").resolve().as_uri() + "?mode=ro", uri=True) as conn:
        orders = [r[0] for r in conn.execute("SELECT provider_order_id FROM provider_orders WHERE "
            "provider='paypal' AND provider_order_id IS NOT NULL")]
        local_net = sum((Decimal(amount) for request, amount in conn.execute(
            "SELECT request_id,amount FROM ledger_entries WHERE state='completed'")
            if request in request_ids), Decimal(0))
    start = datetime.fromisoformat(dates[0]) - timedelta(minutes=2)
    end = datetime.fromisoformat(dates[1]) + timedelta(minutes=2)
    provider = PayPalSandboxProvider(load_config_from_env())
    try:
        reader = PayPalReporting(provider)
        for order_id in orders:
            order = reader.order(order_id)
            print(json.dumps({"order": "…" + order_id[-4:],
                              "status": "HTTP 404" if order is None else order.get("status")}))
        transactions = reader.transactions(start, end)
        owned = [row for row in transactions if row.get("transaction_id") in owned_ids]
        exact = (bool(owned_ids) and len(owned) == len(owned_ids)
                 and {r["transaction_id"] for r in owned} == owned_ids
                 and all(r.get("transaction_status") == "S"
                         and r.get("transaction_amount", {}).get("currency_code") == "USD" for r in owned))
        provider_net = (sum((Decimal(r["transaction_amount"]["value"]) for r in owned), Decimal(0))
                        if exact else None)
        matched = exact and provider_net == local_net
        print(json.dumps({"aged_search": "matched" if matched else "UNVERIFIED",
            "window_start": start.isoformat(), "window_end": end.isoformat(),
            "owned_matches": [{"id": "…" + row["transaction_id"][-4:],
                "status": row.get("transaction_status"), "amount": row["transaction_amount"]["value"]}
                for row in owned], "provider_net": str(provider_net), "local_net": str(local_net),
            "is_real_money": False, "read_only": True}))
        return 0 if matched else 1
    finally:
        provider.http_client.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ProviderConfigError, ReportingError, sqlite3.Error, OSError, ValueError) as exc:
        print(json.dumps({"status": "BLOCKED", "error_type": type(exc).__name__,
                          "http_status": getattr(exc, "status", None)}))
        raise SystemExit(2) from None
