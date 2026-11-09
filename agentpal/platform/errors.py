"""Platform errors. Every refusal leaves the API as `{"error": "<constant code>"}`; no
caller input is ever part of the code."""

from __future__ import annotations


class PlatformError(Exception):
    def __init__(self, status: int, code: str, *, audit: str | None = None,
                 headers: dict[str, str] | None = None):
        super().__init__(code)
        self.status = status
        self.code = code
        self.audit = audit          # if set, the refusal is audited under this reason
        self.headers = headers


def not_found(audit: str | None = None) -> PlatformError:
    """Unknown and not-yours look identical (no existence oracle)."""
    return PlatformError(404, "not_found", audit=audit)


def not_configured() -> PlatformError:
    return PlatformError(503, "not_configured")
