"""Typed API errors. Raw response bodies and credentials are never included."""


class AgentPalError(Exception):
    def __init__(self, code: str, *, status_code: int | None = None,
                 idempotency_key: str | None = None):
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.idempotency_key = idempotency_key


class AuthenticationError(AgentPalError):
    pass


class PermissionDeniedError(AgentPalError):
    pass


class NotFoundError(AgentPalError):
    pass


class ConflictError(AgentPalError):
    pass


class ValidationError(AgentPalError):
    pass


class RateLimitError(AgentPalError):
    pass


class NotConfiguredError(AgentPalError):
    pass


class ProviderError(AgentPalError):
    pass


class TransportError(AgentPalError):
    """Delivery failed; the operation's outcome may be unknown. Never retry blindly.

    Reuse `idempotency_key` for a payment-create retry with the identical body.
    """
