"""Owner counter-offer ("Buy this instead"): every guarantee has a boundary test here.

Real HTTP authorization, policy, ledger and stores; only PayPal HTTP and Channel3 are
replaced by synthetic doubles. A counter approves nothing: it declines the original through
the existing reject path and creates the replacement through the existing agent create path.
"""
import uuid

import httpx
import pytest

from agentpal.providers.config import PayPalSandboxConfig
from agentpal.providers.paypal_sandbox import PayPalSandboxProvider
from test_paypal_refunds import RefundPayPal
from test_platform_workspace import bearer, owner, world

CAPS = ["wallet.read", "payment.prepare", "owner.request_decision", "payment.execute_sandbox"]
OWNER_ONLY = ("counter_of", "counter_source", "counter_saving")


def deal(retailer="known-retailer.example", price="16.49", currency="USD"):
    return {"retailer": retailer, "price": price, "currency": currency, "condition": "new",
            "availability": "in_stock", "you_save": "8.50", "you_save_pct": "34.0",
            "buy_url": None, "on_sale": False}


class DealMarket:
    """Channel3 double: the stored snapshot is the only source of a deal's price."""

    def __init__(self, deals=None):
        self.deals = [deal()] if deals is None else deals
        self.calls = 0

    def check(self, **kw):
        self.calls += 1
        return {"provider": "channel3", "checked_at": "2026-11-09T00:00:00+00:00",
                "product": {"id": "hub", "title": "USB-C hub", "brands": [],
                            "match_confidence": "high", "offer_count": 2},
                "input": {"product_query": "usb-c hub", "currency": "USD"},
                "advisory_level": "review_recommended", "better_deals": self.deals,
                "signals": [], "disclosure": "Advisory", "verify_notice": "Verify",
                "switch_notice": "Review", "discount_codes_available": False,
                "discount_codes_note": "None"}


def workspace(c, d, name="alice", limit="25.00", budget="100.00"):
    """An owner, one ordinary agent with its own API key, and its wallet."""
    h = bearer(owner(d, name))
    reg = c.post("/v1/agents", headers=h, json={"name": "buyer", "capabilities": CAPS})
    assert reg.status_code == 201, reg.text
    agent_id = reg.json()["agent"]["agent_id"]
    w = c.post("/v1/wallets", headers=h, json={"agent_id": agent_id, "currency": "USD",
               "monthly_budget": budget, "per_transaction_limit": limit})
    assert w.status_code == 201, w.text
    return h, bearer(reg.json()["api_key"]), agent_id, w.json()["wallet_id"]


def ask(c, ah, amount="24.99", **over):
    body = {"amount": amount, "currency": "USD", "merchant_name": "Unfamiliar Shop",
            "merchant_domain": "unfamiliar-shop.example", "purpose": "USB-C hub", **over}
    r = c.post("/v1/payment-requests", json=body,
               headers={**ah, "Idempotency-Key": "ask-" + uuid.uuid4().hex})
    assert r.status_code == 201, r.text
    return r.json()


MANUAL = {"amount": "16.49", "merchant_name": "Known Retailer",
          "merchant_domain": "known-retailer.example"}


def counter(c, h, rid, body=None):
    return c.post(f"/v1/payment-requests/{rid}/counter", headers=h, json=body or MANUAL)


def links(d):
    return d.wallet_store.query("SELECT * FROM platform_counter_offers")


def test_counter_declines_original_and_creates_unapproved_replacement(tmp_path):
    with world(tmp_path) as (c, d):
        h, ah, agent_id, wallet_id = workspace(c, d)
        p = ask(c, ah)
        r = counter(c, h, p["request_id"])
        assert r.status_code == 201, r.text
        out = r.json()
        assert out["approves_nothing"] is True
        original, replacement = out["original"], out["replacement"]
        assert original["request_id"] == p["request_id"]
        assert original["status"] == "rejected"
        assert original["owner_decision"]["decision"] == "rejected"
        assert original["countered_by"] == replacement["request_id"] != p["request_id"]
        assert replacement["counter_of"] == p["request_id"]
        assert replacement["counter_source"] == "manual"
        assert replacement["status"] == "awaiting_approval"
        assert replacement["owner_decision"] is None
        assert (replacement["agent_id"], replacement["wallet_id"]) == (agent_id, wallet_id)
        assert replacement["terms_sha256"] != p["terms_sha256"]
        t = replacement["terms"]
        assert (t["amount"], t["currency"], t["merchant_name"], t["merchant_domain"],
                t["purpose"]) == ("16.49", "USD", "Known Retailer", "known-retailer.example",
                                  "USB-C hub")
        assert replacement["counter_saving"] == "8.50"
        assert replacement["counter_saving_verified"] is False
        assert replacement["agent_context"]["business_justification"] == \
            "Owner counter-offer replacing " + p["request_id"]
        pending = [v["request_id"] for v in
                   c.get("/v1/approvals", headers=h).json()["payment_requests"]]
        assert pending == [replacement["request_id"]]


def test_counter_approves_nothing_and_binds_its_own_digest(tmp_path):
    with world(tmp_path) as (c, d):
        h, ah, _, _ = workspace(c, d)
        p = ask(c, ah)
        new = counter(c, h, p["request_id"]).json()["replacement"]
        path = "/v1/payment-requests/" + new["request_id"]
        # The counter alone is not an approval: the agent cannot execute the replacement.
        assert c.post(path + "/execute", headers=ah).status_code == 409
        # The ORIGINAL's digest cannot approve the replacement.
        wrong = c.post(path + "/approve", headers=h, json={"terms_sha256": p["terms_sha256"]})
        assert wrong.status_code == 409 and wrong.json()["error"] == "terms_mismatch"
        assert c.post(path + "/execute", headers=ah).status_code == 409
        # The agent cannot approve it either.
        assert c.post(path + "/approve", headers=ah,
                      json={"terms_sha256": new["terms_sha256"]}).status_code == 403
        assert c.post(path + "/approve", headers=h,
                      json={"terms_sha256": new["terms_sha256"]}).status_code == 200
        done = c.post(path + "/execute", headers=ah)
        assert done.status_code == 200, done.text
        # The declined original can never be executed or approved afterwards.
        old = "/v1/payment-requests/" + p["request_id"]
        assert c.post(old + "/execute", headers=ah).status_code == 409
        assert c.post(old + "/approve", headers=h,
                      json={"terms_sha256": p["terms_sha256"]}).status_code == 409


def test_agent_can_only_execute_the_replacement_unchanged(tmp_path):
    with world(tmp_path) as (c, d):
        h, ah, _, _ = workspace(c, d)
        p = ask(c, ah)
        new = counter(c, h, p["request_id"]).json()["replacement"]
        path = "/v1/payment-requests/" + new["request_id"]
        assert c.post(path + "/approve", headers=h,
                      json={"terms_sha256": new["terms_sha256"]}).status_code == 200
        # Different terms are a NEW request with its own digest that nobody approved.
        other = ask(c, ah, amount="17.49", merchant_name="Known Retailer",
                    merchant_domain="known-retailer.example")
        assert other["request_id"] != new["request_id"]
        assert other["terms_sha256"] != new["terms_sha256"]
        assert c.post("/v1/payment-requests/" + other["request_id"] + "/execute",
                      headers=ah).status_code == 409
        # No agent route can edit the stored terms; the bound digest is unchanged.
        for method in ("put", "patch"):
            assert getattr(c, method)(path, headers=ah, json={"amount": "1.00"}
                                      ).status_code in (404, 405)
        assert c.get(path, headers=ah).json()["terms_sha256"] == new["terms_sha256"]
        assert c.post(path + "/execute", headers=ah).status_code == 200
        assert c.get(path, headers=h).json()["terms"]["amount"] == "16.49"


def test_agent_key_cannot_counter(tmp_path):
    with world(tmp_path) as (c, d):
        h, ah, _, _ = workspace(c, d)
        p = ask(c, ah)
        r = counter(c, ah, p["request_id"])
        assert r.status_code == 403, r.text
        assert r.json()["error"] == "owner_credential_required"
        assert counter(c, {}, p["request_id"]).status_code == 401
        assert links(d) == []
        view = c.get("/v1/payment-requests/" + p["request_id"], headers=h).json()
        assert view["status"] == "awaiting_approval" and view["countered_by"] is None
        assert len(c.get("/v1/payment-requests", headers=h).json()["payment_requests"]) == 1


def test_another_owner_gets_404_and_changes_nothing(tmp_path):
    with world(tmp_path) as (c, d):
        h, ah, _, _ = workspace(c, d)
        bob = bearer(owner(d, "bob"))
        p = ask(c, ah)
        r = counter(c, bob, p["request_id"])
        assert r.status_code == 404
        assert r.json() == counter(c, bob, "pay_" + "0" * 32).json()
        assert links(d) == []
        assert c.get("/v1/payment-requests/" + p["request_id"],
                     headers=h).json()["status"] == "awaiting_approval"


def test_rules_run_again_counter_above_limit_is_blocked(tmp_path):
    with world(tmp_path) as (c, d):
        h, ah, _, _ = workspace(c, d)
        p = ask(c, ah)
        r = counter(c, h, p["request_id"], {**MANUAL, "amount": "30.00"})
        assert r.status_code == 201, r.text
        new = r.json()["replacement"]
        assert new["recommendation"] == "block"
        assert new["policy_block"]["rule"] == "AMOUNT_ABOVE_HARD_LIMIT"
        assert new["status"] != "awaiting_approval"
        assert c.get("/v1/approvals", headers=h).json()["payment_requests"] == []
        path = "/v1/payment-requests/" + new["request_id"]
        assert c.post(path + "/approve", headers=h,
                      json={"terms_sha256": new["terms_sha256"]}).status_code in (403, 409)
        assert c.post(path + "/execute", headers=ah).status_code == 409
        assert r.json()["original"]["status"] == "rejected"


def test_split_detection_still_applies_to_the_replacement(tmp_path):
    with world(tmp_path) as (c, d):
        h, ah, _, _ = workspace(c, d)
        first = ask(c, ah, amount="15.00", merchant_name="Paper Supply",
                    merchant_domain="paper.example", purpose="Paper part one")
        assert first["status"] == "awaiting_approval"
        p = ask(c, ah, amount="5.00")
        r = counter(c, h, p["request_id"], {"amount": "15.00", "merchant_name": "Paper Supply",
                                            "merchant_domain": "paper.example"})
        assert r.status_code == 201, r.text
        new = r.json()["replacement"]
        assert new["recommendation"] == "block"
        assert "SPLIT_TRANSACTION_EXCEEDS_HARD_LIMIT" in {s["code"] for s in new["risk_signals"]}
        assert new["request_id"] not in str(c.get("/v1/approvals", headers=h).json())


def test_counter_is_idempotent_and_a_second_counter_is_refused(tmp_path):
    with world(tmp_path) as (c, d):
        h, ah, _, _ = workspace(c, d)
        p = ask(c, ah)
        first = counter(c, h, p["request_id"])
        again = counter(c, h, p["request_id"])
        assert first.status_code == 201 and again.status_code == 200
        assert again.headers["Idempotent-Replayed"] == "true"
        assert again.json()["replacement"]["request_id"] == first.json()["replacement"]["request_id"]
        different = counter(c, h, p["request_id"], {**MANUAL, "amount": "12.00"})
        assert different.status_code == 409
        assert different.json()["error"] == "already_countered"
        assert len(links(d)) == 1
        assert len(c.get("/v1/payment-requests", headers=h).json()["payment_requests"]) == 2
        decisions = d.wallet_store.query("SELECT * FROM wallet_owner_decisions")
        assert [x["decision"] for x in decisions] == ["declined"]


@pytest.mark.parametrize("state", ["approved", "rejected", "executed", "blocked"])
def test_only_awaiting_approval_can_be_countered(tmp_path, state):
    with world(tmp_path) as (c, d):
        h, ah, _, _ = workspace(c, d)
        p = ask(c, ah, amount="30.00" if state == "blocked" else "24.99")
        path = "/v1/payment-requests/" + p["request_id"]
        if state in ("approved", "executed"):
            assert c.post(path + "/approve", headers=h,
                          json={"terms_sha256": p["terms_sha256"]}).status_code == 200
        if state == "executed":
            assert c.post(path + "/execute", headers=ah).status_code == 200
        if state == "rejected":
            assert c.post(path + "/reject", headers=h).status_code == 200
        before = c.get(path, headers=h).json()
        r = counter(c, h, p["request_id"])
        assert r.status_code == 409, r.text
        assert r.json()["error"] == "not_awaiting_decision"
        assert links(d) == []
        after = c.get(path, headers=h).json()
        assert after["status"] == before["status"] and after["countered_by"] is None
        assert len(c.get("/v1/payment-requests", headers=h).json()["payment_requests"]) == 1


def test_countered_original_and_approved_replacement_cannot_be_countered(tmp_path):
    with world(tmp_path) as (c, d):
        h, ah, _, _ = workspace(c, d)
        p = ask(c, ah)
        new = counter(c, h, p["request_id"]).json()["replacement"]
        assert c.post("/v1/payment-requests/" + new["request_id"] + "/approve", headers=h,
                      json={"terms_sha256": new["terms_sha256"]}).status_code == 200
        r = counter(c, h, new["request_id"], {**MANUAL, "amount": "10.00"})
        assert r.status_code == 409 and r.json()["error"] == "not_awaiting_decision"
        r = counter(c, h, p["request_id"], {**MANUAL, "amount": "10.00"})
        assert r.status_code == 409 and r.json()["error"] == "already_countered"
        assert len(links(d)) == 1


def test_identical_terms_are_refused_before_anything_changes(tmp_path):
    with world(tmp_path) as (c, d):
        h, ah, _, _ = workspace(c, d)
        p = ask(c, ah)
        r = counter(c, h, p["request_id"], {"amount": "24.99", "merchant_name": "Unfamiliar Shop",
                                            "merchant_domain": "unfamiliar-shop.example"})
        assert r.status_code == 422 and r.json()["error"] == "counter_same_terms"
        assert links(d) == []
        assert c.get("/v1/payment-requests/" + p["request_id"],
                     headers=h).json()["status"] == "awaiting_approval"


@pytest.mark.parametrize("body", [
    {}, {"amount": "5.00"}, {"merchant_name": "X"}, {"amount": "0", "merchant_name": "X"},
    {"amount": "0.00", "merchant_name": "X"}, {"amount": 5.0, "merchant_name": "X"},
    {"amount": "-1.00", "merchant_name": "X"}, {**MANUAL, "currency": "EUR"},
    {**MANUAL, "status": "approved"}, {**MANUAL, "terms_sha256": "0" * 64},
    {**MANUAL, "purpose": "Something else"}, {**MANUAL, "agent_id": "agt_" + "0" * 32},
    {**MANUAL, "merchant_paypal_email": "not an email"}, {"deal_index": -1},
    {**MANUAL, "merchant_name": "Known‮Retailer"}])
def test_invalid_counter_bodies_change_nothing(tmp_path, body):
    with world(tmp_path) as (c, d):
        h, ah, _, _ = workspace(c, d)
        p = ask(c, ah)
        r = c.post(f"/v1/payment-requests/{p['request_id']}/counter", headers=h, json=body)
        assert r.status_code == 422, r.text
        assert links(d) == []
        assert c.get("/v1/payment-requests/" + p["request_id"],
                     headers=h).json()["status"] == "awaiting_approval"


def test_deal_counter_uses_the_stored_deal_never_the_body(tmp_path):
    env = {"CHANNEL3_API_KEY": "synthetic-server-channel"}
    with world(tmp_path, env) as (c, d):
        h, ah, _, _ = workspace(c, d)
        d.market_checker = DealMarket([deal(), deal("eur-shop.example", "9.00", "EUR")])
        p = ask(c, ah, product_query="usb-c hub")
        rid = p["request_id"]
        assert c.get("/v1/payment-requests/" + rid, headers=h).json()[
            "market_check"]["better_deals"][0]["price"] == "16.49"
        for bad in (2, 99):
            r = counter(c, h, rid, {"deal_index": bad})
            assert r.status_code == 422 and r.json()["error"] == "invalid_deal"
        # A deal in another currency is never converted or guessed.
        r = counter(c, h, rid, {"deal_index": 1})
        assert r.status_code == 422 and r.json()["error"] == "invalid_deal"
        assert links(d) == []
        # The browser cannot supply a deal's price or merchant.
        r = counter(c, h, rid, {"deal_index": 0, "amount": "0.01", "merchant_name": "Evil",
                                "merchant_domain": "evil.example"})
        assert r.status_code == 201, r.text
        new = r.json()["replacement"]
        t = new["terms"]
        assert (t["amount"], t["merchant_name"], t["merchant_domain"]) == (
            "16.49", "known-retailer.example", "known-retailer.example")
        assert t["merchant_paypal_email"] is None
        assert new["counter_source"] == "channel3_deal"
        assert new["counter_saving"] == "8.50"
        assert new["agent_context"]["product_query"] == "usb-c hub"
        assert new["agent_context"]["product_url"] is None


def test_deal_counter_without_a_market_check_is_refused(tmp_path):
    with world(tmp_path) as (c, d):
        h, ah, _, _ = workspace(c, d)
        p = ask(c, ah)
        r = counter(c, h, p["request_id"], {"deal_index": 0})
        assert r.status_code == 422 and r.json()["error"] == "invalid_deal"
        assert c.get("/v1/payment-requests/" + p["request_id"],
                     headers=h).json()["status"] == "awaiting_approval"


def available(c, h, wallet_id):
    return c.get("/v1/wallets/" + wallet_id, headers=h).json()["allowance"]["available"]


def test_original_reservation_is_released_and_replacement_is_held(tmp_path):
    with world(tmp_path) as (c, d):
        h, ah, _, wallet_id = workspace(c, d)
        assert available(c, h, wallet_id) == "100.00"
        p = ask(c, ah)
        assert available(c, h, wallet_id) == "75.01"
        counter(c, h, p["request_id"])
        assert available(c, h, wallet_id) == "83.51"


def test_suspended_agent_cannot_receive_a_counter(tmp_path):
    with world(tmp_path) as (c, d):
        h, ah, agent_id, _ = workspace(c, d)
        p = ask(c, ah)
        assert c.post(f"/v1/agents/{agent_id}/suspend", headers=h).status_code == 200
        r = counter(c, h, p["request_id"])
        assert r.status_code in (403, 404), r.text
        assert links(d) == []
        # Refused BEFORE the original was declined: nothing changed.
        assert c.get("/v1/payment-requests/" + p["request_id"],
                     headers=h).json()["status"] == "awaiting_approval"


def test_agent_without_prepare_permission_cannot_receive_a_counter(tmp_path):
    with world(tmp_path) as (c, d):
        h, ah, agent_id, _ = workspace(c, d)
        p = ask(c, ah)
        assert c.put(f"/v1/agents/{agent_id}/permissions", headers=h,
                     json={"capabilities": ["wallet.read"]}).status_code == 200
        r = counter(c, h, p["request_id"])
        assert r.status_code == 403 and r.json()["error"] == "permission_denied"
        assert links(d) == []
        assert c.get("/v1/payment-requests/" + p["request_id"],
                     headers=h).json()["status"] == "awaiting_approval"


def test_frozen_wallet_refuses_counter_before_declining(tmp_path):
    with world(tmp_path) as (c, d):
        h, ah, _, wallet_id = workspace(c, d)
        p = ask(c, ah)
        assert c.post(f"/v1/wallets/{wallet_id}/freeze", headers=h).status_code == 200
        r = counter(c, h, p["request_id"])
        assert r.status_code == 409 and r.json()["error"] == "wallet_frozen"
        assert links(d) == []
        assert c.get("/v1/payment-requests/" + p["request_id"],
                     headers=h).json()["status"] == "awaiting_approval"


def test_agent_cannot_squat_the_reserved_counter_idempotency_key(tmp_path):
    with world(tmp_path) as (c, d):
        h, ah, _, _ = workspace(c, d)
        p = ask(c, ah)
        r = c.post("/v1/payment-requests", json={
            "amount": "1.00", "currency": "USD", "merchant_name": "X", "purpose": "squat"},
            headers={**ah, "Idempotency-Key": "owner-counter:" + p["request_id"]})
        assert r.status_code == 422
        assert counter(c, h, p["request_id"]).status_code == 201


def test_interrupted_counter_resumes_only_with_the_same_terms(tmp_path, monkeypatch):
    with world(tmp_path) as (c, d):
        h, ah, _, _ = workspace(c, d)
        p = ask(c, ah)
        desk = c.app.state.platform.payments
        real = desk.create

        def boom(*a, **kw):
            raise RuntimeError("synthetic crash after the decline committed")
        monkeypatch.setattr(desk, "create", boom)
        with pytest.raises(RuntimeError):
            counter(c, h, p["request_id"])
        monkeypatch.setattr(desk, "create", real)
        view = c.get("/v1/payment-requests/" + p["request_id"], headers=h).json()
        # Worst case of an interrupted counter is a plain rejection: nothing was approved.
        assert view["status"] == "rejected" and view["countered_by"] is None
        assert len(c.get("/v1/payment-requests", headers=h).json()["payment_requests"]) == 1
        r = counter(c, h, p["request_id"])
        assert r.status_code == 201, r.text
        new = r.json()["replacement"]
        assert new["status"] == "awaiting_approval" and new["counter_of"] == p["request_id"]
        assert r.json()["original"]["countered_by"] == new["request_id"]
        assert [x["decision"] for x in
                d.wallet_store.query("SELECT * FROM wallet_owner_decisions")] == ["declined"]


def test_plain_rejection_cannot_be_turned_into_a_counter(tmp_path):
    with world(tmp_path) as (c, d):
        h, ah, _, _ = workspace(c, d)
        p = ask(c, ah)
        assert c.post(f"/v1/payment-requests/{p['request_id']}/reject", headers=h).status_code == 200
        assert counter(c, h, p["request_id"]).status_code == 409
        assert links(d) == []


def paypal_world(d, backend):
    d.provider = PayPalSandboxProvider(
        PayPalSandboxConfig(client_id="synthetic-client", client_secret="synthetic-private"),
        http_client=httpx.Client(transport=httpx.MockTransport(backend.handler)))
    d.lifecycle.provider = d.provider


def test_savings_are_verified_only_after_paypal_confirms_the_replacement(tmp_path):
    backend = RefundPayPal()
    with world(tmp_path) as (c, d):
        paypal_world(d, backend)
        h, ah, _, _ = workspace(c, d)
        p = ask(c, ah, amount="3.00")
        new = counter(c, h, p["request_id"], {**MANUAL, "amount": "1.00"}).json()["replacement"]
        path = "/v1/payment-requests/" + new["request_id"]

        def saving():
            v = c.get(path, headers=h).json()
            return v["counter_saving"], v["counter_saving_verified"]
        assert saving() == ("2.00", False)
        assert c.post(path + "/approve", headers=h,
                      json={"terms_sha256": new["terms_sha256"]}).status_code == 200
        assert saving() == ("2.00", False)
        assert c.post(path + "/execute", headers=ah).status_code == 200
        assert saving() == ("2.00", False)
        backend.approve()
        assert saving() == ("2.00", False)
        done = c.post(path + "/capture", headers=h)
        assert done.status_code == 200 and done.json()["status"] == "provider_confirmed"
        view = c.get(path, headers=h).json()
        assert view["verification"]["verified_by"] == "paypal_read"
        assert saving() == ("2.00", True)
        # The original never claims a saving, and the agent never sees one.
        old = c.get("/v1/payment-requests/" + p["request_id"], headers=h).json()
        assert old["counter_saving"] is None and old["counter_saving_verified"] is False
        agent = c.get(path, headers=ah).json()
        assert agent["counter_saving"] is None and agent["counter_saving_verified"] is False


def test_capture_without_a_verified_paypal_read_is_not_a_verified_saving(tmp_path):
    backend = RefundPayPal()

    def handle(req):
        if (req.method == "GET" and "/checkout/orders/" in req.url.path
                and backend.status == "COMPLETED"):
            return httpx.Response(503, json={})
        return backend.handler(req)

    with world(tmp_path) as (c, d):
        d.provider = PayPalSandboxProvider(
            PayPalSandboxConfig(client_id="synthetic-client", client_secret="synthetic-private"),
            http_client=httpx.Client(transport=httpx.MockTransport(handle)))
        d.lifecycle.provider = d.provider
        h, ah, _, _ = workspace(c, d)
        p = ask(c, ah, amount="3.00")
        new = counter(c, h, p["request_id"], {**MANUAL, "amount": "1.00"}).json()["replacement"]
        path = "/v1/payment-requests/" + new["request_id"]
        c.post(path + "/approve", headers=h, json={"terms_sha256": new["terms_sha256"]})
        assert c.post(path + "/execute", headers=ah).status_code == 200
        backend.approve()
        assert c.post(path + "/capture", headers=h).json()["status"] == "provider_confirmed"
        view = c.get(path, headers=h).json()
        assert view["verification"] is None
        assert (view["counter_saving"], view["counter_saving_verified"]) == ("2.00", False)


def test_simulator_execution_is_never_a_verified_saving(tmp_path):
    with world(tmp_path) as (c, d):
        h, ah, _, _ = workspace(c, d)
        p = ask(c, ah)
        new = counter(c, h, p["request_id"]).json()["replacement"]
        path = "/v1/payment-requests/" + new["request_id"]
        c.post(path + "/approve", headers=h, json={"terms_sha256": new["terms_sha256"]})
        assert c.post(path + "/execute", headers=ah).status_code == 200
        view = c.get(path, headers=h).json()
        assert view["provider_mode"] == "simulated"
        assert view["counter_saving_verified"] is False


def test_a_more_expensive_counter_reports_a_negative_difference_never_a_saving(tmp_path):
    with world(tmp_path) as (c, d):
        h, ah, _, _ = workspace(c, d)
        p = ask(c, ah, amount="10.00")
        new = counter(c, h, p["request_id"], {**MANUAL, "amount": "12.50"}).json()["replacement"]
        assert new["counter_saving"] == "-2.50"
        assert new["counter_saving_verified"] is False


def test_re_counter_measures_against_the_agents_request(tmp_path):
    with world(tmp_path) as (c, d):
        h, ah, _, _ = workspace(c, d)
        p = ask(c, ah)
        b = counter(c, h, p["request_id"]).json()["replacement"]
        r = counter(c, h, b["request_id"], {**MANUAL, "amount": "14.00"})
        assert r.status_code == 201, r.text
        assert r.json()["replacement"]["counter_saving"] == "10.99"
        assert r.json()["replacement"]["counter_of"] == b["request_id"]
        assert r.json()["original"]["countered_by"] == r.json()["replacement"]["request_id"]


def test_timeline_records_the_counter_on_both_requests(tmp_path):
    with world(tmp_path) as (c, d):
        h, ah, _, _ = workspace(c, d)
        p = ask(c, ah)
        new = counter(c, h, p["request_id"]).json()["replacement"]

        def events(rid):
            return c.get(f"/v1/payment-requests/{rid}/timeline", headers=h).json()["events"]
        old = [e for e in events(p["request_id"]) if e["kind"] == "counter_offer"]
        assert len(old) == 1 and old[0]["actor"] == "owner"
        assert new["request_id"] in old[0]["detail"]
        mine = [e for e in events(new["request_id"]) if e["kind"] == "counter_offer"]
        assert len(mine) == 1 and mine[0]["actor"] == "owner"
        assert p["request_id"] in mine[0]["detail"]
        # The original still records the human rejection through the existing path.
        assert any(e["kind"] == "owner_decision" and "rejected" in e["label"]
                   for e in events(p["request_id"]))
        assert not any(e["kind"] == "owner_decision" for e in events(new["request_id"]))
        assert c.get(f"/v1/payment-requests/{new['request_id']}/timeline",
                     headers=ah).status_code == 403


def test_agent_sees_only_the_countered_by_link(tmp_path):
    with world(tmp_path) as (c, d):
        h, ah, _, _ = workspace(c, d)
        p = ask(c, ah)
        new = counter(c, h, p["request_id"]).json()["replacement"]
        old_agent = c.get("/v1/payment-requests/" + p["request_id"], headers=ah).json()
        assert old_agent["status"] == "rejected"
        assert old_agent["countered_by"] == new["request_id"]
        new_agent = c.get("/v1/payment-requests/" + new["request_id"], headers=ah).json()
        listed = c.get("/v1/payment-requests", headers=ah).json()["payment_requests"]
        assert {v["request_id"] for v in listed} == {p["request_id"], new["request_id"]}
        for view in [old_agent, new_agent, *listed]:
            for name in OWNER_ONLY:
                assert view[name] is None, name
            assert view["counter_saving_verified"] is False
            assert view["risk_signals"] == []
            assert view["owner_decision"] is None
            assert view["policy_block"] is None
            assert view["agent_context"] is None
        # Another owner's agent cannot read either request.
        _, other, _, _ = workspace(c, d, "bob")
        for rid in (p["request_id"], new["request_id"]):
            assert c.get("/v1/payment-requests/" + rid, headers=other).status_code == 404


def test_lab_requests_can_be_countered_and_executed_by_the_lab_agent(tmp_path):
    with world(tmp_path) as (c, d):
        h = bearer(owner(d))
        p = c.post("/v1/lab/payment-requests", headers=h, json={
            "amount": "24.99", "currency": "USD", "merchant_name": "Unfamiliar Shop",
            "merchant_domain": "unfamiliar-shop.example", "purpose": "USB-C hub"}).json()
        new = counter(c, h, p["request_id"]).json()["replacement"]
        lab = "/v1/lab/payment-requests/" + new["request_id"] + "/execute"
        assert c.post(lab, headers=h).status_code == 409
        assert c.post("/v1/payment-requests/" + new["request_id"] + "/approve", headers=h,
                      json={"terms_sha256": new["terms_sha256"]}).status_code == 200
        done = c.post(lab, headers=h)
        assert done.status_code == 200, done.text
        assert done.json()["counter_of"] == p["request_id"]


def test_hosted_amount_ceiling_applies_to_the_counter(tmp_path):
    from cryptography.fernet import Fernet
    env = {"AGENTPAL_MODE": "hosted", "AGENTPAL_SECRETS_KEY": Fernet.generate_key().decode(),
           "PAYPAL_SANDBOX_CLIENT_ID": "synthetic-server-client",
           "PAYPAL_SANDBOX_CLIENT_SECRET": "synthetic-server-private"}
    with world(tmp_path, env) as (c, d):
        h, ah, _, _ = workspace(c, d, limit="50.00")
        # A pre-existing large wallet must not let a counter escape the hosted ceiling.
        with d.wallet_store.tx() as db:
            db.execute("UPDATE platform_wallets SET monthly_budget='1000',"
                       "per_transaction_hard_limit='1000'")
        p = ask(c, ah, amount="10.00")
        r = counter(c, h, p["request_id"], {**MANUAL, "amount": "50.01"})
        assert r.status_code == 422, r.text
        assert r.json() == {"error": "hosted_amount_limit_exceeded"}
        assert links(d) == []
        assert c.get("/v1/payment-requests/" + p["request_id"],
                     headers=h).json()["status"] == "awaiting_approval"


def test_recurring_request_is_never_turned_into_a_one_off(tmp_path):
    with world(tmp_path) as (c, d):
        h, ah, _, _ = workspace(c, d)
        p = ask(c, ah, amount="9.00", recurring=True, billing_interval="monthly")
        if p["status"] != "awaiting_approval":
            pytest.skip("recurring requests do not reach approval in this policy")
        r = counter(c, h, p["request_id"])
        assert r.status_code == 409 and r.json()["error"] == "counter_recurring_unsupported"
        assert links(d) == []
        assert c.get("/v1/payment-requests/" + p["request_id"],
                     headers=h).json()["status"] == "awaiting_approval"


def test_concurrent_counters_create_exactly_one_replacement(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    with world(tmp_path) as (c, d):
        h, ah, _, _ = workspace(c, d)
        p = ask(c, ah)
        bodies = [MANUAL, MANUAL, {**MANUAL, "amount": "12.00"}, {**MANUAL, "amount": "11.00"}]
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda b: counter(c, h, p["request_id"], b), bodies))
        assert all(r.status_code in (200, 201, 409) for r in results), [r.text for r in results]
        rows = links(d)
        assert len(rows) == 1
        views = c.get("/v1/payment-requests", headers=h).json()["payment_requests"]
        assert len(views) == 2
        new = next(v for v in views if v["request_id"] != p["request_id"])
        # The link names the one replacement that exists, with its real amount.
        assert rows[0]["replacement_request_id"] == new["request_id"]
        assert rows[0]["replacement_amount"] == new["terms"]["amount"]
        assert new["status"] == "awaiting_approval" and new["counter_of"] == p["request_id"]
        # Exactly one decline took effect; losing attempts are audited refusals, and
        # nothing was ever approved.
        decisions = [x["decision"] for x in
                     d.wallet_store.query("SELECT * FROM wallet_owner_decisions")]
        assert decisions.count("declined") == 1 and "approved" not in decisions


def test_sdk_owner_counter_and_agent_client_has_no_counter_method():
    import asyncio
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk" / "python"))
    from agentpal_sdk import AgentClient, OwnerClient, PermissionDeniedError
    from test_platform_fixtures import ALL_PAY, OWNER_A, build_world
    w = build_world()
    _, key, _ = w.funded_agent(caps=ALL_PAY)
    transport = httpx.ASGITransport(app=w.app)
    assert not hasattr(AgentClient, "counter_payment_request")

    async def run():
        async with AgentClient(base_url="http://testserver", credential=key,
                               transport=transport) as agent, \
                OwnerClient(base_url="http://testserver", credential=OWNER_A,
                            transport=transport) as boss:
            pay = await agent.create_payment_request(
                amount="9.00", currency="USD", merchant_name="Unfamiliar Shop",
                merchant_domain="unfamiliar-shop.example", purpose="USB-C hub")
            with pytest.raises(TypeError):
                await boss.counter_payment_request(pay["request_id"], amount=5.0,
                                                   merchant_name="Known Retailer")
            out = await boss.counter_payment_request(
                pay["request_id"], amount="6.00", merchant_name="Known Retailer",
                merchant_domain="known-retailer.example")
            new = out["replacement"]
            assert out["original"]["countered_by"] == new["request_id"]
            assert new["status"] == "awaiting_approval" and new["counter_saving"] == "3.00"
            seen = await agent.get_payment_request(pay["request_id"])
            assert seen["countered_by"] == new["request_id"] and seen["counter_saving"] is None
            # An agent credential pointed at the owner client is refused by the server.
            with pytest.raises((PermissionDeniedError, ValueError)):
                async with OwnerClient(base_url="http://testserver", credential=key,
                                       transport=transport) as fake:
                    await fake.counter_payment_request(new["request_id"], amount="1.00",
                                                       merchant_name="X")
    asyncio.run(run())
