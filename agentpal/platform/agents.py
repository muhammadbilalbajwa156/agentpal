"""External agent registration, profiles, credentials and permissions (owner side).

Agents are identities in the existing wallet registry (`wallet_agents`, `wallet_grants`),
so `PermissionChecker` decides for them exactly as for workforce agents. The platform adds
a profile (name, description, metadata) and API keys. Agent ids are server-generated
(`agt_<32 hex>`); a caller can never choose or squat one.

Every operation is owner-scoped: an agent that is unknown or belongs to another owner is
reported as `not_found` (the cross-owner attempt is audited in `wallet_grant_events`).
Grants change only through these owner operations and only within the role ceiling.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterable
from datetime import datetime, timezone

from ..contracts import Capability
from ..infra_contracts import AuthenticatedOwner
from ..wallet.models import AgentIdentity, AgentStatus
from ..wallet.registry import AgentRegistry, RegistryError, owner_actor
from ..workforce.roles import ROLE_CAPABILITY_CEILING, Role
from .credentials import CredentialStore, IssuedKey, insert_key
from .deps import PlatformLimits
from .errors import PlatformError, not_found


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class AgentDirectory:
    def __init__(self, registry: AgentRegistry, credentials: CredentialStore,
                 limits: PlatformLimits, clock: Callable[[], datetime]):
        self.registry = registry
        self.store = registry.store
        self.credentials = credentials
        self.limits = limits
        self.clock = clock

    # ------------------------------------------------------------- reading
    def profile(self, agent_id: str) -> dict | None:
        rows = self.store.query("SELECT * FROM platform_agent_profiles WHERE agent_id=?",
                                (agent_id,))
        return rows[0] if rows else None

    def owned(self, owner: AuthenticatedOwner, agent_id: str) -> AgentIdentity:
        agent = self.registry.get_agent(agent_id)
        if agent is None or self.profile(agent_id) is None:
            raise not_found()
        if agent.owner_id != owner.owner_id:
            self.registry.record_refusal(owner, agent_id, "CROSS_OWNER", "platform")
            raise not_found("cross_owner_agent")
        return agent

    def view(self, agent: AgentIdentity) -> dict:
        p = self.profile(agent.agent_id) or {}
        return {"agent_id": agent.agent_id, "owner_id": agent.owner_id,
                "name": p.get("name", ""), "description": p.get("description", ""),
                "role": agent.role, "status": agent.status.value,
                "capabilities": sorted(c.value for c in self.grants(agent.agent_id)),
                "metadata": json.loads(p.get("metadata_json") or "{}"),
                "created_at": _iso(agent.created_at),
                "keys": self.credentials.keys_of(agent.agent_id)}

    def grants(self, agent_id: str) -> frozenset[Capability]:
        """Stored grants within the ceiling, whatever the agent's status (owner view)."""
        agent = self.registry.get_agent(agent_id)
        if agent is None:
            return frozenset()
        rows = self.store.query("SELECT capability FROM wallet_grants WHERE agent_id=? AND "
                                "owner_id=?", (agent_id, agent.owner_id))
        caps = set()
        for r in rows:
            try:
                caps.add(Capability(r["capability"]))
            except ValueError:
                continue
        return frozenset(caps) & ROLE_CAPABILITY_CEILING[agent.role]

    def list(self, owner: AuthenticatedOwner) -> list[dict]:
        rows = self.store.query(
            "SELECT a.agent_id FROM wallet_agents a JOIN platform_agent_profiles p "
            "ON p.agent_id = a.agent_id WHERE a.owner_id=? ORDER BY a.created_at, a.agent_id",
            (owner.owner_id,))
        return [self.view(a) for a in (self.registry.get_agent(r["agent_id"]) for r in rows)
                if a is not None]

    # ------------------------------------------------------------ mutation
    def register(self, owner: AuthenticatedOwner, name: str, description: str, role: Role,
                 capabilities: Iterable[Capability] | None,
                 metadata: dict[str, str]) -> tuple[dict, IssuedKey]:
        count = self.store.query("SELECT COUNT(*) AS n FROM wallet_agents a JOIN "
                                 "platform_agent_profiles p ON p.agent_id=a.agent_id "
                                 "WHERE a.owner_id=?", (owner.owner_id,))[0]["n"]
        if count >= self.limits.max_agents_per_owner:
            raise PlatformError(409, "agent_limit_reached")
        agent_id = "agt_" + uuid.uuid4().hex
        actor = owner_actor(owner).decided_by
        now = self.clock()
        issued: list[IssuedKey] = []

        def in_tx(c) -> None:
            c.execute("INSERT INTO platform_agent_profiles (agent_id, name, description, "
                      "metadata_json, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                      (agent_id, name, description, json.dumps(metadata, sort_keys=True),
                       _iso(now), _iso(now)))
            issued.append(insert_key(c, agent_id, owner.owner_id, actor, now))

        try:
            agent = self.registry.register_agent(owner, agent_id, role, capabilities,
                                                 in_tx=in_tx)
        except RegistryError as exc:
            if exc.code == "ABOVE_ROLE_CEILING":
                raise PlatformError(400, "above_role_ceiling") from None
            raise PlatformError(409, "registration_refused") from None
        return self.view(agent), issued[0]

    def set_permissions(self, owner: AuthenticatedOwner, agent_id: str,
                        wanted: Iterable[Capability]) -> dict:
        agent = self.owned(owner, agent_id)
        wanted = frozenset(Capability(c) for c in wanted)
        over = wanted - ROLE_CAPABILITY_CEILING[agent.role]
        if over:
            for cap in sorted(over, key=lambda c: c.value):
                try:  # audited refusal, changes nothing
                    self.registry.grant(owner, agent_id, cap)
                except RegistryError:
                    pass
            raise PlatformError(400, "above_role_ceiling")
        current = self.grants(agent_id)
        for cap in sorted(wanted - current, key=lambda c: c.value):
            self.registry.grant(owner, agent_id, cap)
        for cap in sorted(current - wanted, key=lambda c: c.value):
            self.registry.revoke(owner, agent_id, cap)
        return self.permissions(agent)

    def permissions(self, agent: AgentIdentity) -> dict:
        return {"agent_id": agent.agent_id, "role": agent.role,
                "capabilities": sorted(c.value for c in self.grants(agent.agent_id)),
                "ceiling": sorted(c.value for c in ROLE_CAPABILITY_CEILING[agent.role])}

    def _refuse_retired_saver(self, agent_id: str) -> None:
        if self.store.query("SELECT 1 FROM platform_savers WHERE agent_id=? AND status='retired'",
                            (agent_id,)):
            raise PlatformError(409, "saver_retired")

    def set_status(self, owner: AuthenticatedOwner, agent_id: str,
                   status: AgentStatus) -> dict:
        self.owned(owner, agent_id)
        with self.store.tx():
            self._refuse_retired_saver(agent_id)
            self.registry.set_status(owner, agent_id, status)
        return self.view(self.registry.get_agent(agent_id))

    def rotate(self, owner: AuthenticatedOwner, agent_id: str,
               overlap_seconds: int) -> tuple[IssuedKey, str | None]:
        self.owned(owner, agent_id)
        if overlap_seconds > self.limits.max_overlap_seconds:
            raise PlatformError(400, "overlap_too_long")
        with self.store.tx():
            self._refuse_retired_saver(agent_id)
            return self.credentials.rotate(agent_id, owner.owner_id,
                                           owner_actor(owner).decided_by, overlap_seconds)

    def revoke(self, owner: AuthenticatedOwner, agent_id: str, key_id: str) -> None:
        self.owned(owner, agent_id)
        if not self.credentials.revoke(agent_id, owner.owner_id, key_id,
                                       owner_actor(owner).decided_by):
            raise not_found()
