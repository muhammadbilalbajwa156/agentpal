"""Independent authenticated reads of F1's own sandbox receipts; no mutations."""
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from agentpal.providers.config import load_config_from_env
from agentpal.providers.paypal_refunds import PayPalRefunds
from agentpal.providers.paypal_sandbox import PayPalSandboxProvider


def verify(directory):
    directory = Path(directory).resolve()
    if directory.parent != Path("/tmp/m8-f1") or not directory.name.startswith("live-"):
        raise ValueError("only this workstream's own live database directory is allowed")
    provider = PayPalSandboxProvider(load_config_from_env())
    result = {"orders": [], "refunds": [], "read_only": True}
    try:
        with sqlite3.connect(f"file:{directory / 'ledger.db'}?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            orders = db.execute('SELECT o.provider_order_id,p.terms_json,p.email,p.merchant_id '
                'FROM provider_orders o JOIN paypal_approved_terms p USING(agent_id,request_id) '
                "WHERE o.provider_order_id IS NOT NULL AND o.provider='paypal' "
                "AND o.last_status='completed'").fetchall()
            assert 0 < len(orders) <= 4
            for row in orders:
                terms = json.loads(row["terms_json"])
                event = provider.get_order_status(row["provider_order_id"])
                proof = event.order_evidence
                assert event.verified and proof is not None
                assert proof.amount == Decimal(terms["amount"]) and proof.currency == terms["currency"]
                assert proof.reference_id == terms["request_id"] and proof.custom_id == terms["request_id"]
                requested = terms.get("merchant_paypal_email")
                if requested:
                    assert proof.email == requested == row["email"]
                else:
                    assert proof.merchant_id == row["merchant_id"]
                result["orders"].append({"merchant": terms["merchant_name"],
                    "order_suffix": row["provider_order_id"][-4:], "verified": event.verified,
                    "exact_amount_currency_and_references": True, "pinned_payee_matches": True})
        with sqlite3.connect(f"file:{directory / 'ledger.db.wallet.db'}?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            refunds = db.execute("SELECT provider_refund_id,amount,currency FROM platform_paypal_refunds "
                                 "WHERE status='COMPLETED'").fetchall()
            assert len(refunds) == 2
            for row in refunds:
                read = PayPalRefunds(provider).read(row["provider_refund_id"])
                assert read["status"] == "COMPLETED"
                assert Decimal(read["amount"]["value"]) == Decimal(row["amount"])
                assert read["amount"]["currency_code"] == row["currency"]
                result["refunds"].append({"refund_suffix": row["provider_refund_id"][-4:],
                                          "amount": row["amount"], "verified_status": read["status"]})
        return result
    finally:
        provider.http_client.close()


if __name__ == "__main__":
    # No raw response, credential, full ID or exception text is ever printed.
    try:
        data = verify(sys.argv[1])
    except Exception:
        print("Receipt verification failed; provider details suppressed")
        raise SystemExit(1) from None
    Path("/tmp/m8-f1/receipt-evidence.json").write_text(json.dumps(data, indent=2) + "\n")
    print("Verified own order payees, exact references and refund receipts (GET/OAuth only)")
