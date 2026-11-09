"""Shared plumbing for `contracts.Tool` implementations.

Every tool takes an injected permission authority (in practice
`wallet.permissions.PermissionChecker`) and asks it before doing anything. A tool never
reads `ctx.capabilities` to decide: that field is advisory. A tool never touches the wallet,
the ledger or a payment provider; holding a research capability cannot move money.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Protocol

from ..contracts import AgentContext, Capability, ToolError, ToolResult


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class _Decision(Protocol):
    allowed: bool
    code: str


class PermissionAuthority(Protocol):
    def check(self, ctx: AgentContext, capability: Capability, *,
              resource_owner_id: str | None = None, resource_agent_id: str | None = None,
              action: str = "") -> _Decision: ...


def failed(tool: str, error: ToolError, detail: str, started: datetime,
           clock: Callable[[], datetime] = utc_now) -> ToolResult:
    return ToolResult(tool=tool, ok=False, error=error, error_detail=detail[:300],
                      started_at=started, finished_at=clock())


def gate(tool: str, capability: Capability, ctx: AgentContext,
         checker: PermissionAuthority | None, started: datetime,
         clock: Callable[[], datetime] = utc_now) -> ToolResult | None:
    """None if the caller may use the tool, else a `not_permitted` result. Fails closed when
    no authority was injected."""
    if checker is None:
        return failed(tool, ToolError.not_permitted, "no permission authority configured",
                      started, clock)
    decision = checker.check(ctx, capability, action=f"tool:{tool}")
    if not decision.allowed:
        return failed(tool, ToolError.not_permitted,
                      f"{decision.code}: {capability.value} required", started, clock)
    return None
