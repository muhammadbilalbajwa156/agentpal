"""First-owner provisioning, serialized in the identity database across workers."""
from __future__ import annotations

from datetime import timedelta
import hmac
from pathlib import Path
import secrets

from ..identity.service import _hash
from ..ratelimit import LimiterUnavailable, SQLiteSlidingWindowLimiter
from .errors import PlatformError
from .secrets import private_file


class SetupService:
    def __init__(self, identity, env, data_dir: Path | None, hosted: bool, rate_path: str):
        self.identity, self.hosted = identity, hosted
        self._invite = env.get("AGENTPAL_SIGNUP_INVITE_CODE", "")
        self._code_path = data_dir / "setup-code" if data_dir else None
        self._code = None
        self.limiter = SQLiteSlidingWindowLimiter(rate_path, "workspace-signup", 10)
        if not hosted and not self.has_owners():
            self._code = (private_file(self._code_path, secrets.token_urlsafe(24).encode()).decode().strip()
                          if self._code_path else secrets.token_urlsafe(24))

    def has_owners(self):
        return bool(self.identity.store.query("SELECT 1 FROM identity_owners LIMIT 1"))

    def status(self):
        return {"mode": "hosted" if self.hosted else "self_hosted",
                "needs_setup": not self.hosted and not self.has_owners(),
                "signup": ("invite_code" if self._invite else "disabled") if self.hosted else "setup_code"}

    def console_code(self):
        return self._code if not self.hosted and not self.has_owners() else None

    def signup(self, owner_id: str, display_name: str, code: str, peer: str):
        from ..api import is_loopback_peer
        try:
            allowed, retry = self.limiter.hit(peer)
        except LimiterUnavailable:
            raise PlatformError(503, "signup_unavailable") from None
        if not allowed:
            raise PlatformError(429, "rate_limited", headers={"Retry-After": str(retry)})
        if not self.hosted and not is_loopback_peer(peer):
            raise PlatformError(403, "loopback_required")
        if self.hosted and not self._invite:
            raise PlatformError(403, "signup_disabled")
        now = self.identity.clock()
        credential_id, secret, salt = secrets.token_hex(12), secrets.token_urlsafe(32), secrets.token_hex(16)
        with self.identity.store.tx() as c:
            if not self.hosted and c.execute("SELECT 1 FROM identity_owners LIMIT 1").fetchone():
                raise PlatformError(403, "setup_complete")
            expected = self._invite if self.hosted else self._code
            if not expected or not hmac.compare_digest(code.encode(), expected.encode()):
                raise PlatformError(403, "invalid_setup_code")
            if c.execute("SELECT 1 FROM identity_owners WHERE owner_id=?", (owner_id,)).fetchone():
                raise PlatformError(409, "owner_exists")
            c.execute("INSERT INTO identity_owners (owner_id,display_name,status,created_at) VALUES (?,?,'active',?)",
                      (owner_id, display_name, now.isoformat()))
            c.execute("INSERT INTO identity_owner_credentials (credential_id,owner_id,salt,secret_sha256,label,created_at,expires_at) VALUES (?,?,?,?,?,?,?)",
                      (credential_id, owner_id, salt, _hash(salt, secret), "signup", now.isoformat(),
                       (now + timedelta(days=30)).isoformat()))
            self.identity._event(c, owner_id, None, "create_owner")
            self.identity._event(c, owner_id, credential_id, "issue_token")
        if not self.hosted:
            self._code = None
            if self._code_path:
                self._code_path.unlink(missing_ok=True)
        return {"owner_id": owner_id, "token": f"apo_{credential_id}_{secret}"}
