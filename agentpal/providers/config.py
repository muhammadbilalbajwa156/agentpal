"""PayPal SANDBOX configuration (Specialist 4).

Sandbox only. The only accepted API base URL is ``https://api-m.sandbox.paypal.com``
(the sandbox server listed in the Orders v2 OpenAPI document,
https://developer.paypal.com/api/orders/v2/schema.json). Anything else, including the
production host ``api-m.paypal.com``, is refused when the configuration is built.

Credentials come from the environment only:

* ``PAYPAL_SANDBOX_CLIENT_ID``
* ``PAYPAL_SANDBOX_CLIENT_SECRET``
* ``PAYPAL_SANDBOX_WEBHOOK_ID`` (needed to verify webhooks; optional for API calls)

The client id and secret are excluded from ``repr``; the secret is a ``SecretStr``.
"""

from __future__ import annotations

import os
import hmac
import re
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

SANDBOX_BASE_URL = "https://api-m.sandbox.paypal.com"

ENV_CLIENT_ID = "PAYPAL_SANDBOX_CLIENT_ID"
ENV_CLIENT_SECRET = "PAYPAL_SANDBOX_CLIENT_SECRET"
ENV_SECRET_ALIAS = "PAYPAL_SANDBOX_SECRET"
ENV_WEBHOOK_ID = "PAYPAL_SANDBOX_WEBHOOK_ID"

# The placeholder webhook id PayPal's simulator puts on mock events
# (https://developer.paypal.com/api/rest/webhooks/rest/). Never a real subscription.
SIMULATOR_WEBHOOK_ID = "WEBHOOK_ID"


class ProviderConfigError(ValueError):
    """The provider configuration is missing, invalid or not sandbox."""


def assert_sandbox_base_url(url: str) -> str:
    """Hard sandbox check: the base URL must be exactly the PayPal sandbox API host."""
    if not isinstance(url, str) or url.rstrip("/") != SANDBOX_BASE_URL:
        raise ProviderConfigError("only the PayPal sandbox base URL is allowed")
    return SANDBOX_BASE_URL


class PayPalSandboxConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    client_id: str = Field(min_length=1, max_length=256, repr=False)
    client_secret: SecretStr = Field(repr=False)
    webhook_id: str | None = Field(default=None, max_length=128)
    base_url: str = SANDBOX_BASE_URL
    connect_timeout_s: float = Field(default=5.0, gt=0, le=60)
    read_timeout_s: float = Field(default=20.0, gt=0, le=120)
    negative_testing: bool = False
    buyer_approval_timeout_s: int = Field(default=10800, ge=60, le=86400)
    # AgentPal policy, not a PayPal rule: PayPal documents no freshness window.
    webhook_max_age_s: int = Field(default=300, ge=30, le=3600)
    webhook_verification: Literal["self", "postback"] = "self"
    # AgentPal identity policy, not a PayPal guarantee. Mismatches fail closed.
    webhook_signing_identity: str = "messageverificationcerts.sandbox.paypal.com"
    webhook_intermediates_file: str | None = Field(default=None, repr=False)

    @field_validator("webhook_signing_identity")
    @classmethod
    def _signing_identity(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", value):
            raise ValueError("invalid signing identity")
        return value

    @field_validator("base_url")
    @classmethod
    def _sandbox_only(cls, v: str) -> str:
        return assert_sandbox_base_url(v)

    @field_validator("client_secret")
    @classmethod
    def _secret_present(cls, v: SecretStr) -> SecretStr:
        if not v.get_secret_value():
            raise ValueError("client secret is empty")
        return v

    @field_validator("webhook_id")
    @classmethod
    def _real_webhook_id(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if v == SIMULATOR_WEBHOOK_ID:
            raise ValueError("the simulator placeholder webhook id cannot be configured")
        return v


def load_config_from_env(environ: Mapping[str, str] | None = None,
                         **overrides) -> PayPalSandboxConfig:
    """Build the config from ``PAYPAL_SANDBOX_*`` variables. Raises ProviderConfigError
    when the client id or secret is missing (the message never contains values)."""
    env = os.environ if environ is None else environ
    if (env.get("PAYPAL_SANDBOX_BASE_URL") or "").strip():
        assert_sandbox_base_url(env["PAYPAL_SANDBOX_BASE_URL"].strip())
    client_id = (env.get(ENV_CLIENT_ID) or "").strip()
    secret = (env.get(ENV_CLIENT_SECRET) or "").strip()
    alias = (env.get(ENV_SECRET_ALIAS) or "").strip()
    if secret and alias and not hmac.compare_digest(secret.encode(), alias.encode()):
        raise ProviderConfigError("conflicting PayPal sandbox secret settings")
    secret = secret or alias
    if not client_id or not secret:
        raise ProviderConfigError(
            f"{ENV_CLIENT_ID} and {ENV_CLIENT_SECRET} or {ENV_SECRET_ALIAS} "
            "must be set (sandbox credentials)")
    try:
        options = {"webhook_signing_identity": env.get("PAYPAL_SANDBOX_SIGNING_IDENTITY")
                   or "messageverificationcerts.sandbox.paypal.com",
                   "webhook_intermediates_file": env.get("PAYPAL_SANDBOX_CERT_INTERMEDIATES_FILE")
                   or None,
                   "negative_testing": env.get("PAYPAL_SANDBOX_NEGATIVE_TESTING", "0") == "1",
                   "buyer_approval_timeout_s": env.get("PAYPAL_SANDBOX_BUYER_APPROVAL_TIMEOUT_S")
                   or 10800, **overrides}
        return PayPalSandboxConfig(client_id=client_id, client_secret=SecretStr(secret),
                                   webhook_id=env.get(ENV_WEBHOOK_ID) or None, **options)
    except ValueError as exc:  # pydantic ValidationError is a ValueError
        if isinstance(exc, ProviderConfigError):
            raise
        raise ProviderConfigError("invalid PayPal sandbox configuration") from None
