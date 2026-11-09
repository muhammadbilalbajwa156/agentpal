"""Internal ledger/request state -> externally visible `PaymentLifecycle` (pure function)."""

from __future__ import annotations

from datetime import datetime

from ..infra_contracts import PaymentLifecycle

_EXPIRY_REASONS = {"reservation_expired", "approval_expired"}

_BY_LEDGER_STATE = {
    "approved": PaymentLifecycle.approved,
    "completed": PaymentLifecycle.provider_confirmed,
    "failed": PaymentLifecycle.failed,
    "cancelled": PaymentLifecycle.failed,
    "reversed": PaymentLifecycle.reversed,
    "reconciliation_required": PaymentLifecycle.reconciliation_required,
}


def to_lifecycle(facts: dict, now: datetime | None = None) -> PaymentLifecycle:
    """`facts` is `AssessmentStore.lifecycle_facts(...)`.

    | ledger state            | condition                         | lifecycle              |
    |-------------------------|-----------------------------------|------------------------|
    | (none)                  | request still being assessed      | prepared               |
    | (none)                  | assessed / error (no hold made)   | rejected               |
    | reserved                | assessment running                | prepared               |
    | reserved                | assessed, not blocked             | awaiting_approval      |
    | released                | reservation/approval expired      | expired                |
    | released                | declined, blocked, error, cancel  | rejected               |
    | approved                |                                   | approved               |
    | pending                 | no provider order id yet          | submitted_to_provider  |
    | pending                 | provider order id known           | provider_pending       |
    | completed               | (verified provider outcome)       | provider_confirmed     |
    | failed / cancelled      |                                   | failed                 |
    | reversed                |                                   | reversed               |
    | reconciliation_required |                                   | reconciliation_required|

    With `now`, the read is time-aware without writing anything: a reserved/approved hold
    past its deadline reads as `expired`, a pending attempt past its deadline as
    `reconciliation_required` (exactly what the next write-side expiry will record).
    """
    if facts.get("last_reason") == "buyer_approval_expired":
        return PaymentLifecycle.expired
    state = facts.get("ledger_state")
    status = facts.get("request_status")
    deadline = facts.get("expires_at")
    if now is not None and deadline and state in ("reserved", "approved", "pending") \
            and datetime.fromisoformat(deadline) <= now:
        return (PaymentLifecycle.reconciliation_required if state == "pending"
                else PaymentLifecycle.expired)
    if state is None:
        return PaymentLifecycle.prepared if status == "claimed" else PaymentLifecycle.rejected
    if state == "reserved":
        if status == "assessed" and facts.get("recommendation") != "block":
            return PaymentLifecycle.awaiting_approval
        return PaymentLifecycle.prepared
    if state == "released":
        return (PaymentLifecycle.expired if facts.get("last_reason") in _EXPIRY_REASONS
                else PaymentLifecycle.rejected)
    if state == "pending":
        return (PaymentLifecycle.provider_pending if facts.get("provider_order_id")
                else PaymentLifecycle.submitted_to_provider)
    try:
        return _BY_LEDGER_STATE[state]
    except KeyError:  # an unknown state is never reported as anything settled
        return PaymentLifecycle.reconciliation_required
