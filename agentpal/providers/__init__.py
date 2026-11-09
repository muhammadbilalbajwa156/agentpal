"""Payment providers (Specialist 4). PayPal SANDBOX only; no real money.

Implements ``agentpal.infra_contracts.PaymentProvider``. Providers emit ``ProviderOrderRef``
and ``ProviderEvent`` values and raise ``ProviderError``; they never write the ledger.
"""

from .config import (
    SANDBOX_BASE_URL,
    PayPalSandboxConfig,
    ProviderConfigError,
    load_config_from_env,
)
from .paypal_sandbox import PayPalSandboxProvider, map_order_status

__all__ = [
    "SANDBOX_BASE_URL",
    "PayPalSandboxConfig",
    "PayPalSandboxProvider",
    "ProviderConfigError",
    "load_config_from_env",
    "map_order_status",
]
