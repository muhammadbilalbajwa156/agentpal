"""Agent registry: owners, isolated agent identities, roles and capability grants.

Rules enforced here:
* every mutation needs an authenticated owner: an `OwnerPrincipal` (legacy operator token)
  or an `infra_contracts.AuthenticatedOwner` (durable owner credential, developer API).
  An agent can never register, grant, revoke or reactivate anything, for itself or for
  anyone else;
* an owner can only act on its own agents (capabilities are owner-scoped);
* nothing outside the role's ceiling can be granted;
* owner ids and agent ids are disjoint namespaces;
* every change, and every refused change, is appended to `wallet_grant_events`.

`context_for` is the only issuer of `contracts.AgentContext`. The `capabilities` field of a
context is ADVISORY (anyone can construct an AgentContext): `PermissionChecker` re-reads
grants from this registry on every check and never trusts it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone

from ..contracts import AgentContext, Capability
from ..infra_contracts import AuthenticatedOwner
from ..workforce.roles import ROLE_CAPABILITY_CEILING, ROLE_DEFAULT_CAPABILITIES, Role
from .models import AgentIdentity, AgentStatus, InstrumentKind, Owner, PaymentInstrumentRef
from .principals import NotAuthorized, OwnerPrincipal, require_owner
from .store import WalletStore


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RegistryError(ValueError):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass(frozen=True)
class _Actor:
    owner_id: str
    decided_by: str


def owner_actor(actor: object) -> _Actor:
    """The acting owner, or refuse (runtime separation of duties). Accepts exactly an
    `OwnerPrincipal` or an `AuthenticatedOwner`; an AgentContext or anything else fails."""
    if isinstance(actor, AuthenticatedOwner):
        return _Actor(actor.owner_id, f"{actor.owner_id}|session:{actor.session_id}")
    actor = require_owner(actor)
    return _Actor(actor.owner_id, actor.decided_by)


class AgentRegistry:
    def __init__(self, store: WalletStore, clock: Callable[[], datetime] = utc_now):
        self.store = store
        self.clock = clock

    # ------------------------------------------------------------- owners
    def register_owner(self, actor: OwnerPrincipal | AuthenticatedOwner,
                         display_name: str = "") -> Owner:
        """An authenticated owner registers themself (their id comes from the token map,
        never from a parameter)."""
        actor = owner_actor(actor)
        now = self.clock()
        owner = Owner(owner_id=actor.owner_id, display_name=display_name, created_at=now)
        with self.store.tx() as c:
            if c.execute("SELECT 1 FROM wallet_agents WHERE agent_id=?",
                         (owner.owner_id,)).fetchone():
                raise RegistryError("ID_COLLISION", "an agent already uses this id")
            c.execute("INSERT OR IGNORE INTO wallet_owners (owner_id, display_name, "
                      "created_at) VALUES (?,?,?)",
                      (owner.owner_id, display_name, now.isoformat()))
        return self.get_owner(owner.owner_id) or owner

    def get_owner(self, owner_id: str) -> Owner | None:
        rows = self.store.query("SELECT * FROM wallet_owners WHERE owner_id=?", (owner_id,))
        if not rows:
            return None
        r = rows[0]
        return Owner(owner_id=r["owner_id"], display_name=r["display_name"],
                     created_at=datetime.fromisoformat(r["created_at"]))

    # ------------------------------------------------------------- agents
    def register_agent(self, actor: OwnerPrincipal | AuthenticatedOwner, agent_id: str,
                       role: Role | str, capabilities: Iterable[Capability] | None = None,
                       *, in_tx: Callable[[sqlite3.Connection], None] | None = None
                       ) -> AgentIdentity:
        """Register an agent for the acting owner. `capabilities` defaults to the role's
        defaults and must be a subset of the role's ceiling. `in_tx`, if given, runs inside
        the same transaction (the developer platform writes the agent profile and its first
        API key there, so registration is all-or-nothing)."""
        actor = owner_actor(actor)
        role = Role(role)
        now = self.clock()
        agent = AgentIdentity(agent_id=agent_id, owner_id=actor.owner_id, role=role,
                              created_at=now)
        caps = frozenset(ROLE_DEFAULT_CAPABILITIES[role] if capabilities is None
                         else (Capability(c) for c in capabilities))
        over = caps - ROLE_CAPABILITY_CEILING[role]
        if over:
            self._refused(actor, agent_id, "ABOVE_ROLE_CEILING",
                          ",".join(sorted(c.value for c in over)))
            raise RegistryError("ABOVE_ROLE_CEILING",
                                f"{role.value} cannot hold {sorted(c.value for c in over)}")
        with self.store.tx() as c:
            if not c.execute("SELECT 1 FROM wallet_owners WHERE owner_id=?",
                             (actor.owner_id,)).fetchone():
                raise RegistryError("UNKNOWN_OWNER", "register the owner first")
            if c.execute("SELECT 1 FROM wallet_owners WHERE owner_id=?",
                         (agent_id,)).fetchone():
                raise RegistryError("ID_COLLISION", "an owner already uses this id")
            if c.execute("SELECT 1 FROM wallet_agents WHERE agent_id=?",
                         (agent_id,)).fetchone():
                raise RegistryError("AGENT_EXISTS", agent_id)
            c.execute("INSERT INTO wallet_agents (agent_id, owner_id, role, status, "
                      "created_at) VALUES (?,?,?,?,?)",
                      (agent_id, actor.owner_id, role.value, AgentStatus.active.value,
                       now.isoformat()))
            self._event(c, agent_id, actor.owner_id, "register", None, actor, role.value, now)
            for cap in sorted(caps, key=lambda x: x.value):
                self._insert_grant(c, agent_id, cap, actor, now)
            if in_tx is not None:
                in_tx(c)
        return agent

    def get_agent(self, agent_id: str) -> AgentIdentity | None:
        rows = self.store.query("SELECT * FROM wallet_agents WHERE agent_id=?", (agent_id,))
        if not rows:
            return None
        r = rows[0]
        return AgentIdentity(agent_id=r["agent_id"], owner_id=r["owner_id"],
                             role=Role(r["role"]), status=AgentStatus(r["status"]),
                             created_at=datetime.fromisoformat(r["created_at"]))

    def agents_of(self, owner_id: str) -> list[AgentIdentity]:
        rows = self.store.query("SELECT agent_id FROM wallet_agents WHERE owner_id=? "
                                "ORDER BY agent_id", (owner_id,))
        return [a for a in (self.get_agent(r["agent_id"]) for r in rows) if a]

    # ------------------------------------------------------------- grants
    def grant(self, actor: OwnerPrincipal | AuthenticatedOwner,
              agent_id: str, capability: Capability) -> None:
        actor = owner_actor(actor)
        capability = Capability(capability)
        agent = self._owned_agent(actor, agent_id)
        if capability not in ROLE_CAPABILITY_CEILING[agent.role]:
            self._refused(actor, agent_id, "ABOVE_ROLE_CEILING", capability.value)
            raise RegistryError("ABOVE_ROLE_CEILING",
                                f"{agent.role.value} cannot hold {capability.value}")
        now = self.clock()
        with self.store.tx() as c:
            self._insert_grant(c, agent_id, capability, actor, now)

    def revoke(self, actor: OwnerPrincipal | AuthenticatedOwner,
               agent_id: str, capability: Capability) -> None:
        actor = owner_actor(actor)
        capability = Capability(capability)
        self._owned_agent(actor, agent_id)
        now = self.clock()
        with self.store.tx() as c:
            c.execute("DELETE FROM wallet_grants WHERE agent_id=? AND capability=?",
                      (agent_id, capability.value))
            self._event(c, agent_id, actor.owner_id, "revoke", capability.value, actor, None,
                        now)

    def set_status(self, actor: OwnerPrincipal | AuthenticatedOwner,
                   agent_id: str, status: AgentStatus) -> None:
        actor = owner_actor(actor)
        status = AgentStatus(status)
        self._owned_agent(actor, agent_id)
        now = self.clock()
        with self.store.tx() as c:
            c.execute("UPDATE wallet_agents SET status=? WHERE agent_id=?",
                      (status.value, agent_id))
            self._event(c, agent_id, actor.owner_id,
                        "suspend" if status == AgentStatus.suspended else "reactivate",
                        None, actor, None, now)

    def granted(self, agent_id: str) -> frozenset[Capability]:
        """Effective capabilities: stored grants within the role ceiling; none if the agent
        is unknown or suspended."""
        agent = self.get_agent(agent_id)
        if agent is None or agent.status != AgentStatus.active:
            return frozenset()
        rows = self.store.query("SELECT capability, owner_id FROM wallet_grants "
                                "WHERE agent_id=?", (agent_id,))
        caps = set()
        for r in rows:
            try:
                cap = Capability(r["capability"])
            except ValueError:
                continue
            if r["owner_id"] == agent.owner_id:
                caps.add(cap)
        return frozenset(caps) & ROLE_CAPABILITY_CEILING[agent.role]

    def grant_events(self, agent_id: str) -> list[dict]:
        return self.store.query("SELECT * FROM wallet_grant_events WHERE agent_id=? "
                                "ORDER BY id", (agent_id,))

    def context_for(self, agent_id: str) -> AgentContext:
        """The registry-issued context for an agent. Its `capabilities` are a convenience
        copy only; permission checks re-read the registry."""
        agent = self.get_agent(agent_id)
        if agent is None:
            raise RegistryError("UNKNOWN_AGENT", agent_id)
        return AgentContext(agent_id=agent.agent_id, owner_id=agent.owner_id,
                            role=agent.role.value, capabilities=self.granted(agent_id))

    # -------------------------------------------------------- instruments
    def add_instrument(self, actor: OwnerPrincipal | AuthenticatedOwner,
                       instrument: PaymentInstrumentRef) -> None:
        """Record a reference to a sandbox instrument (never credentials, never funds)."""
        actor = owner_actor(actor)
        if instrument.owner_id != actor.owner_id:
            raise NotAuthorized("CROSS_OWNER", "instrument belongs to another owner")
        if instrument.kind == InstrumentKind.provider_funds:  # unreachable: model refuses
            raise RegistryError("NOT_SUPPORTED")
        with self.store.tx() as c:
            c.execute("INSERT INTO wallet_instruments (instrument_id, owner_id, kind, "
                      "provider, external_ref, created_at) VALUES (?,?,?,?,?,?)",
                      (instrument.instrument_id, instrument.owner_id, instrument.kind.value,
                       instrument.provider, instrument.external_ref,
                       self.clock().isoformat()))

    def instruments_of(self, owner_id: str) -> list[PaymentInstrumentRef]:
        rows = self.store.query("SELECT * FROM wallet_instruments WHERE owner_id=? "
                                "ORDER BY instrument_id", (owner_id,))
        return [PaymentInstrumentRef(instrument_id=r["instrument_id"], owner_id=r["owner_id"],
                                     kind=InstrumentKind(r["kind"]), provider=r["provider"],
                                     external_ref=r["external_ref"]) for r in rows]

    # ------------------------------------------------------------ helpers
    def _owned_agent(self, actor: _Actor, agent_id: str) -> AgentIdentity:
        agent = self.get_agent(agent_id)
        if agent is None:
            raise RegistryError("UNKNOWN_AGENT", agent_id)
        if agent.owner_id != actor.owner_id:
            self._refused(actor, agent_id, "CROSS_OWNER", "")
            raise NotAuthorized("CROSS_OWNER", "agent belongs to another owner")
        return agent

    def _insert_grant(self, c, agent_id: str, cap: Capability, actor: _Actor,
                      now: datetime) -> None:
        c.execute("INSERT OR IGNORE INTO wallet_grants (agent_id, capability, owner_id, "
                  "granted_by, granted_at) VALUES (?,?,?,?,?)",
                  (agent_id, cap.value, actor.owner_id, actor.decided_by, now.isoformat()))
        self._event(c, agent_id, actor.owner_id, "grant", cap.value, actor, None, now)

    @staticmethod
    def _event(c, agent_id: str, owner_id: str, action: str, capability: str | None,
               actor: _Actor, detail: str | None, now: datetime) -> None:
        c.execute("INSERT INTO wallet_grant_events (agent_id, owner_id, action, capability, "
                  "actor, detail, at) VALUES (?,?,?,?,?,?,?)",
                  (agent_id, owner_id, action, capability, actor.decided_by, detail,
                   now.isoformat()))

    def record_refusal(self, actor: OwnerPrincipal | AuthenticatedOwner, agent_id: str,
                       code: str, detail: str = "") -> None:
        """Audit a refused owner action (e.g. a cross-owner lookup through the developer
        API) in `wallet_grant_events`. Changes nothing else."""
        self._refused(owner_actor(actor), agent_id, code, detail)

    def _refused(self, actor: _Actor, agent_id: str, code: str, detail: str) -> None:
        with self.store.tx() as c:
            self._event(c, agent_id, actor.owner_id, "refused", None, actor,
                        f"{code}:{detail}"[:200], self.clock())
