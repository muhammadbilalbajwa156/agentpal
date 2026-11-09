"""Owner-scoped encrypted integrations and fixed-origin credential verification."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import time

import httpx

from ..ratelimit import LimiterUnavailable, SQLiteSlidingWindowLimiter
from .errors import PlatformError

CATALOG = {
    "paypal_sandbox": ("PayPal Sandbox", "Sandbox orders, captures, refunds, payouts and reconciliation", [
        ("client_id", False, True), ("client_secret", True, True), ("webhook_id", False, False)]),
    "channel3": ("Channel3", "Product search and advisory market comparisons", [("api_key", True, True)]),
    "kernel": ("Kernel", "Read-only browser evidence and page checks", [("api_key", True, True)]),
    "groq": ("Groq", "Test Lab Shopper AI planning", [("api_key", True, True)]),
    "openrouter": ("OpenRouter", "Test Lab Shopper AI planning fallback", [("api_key", True, True)]),
}
ENV_KEYS = {"channel3": "CHANNEL3_API_KEY", "kernel": "KERNEL_API_KEY",
            "groq": "GROQ_API_KEY", "openrouter": "OPENROUTER_API_KEY"}
# No caller/configuration supplied origins, paths, redirects, retries or proxy credentials.
VERIFY_URLS = {
    "paypal_sandbox": "https://api-m.sandbox.paypal.com/v1/oauth2/token",
    "channel3": "https://api.trychannel3.com/v1/search",
    "kernel": "https://api.onkernel.com/browsers?limit=1",
    "groq": "https://api.groq.com/openai/v1/models",
    "openrouter": "https://openrouter.ai/api/v1/key",
}


class VerificationFailed(Exception):
    def __init__(self, code):
        super().__init__("verification_failed")
        self.code = code


class IntegrationVerifier:
    transport_factory = staticmethod(lambda: None)

    def verify(self, provider: str, fields: dict):
        method, kwargs = "GET", {}
        if provider == "paypal_sandbox":
            method = "POST"
            kwargs = {"auth": httpx.BasicAuth(fields["client_id"], fields["client_secret"]),
                      "data": {"grant_type": "client_credentials"}}
        elif provider == "channel3":
            method = "POST"
            kwargs = {"headers": {"x-api-key": fields["api_key"]}, "json": {"query": "pencil"}}
        else:
            kwargs = {"headers": {"Authorization": "Bearer " + fields["api_key"]}}
        try:
            deadline = time.monotonic() + 8
            with httpx.Client(timeout=httpx.Timeout(5, connect=3), follow_redirects=False,
                              transport=self.transport_factory()) as client:  # honour operator egress proxy/CA, like the provider clients
                with client.stream(method, VERIFY_URLS[provider], **kwargs) as response:
                    if response.status_code in {401, 403}:
                        raise VerificationFailed("credentials_rejected")
                    if not 200 <= response.status_code < 300:
                        raise VerificationFailed("provider_unavailable")
                    raw = bytearray()
                    for part in response.iter_bytes():
                        raw.extend(part)
                        if len(raw) > 262144 or time.monotonic() > deadline:
                            raise VerificationFailed("invalid_provider_response")
                    value = json.loads(raw)
                    if not isinstance(value, (dict, list)):
                        raise VerificationFailed("invalid_provider_response")
                    if provider == "paypal_sandbox" and (not isinstance(value, dict) or
                            not isinstance(value.get("access_token"), str) or not value["access_token"]):
                        raise VerificationFailed("invalid_provider_response")
        except VerificationFailed:
            raise
        except Exception:
            # Transport/provider exceptions may embed credentials. Never reflect/log them.
            raise VerificationFailed("provider_unavailable") from None


class Integrations:
    def __init__(self, store, vault, env, rate_path):
        self.store, self.vault, self.env = store, vault, dict(env)
        self.verifier = IntegrationVerifier()
        self.limiter_path = rate_path
        self.limiter = SQLiteSlidingWindowLimiter(rate_path, "workspace-verification", 10)
        with store.tx() as c:
            c.execute("CREATE TABLE IF NOT EXISTS platform_integrations (owner_id TEXT NOT NULL, provider TEXT NOT NULL, ciphertext BLOB, status TEXT NOT NULL, verified_at TEXT, error_code TEXT, PRIMARY KEY(owner_id,provider))")
            c.execute("CREATE TABLE IF NOT EXISTS platform_lab_agents (owner_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, wallet_id TEXT NOT NULL, ciphertext BLOB NOT NULL)")

    def provider(self, provider):
        if provider not in CATALOG:
            raise PlatformError(404, "unknown_provider")

    def row(self, owner_id, provider):
        self.provider(provider)
        rows = self.store.query("SELECT * FROM platform_integrations WHERE owner_id=? AND provider=?", (owner_id, provider))
        return rows[0] if rows else None

    def fields(self, owner_id, provider):
        row = self.row(owner_id, provider)
        return self.vault.decrypt(owner_id, provider, row["ciphertext"]) if row and row["ciphertext"] else None

    def server_fields(self, provider):
        if provider == "paypal_sandbox":
            cid = self.env.get("PAYPAL_SANDBOX_CLIENT_ID")
            secret = self.env.get("PAYPAL_SANDBOX_CLIENT_SECRET") or self.env.get("PAYPAL_SANDBOX_SECRET")
            if cid and secret:
                result = {"client_id": cid, "client_secret": secret}
                if self.env.get("PAYPAL_SANDBOX_WEBHOOK_ID"):
                    result["webhook_id"] = self.env["PAYPAL_SANDBOX_WEBHOOK_ID"]
                return result
            return None
        key = self.env.get(ENV_KEYS[provider])
        return {"api_key": key} if key else None

    def resolve(self, owner_id, provider):
        return self.fields(owner_id, provider) or self.server_fields(provider)

    def item(self, owner_id, provider):
        row = self.row(owner_id, provider)
        label, purpose, fields = CATALOG[provider]
        source = "workspace" if row and row["ciphertext"] else "server" if self.server_fields(provider) else None
        return {"provider": provider, "label": label, "purpose": purpose, "required": False,
                "fields": [{"name": n, "secret": s, "required": r} for n, s, r in fields],
                "status": row["status"] if row else "not_configured", "source": source,
                "hint": None, "verified_at": row["verified_at"] if row else None,
                "error_code": row["error_code"] if row else None}

    def validate(self, provider, fields):
        self.provider(provider)
        schema = CATALOG[provider][2]
        allowed = {n for n, _, _ in schema}
        required = {n for n, _, r in schema if r}
        if (set(fields) - allowed or not required <= set(fields) or any(
                not isinstance(v, str) or not v.strip() or len(v) > 4096 or
                any(ord(ch) < 32 or ord(ch) == 127 for ch in v) for v in fields.values())):
            raise PlatformError(422, "invalid_integration_fields")
        if provider == "paypal_sandbox":
            from ..providers.config import PayPalSandboxConfig
            try:
                PayPalSandboxConfig(**fields)
            except ValueError:
                raise PlatformError(422, "invalid_integration_fields") from None

    def admission(self, owner_id):
        try:
            allowed, retry = self.limiter.hit(owner_id)
        except LimiterUnavailable:
            raise PlatformError(503, "verification_unavailable") from None
        if not allowed:
            raise PlatformError(429, "rate_limited", headers={"Retry-After": str(retry)})

    def put(self, owner_id, provider, fields):
        self.validate(provider, fields)
        self.vault.require()
        self.admission(owner_id)
        self.verifier.verify(provider, fields)
        with self.store.tx() as c:
            c.execute("INSERT INTO platform_integrations VALUES (?,?,?,'verified',?,NULL) ON CONFLICT(owner_id,provider) DO UPDATE SET ciphertext=excluded.ciphertext,status='verified',verified_at=excluded.verified_at,error_code=NULL",
                      (owner_id, provider, self.vault.encrypt(owner_id, provider, fields), datetime.now(timezone.utc).isoformat()))
        return self.item(owner_id, provider)

    def verify(self, owner_id, provider):
        fields = self.resolve(owner_id, provider)
        if not fields:
            raise PlatformError(409, "integration_not_configured")
        self.admission(owner_id)
        try:
            self.verifier.verify(provider, fields)
        except VerificationFailed as exc:
            # Retain encrypted key for the owner to replace/delete, but mark its failed check.
            with self.store.tx() as c:
                c.execute("UPDATE platform_integrations SET status='failed',error_code=? WHERE owner_id=? AND provider=?", (exc.code, owner_id, provider))
            raise
        with self.store.tx() as c:
            c.execute("INSERT INTO platform_integrations VALUES (?,?,NULL,'verified',?,NULL) ON CONFLICT(owner_id,provider) DO UPDATE SET status='verified',verified_at=excluded.verified_at,error_code=NULL",
                      (owner_id, provider, datetime.now(timezone.utc).isoformat()))
        return self.item(owner_id, provider)

    def delete(self, owner_id, provider):
        self.provider(provider)
        with self.store.tx() as c:
            c.execute("DELETE FROM platform_integrations WHERE owner_id=? AND provider=?", (owner_id, provider))
