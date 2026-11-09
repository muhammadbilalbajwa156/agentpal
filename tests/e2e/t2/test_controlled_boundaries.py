"""Offline real-app checks needing a trusted clock or an observable egress seam.

These supplement TCP attacks; they are not live provider/browser evidence.
"""
from datetime import datetime, timedelta, timezone
import base64
import os
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from agentpal.api import create_app
from agentpal.config import Settings
from agentpal.platform.wiring import build_platform


@pytest.fixture
def controlled(tmp_path):
    settings = Settings(db_path=str(tmp_path / "risk.db"),
        auth_mode="required", allowed_hosts=["127.0.0.1"],
        rate_limit_db_path=str(tmp_path / "rate.db"), analyzer_chain=["heuristic"],
        require_mfa_for_approval=False)
    deps = build_platform(settings=settings, environ={})
    clock = [datetime.now(timezone.utc)]
    deps.risk_service.clock = lambda: clock[0]
    deps.owner_authenticator.clock = lambda: clock[0]
    deps.lifecycle.lifecycle.clock = lambda: clock[0]
    deps.owner_authenticator.create_owner("t2_controlled")
    token, _ = deps.owner_authenticator.issue_token("t2_controlled")
    app = create_app(settings=settings, service=deps.risk_service, platform=deps)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        owner = {"Authorization": "Bearer " + token}
        registration = client.post("/v1/agents", headers=owner, json={"name": "T2 controlled",
            "capabilities": ["wallet.read", "payment.prepare", "owner.request_decision",
                             "payment.execute_sandbox", "payout.request", "research.fetch"]}).json()
        agent = {"Authorization": "Bearer " + registration["api_key"]}
        response = client.post("/v1/wallets", headers=owner, json={
            "agent_id": registration["agent"]["agent_id"], "currency": "USD",
            "monthly_budget": "5.00", "per_transaction_limit": "5.00"})
        assert response.status_code == 201
        yield client, deps, owner, agent, clock
    deps.close()


def prepare(controlled):
    client, _, _, agent, _ = controlled
    response = client.post("/v1/payment-requests", headers={**agent,
        "Idempotency-Key": "clock-" + uuid4().hex}, json={"amount": "1.00", "currency": "USD",
        "merchant_name": "Northwind", "merchant_domain": "example.com", "purpose": "T2 expiry"})
    assert response.status_code == 201
    return response.json()


def test_expired_approval_and_late_owner_decision_are_refused(controlled):
    client, deps, owner, agent, clock = controlled
    payment = prepare(controlled)
    path = "/v1/payment-requests/" + payment["request_id"]
    assert client.post(path + "/approve", headers=owner,
                       json={"terms_sha256": payment["terms_sha256"]}).status_code == 200
    clock[0] += timedelta(minutes=16)
    assert client.post(path + "/execute", headers=agent).status_code == 409
    assert deps.risk_service.store.provider_order(payment["request_id"],
        agent_id=payment["agent_id"]) is None
    # A new request also expires without ever acquiring a human decision.
    payment = prepare(controlled)
    clock[0] += timedelta(days=2)
    response = client.post("/v1/payment-requests/" + payment["request_id"] + "/approve",
                           headers=owner, json={"terms_sha256": payment["terms_sha256"]})
    assert response.status_code == 409


def test_mfa_unsatisfied_cannot_mutate_approvals_or_refunds(controlled):
    client, deps, owner, agent, _ = controlled
    deps.require_mfa_for_approval = True
    payment = prepare(controlled)
    path = "/v1/payment-requests/" + payment["request_id"]
    for suffix in ("/approve", "/refunds", "/refunds/reconciliation"):
        body = {"terms_sha256": payment["terms_sha256"]} if suffix == "/approve" else {}
        response = client.post(path + suffix, headers={**owner,
            "X-MFA-Satisfied": "true", "Idempotency-Key": "t2-mfa-required"}, json=body)
        assert (response.status_code, response.json()["error"]) == (403, "mfa_required")
    payout = client.post("/v1/payout-requests", headers={**agent,
        "Idempotency-Key": "t2-mfa-payout"}, json={"amount": "1.00", "currency": "USD",
        "receiver_email": "qa@example.com", "purpose": "T2 MFA payout"}).json()
    response = client.post("/v1/payout-requests/" + payout["request_id"] + "/approve",
        headers=owner, json={"terms_sha256": payout["terms_sha256"]})
    assert (response.status_code, response.json()["error"]) == (403, "mfa_required")


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://169.254.169.254/",
                               "http://[::1]/", "http://localhost/"])
def test_public_url_guard_precedes_browser_provider_io(controlled, url):
    client, deps, _, agent, _ = controlled
    class TrapBrowser:
        calls = 0
        def configured(self):
            return True
        def capture(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("Private target reached browser provider")
    trap = TrapBrowser()
    deps.browser = trap
    response = client.post("/v1/research/browse", headers=agent, json={"url": url})
    assert response.status_code == 200 and response.json()["status"] == "no_data"
    assert trap.calls == 0


def test_default_token_rate_limit_returns_retry_after(controlled):
    client, _, _, agent, _ = controlled
    responses = [client.get("/v1/me", headers=agent) for _ in range(61)]
    assert [r.status_code for r in responses] == [200] * 60 + [429]
    assert responses[-1].json() == {"error": "rate_limited"}
    assert 1 <= int(responses[-1].headers["Retry-After"]) <= 60


def test_totp_lockout_cannot_be_reset_by_reenrollment(controlled, monkeypatch):
    from agentpal.identity import IdentityError
    from agentpal.identity.totp import totp
    _, deps, owner, _, clock = controlled
    monkeypatch.setenv("AGENTPAL_MFA_ENCRYPTION_KEY",
                       base64.urlsafe_b64encode(os.urandom(32)).decode())
    identity = deps.owner_authenticator
    token = owner["Authorization"].removeprefix("Bearer ")
    pending = identity.begin_totp_enrollment(token)
    secret = base64.b32decode(pending.secret)
    now = int(clock[0].timestamp())
    valid = {totp(secret, now + delta) for delta in (-30, 0, 30)}
    bad = next(f"{n:06d}" for n in range(100) if f"{n:06d}" not in valid)
    for _ in range(5):
        with pytest.raises(IdentityError):
            identity.confirm_totp_enrollment(token, bad)
    with pytest.raises(IdentityError):
        identity.confirm_totp_enrollment(token, totp(secret, now))
    with pytest.raises(IdentityError):
        identity.begin_totp_enrollment(token)
    row = identity.store.query("SELECT failures,locked_until FROM identity_totp")[0]
    assert row["failures"] == 5
    assert datetime.fromisoformat(row["locked_until"]) == clock[0] + timedelta(minutes=5)
