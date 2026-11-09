"""Permission boundary: deny by default, registry is the only authority, capabilities are
owner-scoped, an agent can never grant itself (or anyone) anything, every decision is
audited."""

from __future__ import annotations

import pytest

from agentpal.contracts import AgentContext, Capability
from agentpal.wallet import (
    AgentStatus,
    DenyCode,
    NotAuthorized,
    RegistryError,
)
from agentpal.workforce.roles import ROLE_CAPABILITY_CEILING, ROLE_DEFAULT_CAPABILITIES, Role

from test_wallet_fixtures import build_world

ALL = frozenset(Capability)


def test_unknown_agent_is_denied_everything():
    w = build_world()
    ghost = AgentContext(agent_id="ghost_agent", owner_id="owner_alice", role="research",
                         capabilities=ALL)
    for cap in Capability:
        assert w.checker.check(ghost, cap).code == DenyCode.UNKNOWN_AGENT


def test_forged_context_with_extra_capabilities_is_still_denied():
    w = build_world()
    real = w.ctx("alice_research")
    forged = AgentContext(agent_id=real.agent_id, owner_id=real.owner_id, role=real.role,
                          capabilities=ALL)
    for cap in ALL - ROLE_CAPABILITY_CEILING[Role.research]:
        d = w.checker.check(forged, cap)
        assert not d.allowed and d.code == DenyCode.NOT_GRANTED, cap


def test_forged_owner_or_role_is_denied():
    w = build_world()
    real = w.ctx("alice_payment")
    as_bob = AgentContext(agent_id=real.agent_id, owner_id="owner_bob", role=real.role,
                          capabilities=real.capabilities)
    assert w.checker.check(as_bob, Capability.wallet_read).code == DenyCode.OWNER_MISMATCH
    as_finance = AgentContext(agent_id=real.agent_id, owner_id=real.owner_id,
                              role="finance", capabilities=ALL)
    assert w.checker.check(as_finance, Capability.finance_monitor).code == \
        DenyCode.ROLE_MISMATCH


def test_non_context_objects_are_denied():
    w = build_world()
    for bogus in (None, {"agent_id": "alice_payment"}, w.alice):
        assert w.checker.check(bogus, Capability.wallet_read).code == DenyCode.INVALID_CONTEXT


@pytest.mark.parametrize("role", list(Role))
def test_registration_grants_exactly_the_role_defaults(role):
    w = build_world()
    assert w.registry.granted(f"alice_{role.value}") == ROLE_DEFAULT_CAPABILITIES[role]


@pytest.mark.parametrize("role", list(Role))
def test_role_ceiling_is_enforced_at_grant_time(role):
    w = build_world()
    agent = f"alice_{role.value}"
    for cap in ALL - ROLE_CAPABILITY_CEILING[role]:
        with pytest.raises(RegistryError, match="ABOVE_ROLE_CEILING"):
            w.registry.grant(w.alice, agent, cap)
        assert not w.checker.check(w.ctx(agent), cap).allowed
    refused = [e for e in w.registry.grant_events(agent) if e["action"] == "refused"]
    assert len(refused) == len(ALL - ROLE_CAPABILITY_CEILING[role])


def test_role_ceiling_rechecked_even_if_grant_table_is_tampered_with():
    w = build_world()
    with w.wallet_store.tx() as c:
        c.execute("INSERT INTO wallet_grants VALUES ('alice_research', 'payment.prepare', "
                  "'owner_alice', 'tamper', '2026-01-01')")
    assert not w.checker.check(w.ctx("alice_research"), Capability.payment_prepare).allowed


def test_grant_recorded_under_another_owner_does_not_count():
    w = build_world()
    with w.wallet_store.tx() as c:
        c.execute("DELETE FROM wallet_grants WHERE agent_id='alice_research' AND "
                  "capability='research.fetch'")
        c.execute("INSERT INTO wallet_grants VALUES ('alice_research', 'research.fetch', "
                  "'owner_bob', 'bob', '2026-01-01')")
    assert not w.checker.check(w.ctx("alice_research"), Capability.research_fetch).allowed


def test_no_role_can_both_propose_and_execute_and_none_can_approve():
    for caps in ROLE_CAPABILITY_CEILING.values():
        assert not ({Capability.purchase_propose, Capability.payment_execute_sandbox} <= caps)
    # "comms.send_approved" only SENDS what an owner approved; nothing grants approval
    assert not any(part in {"approve", "approval", "approve_payment"}
                   for c in Capability for part in c.value.split("."))


def test_agent_cannot_grant_itself_or_others_anything():
    w = build_world()
    ctx = w.ctx("alice_research")
    for fn in (w.registry.grant, w.registry.revoke):
        with pytest.raises(NotAuthorized, match="SEPARATION_OF_DUTIES"):
            fn(ctx, "alice_research", Capability.research_fetch)
        with pytest.raises(NotAuthorized, match="SEPARATION_OF_DUTIES"):
            fn(ctx, "alice_payment", Capability.payment_prepare)
    with pytest.raises(NotAuthorized):
        w.registry.register_agent(ctx, "sneaky_agent", Role.payment)
    with pytest.raises(NotAuthorized):
        w.registry.set_status(ctx, "alice_research", AgentStatus.active)
    with pytest.raises(NotAuthorized):
        w.registry.register_owner(ctx)


def test_owner_cannot_touch_another_owners_agents():
    w = build_world()
    with pytest.raises(NotAuthorized, match="CROSS_OWNER"):
        w.registry.grant(w.bob, "alice_payment", Capability.wallet_read)
    with pytest.raises(NotAuthorized, match="CROSS_OWNER"):
        w.registry.revoke(w.bob, "alice_payment", Capability.payment_prepare)
    with pytest.raises(NotAuthorized, match="CROSS_OWNER"):
        w.registry.set_status(w.bob, "alice_payment", AgentStatus.suspended)
    assert Capability.payment_prepare in w.registry.granted("alice_payment")


def test_registration_needs_known_owner_and_unique_disjoint_ids():
    w = build_world()
    with pytest.raises(RegistryError, match="AGENT_EXISTS"):
        w.registry.register_agent(w.bob, "alice_payment", Role.payment)
    with pytest.raises(RegistryError, match="ID_COLLISION"):
        w.registry.register_agent(w.alice, "owner_bob", Role.research)
    from agentpal.wallet import OwnerPrincipal
    stranger = OwnerPrincipal(owner_id="owner_carol", token_id="ops-carol")
    with pytest.raises(RegistryError, match="UNKNOWN_OWNER"):
        w.registry.register_agent(stranger, "carol_agent", Role.research)
    with pytest.raises(RegistryError, match="ABOVE_ROLE_CEILING"):
        w.registry.register_agent(w.alice, "alice_research2", Role.research,
                                  [Capability.payment_execute_sandbox])


def test_revocation_and_suspension_take_effect_immediately():
    w = build_world()
    ctx = w.ctx("alice_research")
    assert w.checker.check(ctx, Capability.research_fetch).allowed
    w.registry.revoke(w.alice, "alice_research", Capability.research_fetch)
    assert w.checker.check(ctx, Capability.research_fetch).code == DenyCode.NOT_GRANTED
    w.registry.set_status(w.alice, "alice_research", AgentStatus.suspended)
    assert w.checker.check(ctx, Capability.research_search).code == DenyCode.AGENT_SUSPENDED
    assert w.registry.granted("alice_research") == frozenset()
    w.registry.set_status(w.alice, "alice_research", AgentStatus.active)
    assert w.checker.check(ctx, Capability.research_search).allowed
    actions = [e["action"] for e in w.registry.grant_events("alice_research")]
    assert actions[0] == "register" and "revoke" in actions and "suspend" in actions


def test_resource_scoping():
    w = build_world()
    fin = w.ctx("alice_finance")
    assert w.checker.check(fin, Capability.finance_monitor,
                           resource_owner_id="owner_alice").allowed
    assert w.checker.check(fin, Capability.finance_monitor,
                           resource_owner_id="owner_bob").code == DenyCode.CROSS_OWNER
    pay = w.ctx("alice_payment")
    assert w.checker.check(pay, Capability.wallet_read,
                           resource_agent_id="alice_finance").code == DenyCode.OTHER_AGENT


def test_every_decision_is_audited():
    w = build_world()
    ctx = w.ctx("alice_research")
    w.checker.check(ctx, Capability.research_fetch, action="tool:http_fetch")
    w.checker.check(ctx, Capability.payment_prepare)
    rows = w.wallet_store.permission_audit("alice_research")
    assert [(r["capability"], r["allowed"], r["code"]) for r in rows] == [
        ("research.fetch", 1, "ALLOWED"), ("payment.prepare", 0, "NOT_GRANTED")]
    assert rows[0]["action"] == "tool:http_fetch"


def test_audit_store_is_read_only_through_query():
    w = build_world()
    from agentpal.wallet.store import WalletStoreError
    with pytest.raises(WalletStoreError):
        w.wallet_store.query("DELETE FROM wallet_permission_audit")
