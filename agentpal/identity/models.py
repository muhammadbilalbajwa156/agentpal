"""Identity records: organizations, owners and owner credentials (never their secrets)."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..auth import reject_credential_input

# same identifier pattern as agentpal.wallet.models.SAFE_ID (kept local: no import cycle)
SAFE_ID = r"^[A-Za-z0-9][A-Za-z0-9_\-]{2,63}$"

# credential ids appear inside `apo_<id>_<secret>`, so they never contain '_'
CREDENTIAL_ID_PATTERN = r"^[0-9a-f]{24}$"


class OwnerStatus(str, Enum):
    active = "active"
    disabled = "disabled"


class _Metadata(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def _refuse_credentials(cls, value):
        return reject_credential_input(value)


class Organization(_Metadata):
    model_config = ConfigDict(extra="forbid", frozen=True)
    org_id: str = Field(pattern=SAFE_ID)
    name: str = Field(min_length=1, max_length=100)
    created_at: datetime


class Owner(_Metadata):
    model_config = ConfigDict(extra="forbid", frozen=True)
    owner_id: str = Field(pattern=SAFE_ID)
    org_id: str | None = Field(default=None, pattern=SAFE_ID)
    display_name: str = Field(default="", max_length=100)
    status: OwnerStatus = OwnerStatus.active
    created_at: datetime


class OwnerCredentialInfo(_Metadata):
    """Metadata of an owner credential. The secret is never part of it."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    credential_id: str = Field(pattern=CREDENTIAL_ID_PATTERN)
    owner_id: str
    label: str = Field(default="", max_length=100)
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None


class TotpEnrollment(BaseModel):
    """One-time provisioning result. Never persist or log its secret/URI."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    secret: str = Field(repr=False)
    provisioning_uri: str = Field(repr=False)
    expires_at: datetime
