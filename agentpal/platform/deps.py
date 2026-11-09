"""What the /v1 developer platform needs from the rest of AgentPal, as injected protocols.

The platform (Specialist 2) never implements owner authentication, approval, payment
lifecycle or provider calls itself. It codes against these protocols and the
`infra_contracts` protocols; the lead wires in the real implementations:

* `owner_authenticator`: `infra_contracts.OwnerAuthenticator` (Specialist 1);
* `approvals`:          `ApprovalGateway` -> Specialist 1's `WalletService.approve/decline`;
* `lifecycle`:          `LifecycleGateway` -> Specialist 1's `PaymentLifecycleService`;
* `provider`:           `infra_contracts.PaymentProvider` (Specialist 4's PayPal SANDBOX
                        adapter, or a LocalSandboxSimulator-backed provider);
* `event_sink`:         `infra_contracts.ProviderEventSink` (Specialist 1).

A dependency left as None makes the routes that need it answer 503 `not_configured`;
nothing falls back to an unauthenticated or unverified path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from ..infra_contracts import (
    AuthenticatedOwner,
    OwnerAuthenticator,
    PaymentLifecycle,
    PaymentProvider,
    ProviderEventSink,
    ProviderOrderRef,
)
from ..spending import ApprovalTerms
from ..wallet.store import WalletStore


@runtime_checkable
class ApprovalGateway(Protocol):
    """Owner decisions. Implemented by Specialist 1 (`WalletService.approve/decline`), which
    re-validates the owner session, refuses self- and cross-owner approval and records a
    single-use, time-limited approval bound to `terms_sha256`."""

    def approve(self, owner: AuthenticatedOwner, request_id: str, *,
                terms_sha256: str) -> Any: ...

    def decline(self, owner: AuthenticatedOwner, request_id: str) -> Any: ...


@runtime_checkable
class LifecycleGateway(Protocol):
    """Payment lifecycle (Specialist 1's `PaymentLifecycleService`). The platform never
    moves a ledger entry itself and never calls `record_payment_attempt`."""

    def status(self, request_id: str, *, agent_id: str | None = None
               ) -> PaymentLifecycle: ...

    def begin_execution(self, agent_id: str, request_id: str, terms: ApprovalTerms,
                        provider: PaymentProvider) -> Any: ...

    def submit_to_provider(self, ticket: Any, provider: PaymentProvider
                           ) -> PaymentLifecycle: ...

    def reconcile(self, request_id: str) -> PaymentLifecycle: ...


@runtime_checkable
class CaptureGateway(Protocol):
    """Optional owner-triggered continuation; performs the verified payer read itself."""

    def capture(self, request_id: str) -> PaymentLifecycle: ...


@runtime_checkable
class ProviderReferenceReader(Protocol):
    """OPTIONAL extension of the lifecycle gateway for the reconciliation read. If the
    injected lifecycle object does not implement it, the read returns no references."""

    def provider_references(self, request_id: str) -> list[ProviderOrderRef]: ...


@dataclass(frozen=True)
class CurrencyCap:
    """Operator hard caps for owner-configured virtual allowances in one currency."""

    max_monthly_budget: Decimal
    max_per_transaction: Decimal


def _default_caps() -> dict[str, CurrencyCap]:
    cap = CurrencyCap(Decimal("10000"), Decimal("5000"))
    return {"USD": cap, "EUR": cap, "GBP": cap}


@dataclass(frozen=True)
class PlatformLimits:
    """Operator limits on what owners may configure through the API. A currency that is
    not listed cannot have a wallet at all."""

    caps: dict[str, CurrencyCap] = field(default_factory=_default_caps)
    max_overlap_seconds: int = 86_400          # rotation overlap window ceiling
    idempotency_in_progress_timeout_s: float = 60.0
    max_agents_per_owner: int = 100
    max_wallets_per_owner: int = 500


@dataclass
class PlatformDeps:
    owner_authenticator: OwnerAuthenticator
    wallet_store: WalletStore
    approvals: ApprovalGateway | None = None
    lifecycle: LifecycleGateway | None = None
    provider: PaymentProvider | None = None
    event_sink: ProviderEventSink | None = None
    limits: PlatformLimits = field(default_factory=PlatformLimits)
    require_mfa_for_approval: bool = False
    # (registry, checker, risk_service) -> object with prepare_payment(ctx, draft); the
    # default builds the existing `wallet.service.WalletService`
    wallet_service_factory: Any = None
    # Channel3 MarketChecker (advisory market check); None = not configured
    market_checker: Any = None
    page_checker: Any = None
    browser: Any = None
    evidence_store: Any = None
    setup: Any = None
    integrations: Any = None
    provider_resolver: Any = None

    def paypal_for(self, owner_id: str):
        if self.provider_resolver and self.integrations.fields(owner_id, "paypal_sandbox"):
            return self.provider_resolver.paypal(owner_id)
        return self.provider

    def market_for(self, owner_id: str):
        if self.provider_resolver and self.integrations.fields(owner_id, "channel3"):
            return self.provider_resolver.market(owner_id)
        return self.market_checker

    def kernel_for(self, owner_id: str):
        if self.provider_resolver and self.integrations.fields(owner_id, "kernel"):
            return self.provider_resolver.kernel(owner_id)
        return self.browser, self.page_checker
