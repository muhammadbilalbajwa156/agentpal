"""Wallet data models.

Three different things are never confused here (see docs/WALLET_ARCHITECTURE.md):

* a VIRTUAL SPENDING ALLOWANCE: an internal budget record. Its figures are derived from the
  operator spend policy and the ledger states; it is not money and holds no funds;
* a PROVIDER-CONNECTED PAYMENT INSTRUMENT: a reference to an account at a payment provider
  (today only a PayPal *sandbox* reference or the local simulation). AgentPal stores the
  reference, never credentials and never funds;
* ACTUAL FUNDS HELD BY A REGULATED PROVIDER: not representable today (DESIGN ONLY).

There is deliberately no stored "balance" anywhere: every figure in `CurrencyAllowance` is
computed from `store.spend_breakdown` (ledger states) and the operator's `CurrencyLimits`.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..auth import reject_credential_input
from ..infra_contracts import PaymentLifecycle
from ..schemas import BillingInterval
from ..spending import ApprovalTerms, LedgerState
from ..workforce.roles import Role

# Satisfies BOTH contracts.AgentContext's id pattern and schemas.ID_PATTERN (no ':' or '.'),
# until the lead aligns them.
SAFE_ID = r"^[A-Za-z0-9][A-Za-z0-9_\-]{2,63}$"

ALLOWANCE_LABEL = ("virtual spending allowance: an internal budget record derived from the "
                   "operator spend policy and the ledger. It is not money and no funds are "
                   "held by AgentPal.")


class Owner(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    owner_id: str = Field(pattern=SAFE_ID)
    display_name: str = Field(default="", max_length=100)
    created_at: datetime


class AgentStatus(str, Enum):
    active = "active"
    suspended = "suspended"


class AgentIdentity(BaseModel):
    """An isolated agent identity. It belongs to exactly one owner and has one role."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    agent_id: str = Field(pattern=SAFE_ID)
    owner_id: str = Field(pattern=SAFE_ID)
    role: Role
    status: AgentStatus = AgentStatus.active
    created_at: datetime


class InstrumentKind(str, Enum):
    virtual_allowance = "virtual_allowance"
    provider_sandbox_instrument = "provider_sandbox_instrument"
    provider_funds = "provider_funds"  # DESIGN ONLY: refused today


class PaymentInstrumentRef(BaseModel):
    """A reference to a payment instrument. Never credentials, never a balance, never real
    money: `is_real_money` can only be False, and `provider_funds` is refused."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    instrument_id: str = Field(pattern=SAFE_ID)
    owner_id: str = Field(pattern=SAFE_ID)
    kind: InstrumentKind
    provider: Literal["paypal_sandbox", "local_simulation"]
    external_ref: str = Field(default="", max_length=128)
    is_real_money: Literal[False] = False

    @model_validator(mode="after")
    def _no_real_funds(self) -> "PaymentInstrumentRef":
        if self.kind == InstrumentKind.provider_funds:
            raise ValueError("funds held by a regulated provider are not supported (DESIGN "
                             "ONLY); AgentPal is sandbox/demo only")
        if self.kind == InstrumentKind.virtual_allowance and self.external_ref:
            raise ValueError("a virtual allowance has no provider reference")
        return self


class CurrencyAllowance(BaseModel):
    """One currency of an agent's virtual allowance for the current budget period. Every
    amount is derived from the ledger; `available` is budget minus committed exposure."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    currency: str
    period: str
    monthly_budget: Decimal
    per_transaction_hard_limit: Decimal
    per_transaction_review_limit: Decimal
    rolling_window_limit: Decimal
    reserved: Decimal
    approved: Decimal
    attempted: Decimal
    completed: Decimal       # VERIFIED provider outcomes only (ledger `completed`)
    released: Decimal
    failed: Decimal
    cancelled: Decimal
    # SEC-16: provider outcome unknown; still held, never auto-released
    reconciliation_required: Decimal = Decimal(0)
    reversed: Decimal = Decimal(0)   # verified refund/reversal after completion
    committed: Decimal       # reserved + approved + attempted + reconciliation + completed
    available: Decimal       # max(0, monthly_budget - committed)
    demo_default_budget: bool = False
    label: str = ALLOWANCE_LABEL


class WalletView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    agent_id: str
    owner_id: str
    role: Role
    policy_configuration: Literal["operator", "demo"]
    allowances: list[CurrencyAllowance]
    instruments: list[PaymentInstrumentRef]
    refusal: str | None = None   # e.g. OWNER_MISMATCH / NO_SPENDING_POLICY
    is_real_money: Literal[False] = False
    label: str = ALLOWANCE_LABEL


class LedgerEventView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    from_state: str | None
    to_state: str
    actor: str
    reason: str | None
    at: str


class TransactionRecord(BaseModel):
    """Read-only view of one ledger entry and its append-only state history."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: str
    agent_id: str
    currency: str
    amount: Decimal
    state: LedgerState | None     # None: no hold was created (e.g. blocked by policy)
    period: str | None
    recommendation: str | None
    events: list[LedgerEventView]


class PaymentDraft(BaseModel):
    """What an agent may ask for. It cannot name the request id (server-generated, SEC-13),
    the agent id (taken from its registry identity) or any budget figure (operator-owned)."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    amount: Decimal = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    merchant_name: str = Field(min_length=1, max_length=200)
    merchant_domain: str | None = Field(default=None, max_length=253)
    purpose: str = Field(min_length=1, max_length=500)
    business_justification: str = Field(default="", max_length=2000)
    recurring: bool = False
    billing_interval: BillingInterval | None = None

    @model_validator(mode="before")
    @classmethod
    def _no_credentials(cls, value):
        return reject_credential_input(value)

    @field_validator("amount", mode="before")
    @classmethod
    def _no_float(cls, v: object) -> object:
        if isinstance(v, float):
            raise ValueError("money must be given as a decimal string, not a float")
        return v


class PreparedPayment(BaseModel):
    """A payment request built and assessed for an agent. It is NOT approved: only an
    authenticated owner can approve it, bound to exactly `terms`."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: str
    agent_id: str
    owner_id: str
    terms: ApprovalTerms
    terms_sha256: str
    recommendation: str
    risk_score: int
    ledger_state: LedgerState | None
    lifecycle: PaymentLifecycle | None = None
    requires_owner_approval: Literal[True] = True


class ApprovalRecord(BaseModel):
    """Immutable receipt for a ledger approval; execution consumes the stored row.

    The organization is the owner's identity-service organization, revalidated at
    decision time. Owners have globally unique ids, so another organization's owner
    cannot approve a request belonging to this owner.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    approval_id: int
    agent_id: str
    request_id: str
    owner_id: str
    org_id: str | None
    session_id: str
    auth_method: Literal["operator_token", "owner_api_token", "owner_session"]
    mfa_satisfied: bool
    terms_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None
    consumed_by: str | None = None


class SandboxExecution(BaseModel):
    """Result of a LOCAL SIMULATION of a payment submission. No money moved, no provider
    was contacted, and the ledger is `pending` (attempted), never `completed`."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: str
    simulation_reference: str = Field(pattern=r"^sim-[0-9a-f]{32}$")
    provider: Literal["local_simulation"] = "local_simulation"
    ledger_state: LedgerState
    is_real_money: Literal[False] = False
    note: str = ("LOCAL SIMULATION ONLY: no payment provider was contacted and no money "
                 "moved. The entry stays `attempted` until a verified provider event "
                 "settles it; the simulator never produces one, so after the reconcile "
                 "deadline it moves to reconciliation_required, never to completed.")
