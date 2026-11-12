"""Judge scenarios use real authenticated creation, policy, approval and ledger paths."""
from decimal import Decimal

import pytest
from cryptography.fernet import Fernet

from agentpal.spending import ApprovalTerms
from agentpal.platform.payments import PaymentDesk
from test_platform_workspace import bearer, owner, world

SCENARIOS = {
    "over_limit": ("blocked", "AMOUNT_ABOVE_HARD_LIMIT", 1, "0.00"),
    "split_purchase": ("blocked", "SPLIT_TRANSACTION_EXCEEDS_HARD_LIMIT", 2, "0.00"),
    "changed_terms": ("refused", "terms_mismatch", 2, "0.00"),
    "payee_swap": ("refused", "terms_mismatch", 2, "0.00"),
}


def env(**extra):
    return {"AGENTPAL_MASTER_KEY": Fernet.generate_key().decode(), **extra}


@pytest.mark.parametrize("name", SCENARIOS)
def test_scenario_owner_only(tmp_path, name):
    with world(tmp_path, env()) as (c, d):
        path = "/v1/lab/scenarios/" + name
        assert c.post(path).status_code == 401
        h = bearer(owner(d))
        agent = c.post("/v1/agents", headers=h, json={"name": "External"}).json()
        assert c.post(path, headers=bearer(agent["api_key"])).status_code == 403
        assert not d.wallet_store.query("SELECT * FROM platform_lab_agents")


def test_unknown_scenario_is_404_without_creating_agent(tmp_path):
    with world(tmp_path, env()) as (c, d):
        assert c.post("/v1/lab/scenarios/unknown", headers=bearer(owner(d))).status_code == 404
        assert not d.wallet_store.query("SELECT * FROM platform_lab_agents")


@pytest.mark.parametrize("name", SCENARIOS)
def test_scenario_real_control_and_only_expected_reservations(tmp_path, name, monkeypatch):
    with world(tmp_path, env()) as (c, d):
        h = bearer(owner(d))
        lab = c.post("/v1/lab/agent", headers=h).json()
        # Any execution or provider use fails this test, including a simulator call.
        def forbidden(*args, **kwargs):
            pytest.fail("Scenarios must never execute, submit or capture")
        for method in ("begin_execution", "submit_to_provider", "capture"):
            monkeypatch.setattr(d.lifecycle, method, forbidden)
        approvals = []
        real_approve = PaymentDesk.approve

        def approve(desk, principal, request_id, terms_sha256):
            approvals.append((request_id, terms_sha256))
            return real_approve(desk, principal, request_id, terms_sha256)

        monkeypatch.setattr(PaymentDesk, "approve", approve)
        response = c.post("/v1/lab/scenarios/" + name, headers=h)
        assert response.status_code == 200, response.text
        result = response.json()
        outcome, code, count, reserved = SCENARIOS[name]
        assert result["scenario"] == name
        assert result["outcome"] == outcome
        assert code in result["rule_codes"]
        assert result["plain_reason"]
        assert len(set(result["request_ids"])) == count
        rid = result["request_ids"][-1]
        assert result["timeline_url_hint"] == f"/v1/payment-requests/{rid}/timeline"
        views = [c.get("/v1/payment-requests/" + r, headers=h).json() for r in result["request_ids"]]
        assert all(v["status"] not in {"approved", "provider_pending", "provider_confirmed"} for v in views)
        # Demonstration parts that reached the queue were declined (never approved),
        # so nothing is left awaiting a decision and no allowance stays reserved.
        assert all(v["status"] != "awaiting_approval" for v in views)
        assert all(v["owner_decision"] is None or v["owner_decision"]["decision"] == "rejected" for v in views)
        assert not any(r in str(c.get("/v1/approvals", headers=h).json()) for r in result["request_ids"])
        if outcome == "blocked":
            assert approvals == []
            assert views[-1]["recommendation"] == "block"
            assert code in {s["code"] for s in views[-1]["risk_signals"]}
            assert rid not in str(c.get("/v1/approvals", headers=h).json())
        else:
            assert approvals == [(rid, views[0]["terms_sha256"])]
            assert all(v["status"] == "rejected" and v["owner_decision"]["decision"] == "rejected" for v in views)
            assert views[0]["terms_sha256"] != views[1]["terms_sha256"]
            assert any(a["status"] == "rejected:v1_terms_mismatch" for a in d.risk_service.store.audit_records(rid))
        if name == "payee_swap":
            original, swapped = (ApprovalTerms.model_validate(v["terms"]) for v in views)
            assert original.merchant_paypal_email != swapped.merchant_paypal_email
            # Isolate payee binding from the separately bound request ID.
            assert swapped.model_copy(update={"merchant_paypal_email": original.merchant_paypal_email}).digest() != swapped.digest()
        wallet = c.get("/v1/wallets/" + lab["wallet_id"], headers=h).json()
        allowance = wallet["allowance"]
        assert Decimal(allowance["reserved"]) == Decimal(reserved)
        assert Decimal(allowance["available"]) == Decimal("200.00") - Decimal(reserved)
        assert Decimal(allowance["spent"]) == Decimal(allowance["pending"]) == 0
        assert c.get(result["timeline_url_hint"], headers=h).status_code == 200


@pytest.mark.parametrize("name", SCENARIOS)
def test_scenarios_share_lab_admission_and_daily_quota(tmp_path, name):
    with world(tmp_path, env(GROQ_API_KEY="synthetic-server", AGENTPAL_SERVER_ERRANDS_DAILY="1")) as (c, d):
        h = bearer(owner(d))
        path = "/v1/lab/scenarios/" + name
        assert c.post(path, headers=h).status_code == 200
        before = len(d.wallet_store.query("SELECT * FROM platform_payment_meta"))
        denied = c.post(path, headers=h)
        assert denied.status_code == 429
        assert denied.json() == {"error": "server_errands_daily_limit"}
        assert int(denied.headers["Retry-After"]) > 0
        assert len(d.wallet_store.query("SELECT * FROM platform_payment_meta")) == before
        for _ in range(8):
            assert c.post(path, headers=h).status_code == 429
        assert c.post(path, headers=h).json() == {"error": "rate_limited"}


@pytest.mark.parametrize("name", SCENARIOS)
def test_scenarios_respect_frozen_lab_wallet(tmp_path, name):
    with world(tmp_path, env()) as (c, d):
        h = bearer(owner(d))
        lab = c.post("/v1/lab/agent", headers=h).json()
        assert c.post("/v1/wallets/" + lab["wallet_id"] + "/freeze", headers=h).status_code == 200
        response = c.post("/v1/lab/scenarios/" + name, headers=h)
        assert response.status_code == 409
        assert response.json() == {"error": "wallet_frozen"}


def test_scenario_respects_hosted_amount_ceiling(tmp_path):
    with world(tmp_path, env(AGENTPAL_MODE="hosted", AGENTPAL_HOSTED_MAX_AMOUNT="20.00",
                            AGENTPAL_SECRETS_KEY=Fernet.generate_key().decode(),
                            PAYPAL_SANDBOX_CLIENT_ID="synthetic-client",
                            PAYPAL_SANDBOX_CLIENT_SECRET="synthetic-secret")) as (c, d):
        h = bearer(owner(d))
        response = c.post("/v1/lab/scenarios/over_limit", headers=h)
        assert response.status_code == 422
        assert response.json() == {"error": "hosted_amount_limit_exceeded"}
        assert not d.wallet_store.query("SELECT * FROM platform_payment_meta")
