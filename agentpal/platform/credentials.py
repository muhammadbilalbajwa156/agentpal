"""Agent API keys: `apk_<key_id>_<secret>`.

* `key_id`: 16 lowercase hex characters, a non-secret label (audit, rate-limit key).
* `secret`: 32 random bytes (256 bits), URL-safe base64 without padding (43 characters).
* Only the SHA-256 of the WHOLE key is stored. A 256-bit random secret makes an unsalted
  SHA-256 adequate (it is a lookup check, not a password hash). The plaintext key is
  returned exactly once (registration or rotation) and never written anywhere.
* Verification looks the row up by `key_id` and compares digests with
  `hmac.compare_digest`; an unknown `key_id` still performs a comparison.
* Rotation issues a new key. The previous live key either dies immediately or keeps
  working until the end of an owner-chosen overlap window; at most one key is ever in an
  overlap window, so an agent has at most two live keys.
* A revoked, expired, unknown, malformed or suspended-agent key yields the same
  `AgentAuthError` (callers see a generic 401).

`ApiKeyVerifier` implements `infra_contracts.AgentCredentialVerifier`. The capabilities in
the `AgentContext` it returns are ADVISORY: authorization is always re-decided by the
registry-backed `PermissionChecker`.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..contracts import AgentContext
from ..infra_contracts import AgentAuthError
from ..wallet.models import AgentStatus
from ..wallet.registry import AgentRegistry

KEY_PREFIX = "apk_"
KEY_RE = re.compile(r"^apk_([0-9a-f]{16})_([A-Za-z0-9_-]{43})$")
_DUMMY_DIGEST = hashlib.sha256(b"agentpal-no-such-key").hexdigest()


def hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def key_id_of(api_key: str | None) -> str | None:
    m = KEY_RE.fullmatch(api_key or "")
    return m.group(1) if m else None


@dataclass(frozen=True)
class IssuedKey:
    key_id: str
    api_key: str            # plaintext: returned to the caller once, never stored
    created_at: datetime


def new_key() -> tuple[str, str]:
    key_id = secrets.token_hex(8)
    secret = secrets.token_urlsafe(32)
    api_key = f"{KEY_PREFIX}{key_id}_{secret}"
    assert KEY_RE.fullmatch(api_key)
    return key_id, api_key


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _event(c: sqlite3.Connection, agent_id: str, owner_id: str, key_id: str | None,
           action: str, actor: str, detail: str | None, now: datetime) -> None:
    c.execute("INSERT INTO platform_credential_events (agent_id, owner_id, key_id, action, "
              "actor, detail, at) VALUES (?,?,?,?,?,?,?)",
              (agent_id, owner_id, key_id, action, actor, detail, _iso(now)))


def insert_key(c: sqlite3.Connection, agent_id: str, owner_id: str, actor: str,
               now: datetime, action: str = "issue") -> IssuedKey:
    key_id, api_key = new_key()
    c.execute("INSERT INTO platform_agent_keys (key_id, agent_id, owner_id, key_sha256, "
              "created_at, created_by) VALUES (?,?,?,?,?,?)",
              (key_id, agent_id, owner_id, hash_key(api_key), _iso(now), actor))
    _event(c, agent_id, owner_id, key_id, action, actor, None, now)
    return IssuedKey(key_id=key_id, api_key=api_key, created_at=now)


def _live(row: dict, now: datetime) -> bool:
    if row["revoked_at"] is not None:
        return False
    exp = row["expires_at"]
    return exp is None or datetime.fromisoformat(exp) > now


class CredentialStore:
    def __init__(self, registry: AgentRegistry, clock: Callable[[], datetime]):
        self.registry = registry
        self.store = registry.store
        self.clock = clock

    def keys_of(self, agent_id: str) -> list[dict]:
        now = self.clock()
        rows = self.store.query(
            "SELECT key_id, created_at, expires_at, revoked_at FROM platform_agent_keys "
            "WHERE agent_id=? ORDER BY created_at, key_id", (agent_id,))
        for r in rows:
            r["status"] = ("revoked" if r["revoked_at"] else
                           "live" if _live(r, now) else "expired")
        return rows

    def rotate(self, agent_id: str, owner_id: str, actor: str,
               overlap_seconds: int) -> tuple[IssuedKey, str | None]:
        """Issue a new key. Previous live keys: a key already in an overlap window is
        revoked now; the current primary key expires after `overlap_seconds` (0 = now)."""
        now = self.clock()
        overlap_until = now + timedelta(seconds=overlap_seconds)
        with self.store.tx() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM platform_agent_keys WHERE agent_id=? AND revoked_at IS NULL",
                (agent_id,)).fetchall()]
            kept_until = None
            for r in rows:
                if not _live(r, now):
                    continue
                if r["expires_at"] is not None or overlap_seconds == 0:
                    c.execute("UPDATE platform_agent_keys SET revoked_at=?, revoked_reason="
                              "'rotated' WHERE key_id=?", (_iso(now), r["key_id"]))
                    _event(c, agent_id, owner_id, r["key_id"], "revoke", actor, "rotated",
                           now)
                else:
                    c.execute("UPDATE platform_agent_keys SET expires_at=? WHERE key_id=?",
                              (_iso(overlap_until), r["key_id"]))
                    _event(c, agent_id, owner_id, r["key_id"], "overlap_expiry", actor,
                           f"overlap_seconds={overlap_seconds}", now)
                    kept_until = _iso(overlap_until)
            issued = insert_key(c, agent_id, owner_id, actor, now, action="rotate")
        return issued, kept_until

    def revoke(self, agent_id: str, owner_id: str, key_id: str, actor: str) -> bool:
        now = self.clock()
        with self.store.tx() as c:
            row = c.execute("SELECT revoked_at FROM platform_agent_keys WHERE key_id=? AND "
                            "agent_id=?", (key_id, agent_id)).fetchone()
            if row is None:
                return False
            if row["revoked_at"] is None:
                c.execute("UPDATE platform_agent_keys SET revoked_at=?, revoked_reason="
                          "'revoked_by_owner' WHERE key_id=?", (_iso(now), key_id))
            _event(c, agent_id, owner_id, key_id, "revoke", actor, "revoked_by_owner", now)
        return True


class ApiKeyVerifier:
    """`infra_contracts.AgentCredentialVerifier` for `apk_` keys."""

    def __init__(self, registry: AgentRegistry, clock: Callable[[], datetime]):
        self.registry = registry
        self.clock = clock

    def verify(self, api_key: str) -> AgentContext:
        m = KEY_RE.fullmatch(api_key or "") if isinstance(api_key, str) else None
        presented = hash_key(api_key) if m else _DUMMY_DIGEST
        rows = self.registry.store.query(
            "SELECT * FROM platform_agent_keys WHERE key_id=?",
            (m.group(1) if m else "",))
        stored = rows[0]["key_sha256"] if rows else _DUMMY_DIGEST
        matches = hmac.compare_digest(stored, presented)
        if not (m and rows and matches):
            raise AgentAuthError("invalid agent credential")
        row = rows[0]
        if not _live(row, self.clock()):
            raise AgentAuthError("invalid agent credential")
        agent = self.registry.get_agent(row["agent_id"])
        if agent is None or agent.status != AgentStatus.active \
                or agent.owner_id != row["owner_id"]:
            raise AgentAuthError("invalid agent credential")
        return self.registry.context_for(agent.agent_id)
