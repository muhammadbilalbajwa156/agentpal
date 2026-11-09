"""Regression tests for durable responses, crash recovery and current operator caps."""

import pytest

from test_platform_fixtures import OWNER_A, build_world


def test_replay_returns_stored_response_after_approval_and_freeze():
    w = build_world()
    _, key, wallet = w.funded_agent()
    first = w.pay(key, idem="snapshot-key").json()
    w.client.post(f"/v1/payment-requests/{first['request_id']}/approve",
                  json={"terms_sha256": first["terms_sha256"]}, headers=w.h(OWNER_A))
    w.client.post(f"/v1/wallets/{wallet['wallet_id']}/freeze", headers=w.h(OWNER_A))
    replay = w.pay(key, idem="snapshot-key")
    assert replay.status_code == 200
    assert replay.json() == first
    assert w.wallet_store.query("SELECT response_json FROM platform_idempotency")[0][
        "response_json"] is not None


def test_prepare_failure_after_reservation_keeps_request_identity(monkeypatch):
    w = build_world()
    agent_id, key, _ = w.funded_agent()
    service = w.platform.payments.wallet_service()
    original = service.prepare_payment
    prepared_ids = []

    def crash(ctx, draft):
        from agentpal.schemas import PaymentRequest
        from agentpal.platform.payments import take_pending_request_id
        rid = take_pending_request_id()
        req = PaymentRequest(**draft.model_dump(), request_id=rid, agent_id=ctx.agent_id)
        w.risk.assess(req)
        prepared_ids.append(rid)
        raise RuntimeError("worker interrupted")

    monkeypatch.setattr(service, "prepare_payment", crash)
    with pytest.raises(RuntimeError, match="worker interrupted"):
        w.pay(key, idem="reserved-crash")
    assert w.risk.store.ledger_entry(prepared_ids[0])["state"] == "reserved"
    monkeypatch.setattr(service, "prepare_payment", original)
    w.clock.advance(seconds=61)
    retried = w.pay(key, idem="reserved-crash")
    assert retried.status_code == 201
    assert retried.json()["request_id"] == prepared_ids[0]
    assert retried.json()["recommendation"] != "block"
    assert len(w.wallet_store.query("SELECT * FROM wallet_payment_requests")) == 1
    assert str(w.risk.store.spend_breakdown(agent_id, "USD", w.clock())["committed"]) == "49.99"


def test_stale_claim_recovers_prepared_row_without_another_prepare(monkeypatch):
    w = build_world()
    _, key, _ = w.funded_agent()
    first = w.pay(key, idem="prepared-crash").json()
    with w.wallet_store.tx() as c:
        c.execute("UPDATE platform_idempotency SET status='in_progress', response_json=NULL")
        c.execute("DELETE FROM platform_payment_meta")
    w.clock.advance(seconds=61)

    def forbidden(*args):
        raise AssertionError("must recover existing preparation")

    monkeypatch.setattr(w.platform.payments.wallet_service(), "prepare_payment", forbidden)
    replay = w.pay(key, idem="prepared-crash")
    assert replay.status_code == 200
    assert replay.json()["request_id"] == first["request_id"]
    assert w.wallet_store.query("SELECT response_json FROM platform_idempotency")[0][
        "response_json"] is not None


def test_operator_caps_apply_again_when_loading_existing_wallet():
    from decimal import Decimal
    from agentpal.platform import CurrencyCap
    w = build_world()
    agent_id, _, _ = w.funded_agent()
    w.deps.limits.caps["USD"] = CurrencyCap(Decimal("20"), Decimal("10"))
    limits = w.risk.spend_policy.for_agent(agent_id).currencies["USD"]
    assert limits.monthly_budget == Decimal("20")
    assert limits.rolling_window_limit == Decimal("20")
    assert limits.per_transaction_hard_limit == Decimal("10")
    assert limits.per_transaction_review_limit <= Decimal("10")
    del w.deps.limits.caps["USD"]
    assert w.risk.spend_policy.for_agent(agent_id) is None
