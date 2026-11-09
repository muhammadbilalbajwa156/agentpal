"""Bounded contention and exact shared-ledger accounting, with offline PayPal HTTP."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

import pytest


def parallel(actions):
    gate = Barrier(len(actions))
    def run(action):
        gate.wait(timeout=10)
        return action()
    with ThreadPoolExecutor(max_workers=len(actions)) as pool:
        return list(pool.map(run, actions))


@pytest.mark.parametrize("currency", ["USD", "EUR", "GBP"])
@pytest.mark.parametrize("recurring", [False, True])
def test_small_payment_and_payout_reservations_share_one_budget(world, currency, recurring):
    world.register("budget", budget="0.05")
    if currency != "USD":
        world.wallet("budget", currency=currency, budget="0.05")
    def reserve(index):
        changes = dict(amount="0.01", currency=currency)
        if index % 2 == 0:
            changes.update(recurring=recurring, billing_interval="monthly" if recurring else None)
        return world.create("payment" if index % 2 == 0 else "payout", "budget", **changes)
    responses = parallel([lambda i=i: reserve(i) for i in range(10)])
    assert all(r.status_code in (201, 409) for r in responses)
    accepted = [r for r in responses if r.status_code == 201 and r.json()["status"] == "awaiting_approval"]
    assert len(accepted) == 5
    allowance = world.allowance("budget", currency)
    assert Decimal(allowance["committed"]) == Decimal("0.05")
    assert Decimal(allowance["available"]) == 0
    # Each currency has a separate policy; no borrowing or implicit conversion.
    other = "EUR" if currency == "USD" else "USD"
    if currency == "USD":
        world.wallet("budget", currency=other, budget="0.05")
    assert Decimal(world.allowance("budget", other)["committed"]) == 0
    assert not world.backend.calls


@pytest.mark.parametrize("kind", ["payment", "payout"])
@pytest.mark.parametrize("amount", ["0.001", "1e-2", "NaN", "Infinity", "-0.01", "0", 0.01, True])
def test_invalid_minor_units_and_numeric_tricks_never_reserve(world, kind, amount):
    r = world.create(kind, amount=amount)
    assert r.status_code in (409, 422)
    assert Decimal(world.allowance()["committed"]) == 0
    assert not world.backend.calls


@pytest.mark.parametrize("kind", ["payment", "payout"])
def test_per_transaction_and_rolling_limits_cannot_be_split_or_crossed(world, kind):
    world.register("limit", budget="5.00")
    wallet_id = world.wallets["limit", "USD"]
    r = world.call("PATCH", "/v1/wallets/" + wallet_id, json={
        "per_transaction_limit": "2.00", "per_transaction_review_limit": "2.00",
        "rolling_window_limit": "2.00"})
    assert r.status_code == 200
    r = world.create(kind, "limit", amount="2.01")
    assert r.status_code == 409 or r.json()["status"] == "rejected"
    assert Decimal(world.allowance("limit")["committed"]) == 0
    world.prepared(kind, "limit", amount="1.50")
    r = world.create(kind, "limit", amount="0.51")
    assert r.status_code == 409 or r.json()["status"] == "rejected"
    assert Decimal(world.allowance("limit")["committed"]) == Decimal("1.50")
    assert not world.backend.calls


@pytest.mark.parametrize("kind", ["payment", "payout"])
def test_concurrent_create_replay_is_one_hold_and_binds_body(world, kind):
    key = "h2-concurrent-identical-create"
    rs = parallel([lambda: world.create(kind, key=key) for _ in range(6)])
    assert all(r.status_code in (200, 201, 409) for r in rs)
    successful = [r.json() for r in rs if r.status_code in (200, 201)]
    assert successful
    assert len({r["request_id"] for r in successful}) == 1
    assert Decimal(world.allowance()["committed"]) == 1
    assert world.create(kind, key=key, amount="1.01").status_code == 409
    assert not world.backend.calls


@pytest.mark.parametrize("kind", ["payment", "payout"])
def test_approval_execution_race_consumes_one_approval_and_submits_once(world, kind):
    data, path = world.prepared(kind)
    rs = parallel([lambda: world.call("POST", path + "/approve",
                    json={"terms_sha256": data["terms_sha256"]}) for _ in range(4)])
    assert all(r.status_code in (200, 409) for r in rs)
    assert sum(r.status_code == 200 for r in rs) == 1
    rs = parallel([lambda: world.call("POST", path + "/execute", "a") for _ in range(6)])
    assert all(r.status_code in (200, 409) for r in rs)
    suffix = "/v2/checkout/orders" if kind == "payment" else "/v1/payments/payouts"
    assert len(world.backend.posts(suffix)) == 1
    approvals = world.deps.risk_service.store.approvals(data["request_id"])
    assert len(approvals) == 1 and approvals[0]["consumed_at"] is not None
    assert world.call("POST", path + "/approve",
                       json={"terms_sha256": data["terms_sha256"]}).status_code == 409
    assert Decimal(world.allowance()["committed"]) == 1


def test_approve_reject_execute_contention_has_one_consistent_outcome(world):
    data, path = world.prepared()
    rs = parallel([
        lambda: world.call("POST", path + "/approve", json={"terms_sha256": data["terms_sha256"]}),
        lambda: world.call("POST", path + "/reject"),
        lambda: world.call("POST", path + "/execute", "a"),
    ])
    assert all(r.status_code in (200, 409) for r in rs)
    entry = world.deps.risk_service.store.ledger_entry(data["request_id"])
    assert entry["state"] in ("approved", "released", "pending")
    assert len(world.backend.posts("/v2/checkout/orders")) <= 1
    if entry["state"] == "released":
        assert not world.backend.calls
        assert Decimal(world.allowance()["committed"]) == 0
    else:
        assert len(world.deps.risk_service.store.approvals(data["request_id"])) == 1
        assert Decimal(world.allowance()["committed"]) == 1


def test_concurrent_capture_is_one_verified_charge(world):
    data, path = world.approved()
    assert world.call("POST", path + "/execute", "a").status_code == 200
    assert world.call("POST", path + "/capture").status_code == 409
    assert not world.backend.posts("/capture")
    world.backend.approve()
    rs = parallel([lambda: world.call("POST", path + "/capture") for _ in range(6)])
    assert all(r.status_code in (200, 409) for r in rs)
    assert len(world.backend.posts("/capture")) == 1
    outcomes = world.deps.risk_service.store.query(
        "SELECT * FROM payment_outcomes WHERE request_id=? AND verified=1", (data["request_id"],))
    assert len(outcomes) == 1
    assert Decimal(world.allowance()["spent"]) == 1


def test_concurrent_refunds_cannot_exceed_capture_or_credit_twice(world):
    _, path = world.captured()
    def refund(key, amount="0.40"):
        return world.call("POST", path + "/refunds", json={"amount": amount},
                          headers={"Idempotency-Key": key})
    rs = parallel([lambda i=i: refund(f"h2-refund-race-{i}") for i in range(4)])
    assert sorted(r.status_code for r in rs) == [201, 201, 409, 409]
    assert all(r.json()["status"] == "COMPLETED" for r in rs if r.status_code == 201)
    assert len(world.backend.posts("/refund")) == 2
    assert Decimal(world.allowance()["spent"]) == Decimal("0.20")
    assert refund("h2-excess-remainder", "0.21").status_code == 409
    rs = parallel([lambda: refund("h2-refund-exact-replay", "0.20") for _ in range(4)])
    assert sorted(r.status_code for r in rs) == [200, 200, 200, 201]
    assert len({r.json()["refund_id"] for r in rs}) == 1
    assert len(world.backend.posts("/refund")) == 3
    assert world.call("GET", path).json()["status"] == "reversed"
    assert Decimal(world.allowance()["spent"]) == 0
    assert refund("h2-refund-exact-replay", "0.19").status_code == 409
    assert world.call("POST", path + "/refunds/reconciliation").status_code == 200
    assert Decimal(world.allowance()["spent"]) == 0


def test_over_refund_is_refused_before_any_provider_mutation(world):
    _, path = world.captured()
    r = world.call("POST", path + "/refunds", json={"amount": "1.01"},
                   headers={"Idempotency-Key": "h2-capture-excess-bound"})
    assert r.status_code == 409
    assert not world.backend.posts("/refund")
    assert Decimal(world.allowance()["spent"]) == 1


@pytest.mark.parametrize("operation", ["capture", "refund", "payout"])
def test_lost_provider_response_never_retries_mutation_or_releases_unknown_hold(world, operation):
    if operation == "refund":
        _, path = world.captured()
        suffix, actor, amount = "/refunds", "owner_a", "0.75"
        kwargs = dict(json={"amount": amount}, headers={"Idempotency-Key": "h2-lost-refund-response"})
        post_suffix = "/refund"
    elif operation == "capture":
        _, path = world.approved()
        world.call("POST", path + "/execute", "a")
        world.backend.approve()
        suffix, actor, kwargs, post_suffix = "/capture", "owner_a", {}, "/capture"
    else:
        _, path = world.approved("payout")
        suffix, actor, kwargs, post_suffix = "/execute", "a", {}, "/v1/payments/payouts"
    world.backend.timeout_operation = operation
    first = world.call("POST", path + suffix, actor, **kwargs)
    assert first.status_code in (200, 201)
    assert first.json()["status"] == "reconciliation_required"
    for _ in range(3):
        replay = world.call("POST", path + suffix, actor, **kwargs)
        assert replay.status_code in (200, 409)
    assert len(world.backend.posts(post_suffix)) == 1
    assert Decimal(world.allowance()["committed"]) == 1
    # Unknown 0.75 refund must reserve the disputed amount against further refunds.
    if operation == "refund":
        r = world.call("POST", path + "/refunds", json={"amount": "0.26"},
                       headers={"Idempotency-Key": "h2-unknown-not-available"})
        assert r.status_code == 409


@pytest.mark.parametrize("status", ["PENDING", "UNCLAIMED", "ONHOLD", "FAILED", "DENIED", "BLOCKED"])
def test_successful_batch_header_never_substitutes_for_successful_payout_item(world, status):
    world.backend.payout_status = status
    data, path = world.approved("payout")
    r = world.call("POST", path + "/execute", "a")
    assert r.status_code == 200
    assert r.json()["status"] != "provider_confirmed"
    assert not world.deps.risk_service.store.query(
        "SELECT * FROM payment_outcomes WHERE request_id=? AND outcome='completed' AND verified=1",
        (data["request_id"],))
