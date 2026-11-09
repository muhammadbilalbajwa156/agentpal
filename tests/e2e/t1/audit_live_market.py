"""Extra live owner-queue/disclosure/risk invariants, one paid search, zero orders."""
from decimal import Decimal
import json
from pathlib import Path
import sys
import uuid

from run_acceptance import Journey, redacted, server


def audit(directory):
    report = {"http": [], "allowances": []}
    with server(directory, live=True, resume=True) as (client, owner, _):
        j = Journey(client, owner, report)
        agents = j.call("GET", "/v1/agents")["agents"]
        aid = next(x["agent_id"] for x in agents if x["name"] == "T1 integrator")
        key = j.call("POST", f"/v1/agents/{aid}/credentials", body={"overlap_seconds": 0})["api_key"]
        for old in j.call("GET", "/v1/payment-requests")["payment_requests"]:
            if old["terms"]["merchant_name"] == "Unlisted seller" and old["status"] == "awaiting_approval":
                j.call("POST", f"/v1/payment-requests/{old['request_id']}/reject")
        before = j.allowance(key, "before queue audit")
        body = j.payment_body(amount="250.00", merchant_domain="shady-deals.example",
            merchant_name="Unlisted seller", product_query="Logitech MX Master 3S wireless mouse")
        idem = uuid.uuid4().hex
        payment = j.call("POST", "/v1/payment-requests", key, body, key=idem)
        rid = payment["request_id"]
        market = payment["market_check"]
        assert market["advisory_only"] is True and market["authoritative"] is False
        assert market["discount_codes_available"] is False
        assert "commission" in market["disclosure"]
        assert market["verify_notice"] and market["switch_notice"]
        assert market["better_deals"]
        for deal in market["better_deals"]:
            assert Decimal(deal["you_save"]) == Decimal("250.00") - Decimal(deal["price"])
        approvals = j.call("GET", "/v1/approvals")["payment_requests"]
        listed = next(x for x in approvals if x["request_id"] == rid)
        assert listed["market_check"] == market
        j.call("POST", f"/v1/payment-requests/{rid}/market-check", key, expected=(403,))
        refreshed = j.call("POST", f"/v1/payment-requests/{rid}/market-check")
        current = j.call("GET", f"/v1/payment-requests/{rid}")
        for field in ("risk_score", "recommendation", "terms_sha256", "status", "requires_owner_approval"):
            assert current[field] == payment[field]
        replay = j.call("POST", "/v1/payment-requests", key, body, key=idem)
        assert replay == payment
        j.call("POST", f"/v1/payment-requests/{rid}/reject")
        assert j.allowance(key, "after queue audit") == before
        report.update(request_id=rid, market=market, refresh=refreshed,
                      queue_snapshot_equal=True, risk_and_terms_unchanged=True,
                      historical_replay_equal=True, allowance_restored=True)
    (directory / "market-audit.json").write_text(json.dumps(redacted(report), indent=2) + "\n")
    print("Owner queue, disclosure, savings, digest, risk, replay and restored allowance: PASS")


if __name__ == "__main__":
    audit(Path(sys.argv[1]))
