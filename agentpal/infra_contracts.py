"""Shared contracts for Developer Infrastructure v1 (lead-owned, 2026-10-31).

Four workstreams build against these types and never change them without lead review:

* Specialist 1 (security & financial control) IMPLEMENTS `OwnerAuthenticator`, the
  payment lifecycle mapping to `PaymentLifecycle`, approval expiry/single-use, SEC-13/15/16;
* Specialist 2 (developer API & SDK) IMPLEMENTS `AgentCredentialVerifier` and the `/v1`
  HTTP API and USES `OwnerAuthenticator` / `PaymentProvider` only through these protocols;
* Specialist 4 (PayPal Sandbox foundation) IMPLEMENTS `PaymentProvider` for the PayPal
  SANDBOX only and produces `ProviderEvent`s; it never writes the ledger itself;
* Specialist 3 (Postman) consumes the published OpenAPI document only.

Invariants (the 12 security rules, restated for these types):
* a virtual allowance is never money; a `ProviderEvent` is the only thing that can say a
  provider moved money, and only when `verified` is True;
* an agent credential can never yield an owner identity, and vice versa;
* nothing here can approve a payment; approval is an owner action bound to exact terms.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from .contracts import AgentContext
from .schemas import ID_PATTERN
from .spending import ApprovalTerms


class PaymentLifecycle(str, Enum):
    """Externally visible status of a payment request (API + ledger reporting).

    Internal ledger states map onto these; the mapping is owned by Specialist 1."""

    prepared = "prepared"                          # validated, risk-assessed, hold reserved
    awaiting_approval = "awaiting_approval"        # needs an authenticated owner decision
    approved = "approved"                          # owner approved the exact terms digest
    rejected = "rejected"                          # owner declined, or rules blocked
    expired = "expired"                            # request or approval timed out; hold released
    submitted_to_provider = "submitted_to_provider"  # provider call made (idempotency key stored)
    provider_pending = "provider_pending"          # provider says not final yet
    provider_confirmed = "provider_confirmed"      # VERIFIED provider outcome: completed
    failed = "failed"                              # verified provider failure / decline
    reversed = "reversed"                          # verified refund / reversal after confirm
    reconciliation_required = "reconciliation_required"  # outcome unknown: timeout, crash,
    #                                                       conflicting or unverified events


TERMINAL_LIFECYCLE = frozenset({PaymentLifecycle.rejected, PaymentLifecycle.expired,
                                PaymentLifecycle.failed, PaymentLifecycle.reversed})


class AuthenticatedOwner(BaseModel):
    """A human owner authenticated by a durable credential (not an in-process object).

    `session_id` identifies the credential/session that authenticated them and is recorded
    with every decision (SEC-15). `mfa_satisfied` is False until an MFA factor exists."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    owner_id: str = Field(pattern=ID_PATTERN)
    org_id: str | None = Field(default=None, pattern=ID_PATTERN)
    session_id: str = Field(min_length=8, max_length=64)
    auth_method: Literal["owner_api_token", "owner_session"]
    authenticated_at: datetime
    mfa_satisfied: bool = False


class OwnerAuthError(PermissionError):
    """Unknown, revoked, expired or malformed owner credential (always generic to callers)."""


@runtime_checkable
class OwnerAuthenticator(Protocol):
    def authenticate(self, bearer: str) -> AuthenticatedOwner: ...  # raises OwnerAuthError


class AgentAuthError(PermissionError):
    """Unknown, revoked, rotated-out or suspended agent credential."""


@runtime_checkable
class AgentCredentialVerifier(Protocol):
    """API key -> the agent's isolated identity. Capabilities in the returned context are
    still ADVISORY; authorization is decided by the registry-backed PermissionChecker."""

    def verify(self, api_key: str) -> AgentContext: ...  # raises AgentAuthError


class ProviderStatus(str, Enum):
    created = "created"
    payer_action_required = "payer_action_required"
    approved_by_payer = "approved_by_payer"
    pending = "pending"
    completed = "completed"
    declined = "declined"
    refunded = "refunded"
    reversed = "reversed"
    unknown = "unknown"


class ProviderOrderRef(BaseModel):
    """What AgentPal stores after a provider call (never card data, never money)."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: str = Field(pattern=r"^[a-z0-9_]{2,32}$")
    environment: Literal["sandbox"]
    provider_order_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=8, max_length=128)
    status: ProviderStatus
    payer_action_url: str | None = Field(default=None, max_length=2048)


class ProviderEvent(BaseModel):
    """A provider-originated status observation (webhook or status query).

    `verified` is True only after signature/postback verification (webhook) or an
    authenticated server-to-server status read. Unverified events may trigger a status
    re-check but must never settle a payment."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: str = Field(pattern=r"^[a-z0-9_]{2,32}$")
    environment: Literal["sandbox"]
    event_id: str = Field(min_length=1, max_length=128)   # dedup key (webhook id / query id)
    event_type: str = Field(min_length=1, max_length=128)
    provider_order_id: str = Field(min_length=1, max_length=128)
    provider_capture_id: str | None = Field(default=None, max_length=128)
    status: ProviderStatus
    amount: Decimal | None = None
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    verified: bool
    occurred_at: datetime | None = None
    received_at: datetime
    raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProviderError(RuntimeError):
    """Provider call failed. `outcome_unknown` is True for timeouts/5xx after sending:
    the caller must move the payment to reconciliation_required, never retry blindly."""

    def __init__(self, message: str, *, outcome_unknown: bool):
        super().__init__(message)
        self.outcome_unknown = outcome_unknown


@runtime_checkable
class PaymentProvider(Protocol):
    name: str
    environment: Literal["sandbox"]

    def create_order(self, terms: ApprovalTerms, *, idempotency_key: str,
                     return_url: str | None = None) -> ProviderOrderRef: ...

    def capture_order(self, provider_order_id: str, *, idempotency_key: str
                      ) -> ProviderEvent: ...

    def get_order_status(self, provider_order_id: str) -> ProviderEvent: ...

    def verify_webhook(self, headers: dict[str, str], raw_body: bytes) -> ProviderEvent: ...


@runtime_checkable
class ProviderEventSink(Protocol):
    """Implemented by Specialist 1's lifecycle service; called by the webhook endpoint and
    by reconciliation. Applies an event idempotently (dedup by provider + event_id) and
    returns the resulting lifecycle state. Unverified events never settle a payment."""

    def apply_provider_event(self, event: ProviderEvent) -> PaymentLifecycle: ...
