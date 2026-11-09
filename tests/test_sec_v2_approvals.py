"""Authenticated, digest-bound approval boundaries (WS1 SEC-15/23)."""

from datetime import datetime, timedelta

import pytest

from agentpal.identity import IdentityStore, OwnerIdentityService
from agentpal.infra_contracts import AuthenticatedOwner
from agentpal.spending import ApprovalTermsMismatch, InvalidLedgerTransition, LedgerState
from agentpal.wallet.principals import NotAuthorized, OwnerPrincipal

from test_wallet_fixtures import build_world, draft


def world():
    w = build_world()
    identity = OwnerIdentityService(IdentityStore(), clock=w.clock)
    identity.create_org("org_alice", "Alice organization")
    identity.create_org("org_bob", "Bob organization")
    identity.create_owner("owner_alice", org_id="org_alice")
    identity.create_owner("owner_bob", org_id="org_bob")
    token, info = identity.issue_token("owner_alice", timedelta(hours=1))
    owner = identity.authenticate(token)
    w.wallet.identity = identity
    w.wallet.require_authenticated_owner = True
    p = w.wallet.prepare_payment(w.ctx("alice_payment"), draft())
    return w, identity, owner, info, p


def test_supplied_digest_is_checked_even_when_exact_terms_are_also_supplied():
    w, _, owner, _, p = world()
    with pytest.raises(ApprovalTermsMismatch):
        w.wallet.approve(owner, p.request_id, p.terms, terms_sha256="0" * 64)
    assert w.risk.store.ledger_state(p.request_id) == LedgerState.reserved
    assert w.risk.store.approvals(p.request_id) == []
    assert w.wallet.owner_decisions(p.request_id)[-1]["decision"] == "refused"


def test_approval_returns_immutable_typed_record_and_persists_all_fields():
    from agentpal.wallet.models import ApprovalRecord

    w, _, owner, _, p = world()
    record = w.wallet.approve(owner, p.request_id, terms_sha256=p.terms_sha256)
    assert isinstance(record, ApprovalRecord)
    assert record.owner_id == owner.owner_id
    assert record.org_id == owner.org_id
    assert record.session_id == owner.session_id
    assert record.auth_method == "owner_api_token"
    assert record.mfa_satisfied is False
    assert record.terms_sha256 == p.terms_sha256
    assert record.approved_at == w.clock()
    assert record.expires_at == w.clock() + timedelta(minutes=15)
    assert record.consumed_at is None
    with pytest.raises(ValueError):
        record.owner_id = "owner_bob"
    stored = w.risk.store.approvals(p.request_id)[0]
    assert stored["approval_id"] == record.approval_id
    assert datetime.fromisoformat(stored["expires_at"]) == record.expires_at


@pytest.mark.parametrize("change", [
    {"session_id": "0" * 24},
    {"owner_id": "owner_bob"},
    {"org_id": "org_bob"},
    {"org_id": None},
    {"auth_method": "owner_session"},
    {"mfa_satisfied": True},
])
def test_forged_owner_session_cannot_approve(change):
    w, _, owner, _, p = world()
    forged = AuthenticatedOwner.model_validate({**owner.model_dump(), **change})
    with pytest.raises(NotAuthorized, match="INVALID_OWNER_SESSION"):
        w.wallet.approve(forged, p.request_id, terms_sha256=p.terms_sha256)
    assert w.risk.store.approvals(p.request_id) == []


@pytest.mark.parametrize("invalidate", ["expired", "revoked", "disabled"])
def test_session_is_revalidated_at_decision_time(invalidate):
    w, identity, owner, info, p = world()
    if invalidate == "expired":
        w.clock.advance(hours=1)
    elif invalidate == "revoked":
        identity.revoke_token(info.credential_id)
    else:
        identity.disable_owner(owner.owner_id)
    with pytest.raises(NotAuthorized, match="INVALID_OWNER_SESSION"):
        w.wallet.approve(owner, p.request_id, terms_sha256=p.terms_sha256)
    assert w.risk.store.approvals(p.request_id) == []


def test_genuine_owner_in_another_organization_cannot_approve():
    w, identity, _, _, p = world()
    bob = identity.authenticate(identity.issue_token("owner_bob")[0])
    with pytest.raises(NotAuthorized, match="CROSS_OWNER"):
        w.wallet.approve(bob, p.request_id, terms_sha256=p.terms_sha256)
    assert w.risk.store.approvals(p.request_id) == []


def test_agent_identity_cannot_approve_even_with_a_valid_owner_session():
    w, identity, _, _, p = world()
    identity.create_owner("alice_payment", org_id="org_alice")
    agent_owner = identity.authenticate(identity.issue_token("alice_payment")[0])
    # Simulate a corrupted owner mapping: separation of duties remains an independent gate.
    with w.wallet_store.tx() as c:
        c.execute("UPDATE wallet_payment_requests SET owner_id='alice_payment'")
    with w.risk.store._tx() as c:
        c.execute("UPDATE ledger_entries SET owner_id='alice_payment'")
    with pytest.raises(NotAuthorized, match="SELF_APPROVAL"):
        w.wallet.approve(agent_owner, p.request_id, terms_sha256=p.terms_sha256)
    assert w.risk.store.approvals(p.request_id) == []


def test_strict_owner_flag_refuses_legacy_and_session_claiming_principals():
    w, _, owner, _, p = world()
    for principal in (w.alice, OwnerPrincipal.from_authenticated(owner)):
        with pytest.raises(NotAuthorized, match="DURABLE_OWNER_AUTH_REQUIRED"):
            w.wallet.approve(principal, p.request_id, p.terms)
    w.wallet.require_authenticated_owner = False
    with pytest.raises(NotAuthorized, match="INVALID_OWNER_SESSION"):
        w.wallet.approve(OwnerPrincipal.from_authenticated(owner), p.request_id, p.terms)


def test_structured_approval_is_single_use_and_double_execution_is_refused():
    w, _, owner, _, p = world()
    w.wallet.approve(owner, p.request_id, p.terms, terms_sha256=p.terms_sha256)
    with pytest.raises(InvalidLedgerTransition):
        w.wallet.approve(owner, p.request_id, terms_sha256=p.terms_sha256)
    ticket = w.wallet.lifecycle.begin_execution(p.agent_id, p.request_id, p.terms, "fake")
    assert ticket.approval_id == w.risk.store.approvals(p.request_id)[0]["approval_id"]
    assert w.risk.store.approvals(p.request_id)[0]["consumed_at"] is not None
    with pytest.raises(InvalidLedgerTransition):
        w.wallet.lifecycle.begin_execution(p.agent_id, p.request_id, p.terms, "fake")


def test_expired_approval_cannot_be_consumed():
    w, _, owner, _, p = world()
    w.wallet.approve(owner, p.request_id, terms_sha256=p.terms_sha256)
    w.clock.advance(minutes=15)
    with pytest.raises(InvalidLedgerTransition):
        w.wallet.lifecycle.begin_execution(p.agent_id, p.request_id, p.terms, "fake")
    assert w.risk.store.approvals(p.request_id)[0]["consumed_at"] is None
    assert w.risk.store.ledger_state(p.request_id) == LedgerState.released


def test_wallet_constructor_refuses_cross_database_digest_corruption():
    from agentpal.lifecycle.recovery import ConsistencyError
    from agentpal.wallet.service import WalletService

    w, _, _, _, p = world()
    with w.wallet_store.tx() as c:
        c.execute("UPDATE wallet_payment_requests SET terms_sha256=? WHERE request_id=?",
                  ("0" * 64, p.request_id))
    with pytest.raises(ConsistencyError) as exc:
        WalletService(w.registry, w.checker, w.risk)
    assert "terms_digest_differs" in {i.kind for i in exc.value.report.issues}
    assert w.risk.store.ledger_state(p.request_id) == LedgerState.reserved
