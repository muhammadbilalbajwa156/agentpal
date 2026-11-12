"""`Platform`: the assembled /v1 runtime (registry, permission checker, credentials,
wallets, payments) plus credential classification for the SecurityGate."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from ..contracts import AgentContext
from ..infra_contracts import AgentAuthError, AuthenticatedOwner, OwnerAuthError
from ..wallet.models import SAFE_ID
from ..wallet.permissions import PermissionChecker
from ..wallet.registry import AgentRegistry
from . import spend_policy
from .agents import AgentDirectory
from .credentials import ApiKeyVerifier, CredentialStore, key_id_of
from .deps import PlatformDeps
from .errors import PlatformError
from .payments import PaymentDesk
from .wallets import WalletBook

log = logging.getLogger("agentpal.platform")

AGENT_KEY_PREFIX = "apk_"
OWNER_CREDENTIAL_PREFIX = "apo_"
_SAFE_ID = re.compile(SAFE_ID)


@dataclass(frozen=True)
class AgentCaller:
    ctx: AgentContext
    key_id: str

    @property
    def rate_key(self) -> str:
        return f"v1agent:{self.key_id}"


@dataclass(frozen=True)
class OwnerCaller:
    owner: AuthenticatedOwner

    @property
    def rate_key(self) -> str:
        return f"v1owner:{self.owner.session_id}"


class Platform:
    def __init__(self, deps: PlatformDeps, risk_getter: Callable):
        self.deps = deps
        self.risk = risk_getter
        self.clock: Callable[[], datetime] = lambda: self.risk().clock()
        self.registry = AgentRegistry(deps.wallet_store, clock=self.clock)
        self.checker = PermissionChecker(self.registry, clock=self.clock)
        self.credentials = CredentialStore(self.registry, self.clock)
        self.verifier = ApiKeyVerifier(self.registry, self.clock)
        self.agents = AgentDirectory(self.registry, self.credentials, deps.limits,
                                     self.clock)
        self.wallets = WalletBook(deps.wallet_store, deps.limits, self.clock, deps.resources)
        self.payments = PaymentDesk(self.registry, self.checker, self.wallets, deps,
                                    risk_getter, self.clock)
        from .savers import SaverService
        self.savers = SaverService(self)
        from .lab import TestLab
        self.lab = TestLab(self) if deps.integrations is not None else None

    def install(self, risk_service) -> None:
        """Make owner-configured wallets the budget source for platform agents (D1)."""
        spend_policy.install(risk_service, self.wallets, self._owner_of)

    def _owner_of(self, agent_id: str) -> str | None:
        agent = self.registry.get_agent(agent_id)
        return agent.owner_id if agent else None

    # ------------------------------------------------------- authentication
    def authenticate(self, token: str | None) -> AgentCaller | OwnerCaller | None:
        """Classify and verify a /v1 bearer credential. Never raises; None = 401.
        Agent keys and owner credentials never cross over: the prefix selects exactly one
        verifier, and each verifier only accepts its own format."""
        if not token:
            return None
        try:
            if token.startswith(AGENT_KEY_PREFIX):
                ctx = self.verifier.verify(token)
                return AgentCaller(ctx=ctx, key_id=key_id_of(token) or "?")
            if token.startswith(OWNER_CREDENTIAL_PREFIX):
                owner = self.deps.owner_authenticator.authenticate(token)
                if not isinstance(owner, AuthenticatedOwner):
                    return None
                return OwnerCaller(owner=owner)
        except (AgentAuthError, OwnerAuthError):
            return None
        except Exception as exc:  # an authenticator fault never authenticates anyone
            log.error("v1_authentication_error", extra={"error_type": type(exc).__name__})
            return None
        return None

    def ensure_owner(self, owner: AuthenticatedOwner) -> AuthenticatedOwner:
        if not _SAFE_ID.fullmatch(owner.owner_id):
            raise PlatformError(403, "owner_id_unsupported", audit="owner_id_unsupported")
        if self.registry.get_owner(owner.owner_id) is None:
            try:
                self.registry.register_owner(owner)
            except Exception:
                raise PlatformError(403, "owner_id_unsupported",
                                    audit="owner_registration_refused") from None
        return owner
