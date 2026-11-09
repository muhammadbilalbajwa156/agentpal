"""Credentials, role ceilings, tenant oracles and exact approval terms."""
import asyncio
from datetime import timedelta
import json

import httpx
import pytest

from agentpal.contracts import AgentContext, Capability
from agentpal.infra_contracts import AuthenticatedOwner
from agentpal.platform.payout_models import digest
from agentpal.spending import ApprovalTerms
from agentpal.wallet.principals import NotAuthorized
from agentpal.workforce.roles import Role, ROLE_CAPABILITY_CEILING


@pytest.mark.parametrize("role", list(Role))
@pytest.mark.parametrize("kind", ["payment", "payout"])
def test_no_agent_role_can_approve_or_reject(world, role, kind):
    world.register("role", role=role.value,
                   capabilities=[c.value for c in ROLE_CAPABILITY_CEILING[role]])
    data, path = world.prepared(kind)
    for suffix in ("/approve", "/reject"):
        r = world.call("POST", path + suffix, "role", json={"terms_sha256": data["terms_sha256"]})
        assert r.status_code == 403
    assert not world.deps.risk_service.store.approvals(data["request_id"])
    assert not world.backend.calls


def test_sdk_and_forged_principal_do_not_turn_an_agent_key_into_owner(world):
    from agentpal_sdk import OwnerClient, AgentClient, PermissionDeniedError
    data, path = world.prepared()
    with pytest.raises(ValueError, match="wrong credential type"):
        OwnerClient(base_url="http://127.0.0.1", credential=world.tokens["a"])

    async def exercise():
        async with AgentClient(base_url="http://127.0.0.1", credential=world.tokens["a"],
                               transport=httpx.ASGITransport(app=world.app)) as agent:
            with pytest.raises(PermissionDeniedError):
                await agent._request("POST", path.removeprefix("/v1/") + "/approve",
                                     body={"terms_sha256": data["terms_sha256"]})
    asyncio.run(exercise())
    # Trusted in-process gateway independently rechecks a claimed owner session.
    real = world.deps.owner_authenticator.authenticate(world.tokens["owner_a"])
    forged = AuthenticatedOwner(**{**real.model_dump(), "owner_id": world.agents["a"]})
    with pytest.raises(NotAuthorized):
        world.deps.approvals.approve(forged, data["request_id"], terms_sha256=data["terms_sha256"])
    assert not world.deps.risk_service.store.approvals(data["request_id"])


@pytest.mark.parametrize("role", list(Role))
def test_role_ceiling_cannot_be_raised_at_registration_or_grant(world, role):
    forbidden = next(c for c in Capability if c not in ROLE_CAPABILITY_CEILING[role])
    r = world.call("POST", "/v1/agents", json=dict(name="over ceiling", role=role.value,
                                                    capabilities=[forbidden.value]))
    assert (r.status_code, r.json()["error"]) == (400, "above_role_ceiling")
    world.register("limited", role=role.value, capabilities=[])
    r = world.call("PUT", f"/v1/agents/{world.agents['limited']}/permissions",
                   json={"capabilities": [forbidden.value]})
    assert (r.status_code, r.json()["error"]) == (400, "above_role_ceiling")
    assert world.call("GET", f"/v1/agents/{world.agents['limited']}/permissions").json()["capabilities"] == []


@pytest.mark.parametrize("field,value", [
    ("amount", "2.00"), ("currency", "EUR"), ("merchant_name", "Substituted merchant"),
    ("merchant_domain", "other.example"), ("merchant_paypal_email", "other@example.com"),
    ("purpose", "Changed purpose"), ("agent_id", "other_agent"), ("request_id", "other_request")])
def test_payment_ledger_independently_rejects_changed_approved_terms(world, field, value):
    data, path = world.approved(merchant_paypal_email="app@business.example.com")
    # Controlled corruption, NOT an HTTP exploit: even recomputing the wallet digest
    # must not change the independent assessment/approval authority.
    rows = world.deps.wallet_store.query("SELECT terms_json FROM wallet_payment_requests WHERE request_id=?",
                                          (data["request_id"],))
    changed = ApprovalTerms.model_validate({**json.loads(rows[0]["terms_json"]), field: value})
    with world.deps.wallet_store.tx() as c:
        c.execute("UPDATE wallet_payment_requests SET terms_json=?,terms_sha256=? WHERE request_id=?",
                  (changed.model_dump_json(), changed.digest(), data["request_id"]))
    assert world.call("POST", path + "/execute", "a").status_code == 409
    assert not world.backend.calls
    assert world.deps.risk_service.store.approvals(data["request_id"])[0]["consumed_at"] is None


@pytest.mark.parametrize("field,value", [("amount", "2.00"), ("currency", "EUR"),
    ("receiver_email", "other@example.com"), ("note", "Changed note"), ("purpose", "Changed purpose")])
def test_payout_ledger_independently_rejects_changed_approved_terms(world, field, value):
    data, path = world.approved("payout")
    row = world.deps.wallet_store.query("SELECT terms_json FROM platform_payout_requests WHERE request_id=?",
                                       (data["request_id"],))[0]
    changed = {**json.loads(row["terms_json"]), field: value}
    with world.deps.wallet_store.tx() as c:
        c.execute("UPDATE platform_payout_requests SET terms_json=?,terms_sha256=? WHERE request_id=?",
                  (json.dumps(changed), digest(changed), data["request_id"]))
    assert world.call("POST", path + "/execute", "a").status_code == 409
    assert not world.backend.calls
    assert world.deps.risk_service.store.approvals(data["request_id"])[0]["consumed_at"] is None


@pytest.mark.parametrize("kind", ["payment", "payout"])
def test_approval_digest_cannot_replay_across_requests_agents_or_owners(world, kind):
    approved, _ = world.approved(kind)
    for actor in ("a", "sibling", "b"):
        data, path = world.prepared(kind, actor)
        owner = "owner_b" if actor == "b" else "owner_a"
        r = world.call("POST", path + "/approve", owner,
                        json={"terms_sha256": approved["terms_sha256"]})
        assert r.status_code == 409
        assert not world.deps.risk_service.store.approvals(data["request_id"])
        assert world.call("POST", path + "/execute", actor).status_code == 409
    assert not world.backend.calls


@pytest.mark.parametrize("kind", ["payment", "payout"])
def test_http_body_cannot_substitute_approved_terms(world, kind):
    data, path = world.prepared(kind)
    for field, value in (("amount", "0.01"), ("purpose", "approved by agent"),
                         ("merchant_paypal_email", "evil@example.com"),
                         ("receiver_email", "evil@example.com"), ("note", "changed")):
        r = world.call("POST", path + "/approve", json={"terms_sha256": data["terms_sha256"], field: value})
        assert r.status_code == 422
    assert world.call("POST", path + "/approve", json={"terms_sha256": data["terms_sha256"]}).status_code == 200
    assert world.call("PATCH", path, "a", json={"amount": "0.01"}).status_code == 405
    r = world.call("POST", path + "/execute", "a", json={"amount": "0.01",
        "receiver_email": "evil@example.com", "merchant_paypal_email": "evil@example.com"})
    assert r.status_code == 200
    if kind == "payment":
        unit = next(iter(world.backend.orders.values()))["purchase_units"][0]
        assert unit["amount"]["value"] == "1.00"
        assert unit["payee"]["email_address"] == "app@business.example.com"
    else:
        item = next(iter(world.backend.batches.values()))["items"][0]
        assert item["amount"]["value"] == "1.00"
        assert item["receiver"] == "freelancer@example.com"


@pytest.mark.parametrize("kind", ["payment", "payout"])
def test_expired_approval_cannot_move_money(world, kind):
    data, path = world.approved(kind)
    world.now += timedelta(minutes=16)
    assert world.call("POST", path + "/execute", "a").status_code == 409
    assert not world.backend.calls
    assert world.deps.risk_service.store.approvals(data["request_id"])[0]["consumed_at"] is None


@pytest.mark.parametrize("kind", ["payment", "payout"])
def test_foreign_owner_ids_are_identical_to_missing_for_all_money_routes(world, kind):
    data, path = world.prepared(kind)
    missing = f"/v1/{kind}-requests/pay_" + "0" * 32
    routes = [("GET", ""), ("POST", "/approve"), ("POST", "/reject"), ("POST", "/reconciliation")]
    if kind == "payment":
        routes += [("POST", "/capture"), ("GET", "/reconciliation"), ("GET", "/refunds"),
                   ("POST", "/refunds"), ("POST", "/refunds/reconciliation"),
                   ("POST", "/market-check"), ("POST", "/page-check")]
    for method, suffix in routes:
        body = {"terms_sha256": data["terms_sha256"]} if suffix == "/approve" else {}
        rs = [world.call(method, p + suffix, "owner_b", json=body,
                         headers={"Idempotency-Key": "h2-foreign-resource"}) for p in (path, missing)]
        assert [(r.status_code, r.json()) for r in rs] == [(404, {"error": "not_found"})] * 2
    for actor in ("sibling", "b"):
        rs = [world.call("POST", p + "/execute", actor) for p in (path, missing)]
        assert [(r.status_code, r.json()) for r in rs] == [(404, {"error": "not_found"})] * 2
    assert not world.backend.calls


@pytest.mark.parametrize("alias", ["email", "identity_email", "receiver_email", "name"])
def test_self_payout_is_refused_for_every_owner_maintained_alias(world, alias):
    email = "self@example.com"
    name = email if alias == "name" else "selftest"
    world.register(name, metadata={alias: email.upper()} if alias != "name" else {})
    r = world.create("payout", name, receiver_email=email.upper())
    assert (r.status_code, r.json()["error"]) == (409, "self_payout_forbidden")
    assert not world.backend.calls
    assert world.allowance(name)["committed"] == "0.00"


def test_foreign_agent_and_wallet_ids_are_identical_to_absent(world):
    for method, template, body in [
        ("GET", "/agents/{id}", {}), ("GET", "/agents/{id}/permissions", {}),
        ("PUT", "/agents/{id}/permissions", {"capabilities": []}),
        ("POST", "/agents/{id}/credentials", {}),
        ("DELETE", "/agents/{id}/credentials/" + world.keys["a"], {}),
        ("POST", "/agents/{id}/suspend", {}), ("POST", "/agents/{id}/reactivate", {}),
        ("GET", "/wallets/{id}", {}), ("GET", "/wallets/{id}/transactions", {}),
        ("PATCH", "/wallets/{id}", {"monthly_budget": "1.00"}),
        ("POST", "/wallets/{id}/freeze", {}), ("POST", "/wallets/{id}/unfreeze", {}),
    ]:
        wallet = "/wallets/" in template
        target = world.wallets["a", "USD"] if wallet else world.agents["a"]
        absent = ("wal_" if wallet else "agt_") + "0" * 32
        rs = [world.call(method, "/v1" + template.format(id=rid), "owner_b", json=body)
              for rid in (target, absent)]
        assert [(r.status_code, r.json()) for r in rs] == [(404, {"error": "not_found"})] * 2
    assert world.call("GET", "/v1/me", "a").status_code == 200


def test_forged_agent_context_capabilities_owner_and_role_are_not_authority(world):
    world.register("unprivileged", capabilities=["wallet.read"])
    actual = world.platform.authenticate(world.tokens["unprivileged"]).ctx
    for changes in ({"capabilities": frozenset(Capability)},
                    {"role": "payment", "capabilities": frozenset(Capability)},
                    {"owner_id": "h2_owner_b", "capabilities": frozenset(Capability)}):
        ctx = AgentContext(**{**actual.model_dump(), **changes})
        decision = world.platform.checker.check(ctx, Capability.payment_execute_sandbox)
        assert not decision.allowed


def test_plaintext_credentials_never_enter_durable_stores_or_scoped_reads(world):
    data, path = world.approved()
    world.call("POST", path + "/execute", "a")
    views = [world.call("GET", p).text for p in (path, "/v1/agents", "/v1/wallets", "/v1/approvals")]
    stores = (world.deps.wallet_store, world.deps.risk_service.store,
              world.deps.owner_authenticator.store)
    # Open read-only snapshots separately; no direct access to private connection state.
    import sqlite3
    for store in stores:
        with sqlite3.connect("file:" + str(store.path) + "?mode=ro", uri=True) as c:
            views.append("\n".join(c.iterdump()))
    for token in world.tokens.values():
        assert all(token not in view for view in views)
    assert data["request_id"]
