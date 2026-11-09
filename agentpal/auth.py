"""API authentication (SEC-01/SEC-02): per-agent and operator bearer tokens.

* The operator creates tokens with `python -m agentpal new-token`. Only the SHA-256 of
  each token is stored, in the JSON file named by AGENTPAL_API_TOKENS_FILE. Plaintext
  tokens are never written to disk by AgentPal and must never be committed.
* Tokens are 256-bit random values, so an unsalted SHA-256 is adequate (it is a lookup
  key, not a password hash).
* Lookup hashes the presented token once and compares it with EVERY configured hash using
  `hmac.compare_digest`, without an early exit.
* An `agent` token authenticates exactly one `agent_id`; an `operator` token may read
  any assessment but cannot submit one.

File format::

    {"tokens": [
      {"id": "agent-001-key-1", "role": "agent", "agent_id": "agent_demo_001",
       "sha256": "<64 lowercase hex>"},
      {"id": "ops-1", "role": "operator", "sha256": "<64 lowercase hex>"}
    ]}

`id` is a non-secret label used in audit records and rate-limit keys.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .schemas import ID_PATTERN

# printable ASCII without spaces; bounded so hashing cost is bounded
_TOKEN_RE = re.compile(r"^[\x21-\x7e]{20,256}$")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_token() -> str:
    return "apt_" + secrets.token_urlsafe(32)


class TokenEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
    role: Literal["agent", "operator"]
    agent_id: str | None = Field(default=None, pattern=ID_PATTERN)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _role_binding(self) -> "TokenEntry":
        if self.role == "agent" and not self.agent_id:
            raise ValueError("an agent token must name exactly one agent_id")
        if self.role == "operator" and self.agent_id is not None:
            raise ValueError("an operator token must not name an agent_id")
        return self


class Principal(BaseModel):
    model_config = ConfigDict(frozen=True)
    token_id: str
    role: Literal["agent", "operator"]
    agent_id: str | None = None

    def may_read(self, owner_agent_id: str) -> bool:
        return self.role == "operator" or (
            self.agent_id is not None and hmac.compare_digest(self.agent_id, owner_agent_id))


class TokenRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tokens: list[TokenEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> "TokenRegistry":
        if len({t.sha256 for t in self.tokens}) != len(self.tokens):
            raise ValueError("duplicate token hash in the token file")
        if len({t.id for t in self.tokens}) != len(self.tokens):
            raise ValueError("duplicate token id in the token file")
        return self

    @classmethod
    def load(cls, path: str | Path | None) -> "TokenRegistry":
        if not path:
            return cls()
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))

    def authenticate(self, token: str | None) -> Principal | None:
        if not token or not _TOKEN_RE.fullmatch(token):
            return None
        digest = hash_token(token)
        match: TokenEntry | None = None
        for entry in self.tokens:  # no early exit: constant work per configured token
            if hmac.compare_digest(entry.sha256, digest):
                match = entry
        if match is None:
            return None
        return Principal(token_id=match.id, role=match.role, agent_id=match.agent_id)


def bearer_token(headers: list[tuple[bytes, bytes]]) -> str | None:
    """The token from exactly one `Authorization: Bearer <token>` header, else None."""
    values = [v for k, v in headers if k.lower() == b"authorization"]
    if len(values) != 1:
        return None
    try:
        scheme, _, token = values[0].decode("latin-1").partition(" ")
    except UnicodeDecodeError:
        return None
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None


# Shared ingress/log guards. The ledger's redactor is a separate integration handoff.
_CREDENTIAL_TEXT = re.compile(r"(?:apk|apo|apt)_[A-Za-z0-9_-]+", re.IGNORECASE)
_SECRET_TEXT = re.compile(
    r"Bearer\s+\S+|(?:password|passwd|secret|api[_-]?key|access[_-]?token|totp[_-]?code)"
    r"\s*[:=]\s*\S+", re.IGNORECASE)
_COMMON_SECRET_TEXT = re.compile(r"\b(?:sk|pk|rk)[_-][A-Za-z0-9_-]{12,}\b|"
                                 r"\bEAA?[A-Za-z0-9]{30,}\b|\b[0-9]{13,19}\b")
_SENSITIVE_LOG_KEY = re.compile(r"authorization|password|passwd|secret|seed|(?:^|_)otp|totp|"
                                r"(?:^|_)token$|api[_-]?key", re.IGNORECASE)


def _environment_secrets():
    import os
    for name, value in os.environ.items():
        if (len(value) >= 8 and any(word in name.upper() for word in
                ("SECRET", "TOKEN", "API_KEY", "AUTH_KEY", "MFA_ENCRYPTION_KEY"))):
            yield value


def redact_sensitive_text(value: str) -> str:
    for secret in _environment_secrets():
        value = value.replace(secret, "[REDACTED]")
    value = _CREDENTIAL_TEXT.sub("[REDACTED]", value)
    value = _COMMON_SECRET_TEXT.sub("[REDACTED]", value)
    value = _SECRET_TEXT.sub("[REDACTED]", value)
    # Also protect unlabelled six-digit OTPs and conventional 160-bit base32 seeds.
    value = re.sub(r"(?<![0-9])[0-9]{6}(?![0-9])|(?<![A-Z2-7])[A-Z2-7]{32}(?![A-Z2-7])",
                   "[REDACTED]", value)
    return value


def safe_log_value(value):
    if isinstance(value, str):
        return redact_sensitive_text(value)
    if isinstance(value, dict):
        return {redact_sensitive_text(str(k)): "[REDACTED]" if _SENSITIVE_LOG_KEY.search(str(k))
                else safe_log_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_log_value(v) for v in value]
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return "[" + type(value).__name__ + "]"  # never invoke arbitrary object representations


def reject_credential_input(value):
    """Reject pasted credentials before business metadata can persist or echo them."""
    if isinstance(value, str):
        if (_CREDENTIAL_TEXT.search(value) or _COMMON_SECRET_TEXT.search(value) or _SECRET_TEXT.search(value)
                or any(secret in value for secret in _environment_secrets())):
            raise ValueError("credentials are not allowed in business metadata")
    elif isinstance(value, dict):
        for key, item in value.items():
            reject_credential_input(key)
            reject_credential_input(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            reject_credential_input(item)
    return value
