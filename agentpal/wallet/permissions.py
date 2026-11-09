"""Deny-by-default, auditable permission checks.

`PermissionChecker.check` is the ONLY authority for "may this agent do X". It ignores
`ctx.capabilities` (an AgentContext can be constructed by anyone, so its capability set is
advisory) and re-reads the registry every time. A check is allowed only when ALL hold:

* the agent is registered and active;
* the context's owner and role equal the registry's (no impersonation by a forged context);
* the capability is granted by that agent's owner and is within the role ceiling;
* the resource, if any, belongs to the same owner (and, if given, to the same agent).

Every decision, allowed or denied, is appended to `wallet_permission_audit`. Holding a
capability never bypasses spending policy, the risk rules or human approval: callers still
go through the wallet service, which goes through the ledger.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from ..contracts import AgentContext, Capability
from .models import AgentStatus
from .registry import AgentRegistry, utc_now


class DenyCode:
    ALLOWED = "ALLOWED"
    INVALID_CONTEXT = "INVALID_CONTEXT"
    UNKNOWN_AGENT = "UNKNOWN_AGENT"
    OWNER_MISMATCH = "OWNER_MISMATCH"
    ROLE_MISMATCH = "ROLE_MISMATCH"
    AGENT_SUSPENDED = "AGENT_SUSPENDED"
    NOT_GRANTED = "NOT_GRANTED"
    CROSS_OWNER = "CROSS_OWNER"
    OTHER_AGENT = "OTHER_AGENT"


@dataclass(frozen=True)
class PermissionDecision:
    allowed: bool
    code: str
    agent_id: str
    capability: Capability


class PermissionDenied(PermissionError):
    def __init__(self, decision: PermissionDecision):
        super().__init__(f"{decision.code}: {decision.agent_id} may not "
                         f"{decision.capability.value}")
        self.decision = decision
        self.code = decision.code


class PermissionChecker:
    def __init__(self, registry: AgentRegistry,
                 clock: Callable[[], datetime] | None = None):
        self.registry = registry
        self.clock = clock or registry.clock or utc_now

    def check(self, ctx: AgentContext, capability: Capability, *,
              resource_owner_id: str | None = None, resource_agent_id: str | None = None,
              action: str = "") -> PermissionDecision:
        capability = Capability(capability)
        code = self._decide(ctx, capability, resource_owner_id, resource_agent_id)
        agent_id = getattr(ctx, "agent_id", "?")
        decision = PermissionDecision(allowed=code == DenyCode.ALLOWED, code=code,
                                      agent_id=str(agent_id), capability=capability)
        self.registry.store.audit_permission(
            agent_id=str(agent_id), owner_id=getattr(ctx, "owner_id", None),
            capability=capability.value, allowed=decision.allowed, code=code,
            action=action or capability.value, at=self.clock())
        return decision

    def require(self, ctx: AgentContext, capability: Capability, **kw) -> PermissionDecision:
        decision = self.check(ctx, capability, **kw)
        if not decision.allowed:
            raise PermissionDenied(decision)
        return decision

    def _decide(self, ctx: object, capability: Capability, resource_owner_id: str | None,
                resource_agent_id: str | None) -> str:
        if not isinstance(ctx, AgentContext):
            return DenyCode.INVALID_CONTEXT
        agent = self.registry.get_agent(ctx.agent_id)
        if agent is None:
            return DenyCode.UNKNOWN_AGENT
        if ctx.owner_id != agent.owner_id:
            return DenyCode.OWNER_MISMATCH
        if ctx.role != agent.role.value:
            return DenyCode.ROLE_MISMATCH
        if agent.status != AgentStatus.active:
            return DenyCode.AGENT_SUSPENDED
        if capability not in self.registry.granted(agent.agent_id):  # never ctx.capabilities
            return DenyCode.NOT_GRANTED
        if resource_owner_id is not None and resource_owner_id != agent.owner_id:
            return DenyCode.CROSS_OWNER
        if resource_agent_id is not None and resource_agent_id != agent.agent_id:
            return DenyCode.OTHER_AGENT
        return DenyCode.ALLOWED
