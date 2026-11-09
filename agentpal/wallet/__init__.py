"""AgentPal agent wallet: owners, isolated agent identities, capability grants, a virtual
spending allowance per agent (derived from the operator spend policy and the ledger; never
real money), payment requests, authenticated owner approval and a LOCAL SIMULATION of
execution. See docs/WALLET_ARCHITECTURE.md."""

from .models import (
    ALLOWANCE_LABEL,
    AgentIdentity,
    AgentStatus,
    CurrencyAllowance,
    InstrumentKind,
    Owner,
    PaymentDraft,
    PaymentInstrumentRef,
    PreparedPayment,
    SandboxExecution,
    TransactionRecord,
    WalletView,
)
from .permissions import DenyCode, PermissionChecker, PermissionDecision, PermissionDenied
from .principals import (
    NotAuthorized,
    OwnerPrincipal,
    OwnerTokenMap,
    owner_principal_from,
    require_owner,
)
from .registry import AgentRegistry, RegistryError
from .sandbox import LocalSandboxSimulator
from .service import WalletRefused, WalletService
from .store import WalletStore

__all__ = [
    "ALLOWANCE_LABEL", "AgentIdentity", "AgentRegistry", "AgentStatus", "CurrencyAllowance",
    "DenyCode", "InstrumentKind", "LocalSandboxSimulator", "NotAuthorized", "Owner",
    "OwnerPrincipal", "OwnerTokenMap", "PaymentDraft", "PaymentInstrumentRef",
    "PermissionChecker", "PermissionDecision", "PermissionDenied", "PreparedPayment",
    "RegistryError", "SandboxExecution", "TransactionRecord", "WalletRefused",
    "WalletService", "WalletStore", "WalletView", "owner_principal_from", "require_owner",
]
