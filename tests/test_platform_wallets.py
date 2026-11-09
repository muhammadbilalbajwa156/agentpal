"""Virtual wallets: limits within operator caps, allowance derived from the ledger, freeze,
and the wallet-backed spend policy that the deterministic reservation uses."""

from __future__ import annotations

from decimal import Decimal

import pytest

from agentpal.platform import CurrencyCap, PlatformLimits, WalletBackedSpendPolicy
from agentpal.spending import SpendPolicy

from test_platform_fixtures import ALL_PAY, OWNER_A, OWNER_B, build_world


@pytest.fixture
def w():
    return build_world()


def _allowance(w, wallet_id, owner=OWNER_A):
    return w.client.get(f"/v1/wallets/{wallet_id}", headers=w.h(owner)).json()["allowance"]


def test_wallet_is_labelled_virtual_and_has_no_balance_column(w):
    _, _, wal = w.funded_agent()
    assert wal["unit"] == "virtual_allowance" and wal["is_real_money"] is False
    assert "not money" in wal["label"]
    cols = {r["name"] for r in w.wallet_store.query(
        "SELECT name FROM pragma_table_info('platform_wallets')")}
    assert not {c for c in cols if "balance" in c or c in ("available", "spent")}


def test_allowance_is_derived_from_the_ledger(w):
    agent_id, key, wal = w.funded_agent(caps=ALL_PAY)
    wid = wal["wallet_id"]
    assert _allowance(w, wid)["available"] == "100.00"
    p1 = w.pay(key).json()
    a = _allowance(w, wid)
    assert (a["reserved"], a["available"], a["committed"]) == ("49.99", "50.01", "49.99")
    w.client.post(f"/v1/payment-requests/{p1['request_id']}/approve",
                  json={"terms_sha256": p1["terms_sha256"]}, headers=w.h(OWNER_A))
    assert _allowance(w, wid)["reserved"] == "49.99"
    w.client.post(f"/v1/payment-requests/{p1['request_id']}/execute", headers=w.h(key))
    a = _allowance(w, wid)
    assert (a["reserved"], a["pending"], a["spent"]) == ("0.00", "49.99", "0.00")
    # the ledger, not the wallet, is the source: it equals spend_breakdown exactly
    b = w.risk.store.spend_breakdown(agent_id, "USD", w.clock())
    assert Decimal(a["committed"]) == b["committed"]
    p2 = w.pay(key, amount="10.00").json()
    w.client.post(f"/v1/payment-requests/{p2['request_id']}/reject", headers=w.h(OWNER_A))
    a = _allowance(w, wid)
    assert (a["committed"], a["available"]) == ("49.99", "50.01")


def test_owner_cannot_exceed_the_operator_cap():
    w = build_world(limits=PlatformLimits(caps={"USD": CurrencyCap(Decimal("200"),
                                                                    Decimal("80"))}))
    agent_id, key = w.register()
    r = w.client.post("/v1/wallets", json={"agent_id": agent_id, "currency": "USD",
                                           "monthly_budget": "201.00",
                                           "per_transaction_limit": "50.00"},
                      headers=w.h(OWNER_A))
    assert r.status_code == 400 and r.json() == {"error": "above_operator_cap"}
    wal = w.wallet(agent_id, monthly_budget="200.00", per_transaction_limit="50.00")
    for change in ({"monthly_budget": "200.01"}, {"per_transaction_limit": "80.01"},
                   {"rolling_window_limit": "999.00"}):
        r = w.client.patch(f"/v1/wallets/{wal['wallet_id']}", json=change,
                           headers=w.h(OWNER_A))
        assert r.status_code == 400 and r.json() == {"error": "above_operator_cap"}
    assert w.client.get(f"/v1/wallets/{wal['wallet_id']}", headers=w.h(OWNER_A)).json()[
        "limits"]["monthly_budget"] == "200.00"
    # the reservation enforces the WALLET's limits (not the cap, not the request)
    blocked = w.pay(key, amount="60.00").json()
    assert blocked["status"] == "rejected" and blocked["recommendation"] == "block"
    assert w.risk.store.ledger_entry(blocked["request_id"]) is None
    w.client.patch(f"/v1/wallets/{wal['wallet_id']}", json={"per_transaction_limit": "80.00"},
                   headers=w.h(OWNER_A))
    ok = w.pay(key, amount="60.00").json()
    assert ok["status"] == "awaiting_approval"
    assert w.risk.store.ledger_entry(ok["request_id"])["state"] == "reserved"
    policy = w.risk.spend_policy.for_agent(agent_id)
    assert policy.owner_id == "owner_alice"
    assert policy.currencies["USD"].per_transaction_hard_limit == Decimal("80.00")


def test_monthly_budget_is_enforced_by_the_reservation(w):
    _, key, _ = w.funded_agent(monthly_budget="100.00", per_transaction_limit="60.00")
    # different merchants: the per-merchant split-transaction cap is not what is tested
    for i in (1, 2):
        r = w.pay(key, amount="49.99", merchant_name=f"Vendor {i}",
                  merchant_domain=f"vendor{i}.example")
        assert r.json()["status"] == "awaiting_approval"
    third = w.pay(key, amount="5.00", merchant_name="Vendor 3",
                  merchant_domain="vendor3.example").json()
    assert third["status"] == "rejected"


def test_limits_validation(w):
    agent_id, _ = w.register()
    base = {"agent_id": agent_id, "currency": "USD", "monthly_budget": "100.00",
            "per_transaction_limit": "50.00"}
    cases = [({"per_transaction_review_limit": "60.00"}, 400, "inconsistent_limits"),
             ({"per_transaction_limit": "150.00"}, 400, "inconsistent_limits"),
             ({"currency": "JPY"}, 400, "currency_not_enabled"),
             ({"monthly_budget": "100.001"}, 400, "invalid_amount"),
             ({"monthly_budget": "0"}, 400, "invalid_amount"),
             ({"monthly_budget": 100.5}, 422, None),
             ({"monthly_budget": "1e3"}, 422, None),
             ({"balance": "5"}, 422, None)]
    for change, status, code in cases:
        r = w.client.post("/v1/wallets", json={**base, **change}, headers=w.h(OWNER_A))
        assert r.status_code == status, change
        if code:
            assert r.json() == {"error": code}
    assert w.client.get("/v1/wallets", headers=w.h(OWNER_A)).json()["wallets"] == []


def test_one_wallet_per_agent_and_currency(w):
    agent_id, _ = w.register()
    w.wallet(agent_id)
    r = w.client.post("/v1/wallets", json={"agent_id": agent_id, "currency": "USD",
                                           "monthly_budget": "10.00",
                                           "per_transaction_limit": "5.00"},
                      headers=w.h(OWNER_A))
    assert r.status_code == 409 and r.json() == {"error": "wallet_exists"}
    w.wallet(agent_id, currency="EUR")


def test_freeze_fails_closed(w):
    agent_id, key, wal = w.funded_agent(caps=ALL_PAY)
    p = w.pay(key).json()
    w.client.post(f"/v1/payment-requests/{p['request_id']}/approve",
                  json={"terms_sha256": p["terms_sha256"]}, headers=w.h(OWNER_A))
    r = w.client.post(f"/v1/wallets/{wal['wallet_id']}/freeze", headers=w.h(OWNER_A))
    assert r.json()["status"] == "frozen"
    assert w.pay(key).json() == {"error": "wallet_frozen"}
    r = w.client.post(f"/v1/payment-requests/{p['request_id']}/execute", headers=w.h(key))
    assert r.status_code == 409 and r.json() == {"error": "wallet_frozen"}
    assert w.lifecycle.begun == []
    # defence in depth: the spend policy no longer knows the currency at all
    assert w.risk.spend_policy.for_agent(agent_id) is None
    w.client.post(f"/v1/wallets/{wal['wallet_id']}/unfreeze", headers=w.h(OWNER_A))
    assert w.pay(key).status_code == 201
    actions = [e["action"] for e in w.platform.wallets.events(wal["wallet_id"])]
    assert actions == ["create", "freeze", "unfreeze"]


def test_payment_needs_a_wallet_in_that_currency(w):
    _, key, _ = w.funded_agent()
    assert w.pay(key, currency="EUR").json() == {"error": "no_wallet_for_currency"}


def test_spend_policy_falls_back_to_the_operator_file_for_other_agents():
    w = build_world()
    limits = {"USD": {"monthly_budget": "500", "rolling_window_limit": "500",
                      "per_transaction_hard_limit": "100",
                      "per_transaction_review_limit": "50"}}
    base = SpendPolicy.model_validate({"policy_version": "t", "agents": {
        "agent_demo_001": {"owner_id": "o", "currencies": limits}}})
    wrapped = WalletBackedSpendPolicy.wrap(base, w.platform.wallets, lambda a: None)
    assert wrapped.for_agent("agent_demo_001") == base.for_agent("agent_demo_001")
    assert wrapped.for_agent("unknown_agent") is None
    assert isinstance(w.risk.spend_policy, WalletBackedSpendPolicy)
    agent_id, _ = w.register()
    assert w.risk.spend_policy.for_agent(agent_id) is None       # no wallet yet
    assert w.risk.spend_policy.uses_demo_default(agent_id) is False


def test_wallet_routes_are_owner_scoped(w):
    agent_id, _, wal = w.funded_agent()
    wid = wal["wallet_id"]
    for method, path, body in [("get", f"/v1/wallets/{wid}", None),
                               ("patch", f"/v1/wallets/{wid}", {"monthly_budget": "1.00"}),
                               ("post", f"/v1/wallets/{wid}/freeze", None),
                               ("get", f"/v1/wallets/{wid}/transactions", None)]:
        r = w.client.request(method, path, json=body, headers=w.h(OWNER_B))
        assert r.status_code == 404 and r.json() == {"error": "not_found"}, path
    assert w.client.get("/v1/wallets", headers=w.h(OWNER_B)).json()["wallets"] == []
    r = w.client.post("/v1/wallets", json={"agent_id": agent_id, "currency": "EUR",
                                           "monthly_budget": "10.00",
                                           "per_transaction_limit": "5.00"},
                      headers=w.h(OWNER_B))
    assert r.status_code == 404
    assert _allowance(w, wid)["available"] == "100.00"
    refused = [e for e in w.platform.wallets.events(wid) if e["action"] == "refused"]
    assert len(refused) == 4


def test_transaction_history(w):
    agent_id, key, wal = w.funded_agent()
    p = w.pay(key).json()
    for path, token in [(f"/v1/wallets/{wal['wallet_id']}/transactions", OWNER_A),
                        ("/v1/me/wallet/transactions", key)]:
        body = w.client.get(path, headers=w.h(token)).json()
        assert body["is_real_money"] is False
        [tx] = body["transactions"]
        assert tx["request_id"] == p["request_id"] and tx["amount"] == "49.99"
        assert tx["ledger_state"] == "reserved"
        assert tx["events"][0]["to_state"] == "reserved"
