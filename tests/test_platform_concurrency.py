"""Concurrent stale-claim recovery must never reserve twice for one operation."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Event, Lock

from agentpal.platform.payments import take_pending_request_id
from test_platform_fixtures import build_world


def _assert_one_reservation(world, agent_id, request_id):
    rows = world.wallet_store.query("SELECT request_id FROM wallet_payment_requests")
    assert rows == [{"request_id": request_id}]
    assert world.risk.store.spend_breakdown(agent_id, "USD", world.clock())["committed"] == \
        Decimal("49.99")
    claims = world.wallet_store.query("SELECT request_id, status FROM platform_idempotency")
    assert claims == [{"request_id": request_id, "status": "done"}]


def test_stale_claim_during_preparation_cannot_duplicate_reservation(monkeypatch):
    world = build_world()
    agent_id, key, _ = world.funded_agent()
    desk = world.platform.payments
    service = desk.wallet_service()
    original_prepare, original_claim = service.prepare_payment, desk._claim
    preparing, release_first = Event(), Event()
    second_claim_entered, second_claim_finished = Event(), Event()
    call_lock = Lock()
    prepare_calls = 0

    def blocked_prepare(ctx, draft):
        nonlocal prepare_calls
        with call_lock:
            prepare_calls += 1
            first = prepare_calls == 1
        if first:
            preparing.set()
            assert release_first.wait(5), "test did not release first preparation"
        return original_prepare(ctx, draft)

    def observed_claim(*args):
        second = preparing.is_set()
        if second:
            second_claim_entered.set()
        result = original_claim(*args)
        if second:
            second_claim_finished.set()
        return result

    monkeypatch.setattr(service, "prepare_payment", blocked_prepare)
    monkeypatch.setattr(desk, "_claim", observed_claim)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(world.pay, key, "concurrent-stale")
        try:
            assert preparing.wait(5), "first request did not start preparation"
            world.clock.advance(seconds=61)
            second = pool.submit(world.pay, key, "concurrent-stale")
            # A serialized wallet transaction can block the second worker during
            # authentication, before _claim. Release the first after a bounded wait.
            # Without serialization, wait for the stale replacement to complete.
            if second_claim_entered.wait(0.25):
                second_claim_finished.wait(0.25)
        finally:
            release_first.set()
        responses = [first.result(timeout=5), second.result(timeout=5)]
    assert {response.status_code for response in responses} == {200, 201}
    assert responses[0].json() == responses[1].json()
    _assert_one_reservation(world, agent_id, responses[0].json()["request_id"])


def test_replaced_claim_is_fenced_before_original_worker_prepares(monkeypatch):
    world = build_world()
    agent_id, key, _ = world.funded_agent()
    desk = world.platform.payments
    original_claim = desk._claim
    service = desk.wallet_service()
    original_prepare = service.prepare_payment
    first_claimed, release_first = Event(), Event()
    call_lock = Lock()
    claim_calls = 0
    obsolete_ids, prepared_ids = [], []

    def blocked_claim(*args):
        nonlocal claim_calls
        with call_lock:
            claim_calls += 1
            first = claim_calls == 1
        result = original_claim(*args)
        if first:
            obsolete_ids.append(result[0])
            first_claimed.set()
            assert release_first.wait(5), "test did not release original claim"
        return result

    def observed_prepare(ctx, draft):
        prepared_ids.append(take_pending_request_id())
        return original_prepare(ctx, draft)

    monkeypatch.setattr(desk, "_claim", blocked_claim)
    monkeypatch.setattr(service, "prepare_payment", observed_prepare)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(world.pay, key, "replaced-stale")
        try:
            assert first_claimed.wait(5), "original request did not claim an identity"
            world.clock.advance(seconds=61)
            second = pool.submit(world.pay, key, "replaced-stale").result(timeout=5)
            assert second.status_code == 201
        finally:
            release_first.set()
        resumed = first.result(timeout=5)
    assert resumed.status_code in {200, 409}
    if resumed.status_code == 200:
        assert resumed.json() == second.json()
    assert obsolete_ids[0] not in prepared_ids
    assert prepared_ids == [second.json()["request_id"]]
    _assert_one_reservation(world, agent_id, second.json()["request_id"])
