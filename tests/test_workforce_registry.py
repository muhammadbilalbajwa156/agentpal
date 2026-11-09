"""Role -> capability table and registry-issued contexts."""

from __future__ import annotations

import pytest

from agentpal.contracts import AgentContext, Capability as C
from agentpal.wallet import RegistryError
from agentpal.workforce.roles import ROLE_CAPABILITY_CEILING, ROLE_DEFAULT_CAPABILITIES, Role

from test_wallet_fixtures import build_world

EXPECTED = {
    Role.research: {C.research_search, C.research_fetch, C.wallet_read},
    Role.shopping: {C.purchase_propose, C.research_search, C.research_fetch,
                    C.intelligence_assess, C.wallet_read},
    Role.finance: {C.finance_monitor, C.wallet_read, C.intelligence_assess},
    Role.communication: {C.comms_draft, C.comms_send_approved},
    Role.verification: {C.evidence_record, C.research_fetch, C.intelligence_assess},
    Role.payment: {C.payment_prepare, C.owner_request_decision, C.payment_execute_sandbox,
                   C.wallet_read, C.payout_request},
    Role.external: {C.wallet_read, C.research_search, C.research_fetch, C.payment_prepare,
                    C.owner_request_decision, C.payment_execute_sandbox, C.payout_request},
}


def test_role_ceiling_table_is_exactly_the_documented_one():
    assert set(ROLE_CAPABILITY_CEILING) == set(Role)
    for role, caps in EXPECTED.items():
        assert ROLE_CAPABILITY_CEILING[role] == caps, role


def test_every_capability_belongs_to_some_role():
    covered = set().union(*ROLE_CAPABILITY_CEILING.values())
    assert covered == set(C)


def test_money_capabilities_live_only_in_the_payment_role():
    # the external (developer-connected) role may also prepare and sandbox-execute
    for role, caps in ROLE_CAPABILITY_CEILING.items():
        if role not in (Role.payment, Role.external):
            assert not caps & {C.payment_prepare, C.payment_execute_sandbox, C.payout_request}, role


def test_no_role_can_both_propose_and_execute():
    for role, caps in ROLE_CAPABILITY_CEILING.items():
        assert not {C.purchase_propose, C.payment_execute_sandbox} <= caps, role


def test_sending_messages_is_never_a_default():
    assert C.comms_send_approved not in ROLE_DEFAULT_CAPABILITIES[Role.communication]
    for role in Role:
        assert ROLE_DEFAULT_CAPABILITIES[role] <= ROLE_CAPABILITY_CEILING[role]


def test_owner_may_opt_in_to_send_approved():
    w = build_world()
    w.registry.grant(w.alice, "alice_communication", C.comms_send_approved)
    assert C.comms_send_approved in w.registry.granted("alice_communication")


def test_context_for_reflects_the_registry():
    w = build_world()
    ctx = w.registry.context_for("alice_shopping")
    assert isinstance(ctx, AgentContext)
    assert (ctx.agent_id, ctx.owner_id, ctx.role) == ("alice_shopping", "owner_alice",
                                                     "shopping")
    assert ctx.capabilities == ROLE_DEFAULT_CAPABILITIES[Role.shopping]
    with pytest.raises(RegistryError, match="UNKNOWN_AGENT"):
        w.registry.context_for("nobody_here")


def test_agents_of_lists_only_the_owners_agents():
    w = build_world()
    assert {a.agent_id for a in w.registry.agents_of("owner_bob")} == {"bob_payment"}
    assert len(w.registry.agents_of("owner_alice")) == len(Role) + 1  # one per role + mismatched_agent
