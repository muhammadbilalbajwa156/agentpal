"""Owner authentication: durable, hashed, expiring, revocable owner credentials.

Token format: ``apo_<credential_id>_<secret>``. `credential_id` is 24 hex characters; the
secret is 256 random bits (``secrets.token_urlsafe(32)``). Only ``sha256(salt || secret)``
is stored, and it is compared with ``secrets.compare_digest``. The plaintext token is
returned exactly once, by `issue_token`.

Credential types never cross: an agent API key (``apk_...``) or anything else that is not
an ``apo_`` token is refused before any lookup, so an agent credential can never yield an
`AuthenticatedOwner`.

Every refusal raises a generic `OwnerAuthError("invalid_owner_credential")`: callers
cannot tell an unknown id from a wrong secret, an expired or a revoked credential.

MFA: only a separate five-minute owner_session credential minted after a verified
TOTP counter can satisfy MFA. Parent revocation/expiry and factor removal invalidate it.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from ..auth import redact_sensitive_text
from ..infra_contracts import AuthenticatedOwner, OwnerAuthError
from .models import (
    CREDENTIAL_ID_PATTERN,
    SAFE_ID,
    Organization,
    Owner,
    OwnerCredentialInfo,
    OwnerStatus,
    TotpEnrollment,
)
from .store import IdentityStore

TOKEN_PREFIX = "apo_"
_TOKEN_RE = re.compile(r"^apo_([0-9a-f]{24})_([A-Za-z0-9_\-]{32,128})$")
_GENERIC = "invalid_owner_credential"
# compared against for unknown ids, so every refusal costs one hash comparison
_DUMMY_SALT = "0" * 32
_DUMMY_HASH = hashlib.sha256(b"agentpal-dummy").hexdigest()


class IdentityError(ValueError):
    """An administrative identity operation was refused (bad id, duplicate, unknown)."""


def _utc(at: datetime) -> datetime:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("identity timestamps must be timezone-aware")
    return at.astimezone(timezone.utc)


def _hash(salt: str, secret: str) -> str:
    return hashlib.sha256(bytes.fromhex(salt) + secret.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


class OwnerIdentityService:
    """Implements `infra_contracts.OwnerAuthenticator`."""

    def __init__(self, store: IdentityStore, *,
                 clock: Callable[[], datetime] | None = None,
                 max_token_ttl: timedelta = timedelta(days=90),
                 is_agent_id: Callable[[str], bool] | None = None):
        self.store = store
        self.clock = clock or _now
        self.max_token_ttl = max_token_ttl
        # optional hook (lead wiring): refuses owner ids that are registered agent ids
        self.is_agent_id = is_agent_id

    # ------------------------------------------------------------ admin
    def create_org(self, org_id: str, name: str) -> Organization:
        org = Organization(org_id=org_id, name=name, created_at=_utc(self.clock()))
        try:
            with self.store.tx() as c:
                c.execute("INSERT INTO identity_orgs (org_id, name, created_at) VALUES "
                          "(?,?,?)", (org.org_id, org.name, org.created_at.isoformat()))
        except Exception as exc:  # sqlite3.IntegrityError
            raise IdentityError("ORG_EXISTS") from exc
        return org

    def create_owner(self, owner_id: str, display_name: str = "",
                     org_id: str | None = None) -> Owner:
        if not re.fullmatch(SAFE_ID, owner_id or ""):
            raise IdentityError("INVALID_OWNER_ID")
        if self.is_agent_id is not None and self.is_agent_id(owner_id):
            raise IdentityError("OWNER_ID_IS_AN_AGENT")
        owner = Owner(owner_id=owner_id, org_id=org_id, display_name=display_name,
                      created_at=_utc(self.clock()))
        with self.store.tx() as c:
            if org_id is not None and c.execute(
                    "SELECT 1 FROM identity_orgs WHERE org_id=?", (org_id,)).fetchone() is None:
                raise IdentityError("UNKNOWN_ORG")
            if c.execute("SELECT 1 FROM identity_owners WHERE owner_id=?",
                         (owner_id,)).fetchone():
                raise IdentityError("OWNER_EXISTS")
            c.execute("INSERT INTO identity_owners (owner_id, org_id, display_name, status, "
                      "created_at) VALUES (?,?,?,?,?)",
                      (owner.owner_id, org_id, display_name, owner.status.value,
                       owner.created_at.isoformat()))
            self._event(c, owner_id, None, "create_owner")
        return owner

    def get_owner(self, owner_id: str) -> Owner | None:
        rows = self.store.query("SELECT * FROM identity_owners WHERE owner_id=?", (owner_id,))
        return Owner(**rows[0]) if rows else None

    def disable_owner(self, owner_id: str) -> None:
        with self.store.tx() as c:
            cur = c.execute("UPDATE identity_owners SET status='disabled' WHERE owner_id=?",
                            (owner_id,))
            if cur.rowcount != 1:
                raise IdentityError("UNKNOWN_OWNER")
            self._event(c, owner_id, None, "disable_owner")

    def issue_token(self, owner_id: str, ttl: timedelta = timedelta(days=30),
                    label: str = "") -> tuple[str, OwnerCredentialInfo]:
        """Issue a credential. The plaintext token is returned once and never stored."""
        if ttl <= timedelta(0) or ttl > self.max_token_ttl:
            raise IdentityError("INVALID_TTL")
        now = _utc(self.clock())
        credential_id = secrets.token_hex(12)
        secret = secrets.token_urlsafe(32)
        salt = secrets.token_hex(16)
        info = OwnerCredentialInfo(credential_id=credential_id, owner_id=owner_id,
                                   label=label, created_at=now, expires_at=now + ttl)
        with self.store.tx() as c:
            row = c.execute("SELECT status FROM identity_owners WHERE owner_id=?",
                            (owner_id,)).fetchone()
            if row is None:
                raise IdentityError("UNKNOWN_OWNER")
            if row[0] != OwnerStatus.active.value:
                raise IdentityError("OWNER_DISABLED")
            c.execute("INSERT INTO identity_owner_credentials (credential_id, owner_id, salt, "
                      "secret_sha256, label, created_at, expires_at) VALUES (?,?,?,?,?,?,?)",
                      (credential_id, owner_id, salt, _hash(salt, secret), label,
                       now.isoformat(), info.expires_at.isoformat()))
            self._event(c, owner_id, credential_id, "issue_token")
        return f"{TOKEN_PREFIX}{credential_id}_{secret}", info

    def import_token(self, owner_id: str, token: str, ttl: timedelta = timedelta(days=30),
                     label: str = "bootstrap") -> bool:
        """Register an operator-generated owner token (hosted bootstrap from a secret env
        var). Only its salted hash is stored. Idempotent: returns False when that credential
        id already exists (whatever its state; a revoked token is never revived)."""
        m = _TOKEN_RE.fullmatch(token or "")
        if m is None:
            raise IdentityError("INVALID_TOKEN_FORMAT")
        if ttl <= timedelta(0) or ttl > self.max_token_ttl:
            raise IdentityError("INVALID_TTL")
        credential_id, secret = m.group(1), m.group(2)
        now = _utc(self.clock())
        salt = secrets.token_hex(16)
        with self.store.tx() as c:
            if c.execute("SELECT 1 FROM identity_owner_credentials WHERE credential_id=?",
                         (credential_id,)).fetchone():
                return False
            row = c.execute("SELECT status FROM identity_owners WHERE owner_id=?",
                            (owner_id,)).fetchone()
            if row is None:
                raise IdentityError("UNKNOWN_OWNER")
            if row[0] != OwnerStatus.active.value:
                raise IdentityError("OWNER_DISABLED")
            c.execute("INSERT INTO identity_owner_credentials (credential_id, owner_id, salt, "
                      "secret_sha256, label, created_at, expires_at) VALUES (?,?,?,?,?,?,?)",
                      (credential_id, owner_id, salt, _hash(salt, secret), label,
                       now.isoformat(), (now + ttl).isoformat()))
            self._event(c, owner_id, credential_id, "import_token")
        return True

    def revoke_token(self, credential_id: str) -> None:
        now = _utc(self.clock())
        with self.store.tx() as c:
            row = c.execute("SELECT owner_id FROM identity_owner_credentials WHERE "
                            "credential_id=?", (credential_id,)).fetchone()
            if row is None:
                raise IdentityError("UNKNOWN_CREDENTIAL")
            c.execute("UPDATE identity_owner_credentials SET revoked_at=? WHERE "
                      "credential_id=? AND revoked_at IS NULL", (now.isoformat(), credential_id))
            self._event(c, row[0], credential_id, "revoke_token")

    def list_tokens(self, owner_id: str) -> list[OwnerCredentialInfo]:
        rows = self.store.query(
            "SELECT credential_id, owner_id, label, created_at, expires_at, revoked_at, "
            "last_used_at FROM identity_owner_credentials WHERE owner_id=? "
            "ORDER BY created_at, credential_id", (owner_id,))
        return [OwnerCredentialInfo(**{**r, "label": redact_sensitive_text(r["label"])}) for r in rows]

    # ------------------------------------------------------- authenticate
    def authenticate(self, bearer: str) -> AuthenticatedOwner:
        """`OwnerAuthenticator`: an owner token (optionally prefixed "Bearer ") ->
        `AuthenticatedOwner`. Raises a generic `OwnerAuthError` otherwise."""
        token = bearer.strip() if isinstance(bearer, str) else ""
        if token[:7].lower() == "bearer ":
            token = token[7:].strip()
        m = _TOKEN_RE.fullmatch(token)
        if m is None:  # agent keys (apk_...), operator tokens, garbage: never an owner
            raise OwnerAuthError(_GENERIC)
        now = _utc(self.clock())
        with self.store.tx() as c:
            row = self._active_credential(c, m.group(1), now)
            salt = row["salt"] if row else _DUMMY_SALT
            expected = row["secret_sha256"] if row else _DUMMY_HASH
            ok = secrets.compare_digest(_hash(salt, m.group(2)), expected)
            if not ok or row is None:
                raise OwnerAuthError(_GENERIC)
            c.execute("UPDATE identity_owner_credentials SET last_used_at=? WHERE credential_id=?",
                      (now.isoformat(), m.group(1)))
            return self._principal(row, now)

    def _active_credential(self, c, credential_id, now):
        row = c.execute("SELECT c.*, o.status, o.org_id FROM identity_owner_credentials c "
                        "JOIN identity_owners o ON o.owner_id=c.owner_id "
                        "WHERE credential_id=?", (credential_id,)).fetchone()
        if row is None or row["revoked_at"] is not None or row["status"] != "active" \
                or _utc(datetime.fromisoformat(row["expires_at"])) <= now:
            return None
        proof = (row["parent_credential_id"], row["factor_id"], row["mfa_verified_at"])
        if any(v is not None for v in proof):
            if not all(proof):
                return None
            parent = c.execute("SELECT * FROM identity_owner_credentials WHERE credential_id=?",
                               (row["parent_credential_id"],)).fetchone()
            factor = c.execute("SELECT * FROM identity_totp WHERE owner_id=? AND factor_id=?",
                               (row["owner_id"], row["factor_id"])).fetchone()
            if (parent is None or parent["owner_id"] != row["owner_id"]
                    or parent["revoked_at"] is not None or parent["parent_credential_id"] is not None
                    or _utc(datetime.fromisoformat(parent["expires_at"])) <= now
                    or factor is None or factor["enabled_at"] is None):
                return None
            verified = _utc(datetime.fromisoformat(row["mfa_verified_at"]))
            expires = _utc(datetime.fromisoformat(row["expires_at"]))
            if not (verified <= now < expires <= verified + timedelta(minutes=5)):
                return None
            try:
                self._decrypt_factor(factor)  # missing/wrong key never authenticates MFA
            except IdentityError:
                return None
        return row

    @staticmethod
    def _principal(row, now):
        mfa = row["mfa_verified_at"] is not None
        return AuthenticatedOwner(owner_id=row["owner_id"], org_id=row["org_id"],
            session_id=row["credential_id"], auth_method="owner_session" if mfa else "owner_api_token",
            authenticated_at=now, mfa_satisfied=mfa)

    def assert_session_active(self, owner: AuthenticatedOwner) -> None:
        """Re-read durable identity and factor proof before accepting a decision."""
        if not isinstance(owner, AuthenticatedOwner) or not re.fullmatch(
                CREDENTIAL_ID_PATTERN, owner.session_id):
            raise OwnerAuthError(_GENERIC)
        now = _utc(self.clock())
        with self.store.tx() as c:
            row = self._active_credential(c, owner.session_id, now)
            if row is None:
                raise OwnerAuthError(_GENERIC)
            expected = self._principal(row, now)
            if any(getattr(owner, key) != getattr(expected, key) for key in
                   ("owner_id", "org_id", "session_id", "auth_method", "mfa_satisfied")):
                raise OwnerAuthError(_GENERIC)

    @staticmethod
    def _cipher():
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        try:
            raw = os.environ.get("AGENTPAL_MFA_ENCRYPTION_KEY", "")
            key = base64.b64decode(raw, altchars=b"-_", validate=True)
            if len(key) != 32:
                raise ValueError("invalid key")
            return AESGCM(key)
        except (ValueError, TypeError):
            raise IdentityError("MFA_UNAVAILABLE") from None

    def _decrypt_factor(self, factor):
        from cryptography.exceptions import InvalidTag
        try:
            raw = factor["encrypted_seed"]
            aad = (factor["owner_id"] + ":" + factor["factor_id"]).encode()
            return self._cipher().decrypt(raw[:12], raw[12:], aad)
        except (InvalidTag, ValueError, TypeError):
            raise IdentityError("MFA_UNAVAILABLE") from None

    def _revalidate(self, c, owner, now):
        row = self._active_credential(c, owner.session_id, now)
        if row is None or row["owner_id"] != owner.owner_id or row["org_id"] != owner.org_id:
            raise OwnerAuthError(_GENERIC)
        return row

    def begin_totp_enrollment(self, bearer: str) -> TotpEnrollment:
        from urllib.parse import quote, urlencode
        owner = self.authenticate(bearer)
        now = _utc(self.clock())
        seed = secrets.token_bytes(20)
        factor_id, nonce = secrets.token_hex(12), secrets.token_bytes(12)
        encrypted = nonce + self._cipher().encrypt(nonce, seed,
                                                   (owner.owner_id + ":" + factor_id).encode())
        expires = now + timedelta(minutes=10)
        with self.store.tx() as c:
            self._revalidate(c, owner, now)
            old = c.execute("SELECT * FROM identity_totp WHERE owner_id=?", (owner.owner_id,)).fetchone()
            if old is not None and (old["enabled_at"] is not None or
                    (old["locked_until"] and datetime.fromisoformat(old["locked_until"]) > now) or
                    datetime.fromisoformat(old["enrollment_expires_at"]) > now):
                raise IdentityError("MFA_ENROLLMENT_REFUSED")
            # Preserve failures and lockout even when an expired pending factor is replaced.
            failures, locked = (old["failures"], old["locked_until"]) if old else (0, None)
            c.execute("INSERT OR REPLACE INTO identity_totp (owner_id,factor_id,encrypted_seed,"
                      "enrollment_session_id,enrollment_expires_at,failures,locked_until) "
                      "VALUES (?,?,?,?,?,?,?)", (owner.owner_id, factor_id, encrypted,
                      owner.session_id, expires.isoformat(), failures, locked))
            self._event(c, owner.owner_id, owner.session_id, "totp_enrollment_started")
        secret = base64.b32encode(seed).decode()
        uri = "otpauth://totp/" + quote("AgentPal:" + owner.owner_id, safe="") + "?" + urlencode({
            "secret": secret, "issuer": "AgentPal", "algorithm": "SHA1", "digits": 6, "period": 30})
        return TotpEnrollment(secret=secret, provisioning_uri=uri, expires_at=expires)

    def _verify_code(self, c, factor, otp, now):
        from .totp import match_counter
        if factor["locked_until"] and datetime.fromisoformat(factor["locked_until"]) > now:
            return None
        seed = self._decrypt_factor(factor)
        counter = match_counter(seed, otp, int(now.timestamp()), factor["last_counter"])
        if counter is None:
            failures = factor["failures"] + 1 if not factor["locked_until"] else 1
            locked = (now + timedelta(minutes=5)).isoformat() if failures >= 5 else None
            c.execute("UPDATE identity_totp SET failures=?,locked_until=? WHERE owner_id=?",
                      (failures, locked, factor["owner_id"]))
            self._event(c, factor["owner_id"], None, "totp_verification_failed")
        else:
            c.execute("UPDATE identity_totp SET last_counter=?,failures=0,locked_until=NULL "
                      "WHERE owner_id=?", (counter, factor["owner_id"]))
        return counter

    def confirm_totp_enrollment(self, bearer: str, otp: object) -> None:
        owner = self.authenticate(bearer)
        now, verified = _utc(self.clock()), False
        with self.store.tx() as c:
            self._revalidate(c, owner, now)
            factor = c.execute("SELECT * FROM identity_totp WHERE owner_id=?", (owner.owner_id,)).fetchone()
            if (factor is None or factor["enabled_at"] is not None or
                    factor["enrollment_session_id"] != owner.session_id or
                    datetime.fromisoformat(factor["enrollment_expires_at"]) <= now):
                raise IdentityError("MFA_ENROLLMENT_REFUSED")
            verified = self._verify_code(c, factor, otp, now) is not None
            if verified:
                c.execute("UPDATE identity_totp SET enabled_at=? WHERE owner_id=?",
                          (now.isoformat(), owner.owner_id))
                self._event(c, owner.owner_id, owner.session_id, "totp_enrollment_confirmed")
        # Failed counters MUST commit before the generic refusal is raised.
        if not verified:
            raise IdentityError("MFA_VERIFICATION_FAILED")

    def step_up(self, bearer: str, otp: object) -> tuple[str, OwnerCredentialInfo]:
        owner = self.authenticate(bearer)
        now, result = _utc(self.clock()), None
        with self.store.tx() as c:
            parent = self._revalidate(c, owner, now)
            if parent["parent_credential_id"] is not None:
                raise IdentityError("MFA_PARENT_REQUIRED")
            factor = c.execute("SELECT * FROM identity_totp WHERE owner_id=?", (owner.owner_id,)).fetchone()
            if factor is None or factor["enabled_at"] is None:
                raise IdentityError("MFA_NOT_ENROLLED")
            if self._verify_code(c, factor, otp, now) is not None:
                credential_id, secret, salt = secrets.token_hex(12), secrets.token_urlsafe(32), secrets.token_hex(16)
                expires = min(now + timedelta(minutes=5), datetime.fromisoformat(parent["expires_at"]))
                info = OwnerCredentialInfo(credential_id=credential_id, owner_id=owner.owner_id,
                    label="mfa-step-up", created_at=now, expires_at=expires)
                c.execute("INSERT INTO identity_owner_credentials (credential_id,owner_id,salt,"
                          "secret_sha256,label,created_at,expires_at,parent_credential_id,factor_id,"
                          "mfa_verified_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (credential_id, owner.owner_id,
                          salt, _hash(salt, secret), info.label, now.isoformat(), expires.isoformat(),
                          owner.session_id, factor["factor_id"], now.isoformat()))
                self._event(c, owner.owner_id, credential_id, "totp_step_up")
                result = (f"apo_{credential_id}_{secret}", info)
        if result is None:
            raise IdentityError("MFA_VERIFICATION_FAILED")
        return result

    def remove_totp(self, bearer: str) -> None:
        owner = self.authenticate(bearer)
        if not owner.mfa_satisfied:
            raise IdentityError("MFA_REQUIRED")
        now = _utc(self.clock())
        with self.store.tx() as c:
            self._revalidate(c, owner, now)
            c.execute("DELETE FROM identity_totp WHERE owner_id=?", (owner.owner_id,))
            self._event(c, owner.owner_id, owner.session_id, "totp_removed")

    @staticmethod
    def _event(c, owner_id: str | None, credential_id: str | None, action: str) -> None:
        c.execute("INSERT INTO identity_events (owner_id, credential_id, action, at) VALUES "
                  "(?,?,?,datetime('now'))", (owner_id, credential_id, action))
