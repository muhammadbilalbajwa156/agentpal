"""Who may act: authenticated owners versus agents.

`auth.py` authenticates hashed bearer tokens and yields an `auth.Principal` with role
`agent` or `operator`, but has no notion of an owner. The wallet keeps an EXPLICIT mapping
from operator token ids to owner ids (`OwnerTokenMap`, a wallet config file). Only an
operator principal listed there becomes an `OwnerPrincipal`; an agent principal never does.

Approval, registration and capability grants accept ONLY an `OwnerPrincipal`, or an
`infra_contracts.AuthenticatedOwner` produced by the durable owner authenticator
(`agentpal.identity`), which `require_owner` converts. An `AgentContext` (or anything else)
is refused at runtime, not just by type hints. Both objects can be constructed by code in
the same process, so `WalletService.approve/decline` additionally RE-VALIDATES an
`AuthenticatedOwner` against the identity store (session exists, live, same owner and org)
and, when `require_authenticated_owner` is set, refuses every `OwnerPrincipal` (SEC-15/23).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..auth import Principal
from ..infra_contracts import AuthenticatedOwner
from .models import SAFE_ID


class NotAuthorized(PermissionError):
    """The actor is not an authenticated owner of the resource."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class OwnerPrincipal(BaseModel):
    """An authenticated human owner. `token_id` is the operator token (legacy) or the owner
    credential/session id that authenticated them; it is recorded with every decision they
    make (SEC-15)."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    owner_id: str = Field(pattern=SAFE_ID)
    token_id: str = Field(min_length=1, max_length=64)
    authenticated_via: Literal["operator_token", "owner_api_token",
                               "owner_session"] = "operator_token"
    org_id: str | None = None
    mfa_satisfied: bool = False

    @property
    def decided_by(self) -> str:
        return f"{self.owner_id}|token:{self.token_id}"

    @classmethod
    def from_authenticated(cls, owner: AuthenticatedOwner) -> "OwnerPrincipal":
        return cls(owner_id=owner.owner_id, token_id=owner.session_id,
                   authenticated_via=owner.auth_method, org_id=owner.org_id,
                   mfa_satisfied=owner.mfa_satisfied)


class OwnerTokenMap(BaseModel):
    """Wallet config: operator token id -> owner id. Explicit; nothing is inferred."""

    model_config = ConfigDict(extra="forbid")
    owners: dict[str, str] = Field(default_factory=dict)  # token_id -> owner_id

    @model_validator(mode="after")
    def _ids(self) -> "OwnerTokenMap":
        for token_id, owner_id in self.owners.items():
            if not re.fullmatch(SAFE_ID, owner_id):
                raise ValueError(f"invalid owner id for token {token_id!r}")
        return self

    @classmethod
    def load(cls, path: str | Path) -> "OwnerTokenMap":
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


def owner_principal_from(principal: Principal | None, token_map: OwnerTokenMap
                         ) -> OwnerPrincipal:
    """Turn an authenticated `auth.Principal` into an owner, or refuse."""
    if principal is None:
        raise NotAuthorized("UNAUTHENTICATED")
    if principal.role != "operator":
        raise NotAuthorized("NOT_AN_OWNER", "agent tokens can never act as an owner")
    owner_id = token_map.owners.get(principal.token_id)
    if owner_id is None:
        raise NotAuthorized("NOT_AN_OWNER", "operator token is not mapped to an owner")
    return OwnerPrincipal(owner_id=owner_id, token_id=principal.token_id)


def require_owner(actor: object) -> OwnerPrincipal:
    """Runtime separation of duties: only an owner passes (an AuthenticatedOwner is
    converted). Agent contexts and agent/operator `auth.Principal`s are refused."""
    if isinstance(actor, AuthenticatedOwner):
        return OwnerPrincipal.from_authenticated(actor)
    if not isinstance(actor, OwnerPrincipal):
        raise NotAuthorized("SEPARATION_OF_DUTIES",
                            f"{type(actor).__name__} is not an authenticated owner")
    return actor
