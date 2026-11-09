"""AgentPal developer platform: the /v1 API for EXTERNAL agents that developers bring
(any model, any framework). Agent registration and API keys, virtual wallets (a virtual
allowance, never money), permissions, and payment requests that need authenticated owner
approval and run against the PayPal SANDBOX only. See docs/DEVELOPER_API.md.

Owner authentication, approval, payment lifecycle and provider calls are injected
protocols (`PlatformDeps`); this package never implements them itself.
"""

from .credentials import ApiKeyVerifier
from .deps import (
    ApprovalGateway,
    CurrencyCap,
    LifecycleGateway,
    PlatformDeps,
    PlatformLimits,
    ProviderReferenceReader,
)
from .errors import PlatformError
from .runtime import AgentCaller, OwnerCaller, Platform
from .spend_policy import WalletBackedSpendPolicy

__all__ = [
    "AgentCaller", "ApiKeyVerifier", "ApprovalGateway", "CurrencyCap", "LifecycleGateway",
    "OwnerCaller", "Platform", "PlatformDeps", "PlatformError", "PlatformLimits",
    "ProviderReferenceReader", "WalletBackedSpendPolicy",
]
