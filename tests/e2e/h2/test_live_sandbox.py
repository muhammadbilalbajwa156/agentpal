"""Explicit opt-in: ONE $1 purchase, bounded refunds, ONE $1 payout, no blind retries.

H2_RUN_LIVE=1 python -m pytest -o addopts='' -q -p no:cacheprovider \
    tests/e2e/h2/test_live_sandbox.py
Each invocation creates new sandbox objects. Do not loop this command.
"""
from decimal import Decimal
import json
import os
import time

import pytest

from .tcp import TCPWorld
from .test_money_integrity import parallel

REQUIRED = ("PAYPAL_SANDBOX_CLIENT_ID", "PAYPAL_SANDBOX_CLIENT_SECRET",
            "PAYPAL_SANDBOX_BUYER_EMAIL", "PAYPAL_SANDBOX_BUYER_PASSWORD")
pytestmark = pytest.mark.skipif(os.getenv("H2_RUN_LIVE") != "1" or not all(os.getenv(k) for k in REQUIRED),
    reason="H2 live sandbox requires explicit opt-in and sandbox app/buyer credentials")


@pytest.fixture(scope="module")
def live():
    world = TCPWorld(live=True)
    world.evidence = {"environment": "sandbox", "order_limit": 1, "payout_limit": 1,
                      "purchase_usd": "1.00", "payout_usd": "1.00", "checks": {}}
    try:
        yield world
    finally:
        (world.directory / "evidence.json").write_text(json.dumps(world.evidence, indent=2))
        world.close()


@pytest.fixture(scope="module")
def captured(live):
    from agentpal.demo.sandbox_buyer import approve_sandbox_buyer
    data, path = live.prepared(merchant_name="H2 Sandbox Test Store")
    checks = live.evidence["checks"]
    for actor, expected in (("a", 403), ("owner_b", 404)):
        assert live.call("POST", path + "/approve", actor,
                          json={"terms_sha256": data["terms_sha256"]}).status_code == expected
    assert live.call("POST", path + "/execute", "a").status_code == 409
    assert live.call("POST", path + "/approve", json={"terms_sha256": data["terms_sha256"]}).status_code == 200
    rs = parallel([lambda: live.call("POST", path + "/execute", "a") for _ in range(2)])
    checks["execute_race"] = sorted(r.status_code for r in rs)
    assert checks["execute_race"] == [200, 409]
    refs = live.call("GET", path + "/reconciliation").json()["provider_references"]
    assert len(refs) == 1
    live.evidence["order_suffix"] = refs[0]["provider_order_id"][-4:]
    assert live.call("POST", path + "/capture", "a").status_code == 403
    assert live.call("POST", path + "/capture", "owner_b").status_code == 404
    assert live.call("POST", path + "/capture").status_code == 409
    buyer = approve_sandbox_buyer(refs[0]["payer_action_url"], timeout_s=90)
    checks["buyer"] = buyer.status + ":" + buyer.reason
    assert buyer.status == "approved", buyer.reason
    # Retry only a definite preflight non-capture while payer approval propagates.
    # Never retry unknown outcomes. Once capture succeeds all later requests refuse.
    for _ in range(6):
        rs = parallel([lambda: live.call("POST", path + "/capture") for _ in range(2)])
        checks.setdefault("capture_races", []).append([r.status_code for r in rs])
        status = live.call("GET", path).json()["status"]
        if status != "provider_pending":
            break
        time.sleep(3)
    checks["capture_status"] = status
    assert status == "provider_confirmed"
    return data, path


def test_live_purchase_exact_owner_approval_and_one_capture(live, captured):
    _, path = captured
    assert live.call("POST", path + "/capture").status_code == 409
    assert Decimal(live.allowance()["spent"]) == 1
    live.evidence["checks"]["purchase"] = "verified provider_confirmed; duplicate capture refused"


def test_live_refund_bound_and_idempotency(live, captured):
    _, path = captured
    def refund(key, amount):
        return live.call("POST", path + "/refunds", json={"amount": amount},
                         headers={"Idempotency-Key": key})
    assert refund("h2-live-over-refund", "1.01").status_code == 409
    rs = parallel([lambda: refund("h2-live-exact-refund", "0.40") for _ in range(2)])
    assert sorted(r.status_code for r in rs) == [200, 201]
    assert len({r.json()["refund_id"] for r in rs}) == 1
    verified = next(r.json() for r in rs if r.status_code == 201)
    assert verified["status"] == "COMPLETED"
    assert refund("h2-live-exact-refund", "0.41").status_code == 409
    assert refund("h2-live-over-remainder", "0.61").status_code == 409
    last = refund("h2-live-refund-remainder", "0.60")
    assert last.status_code == 201 and last.json()["status"] == "COMPLETED"
    assert live.call("GET", path).json()["status"] == "reversed"
    assert Decimal(live.allowance()["spent"]) == 0
    live.evidence["refund_suffixes"] = [verified["provider_refund_id"][-4:],
                                         last.json()["provider_refund_id"][-4:]]
    live.evidence["checks"]["refunds"] = "0.40+0.60 verified; replay, changed body and excess refused"


def test_live_payout_exact_receiver_single_batch_and_verified_item(live):
    from agentpal.demo.payout_flow import PRIYA
    data, path = live.prepared("payout", receiver_email=PRIYA)
    assert live.call("POST", path + "/approve", "a", json={"terms_sha256": data["terms_sha256"]}).status_code == 403
    assert live.call("POST", path + "/execute", "a").status_code == 409
    assert live.call("POST", path + "/approve", json={"terms_sha256": data["terms_sha256"]}).status_code == 200
    rs = parallel([lambda: live.call("POST", path + "/execute", "a") for _ in range(2)])
    assert all(r.status_code in (200, 409) for r in rs)
    live.evidence["checks"]["payout_execute_race"] = [r.status_code for r in rs]
    # An in-flight local replay may be HTTP 200 before the winner saves its batch.
    # All workers have joined here; select the actual provider-bearing result.
    outcomes = [r.json() for r in rs if r.status_code == 200 and r.json()["provider_batch_id"]]
    assert outcomes and outcomes[0]["provider_batch_id"]
    outcome = outcomes[0]
    batch = outcome["provider_batch_id"]
    live.evidence["payout_batch_suffix"] = batch[-4:]
    # At most 20 read-only polls over a maximum of 60 seconds.
    for _ in range(20):
        if outcome["status"] not in ("provider_pending", "reconciliation_required"):
            break
        time.sleep(3)
        outcome = live.call("POST", path + "/reconciliation").json()
    assert outcome["status"] == "provider_confirmed"
    assert outcome["provider_item_status"] == "SUCCESS"
    replay = live.call("POST", path + "/execute", "a")
    assert replay.status_code == 200 and replay.json()["provider_batch_id"] == batch
    assert Decimal(live.allowance()["spent"]) == 1
    live.evidence["checks"]["payout"] = "SUCCESS item verified; replay uses same batch"
