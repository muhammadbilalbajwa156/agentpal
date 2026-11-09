"""Operator-owned spending policy and ledger semantics (P0.2).

Trust boundary
--------------
Budgets and limits come ONLY from the operator's spend-policy file (or, later, an
authorized administration service). Request fields such as `monthly_budget` and
`budget_spent_this_period` are untrusted agent claims: they are recorded and compared
with the operator values, but never used to enforce anything.

Ledger states (amounts are exact decimals, always per currency, never converted)
--------------------------------------------------------------------------------
    reserved   created atomically with the request claim when the request passes the
               financial checks; holds budget until the owner decides or it expires.
    approved   owner approved (Phase 2); still a hold, not spending.
    pending    a payment attempt was submitted to a provider; outcome unknown.
    completed  a VERIFIED successful payment outcome. The only state counted as
               completed spending.
    released   hold returned: assessment blocked the request, owner declined,
               reservation expired, or cancelled before a payment attempt.
    failed     verified failed payment outcome; hold returned.
    cancelled  payment attempt cancelled; hold returned.
    reconciliation_required
               the provider outcome is unknown (timeout, crash, attempt deadline passed,
               conflicting verified events). It KEEPS the hold and is never released or
               retried automatically; only a verified provider event / status read moves
               it on (SEC-16).
    reversed   a verified refund/reversal after completion; hold returned.

Exposure used for budget checks = reserved + approved + pending + reconciliation_required
+ completed in the budget period. An assessment never creates completed spending.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import PACKAGE_ROOT
from .schemas import CURRENCY_MINOR_UNITS, PaymentRequest

DEFAULT_SPEND_POLICY_PATH = PACKAGE_ROOT / "config" / "spend_policy.json"

Money = Decimal


class LedgerState(str, Enum):
    reserved = "reserved"
    approved = "approved"
    pending = "pending"
    completed = "completed"
    released = "released"
    failed = "failed"
    cancelled = "cancelled"
    reconciliation_required = "reconciliation_required"
    reversed = "reversed"


ACTIVE_STATES = (LedgerState.reserved, LedgerState.approved, LedgerState.pending,
                 LedgerState.reconciliation_required, LedgerState.completed)
# Moves into and out of reconciliation_required, and completed -> reversed, happen only in
# AssessmentStore's provider-event / execution methods (verified evidence or an unknown
# outcome). transition_ledger can only release (SEC-14), and release is never allowed from
# pending or reconciliation_required: an unknown provider outcome is never auto-released.
ALLOWED_TRANSITIONS: dict[LedgerState, set[LedgerState]] = {
    LedgerState.reserved: {LedgerState.approved, LedgerState.released},
    LedgerState.approved: {LedgerState.pending, LedgerState.released},
    LedgerState.pending: {LedgerState.completed, LedgerState.failed, LedgerState.cancelled,
                          LedgerState.reconciliation_required},
    LedgerState.reconciliation_required: {LedgerState.completed, LedgerState.failed,
                                          LedgerState.cancelled, LedgerState.reversed},
    LedgerState.completed: {LedgerState.reversed, LedgerState.reconciliation_required},
    LedgerState.released: set(),
    LedgerState.failed: {LedgerState.reconciliation_required},
    LedgerState.cancelled: {LedgerState.reconciliation_required},
    LedgerState.reversed: {LedgerState.reconciliation_required},
}


class InvalidLedgerTransition(ValueError):
    pass


class RequestNotApprovable(InvalidLedgerTransition):
    """The request has no finished, non-blocking assessment, so nothing may approve it
    (the deterministic rules have not, or not favourably, decided yet)."""


class AmbiguousRequestId(InvalidLedgerTransition):
    """A request id was given without its agent and more than one agent uses it (SEC-13:
    request ids are scoped per agent). Refused: the caller must name the agent."""


class ApprovalNotUsable(InvalidLedgerTransition):
    """No authenticated, unexpired, unconsumed approval for exactly these terms exists
    (approvals are single-use and time-limited)."""


class LegacyPathDisabled(InvalidLedgerTransition):
    """A legacy, unauthenticated ledger method was called on a strict store."""


class OutcomeConflict(InvalidLedgerTransition):
    """A payment outcome contradicts the outcome that already settled the entry."""


class OutcomeMismatch(ValueError):
    """A payment outcome's amount, currency or attempt reference is not the attempted one."""


class ApprovalTermsMismatch(ValueError):
    """The terms presented for an approval or a payment attempt are not the exact terms of
    the assessed request (final owner decision 5)."""


class ApprovalTerms(BaseModel):
    """The exact terms an owner approves and a payment attempt must match: request, agent,
    amount, currency, recipient (merchant name and domain) and purpose. Only the SHA-256 of
    the canonical form is stored, at claim time, so the bound terms cannot change later."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: str
    agent_id: str
    amount: Decimal
    currency: str
    merchant_name: str
    merchant_domain: str | None
    purpose: str
    merchant_paypal_email: str | None = Field(default=None, max_length=127)

    @field_validator("merchant_paypal_email")
    @classmethod
    def paypal_email(cls, value: str | None) -> str | None:
        import re
        if value is None:
            return None
        # Conservative ASCII mailbox syntax. No display names, quoted locals, IP
        # literals, whitespace or implicit trimming at this financial boundary.
        if len(value) > 127 or value.count("@") != 1:
            raise ValueError("invalid merchant PayPal email")
        local, domain = value.split("@")
        if (not re.fullmatch(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]{1,64}", local)
                or local.startswith(".") or local.endswith(".") or ".." in local
                or "." not in domain or len(domain) > 253
                or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", p)
                       for p in domain.split("."))):
            raise ValueError("invalid merchant PayPal email")
        return value.lower()

    @model_validator(mode="after")
    def _exact_amount(self) -> "ApprovalTerms":
        minor = CURRENCY_MINOR_UNITS.get(self.currency)
        if minor is None:
            raise ValueError(f"unsupported currency: {self.currency}")
        quantum = Decimal(1).scaleb(-minor)
        if self.amount != self.amount.quantize(quantum):
            raise ValueError(f"amount has more decimal places than {self.currency} allows")
        object.__setattr__(self, "amount", self.amount.quantize(quantum))
        return self

    @classmethod
    def from_request(cls, req: PaymentRequest) -> "ApprovalTerms":
        return cls(request_id=req.request_id, agent_id=req.agent_id, amount=req.amount,
                   currency=req.currency, merchant_name=req.merchant_name,
                   merchant_domain=req.merchant_domain, purpose=req.purpose)

    def digest(self) -> str:
        data = self.model_dump(mode="json")
        if self.merchant_paypal_email is None:
            data.pop("merchant_paypal_email")
        canonical = json.dumps(data, sort_keys=True,
                               separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class CurrencyLimits(BaseModel):
    """All limits for ONE currency, in that currency's units (P0.5). Never compared with,
    or converted to, another currency."""

    model_config = ConfigDict(extra="forbid")
    monthly_budget: Money = Field(gt=0)
    rolling_window_limit: Money = Field(gt=0)  # cumulative cap over the rolling window
    per_transaction_hard_limit: Money = Field(gt=0)
    per_transaction_review_limit: Money = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> "CurrencyLimits":
        if self.per_transaction_review_limit > self.per_transaction_hard_limit:
            raise ValueError("review limit must not exceed hard limit")
        return self

    @field_validator("monthly_budget", "rolling_window_limit", "per_transaction_hard_limit",
                     "per_transaction_review_limit", mode="before")
    @classmethod
    def _decimal_string(cls, v: object) -> object:
        if isinstance(v, float):
            raise ValueError("money must be given as a decimal string, not a float")
        return v


class AgentSpendPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner_id: str = Field(min_length=1)
    currencies: dict[str, CurrencyLimits]

    @field_validator("currencies")
    @classmethod
    def _currency_units(cls, v: dict[str, CurrencyLimits]) -> dict[str, CurrencyLimits]:
        out = {}
        for code, lim in v.items():
            code = code.upper()
            if code not in CURRENCY_MINOR_UNITS:
                raise ValueError(f"unsupported currency in spend policy: {code}")
            minor = CURRENCY_MINOR_UNITS[code]
            for name, amount in lim.model_dump().items():
                if amount.as_tuple().exponent < -minor:
                    raise ValueError(f"{code} {name}={amount} has more than {minor} decimals")
            out[code] = lim
        return out


class OwnerPolicy(BaseModel):
    """Owner-level settings (decision 8). Sharing is OFF unless an owner opts in."""

    model_config = ConfigDict(extra="forbid")
    # When true, VERIFIED completed payments of one agent may establish merchant trust
    # (known merchant / merchant-domain baseline) for the owner's other agents. Never
    # shares spend baselines, approvals, assessments or unverified outcomes.
    share_verified_merchant_history_across_agents: bool = False


class SpendPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy_version: str
    # Final owner decision 4: an "operator" configuration (the default) can never hold a
    # default budget, so an agent it does not list is rejected (NO_SPENDING_POLICY) until
    # an owner configures one. Placeholder budgets for unlisted agents are only accepted in
    # a configuration explicitly labelled "demo", and using one always requires review.
    configuration: Literal["operator", "demo"] = "operator"
    note: str = ""
    budget_period: str = "calendar_month_utc"
    rolling_window_hours: int = Field(24, gt=0)
    # Owner decision 6: an owner approval holds the reservation for this long; after that
    # the hold is released (reason approval_expired) and a payment attempt is refused.
    approval_validity_minutes: int = Field(15, gt=0, le=24 * 60)
    # SEC-16: a payment attempt (ledger `pending`) whose provider outcome is still unknown
    # after this long moves to `reconciliation_required` (never released).
    attempt_reconcile_after_minutes: int = Field(1440, gt=0, le=7 * 24 * 60)
    # DEMO ONLY: placeholder limits for agents not listed in `agents` (see `configuration`).
    # Without it, unlisted agents get NO_SPENDING_POLICY (block).
    default_agent: AgentSpendPolicy | None = None
    agents: dict[str, AgentSpendPolicy] = Field(default_factory=dict)
    owners: dict[str, OwnerPolicy] = Field(default_factory=dict)

    def shares_merchant_history(self, owner_id: str | None) -> bool:
        owner = self.owners.get(owner_id) if owner_id else None
        return bool(owner and owner.share_verified_merchant_history_across_agents)

    @model_validator(mode="after")
    def _period(self) -> "SpendPolicy":
        if self.budget_period != "calendar_month_utc":
            raise ValueError("only budget_period=calendar_month_utc is supported")
        if self.default_agent is not None and self.configuration != "demo":
            raise ValueError("default_agent (placeholder budgets for unlisted agents) is only "
                             "allowed in a configuration labelled \"demo\"; an operator "
                             "configuration must list every agent's budget explicitly")
        return self

    def for_agent(self, agent_id: str) -> AgentSpendPolicy | None:
        return self.agents.get(agent_id, self.default_agent)

    def uses_demo_default(self, agent_id: str) -> bool:
        return agent_id not in self.agents and self.default_agent is not None


def load_spend_policy(path: str | Path | None = None) -> SpendPolicy:
    p = Path(path) if path else DEFAULT_SPEND_POLICY_PATH
    if not p.is_absolute() and not p.exists():
        p = PACKAGE_ROOT / p
    return SpendPolicy.model_validate(json.loads(p.read_text(encoding="utf-8")))


def budget_period(at: datetime) -> str:
    return at.strftime("%Y-%m")


class SpendSnapshot(BaseModel):
    """Trusted spending state for one (agent, currency), *excluding* the request being
    assessed. Built by the service inside the claim transaction, or by a fixture."""

    currency: str
    policy_found: bool
    owner_id: str | None = None
    monthly_budget: Money | None = None
    rolling_window_hours: int = 24
    rolling_window_limit: Money | None = None
    per_transaction_hard_limit: Money | None = None
    per_transaction_review_limit: Money | None = None
    committed_in_period: Money = Money(0)     # reserved+approved+pending+completed
    completed_in_period: Money = Money(0)     # verified completed only
    rolling_window_committed: Money = Money(0)
    merchant_window_committed: Money = Money(0)
    reservation_created: bool = False
    source: str = "ledger"                    # "ledger" | "synthetic_fixture"
    demo_default_budget: bool = False         # limits came from a demo default_agent

    @classmethod
    def missing(cls, currency: str) -> "SpendSnapshot":
        return cls(currency=currency, policy_found=False)

    @classmethod
    def from_synthetic_fixture(cls, monthly_budget: Money | float | None,
                               spent: Money | float | None, currency: str = "USD",
                               rolling_window_limit: Money | float | None = None,
                               per_transaction_hard_limit: str = "5000",
                               per_transaction_review_limit: str = "1000",
                               ) -> "SpendSnapshot":
        """For OFFLINE synthetic datasets and unit tests only: treats the scenario's
        budget fields as if they were operator configuration. Never used by the service."""
        if monthly_budget is None:
            return cls.missing(currency)
        budget = Money(str(monthly_budget))
        return cls(currency=currency, policy_found=True, owner_id="fixture",
                   monthly_budget=budget,
                   rolling_window_limit=Money(str(rolling_window_limit))
                   if rolling_window_limit is not None else budget,
                   per_transaction_hard_limit=Money(per_transaction_hard_limit),
                   per_transaction_review_limit=Money(per_transaction_review_limit),
                   committed_in_period=Money(str(spent or 0)), source="synthetic_fixture")


class ReservationRequest(BaseModel):
    """What the service asks the store to reserve, with the trusted limits to check."""

    owner_id: str | None
    limits: CurrencyLimits | None
    rolling_window_hours: int
    expires_at: datetime                      # server-computed effective expiry
    merchant_key: str
    demo_default_budget: bool = False

    def within_limits(self, req, snap: "SpendSnapshot") -> bool:
        amount = req.amount
        hard = snap.per_transaction_hard_limit
        return (snap.policy_found and snap.currency == req.currency
                and amount <= hard
                and amount <= snap.monthly_budget - snap.committed_in_period
                and snap.rolling_window_committed + amount <= snap.rolling_window_limit
                and snap.merchant_window_committed + amount <= hard)


def merchant_key(merchant_name: str, merchant_domain: str | None) -> str:
    """Normalized merchant identity for split-transaction aggregation (P0.6): the PSL
    registrable domain when known, otherwise the confusable-insensitive name key."""
    from . import textnorm

    if merchant_domain:
        return f"domain:{textnorm.registrable_domain(merchant_domain) or merchant_domain}"
    return f"name:{textnorm.name_key(merchant_name)}"
