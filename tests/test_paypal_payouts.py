"""P3: real API, identity, policy and ledger; only PayPal HTTP is replaced."""

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import json

import httpx
import pytest

from test_infra_integration_platform import bearer, prepare
from test_infra_integration_platform import world as wired_world  # noqa: F401
from agentpal.providers.config import PayPalSandboxConfig
from agentpal.providers.paypal_sandbox import PayPalSandboxProvider


BODY = {"receiver_email": "freelancer@example.com", "amount": "1.00", "currency": "USD",
        "note": "Sandbox freelancer invoice", "purpose": "Sandbox only"}
CAPS = ["wallet.read", "payment.prepare", "owner.request_decision",
        "payment.execute_sandbox", "payout.request"]


class PayoutHTTP:
    """Documented Payouts v1 batch/item shapes, using fake identifiers and credentials."""

    def __init__(self):
        self.requests = []
        self.body = None
        self.status = "SUCCESS"
        self.post_error = None
        self.read_error = None
        self.change = lambda data: data

    @property
    def posts(self):
        return [r for r in self.requests if r.method == "POST"
                and r.url.path == "/v1/payments/payouts"]

    def batch(self):
        return self.change({"batch_header": {"payout_batch_id": "BATCH123456789",
            "batch_status": "SUCCESS", "sender_batch_header": self.body["sender_batch_header"]},
            "items": [{"payout_item_id": "ITEM123456789", "payout_batch_id": "BATCH123456789",
                "transaction_status": self.status, "payout_item": self.body["items"][0]}],
            "total_items": 1, "total_pages": 1})

    def handler(self, request):
        self.requests.append(request)
        assert request.url.host == "api-m.sandbox.paypal.com"
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "fake-payout-token",
                                           "token_type": "Bearer", "expires_in": 3600})
        if request.method == "POST":
            assert request.url.path == "/v1/payments/payouts"
            self.body = json.loads(request.content)
            if self.post_error == "timeout":
                raise httpx.ReadTimeout("injected fake timeout", request=request)
            if self.post_error:
                return httpx.Response(self.post_error, json={"name": "FAKE_ERROR",
                    "message": "fake secret body must never escape"})
            return httpx.Response(201, json={"batch_header": {
                "payout_batch_id": "BATCH123456789", "batch_status": "PENDING",
                "sender_batch_header": self.body["sender_batch_header"]}})
        assert request.url.path == "/v1/payments/payouts/BATCH123456789"
        if self.read_error:
            raise httpx.ReadTimeout("injected fake read error", request=request)
        return httpx.Response(200, json=self.batch())


@pytest.fixture
def world(wired_world):  # noqa: F811
    client, deps, token, _, _, aid, _ = wired_world
    response = client.put(f"/v1/agents/{aid}/permissions", headers=bearer(token),
                          json={"capabilities": CAPS})
    assert response.status_code == 200
    fake = PayoutHTTP()
    deps.provider = PayPalSandboxProvider(PayPalSandboxConfig(
        client_id="fake-p3-client", client_secret="fake-p3-secret"),
        http_client=httpx.Client(transport=httpx.MockTransport(fake.handler)))
    yield wired_world, fake


def create(world, *, key="payout-test-0001", body=None):
    w, _ = world
    return w[0].post("/v1/payout-requests", headers={**bearer(w[4]), "Idempotency-Key": key},
                     json=BODY if body is None else body)


def approved(world):
    w, _ = world
    response = create(world)
    assert response.status_code == 201, response.text
    payout = response.json()
    path = "/v1/payout-requests/" + payout["request_id"]
    decision = w[0].post(path + "/approve", headers=bearer(w[2]),
                         json={"terms_sha256": payout["terms_sha256"]})
    assert decision.status_code == 200, decision.text
    return payout, path


def test_owner_approved_payout_settles_shared_ledger_once(world):
    w, fake = world
    payout, path = approved(world)
    result = w[0].post(path + "/execute", headers=bearer(w[4]))
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "provider_confirmed"
    assert result.json()["is_real_money"] is False
    assert w[1].risk_service.store.ledger_entry(payout["request_id"])["state"] == "completed"
    assert w[0].post(path + "/execute", headers=bearer(w[4])).json() == result.json()
    assert len(fake.posts) == 1
    assert len(w[1].risk_service.store.query("SELECT * FROM payment_outcomes")) == 1
    assert w[1].risk_service.store.approvals(payout["request_id"])[0]["consumed_at"]
    assert fake.posts[0].headers["PayPal-Request-Id"]
    assert len(fake.body["items"]) == 1
    assert fake.body["items"][0]["recipient_type"] == "EMAIL"
    assert fake.body["items"][0]["receiver"] == BODY["receiver_email"]
    allowance = w[0].get("/v1/me/wallet", headers=bearer(w[4])).json()["wallets"][0]["allowance"]
    assert Decimal(allowance["spent"]) == Decimal("1.00")


@pytest.mark.parametrize("field,value", [("receiver_email", "other@example.com"),
    ("amount", "2.00"), ("currency", "EUR"), ("note", "changed"), ("purpose", "changed")])
def test_idempotency_replays_original_response_and_conflicts_on_changed_body(world, field, value):
    first = create(world)
    assert first.status_code == 201
    replay = create(world)
    assert replay.status_code == 200 and replay.json() == first.json()
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert create(world, body={**BODY, field: value}).status_code == 409
    assert world[1].requests == []


@pytest.mark.parametrize("body", [{**BODY, "amount": 1.0}, {**BODY, "amount": "NaN"},
    {**BODY, "amount": "0.001"}, {**BODY, "amount": "0.00"},
    {**BODY, "note": "x" * 256}, {**BODY, "purpose": "x" * 256},
    {**BODY, "note": "line\nbreak"}, {**BODY, "purpose": "tab\tcontrol"},
    {**BODY, "receiver_email": "a@localhost"}, {**BODY, "receiver_email": "A <a@example.com>"},
    {**BODY, "receiver_email": "a@example.com\n"}, {**BODY, "receiver_email": "a..b@example.com"},
    {**BODY, "receiver_email": "a@-example.com"}, {**BODY, "status": "approved"}])
def test_input_boundaries_are_strict_before_provider_io(world, body):
    assert create(world, body=body).status_code == 422
    assert world[1].requests == []


def test_missing_idempotency_and_current_capability_are_required(world):
    w, fake = world
    assert w[0].post("/v1/payout-requests", headers=bearer(w[4]), json=BODY).status_code == 400
    w[0].put(f"/v1/agents/{w[5]}/permissions", headers=bearer(w[2]),
              json={"capabilities": ["wallet.read"]})
    assert create(world).status_code == 403
    assert fake.requests == []


def test_request_reserves_hold_and_rejection_releases_exactly_once(world):
    w, _ = world
    payout = create(world).json()
    store = w[1].risk_service.store
    assert store.ledger_entry(payout["request_id"])["state"] == "reserved"
    path = "/v1/payout-requests/" + payout["request_id"]
    assert w[0].post(path + "/reject", headers=bearer(w[2])).status_code == 200
    assert store.ledger_entry(payout["request_id"])["state"] == "released"
    assert w[0].post(path + "/reject", headers=bearer(w[2])).status_code == 409
    assert len([e for e in store.ledger_events(payout["request_id"])
                if e["to_state"] == "released"]) == 1


@pytest.mark.parametrize("operation", ["", "/approve", "/reject", "/execute", "/reconciliation"])
def test_other_owner_and_agent_cannot_access_payout(world, operation):
    w, fake = world
    payout, path = approved(world)
    identity = w[1].owner_authenticator
    identity.create_owner("payout_other_owner")
    other, _ = identity.issue_token("payout_other_owner")
    reg = w[0].post("/v1/agents", headers=bearer(other), json={"name": "Other agent",
        "capabilities": CAPS}).json()
    for token in (other, reg["api_key"]):
        method = "GET" if not operation else "POST"
        response = w[0].request(method, path + operation, headers=bearer(token),
            json={"terms_sha256": payout["terms_sha256"]} if operation == "/approve" else None)
        assert response.status_code == (403 if (token == other and operation == "/execute")
                                        or (token == reg["api_key"] and operation in ("", "/approve", "/reject")) else 404)
    assert fake.requests == []


def test_agent_never_approves_and_owner_never_executes(world):
    w, fake = world
    payout = create(world).json()
    path = "/v1/payout-requests/" + payout["request_id"]
    assert w[0].post(path + "/approve", headers=bearer(w[4]),
        json={"terms_sha256": payout["terms_sha256"]}).status_code == 403
    assert w[0].post(path + "/execute", headers=bearer(w[2])).status_code == 403
    assert w[0].post(path + "/execute", headers=bearer(w[4])).status_code == 409
    assert fake.requests == []


def test_digest_mismatch_and_mfa_fail_closed(world):
    w, fake = world
    payout = create(world).json()
    path = "/v1/payout-requests/" + payout["request_id"]
    assert w[0].post(path + "/approve", headers=bearer(w[2]),
                    json={"terms_sha256": "0" * 64}).status_code == 409
    w[1].require_mfa_for_approval = True
    assert w[0].post(path + "/approve", headers=bearer(w[2]),
                    json={"terms_sha256": payout["terms_sha256"]}).status_code == 403
    assert fake.requests == []


@pytest.mark.parametrize("change", ["freeze", "revoke", "suspend", "expiry"])
def test_execute_rechecks_controls_and_approval_expiry(world, change):
    w, fake = world
    _, path = approved(world)
    if change == "freeze":
        w[0].post(f'/v1/wallets/{w[6]["wallet_id"]}/freeze', headers=bearer(w[2]))
    elif change == "revoke":
        w[0].put(f"/v1/agents/{w[5]}/permissions", headers=bearer(w[2]),
                  json={"capabilities": ["payout.request"]})
    elif change == "suspend":
        w[0].post(f"/v1/agents/{w[5]}/suspend", headers=bearer(w[2]))
    else:
        now = w[1].risk_service.clock()
        w[1].risk_service.clock = lambda: now + timedelta(hours=1)
    assert w[0].post(path + "/execute", headers=bearer(w[4])).status_code in (401, 403, 409)
    assert fake.requests == []


@pytest.mark.parametrize("status", ["PENDING", "ONHOLD", "UNCLAIMED"])
def test_nonfinal_item_status_never_completes_and_retains_hold(world, status):
    w, fake = world
    fake.status = status
    payout, path = approved(world)
    result = w[0].post(path + "/execute", headers=bearer(w[4])).json()
    assert result["status"] == "provider_pending"
    assert result["unclaimed"] is (status == "UNCLAIMED")
    assert w[1].risk_service.store.ledger_entry(payout["request_id"])["state"] == "pending"


@pytest.mark.parametrize("status", ["FAILED", "RETURNED", "BLOCKED", "REFUNDED", "DENIED", "REVERSED"])
def test_failed_or_returned_item_releases_hold_once(world, status):
    w, fake = world
    fake.status = status
    payout, path = approved(world)
    result = w[0].post(path + "/execute", headers=bearer(w[4])).json()
    assert result["status"] in ("failed", "reversed")
    assert w[1].risk_service.store.ledger_entry(payout["request_id"])["state"] in ("failed", "reversed")
    for _ in range(2):
        assert w[0].post(path + "/reconciliation", headers=bearer(w[2])).status_code == 200
    assert len(w[1].risk_service.store.query("SELECT * FROM payment_outcomes")) == 1


@pytest.mark.parametrize("field", ["amount", "currency", "receiver", "batch", "item_batch", "sender", "item", "note", "count"])
def test_verified_read_must_match_every_payout_term(world, field):
    w, fake = world
    def change(data):
        data = deepcopy(data)
        item = data["items"][0]["payout_item"]
        if field == "amount":
            item["amount"]["value"] = "2.00"
        elif field == "currency":
            item["amount"]["currency"] = "EUR"
        elif field == "receiver":
            item["receiver"] = "attacker@example.com"
        elif field == "batch":
            data["batch_header"]["payout_batch_id"] = "OTHERBATCH123"
        elif field == "item_batch":
            data["items"][0]["payout_batch_id"] = "OTHERBATCH123"
        elif field == "sender":
            data["batch_header"]["sender_batch_header"]["sender_batch_id"] = "other"
        elif field == "item":
            item["sender_item_id"] = "other"
        elif field == "note":
            item["note"] = "other"
        else:
            data["items"].append(deepcopy(data["items"][0]))
        return data
    fake.change = change
    payout, path = approved(world)
    result = w[0].post(path + "/execute", headers=bearer(w[4])).json()
    assert result["status"] == "reconciliation_required"
    assert w[1].risk_service.store.ledger_entry(payout["request_id"])["state"] == "reconciliation_required"
    assert w[1].risk_service.store.query("SELECT * FROM payment_outcomes") == []


def test_read_timeout_recovers_only_by_verified_read_without_another_post(world):
    w, fake = world
    fake.read_error = True
    payout, path = approved(world)
    first = w[0].post(path + "/execute", headers=bearer(w[4])).json()
    assert first["status"] == "reconciliation_required"
    assert w[0].post(path + "/execute", headers=bearer(w[4])).json()["status"] == "reconciliation_required"
    fake.read_error = False
    assert w[0].post(path + "/reconciliation", headers=bearer(w[4])).json()["status"] == "provider_confirmed"
    assert len(fake.posts) == 1
    assert w[1].risk_service.store.ledger_entry(payout["request_id"])["state"] == "completed"


@pytest.mark.parametrize("error", ["timeout", 500, 302, 400, 409, 422])
def test_create_errors_are_sanitized_and_never_retried(world, error):
    w, fake = world
    fake.post_error = error
    payout, path = approved(world)
    result = w[0].post(path + "/execute", headers=bearer(w[4]))
    assert result.status_code == 200
    assert result.json()["status"] == ("failed" if error == 422 else "reconciliation_required")
    assert "fake secret body" not in result.text
    w[0].post(path + "/execute", headers=bearer(w[4]))
    assert len(fake.posts) == 1
    if error != 422:
        assert w[0].post(path + "/reconciliation", headers=bearer(w[2])).status_code == 409
        assert w[1].risk_service.store.ledger_entry(payout["request_id"])["state"] == "reconciliation_required"


def test_budget_includes_payment_and_payout_holds_in_both_directions(world):
    w, fake = world
    w[0].patch(f'/v1/wallets/{w[6]["wallet_id"]}', headers=bearer(w[2]),
                json={"monthly_budget": "1.00", "per_transaction_limit": "1.00",
                      "per_transaction_review_limit": "1.00", "rolling_window_limit": "1.00"})
    payout = create(world)
    assert payout.status_code == 201
    assert create(world, key="payout-test-0002").json()["error"] == "budget_exceeded"
    payment = w[0].post("/v1/payment-requests", headers={**bearer(w[4]),
        "Idempotency-Key": "payment-after-payout"}, json={"amount": "1.00", "currency": "USD",
        "merchant_name": "Example Hosting", "merchant_domain": "example.com", "purpose": "test"})
    assert payment.json()["status"] == "rejected"
    w[0].post("/v1/payout-requests/" + payout.json()["request_id"] + "/reject", headers=bearer(w[2]))
    prepare(w)
    assert create(world, key="payout-test-0003").json()["error"] == "budget_exceeded"
    assert fake.requests == []


@pytest.mark.parametrize("field", ["receiver_email", "amount", "currency", "note", "purpose", "wallet_id"])
def test_tampered_terms_cannot_use_an_existing_approval_even_with_a_new_digest(world, field):
    from agentpal.platform.payout_models import digest
    w, fake = world
    payout, path = approved(world)
    with w[1].wallet_store.tx() as c:
        row = c.execute("SELECT terms_json FROM platform_payout_requests WHERE request_id=?",
                        (payout["request_id"],)).fetchone()
        terms = json.loads(row[0])
        terms[field] = {"receiver_email": "attacker@example.com", "amount": "2.00",
            "currency": "EUR", "note": "changed", "purpose": "changed",
            "wallet_id": "wal_" + "0" * 32}[field]
        c.execute("UPDATE platform_payout_requests SET terms_json=?,terms_sha256=? WHERE request_id=?",
                  (json.dumps(terms), digest(terms), payout["request_id"]))
    assert w[0].post(path + "/execute", headers=bearer(w[4])).status_code == 409
    assert fake.requests == []
    assert w[1].risk_service.store.ledger_entry(payout["request_id"])["state"] == "approved"


def test_unbound_terms_tampering_is_refused_before_owner_approval(world):
    w, fake = world
    payout = create(world).json()
    with w[1].wallet_store.tx() as c:
        row = c.execute("SELECT terms_json FROM platform_payout_requests WHERE request_id=?",
                        (payout["request_id"],)).fetchone()
        terms = json.loads(row[0])
        terms["note"] = "unapproved terms"
        c.execute("UPDATE platform_payout_requests SET terms_json=? WHERE request_id=?",
                  (json.dumps(terms), payout["request_id"]))
    assert w[0].post("/v1/payout-requests/" + payout["request_id"] + "/approve",
        headers=bearer(w[2]), json={"terms_sha256": payout["terms_sha256"]}).status_code == 409
    assert fake.requests == []


@pytest.mark.parametrize("policy", ["frozen", "missing", "hard_limit", "self_receiver"])
def test_request_policy_failures_are_audited_and_reserve_nothing(world, policy):
    w, fake = world
    body = dict(BODY)
    if policy == "frozen":
        w[0].post(f'/v1/wallets/{w[6]["wallet_id"]}/freeze', headers=bearer(w[2]))
    elif policy == "missing":
        body["currency"] = "EUR"
    elif policy == "hard_limit":
        body["amount"] = "11.00"
    else:
        with w[1].wallet_store.tx() as c:
            c.execute("UPDATE platform_agent_profiles SET metadata_json=? WHERE agent_id=?",
                      (json.dumps({"identity_email": "FREELANCER@example.com"}), w[5]))
    response = create(world, body=body)
    assert response.status_code == 409
    codes = {"frozen": "wallet_frozen", "missing": "no_wallet_for_currency",
             "hard_limit": "per_transaction_limit_exceeded", "self_receiver": "self_payout_forbidden"}
    assert response.json() == {"error": codes[policy]}
    assert w[1].wallet_store.query("SELECT * FROM platform_payout_events WHERE code=?", (codes[policy],))
    assert w[1].risk_service.store.query("SELECT * FROM ledger_entries") == []
    assert fake.requests == []


def test_list_and_get_are_owner_only_and_filtered(world):
    w, _ = world
    payout = create(world).json()
    path = "/v1/payout-requests/" + payout["request_id"]
    assert w[0].get(path, headers=bearer(w[2])).json() == payout
    assert w[0].get("/v1/payout-requests", headers=bearer(w[2])).json()["payout_requests"] == [payout]
    identity = w[1].owner_authenticator
    identity.create_owner("owner_payout_list")
    token, _ = identity.issue_token("owner_payout_list")
    assert w[0].get("/v1/payout-requests", headers=bearer(token)).json()["payout_requests"] == []
    assert w[0].get("/v1/payout-requests", headers=bearer(w[4])).status_code == 403


def test_revoked_session_refused_at_gateway_and_http(world):
    from agentpal.platform.errors import PlatformError
    from agentpal.platform.payouts import PayoutDesk
    w, fake = world
    payout = create(world).json()
    identity = w[1].owner_authenticator
    owner = identity.authenticate(w[2])
    identity.revoke_token(w[3].credential_id)
    path = "/v1/payout-requests/" + payout["request_id"]
    assert w[0].post(path + "/approve", headers=bearer(w[2]),
        json={"terms_sha256": payout["terms_sha256"]}).status_code == 401
    with pytest.raises(PlatformError) as error:
        PayoutDesk(w[0].app.state.platform).decide(owner, payout["request_id"], payout["terms_sha256"])
    assert error.value.status == 403
    assert fake.requests == []


def test_parallel_create_and_execute_reserve_and_submit_once(world):
    from concurrent.futures import ThreadPoolExecutor
    w, fake = world
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: create(world), range(4)))
    assert sorted(r.status_code for r in responses) == [200, 200, 200, 201]
    assert len({r.json()["request_id"] for r in responses}) == 1
    payout = responses[0].json()
    path = "/v1/payout-requests/" + payout["request_id"]
    assert w[0].post(path + "/approve", headers=bearer(w[2]),
        json={"terms_sha256": payout["terms_sha256"]}).status_code == 200
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: w[0].post(path + "/execute", headers=bearer(w[4])), range(4)))
    assert all(r.status_code == 200 for r in responses)
    assert len(fake.posts) == 1
    assert len(w[1].risk_service.store.query("SELECT * FROM ledger_entries")) == 1


def test_restart_preserves_provider_batch_and_never_resends(world):
    from agentpal.api import create_app
    from agentpal.auth import TokenRegistry
    from agentpal.platform.wiring import build_platform
    from fastapi.testclient import TestClient
    from test_infra_integration_platform import settings_for
    w, fake = world
    fake.read_error = True
    payout, path = approved(world)
    assert w[0].post(path + "/execute", headers=bearer(w[4])).json()["status"] == "reconciliation_required"
    settings = settings_for(w[1].risk_service.store.path)
    deps = build_platform(settings=settings, environ={})
    fake.read_error = False
    deps.provider = w[1].provider
    try:
        app = create_app(service=deps.risk_service, settings=settings,
                         registry=TokenRegistry(), platform=deps)
        with TestClient(app, base_url="http://127.0.0.1") as client:
            assert client.post(path + "/execute", headers=bearer(w[4])).json()["status"] == "reconciliation_required"
            assert client.post(path + "/reconciliation", headers=bearer(w[2])).json()["status"] == "provider_confirmed"
        assert len(fake.posts) == 1
        assert deps.risk_service.store.ledger_entry(payout["request_id"])["state"] == "completed"
    finally:
        deps.wallet_store.close()
        deps.owner_authenticator.store.close()
        deps.risk_service.store.close()


def test_verified_reversal_after_success_returns_budget_once(world):
    w, fake = world
    payout, path = approved(world)
    assert w[0].post(path + "/execute", headers=bearer(w[4])).json()["status"] == "provider_confirmed"
    fake.status = "REFUNDED"
    for _ in range(2):
        assert w[0].post(path + "/reconciliation", headers=bearer(w[2])).json()["status"] == "reversed"
    assert len([e for e in w[1].risk_service.store.ledger_events(payout["request_id"])
                if e["to_state"] == "reversed"]) == 1


def test_batch_persistence_failure_is_unknown_and_never_resubmitted(world, monkeypatch):
    w, fake = world
    payout, path = approved(world)
    def unavailable(*args, **kwargs):
        raise RuntimeError("injected database failure after PayPal acceptance")
    monkeypatch.setattr(w[1].risk_service.store, "record_provider_order", unavailable)
    result = w[0].post(path + "/execute", headers=bearer(w[4]))
    assert result.status_code == 200
    assert result.json()["status"] == "reconciliation_required"
    assert w[0].post(path + "/execute", headers=bearer(w[4])).json()["status"] == "reconciliation_required"
    assert len(fake.posts) == 1
    assert w[1].risk_service.store.ledger_entry(payout["request_id"])["state"] == "reconciliation_required"


def test_unbound_terms_tampering_cannot_be_shown_as_approved_terms(world):
    w, _ = world
    payout, path = approved(world)
    with w[1].wallet_store.tx() as c:
        row = c.execute("SELECT terms_json FROM platform_payout_requests WHERE request_id=?",
                        (payout["request_id"],)).fetchone()
        terms = json.loads(row[0])
        terms["note"] = "unapproved terms"
        c.execute("UPDATE platform_payout_requests SET terms_json=? WHERE request_id=?",
                  (json.dumps(terms), payout["request_id"]))
    assert w[0].get(path, headers=bearer(w[2])).status_code == 409


@pytest.mark.parametrize("configured", [False, True])
def test_non_paypal_or_non_sandbox_provider_never_consumes_approval(world, configured):
    from agentpal.platform.wiring import LocalSandboxProvider
    w, fake = world
    payout, path = approved(world)
    if configured:
        w[1].provider.environment = "live"
    else:
        w[1].provider = LocalSandboxProvider()
    assert w[0].post(path + "/execute", headers=bearer(w[4])).status_code == 503
    assert w[1].risk_service.store.approvals(payout["request_id"])[0]["consumed_at"] is None
    assert fake.requests == []
