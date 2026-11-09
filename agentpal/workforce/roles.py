"""Workforce roles and their capability ceilings.

A role's CEILING is the most an owner may grant to an agent of that role. Its DEFAULTS are
what registration grants unless the owner asks for less. Nothing outside the ceiling can be
granted, and the ceiling is checked again on every permission check, so an edited grant
table cannot widen it either.

Separation of duties is built into the table:
* no role holds both `purchase.propose` and `payment.execute_sandbox`;
* no capability approves a payment. Approval is not a capability at all: it needs an
  authenticated owner (`wallet.principals.OwnerPrincipal`), which no agent is.
"""

from __future__ import annotations

from enum import Enum

from ..contracts import Capability as C


class Role(str, Enum):
    research = "research"
    shopping = "shopping"
    finance = "finance"
    communication = "communication"
    verification = "verification"
    payment = "payment"
    # An EXTERNAL agent a developer brings (Claude, OpenAI, open source, any framework) and
    # connects through the /v1 developer API. It can research and prepare payments, but
    # never holds purchase.propose, so it cannot both propose and execute.
    external = "external"


ROLE_CAPABILITY_CEILING: dict[Role, frozenset[C]] = {
    Role.research: frozenset({C.research_search, C.research_fetch, C.wallet_read}),
    Role.shopping: frozenset({C.purchase_propose, C.research_search, C.research_fetch,
                              C.intelligence_assess, C.wallet_read}),
    Role.finance: frozenset({C.finance_monitor, C.wallet_read, C.intelligence_assess}),
    Role.communication: frozenset({C.comms_draft, C.comms_send_approved}),
    Role.verification: frozenset({C.evidence_record, C.research_fetch,
                                  C.intelligence_assess}),
    Role.payment: frozenset({C.payment_prepare, C.owner_request_decision,
                             C.payment_execute_sandbox, C.wallet_read, C.payout_request}),
    Role.external: frozenset({C.wallet_read, C.research_search, C.research_fetch,
                              C.payment_prepare, C.owner_request_decision,
                              C.payment_execute_sandbox, C.payout_request}),
}

# Granted at registration unless the owner passes an explicit subset. Sending messages is
# never a default: the owner opts in per agent.
ROLE_DEFAULT_CAPABILITIES: dict[Role, frozenset[C]] = {
    **ROLE_CAPABILITY_CEILING,
    Role.communication: frozenset({C.comms_draft}),
    # sandbox execution and research are opt-in for external agents
    Role.external: frozenset({C.wallet_read, C.payment_prepare, C.owner_request_decision}),
}


def _check_tables() -> None:
    for role in Role:
        if not ROLE_DEFAULT_CAPABILITIES[role] <= ROLE_CAPABILITY_CEILING[role]:
            raise AssertionError(f"defaults exceed the ceiling for {role.value}")
        caps = ROLE_CAPABILITY_CEILING[role]
        if C.purchase_propose in caps and C.payment_execute_sandbox in caps:
            raise AssertionError(f"{role.value} may both propose and execute payments")


_check_tables()
