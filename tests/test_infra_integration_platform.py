"""Real HTTP/WS1 integration: no fake identity, approval, lifecycle or provider."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
import secrets

from fastapi.testclient import TestClient
import pytest

from agentpal.api import create_app
from agentpal.auth import TokenRegistry
from agentpal.config import Settings
from agentpal.infra_contracts import PaymentLifecycle, ProviderError, ProviderEvent, ProviderStatus
from agentpal.platform.wiring import build_platform
from agentpal.lifecycle.recovery import ConsistencyError
from agentpal.providers.paypal_sandbox import PayPalSandboxProvider
from agentpal.spending import LegacyPathDisabled
from agentpal.wallet.principals import NotAuthorized, OwnerPrincipal


def settings_for(path):
    return Settings(db_path=str(path), auth_mode="required", analyzer_chain=["heuristic"],
                    allowed_hosts=["127.0.0.1"], log_level="ERROR")


def bearer(token):
    return {"Authorization": "Bearer " + token}


@pytest.fixture
def world(tmp_path):
    settings = settings_for(tmp_path / "risk.db")
    deps = build_platform(settings=settings, environ={})
    identity = deps.owner_authenticator
    identity.create_owner("owner_integration")
    token, info = identity.issue_token("owner_integration")
    app = create_app(service=deps.risk_service, settings=settings, registry=TokenRegistry(),
                     platform=deps)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        registered = client.post("/v1/agents", headers=bearer(token), json={
            "name": "integration-bot", "capabilities": ["wallet.read", "payment.prepare",
                "owner.request_decision", "payment.execute_sandbox"]})
        assert registered.status_code == 201, registered.text
        agent = registered.json()
        agent_id = agent["agent"]["agent_id"]
        wallet = client.post("/v1/wallets", headers=bearer(token), json={
            "agent_id": agent_id, "currency": "USD", "monthly_budget": "100.00",
            "per_transaction_limit": "10.00"})
        assert wallet.status_code == 201, wallet.text
        yield client, deps, token, info, agent["api_key"], agent_id, wallet.json()
    deps.close()


def prepare(world):
    client, _, _, _, key, _, _ = world
    response = client.post("/v1/payment-requests", headers={**bearer(key),
        "Idempotency-Key": "integration-create"}, json={"amount": "1.00", "currency": "USD",
        "merchant_name": "Example Hosting", "merchant_domain": "example.com",
        "purpose": "Sandbox integration verification", "request_owner_decision": True})
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "awaiting_approval"
    return response.json()


def approve(world, payment):
    client, _, token, *_ = world
    response = client.post(f'/v1/payment-requests/{payment["request_id"]}/approve',
        headers=bearer(token), json={"terms_sha256": payment["terms_sha256"]})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "approved"


def execute(world, payment):
    return world[0].post(f'/v1/payment-requests/{payment["request_id"]}/execute',
                         headers=bearer(world[4]))


def test_real_http_owner_approval_execution_and_status(world):
    client, deps, token, _, key, agent_id, _ = world
    payment = prepare(world)
    assert execute(world, payment).status_code == 409
    approve(world, payment)
    response = execute(world, payment)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "provider_pending"
    path = f'/v1/payment-requests/{payment["request_id"]}'
    assert client.get(path, headers=bearer(key)).json()["status"] == "provider_pending"
    reconciliation = client.get(path + "/reconciliation", headers=bearer(token))
    assert reconciliation.status_code == 200
    ref = reconciliation.json()["provider_references"][0]
    assert ref["provider"] == "local_simulation"
    assert ref["status"] == "pending"
    assert ref["provider_order_id"].startswith("sim-")
    assert deps.lifecycle.reconcile(payment["request_id"]) == PaymentLifecycle.provider_pending
    assert deps.risk_service.store.ledger_entry(payment["request_id"], agent_id=agent_id)[
        "state"] == "pending"
    assert execute(world, payment).status_code == 409


def test_tampered_digest_and_agent_approval_refused(world):
    client, deps, token, _, key, *_ = world
    payment = prepare(world)
    path = f'/v1/payment-requests/{payment["request_id"]}/approve'
    assert client.post(path, headers=bearer(token), json={"terms_sha256": "0" * 64}
                       ).status_code == 409
    assert client.post(path, headers=bearer(key), json={
        "terms_sha256": payment["terms_sha256"]}).status_code == 403
    # The adapter must preserve WS1's independent digest gate as well.
    with pytest.raises(ValueError):
        deps.approvals.approve(deps.owner_authenticator.authenticate(token),
                               payment["request_id"], terms_sha256="0" * 64)
    assert execute(world, payment).status_code == 409


def test_revoked_owner_refused_at_http_and_gateway(world):
    client, deps, token, info, *_ = world
    payment = prepare(world)
    stale_owner = deps.owner_authenticator.authenticate(token)
    deps.owner_authenticator.revoke_token(info.credential_id)
    path = f'/v1/payment-requests/{payment["request_id"]}/approve'
    assert client.post(path, headers=bearer(token), json={
        "terms_sha256": payment["terms_sha256"]}).status_code == 401
    with pytest.raises(NotAuthorized):
        deps.approvals.approve(stale_owner, payment["request_id"],
                               terms_sha256=payment["terms_sha256"])


@pytest.mark.parametrize("operation", ["read", "approve", "reject", "reconciliation"])
def test_cross_owner_payment_access_is_404(world, operation):
    client, deps, *_ = world
    payment = prepare(world)
    deps.owner_authenticator.create_owner("owner_other")
    other, _ = deps.owner_authenticator.issue_token("owner_other")
    path = f'/v1/payment-requests/{payment["request_id"]}'
    if operation in ("read", "reconciliation"):
        response = client.get(path + ("/reconciliation" if operation == "reconciliation" else ""),
                              headers=bearer(other))
    else:
        response = client.post(path + "/" + operation, headers=bearer(other),
                               json={"terms_sha256": payment["terms_sha256"]})
    assert response.status_code == 404


def test_verified_event_sink_is_the_only_completion_path(world):
    client, deps, _, _, key, *_ = world
    payment = prepare(world)
    approve(world, payment)
    assert execute(world, payment).status_code == 200
    ref = deps.lifecycle.provider_references(payment["request_id"])[0]
    event = ProviderEvent(provider=ref.provider, environment="sandbox", event_id="unverified",
        event_type="integration.completed", provider_order_id=ref.provider_order_id,
        status=ProviderStatus.completed, amount=Decimal("1.00"), currency="USD", verified=False,
        received_at=datetime.now(timezone.utc), raw_sha256="0" * 64)
    assert deps.event_sink.apply_provider_event(event) == PaymentLifecycle.provider_pending
    verified = event.model_copy(update={"event_id": "verified", "verified": True})
    assert deps.event_sink.apply_provider_event(verified) == PaymentLifecycle.provider_confirmed
    path = f'/v1/payment-requests/{payment["request_id"]}'
    assert client.get(path, headers=bearer(key)).json()["status"] == "provider_confirmed"


def test_unsigned_webhook_cannot_complete_real_pending_payment(world):
    client, deps, *_ = world
    payment = prepare(world)
    approve(world, payment)
    assert execute(world, payment).status_code == 200
    response = client.post("/v1/providers/paypal/webhooks", json={"verified": True,
        "status": "completed", "request_id": payment["request_id"]})
    assert response.status_code == 400
    assert deps.lifecycle.status(payment["request_id"]) == PaymentLifecycle.provider_pending
    with pytest.raises(ProviderError):
        deps.provider.capture_order("sim-anything", idempotency_key="integration-capture")


def test_factory_enforces_strict_execution(world):
    _, deps, *_ = world
    payment = prepare(world)
    with pytest.raises(LegacyPathDisabled):
        deps.risk_service.store.record_payment_attempt(payment["request_id"], "legacy", "legacy",
                                                       terms=None)


def test_factory_requires_durable_owner(world):
    _, deps, *_ = world
    payment = prepare(world)
    with pytest.raises(NotAuthorized):
        deps.approvals.approve(OwnerPrincipal(owner_id="owner_integration", token_id="operator"),
                               payment["request_id"], terms_sha256=payment["terms_sha256"])


def test_concurrent_execute_consumes_approval_once(world):
    payment = prepare(world)
    approve(world, payment)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: execute(world, payment), range(2)))
    assert sorted(r.status_code for r in responses) == [200, 409]


def test_reconcile_resolves_agent_from_metadata_and_fails_closed_on_mismatch(world):
    _, deps, *_ = world
    payment = prepare(world)
    approve(world, payment)
    assert execute(world, payment).status_code == 200
    with deps.wallet_store.tx() as c:
        c.execute("UPDATE platform_payment_meta SET agent_id=? WHERE request_id=?",
                  ("agt_" + "0" * 32, payment["request_id"]))
    with pytest.raises(LookupError):
        deps.lifecycle.reconcile(payment["request_id"])


def test_startup_recovery_quarantines_pending_and_checks_both_stores(world):
    _, deps, *_ = world
    payment = prepare(world)
    approve(world, payment)
    assert execute(world, payment).status_code == 200
    restarted = build_platform(settings=settings_for(deps.risk_service.store.path), environ={})
    try:
        assert restarted.lifecycle.status(payment["request_id"]) == (
            PaymentLifecycle.reconciliation_required)
    finally:
        restarted.close()


@pytest.mark.parametrize("pair", ["wallet-risk", "identity-risk", "identity-wallet"])
def test_factory_refuses_shared_sqlite_files(tmp_path, pair):
    risk, wallet, identity = (tmp_path / name for name in ("risk.db", "wallet.db", "identity.db"))
    if pair == "wallet-risk":
        wallet = risk
    elif pair == "identity-risk":
        identity = risk
    else:
        identity = wallet
    with pytest.raises(ValueError, match="separate"):
        build_platform(settings=settings_for(risk), wallet_db_path=wallet,
                       identity_db_path=identity, environ={})


def test_partial_paypal_configuration_fails_closed(tmp_path):
    with pytest.raises(ValueError, match="must be set"):
        build_platform(settings=settings_for(tmp_path / "risk.db"),
                       environ={"PAYPAL_SANDBOX_CLIENT_ID": "integration-client"})


def test_real_paypal_selected_and_unsigned_webhook_refused_without_network(tmp_path):
    settings = settings_for(tmp_path / "paypal-risk.db")
    deps = build_platform(settings=settings, environ={
        "PAYPAL_SANDBOX_CLIENT_ID": "integration-client", "PAYPAL_SANDBOX_WEBHOOK_ID": "WH-local",
        "PAYPAL_SANDBOX_CLIENT_SECRET": secrets.token_urlsafe(32)})
    try:
        assert isinstance(deps.provider, PayPalSandboxProvider)
        app = create_app(service=deps.risk_service, settings=settings,
                         registry=TokenRegistry(), platform=deps)
        with TestClient(app, base_url="http://127.0.0.1") as client:
            response = client.post("/v1/providers/paypal/webhooks", json={"verified": True})
            assert response.status_code == 400
    finally:
        deps.close()


def test_startup_consistency_refuses_corrupted_wallet_projection(world):
    _, deps, *_ = world
    payment = prepare(world)
    with deps.wallet_store.tx() as c:
        c.execute("UPDATE wallet_payment_requests SET terms_sha256=? WHERE request_id=?",
                  ("0" * 64, payment["request_id"]))
    with pytest.raises(ConsistencyError):
        build_platform(settings=settings_for(deps.risk_service.store.path), environ={})


def test_real_owner_reject_releases_reservation(world):
    client, deps, token, *_ = world
    payment = prepare(world)
    response = client.post(f'/v1/payment-requests/{payment["request_id"]}/reject',
                           headers=bearer(token))
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    assert execute(world, payment).status_code == 409
    assert deps.risk_service.store.ledger_entry(payment["request_id"])["state"] == "released"


@pytest.mark.parametrize("alias", ["symlink", "hardlink"])
def test_factory_refuses_database_file_aliases(tmp_path, alias):
    risk, wallet = tmp_path / "risk.db", tmp_path / "wallet.db"
    risk.touch()
    if alias == "symlink":
        wallet.symlink_to(risk)
    else:
        wallet.hardlink_to(risk)
    with pytest.raises(ValueError, match="separate"):
        build_platform(settings=settings_for(risk), wallet_db_path=wallet, environ={})
