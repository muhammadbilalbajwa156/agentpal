"""Deterministic SIGKILL after external acceptance, before local response storage.

OFFLINE synthetic PayPal, real TCP server/owner approvals/SQLite services. These
cover exact crash windows that timing alone cannot establish with a remote API.
"""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
import time

import httpx
import pytest

from .support import Server


@pytest.fixture
def fake(tmp_path):
    server = Server(tmp_path, fake=True).start()
    try:
        yield server
    finally:
        server.close()


def provider(server):
    return json.loads((server.directory / "synthetic-provider.json").read_text())


def approve_buyer(server):
    path = server.directory / "synthetic-provider.json"
    data = json.loads(path.read_text())
    for order in data["orders"].values():
        if order["status"] == "CREATED":
            order["status"] = "APPROVED"
    path.write_text(json.dumps(data))


def prepared(server):
    aid, key, _wallet = server.agent()
    payment = server.payment(key)
    server.approve(payment)
    return aid, key, payment, "/v1/payment-requests/" + payment["request_id"]


def crash_after_provider_acceptance(server, operation, path, *, token=None, **kwargs):
    # Restart with a gate BEFORE executing the operation; no pending request yet
    # for execute/payout, while capture/refund gates are set before initial submit.
    pool = ThreadPoolExecutor(max_workers=1)
    headers = {"Authorization": "Bearer " + (token or server.owner)}
    future = pool.submit(server.client.post, path, headers=headers, **kwargs)
    deadline = time.monotonic() + 8
    try:
        while not (server.directory / "gate-hit").exists():
            if future.done() or time.monotonic() > deadline:
                response = future.result(timeout=1)
                raise AssertionError(f"Synthetic crash gate not reached: HTTP {response.status_code}; "
                                     f"code={response.json().get('error', 'none')}; "
                                     f"status={response.json().get('status', 'none')}")
            time.sleep(.01)
        assert (server.directory / "gate-hit").read_text() == operation
        server.stop(kill=True)
        try:
            future.result(timeout=3)
        except httpx.HTTPError:
            pass
    finally:
        pool.shutdown()
    server.env.pop("T4_GATE", None)
    server.start()


def gated(server, operation):
    server.env["T4_GATE"] = operation
    server.restart()


def test_crash_during_execute_never_reposts_or_releases_unknown_order(fake):
    _aid, key, _payment, path = prepared(fake)
    gated(fake, "execute")
    crash_after_provider_acceptance(fake, "execute", path + "/execute", token=key)
    assert len(provider(fake)["orders"]) == 1
    assert fake.call("GET", path).json()["status"] == "reconciliation_required"
    fake.call("POST", path + "/execute", token=key, expected=(409,))
    assert len(provider(fake)["posts"]) == 1
    assert Decimal(fake.allowance(key)["committed"]) == Decimal("1")
    fake.restart()
    assert Decimal(fake.allowance(key)["committed"]) == Decimal("1")


def test_crash_after_capture_acceptance_recovers_only_by_verified_reads(fake):
    gated(fake, "capture")
    aid, key, _payment, path = prepared(fake)
    fake.call("POST", path + "/execute", token=key)
    approve_buyer(fake)
    crash_after_provider_acceptance(fake, "capture", path + "/capture")
    assert fake.call("GET", path).json()["status"] == "reconciliation_required"
    posts = len(provider(fake)["posts"])
    for _ in range(2):
        assert fake.call("POST", path + "/reconciliation").json()["status"] == "provider_confirmed"
    assert len(provider(fake)["posts"]) == posts == 2
    assert Decimal(fake.allowance(key)["spent"]) == Decimal("1")
    assert len(fake.ledger("SELECT * FROM payment_outcomes WHERE agent_id=? AND verified=1", (aid,))) == 1


def test_crash_during_refund_discovers_lost_id_and_credits_exactly_once(fake):
    gated(fake, "refund")
    _aid, key, _payment, path = prepared(fake)
    fake.call("POST", path + "/execute", token=key)
    approve_buyer(fake)
    assert fake.call("POST", path + "/capture").json()["status"] == "provider_confirmed"
    # Need the stable Idempotency-Key along with owner authentication.
    original = fake.client.headers.get("Idempotency-Key")
    fake.client.headers["Idempotency-Key"] = "t4-lost-refund"
    try:
        crash_after_provider_acceptance(fake, "refund", path + "/refunds", json={"amount": "0.40"})
    finally:
        if original is None:
            del fake.client.headers["Idempotency-Key"]
        else:
            fake.client.headers["Idempotency-Key"] = original
    assert Decimal(fake.allowance(key)["spent"]) == Decimal("1")
    for _ in range(2):
        listing = fake.call("POST", path + "/refunds/reconciliation").json()
        assert Decimal(listing["refunded_total"]) == Decimal("0.40")
    assert Decimal(fake.allowance(key)["spent"]) == Decimal("0.60")
    posts = provider(fake)["posts"]
    assert sum(p.endswith("/refund") for p in posts) == 1
    fake.restart()
    assert Decimal(fake.allowance(key)["spent"]) == Decimal("0.60")


def test_crash_during_payout_preserves_hold_without_blind_resubmission(fake):
    gated(fake, "payout")
    _aid, key, _wallet = fake.agent()
    payout = fake.call("POST", "/v1/payout-requests", token=key, expected=(201,), key="t4-payout",
        json={"receiver_email": "t4-payee@example.com", "amount": "1.00", "currency": "USD",
              "purpose": "Synthetic crash acceptance", "note": "T4"}).json()
    fake.approve(payout, payout=True)
    path = "/v1/payout-requests/" + payout["request_id"]
    crash_after_provider_acceptance(fake, "payout", path + "/execute", token=key)
    assert fake.call("GET", path).json()["status"] == "reconciliation_required"
    assert fake.call("POST", path + "/reconciliation", expected=(409,)).json()["error"] == "payout_batch_unknown"
    assert fake.call("POST", path + "/execute", token=key).json()["status"] == "reconciliation_required"
    assert len(provider(fake)["payouts"]) == 1
    assert Decimal(fake.allowance(key)["committed"]) == Decimal("1")


def test_restart_between_buyer_approval_and_capture_fails_closed(fake):
    _aid, key, _payment, path = prepared(fake)
    fake.call("POST", path + "/execute", token=key)
    approve_buyer(fake)
    fake.restart()
    assert fake.call("POST", path + "/reconciliation").json()["status"] == "reconciliation_required"
    fake.call("POST", path + "/capture", expected=(409,))
    assert len(provider(fake)["posts"]) == 1
    assert Decimal(fake.allowance(key)["committed"]) == Decimal("1")


# Fixed by the lead 2026-11-05 (was strict xfail E2E-T4-3).
def test_uncertain_hold_remains_visible_in_pending_allowance(fake):
    _aid, key, _payment, path = prepared(fake)
    fake.call("POST", path + "/execute", token=key)
    fake.restart()
    allowance = fake.allowance(key)
    assert Decimal(allowance["pending"]) == Decimal("1"), allowance
    assert Decimal(allowance["committed"]) == sum(Decimal(allowance[k]) for k in ("reserved", "pending", "spent"))


@pytest.mark.parametrize("direction", ["forward",
    "backward"])
def test_clock_step_cannot_consume_expired_or_not_yet_valid_approval(fake, direction):
    from datetime import datetime, timedelta, timezone
    clock_file = fake.directory / "synthetic-clock"
    clock_file.write_text(datetime.now(timezone.utc).isoformat())
    fake.env["T4_CLOCK_FILE"] = str(clock_file)
    fake.restart()
    _aid, key, payment, path = prepared(fake)
    approval = fake.ledger("SELECT approved_at,expires_at FROM approvals WHERE request_id=?", (payment["request_id"],))[0]
    at = (datetime.fromisoformat(approval["expires_at"]) + timedelta(seconds=1) if direction == "forward"
          else datetime.fromisoformat(approval["approved_at"]) - timedelta(seconds=1))
    clock_file.write_text(at.isoformat())
    response = fake.call("POST", path + "/execute", token=key, expected=(200, 409))
    poisoned = False
    if response.status_code == 200:
        row = fake.ledger("SELECT approved_at,consumed_at FROM approvals WHERE request_id=?", (payment["request_id"],))[0]
        assert row["consumed_at"] < row["approved_at"]
        try:
            fake.restart()
        except RuntimeError:
            poisoned = b"ConsistencyError" in fake.logs
    assert response.status_code == 409, (f"HTTP {response.status_code}; "
                                        f"consumption before approval; startup refused={poisoned}")
    assert not provider(fake)["posts"]
    assert fake.ledger("SELECT consumed_at FROM approvals WHERE request_id=?", (payment["request_id"],))[0]["consumed_at"] is None
