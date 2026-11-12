"""HTTP API (FastAPI).

The legacy endpoints (/health, /model-info, /assess, /assessments/{id}) assess risk only and
authenticate with `TokenRegistry` tokens, unchanged. When `create_app(platform=...)` is
given `PlatformDeps`, the /v1 developer API (agentpal.platform) is mounted as well: it
authenticates agent API keys (`apk_...`) and owner credentials (`apo_...`) instead, behind
loopback, Host allow-list, body-size and rate-limit protections, with one opt-in
HTTPS-only public webhook exception. Without
`platform`, no /v1 route exists.
"""

from __future__ import annotations

import asyncio

import ipaddress
import json
import logging
import re
import sys
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, FastAPI, Path, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from .dashboard import Dashboard, load_site
from .dashboard import matches as dashboard_matches
from . import __version__
from .auth import (Principal, TokenRegistry, bearer_token, redact_sensitive_text,
                   reject_credential_input, safe_log_value)
from .config import Settings, UnsafeConfiguration, get_settings
from .ratelimit import LimiterUnavailable, SQLiteSlidingWindowLimiter, SlidingWindowLimiter
from .schemas import ID_PATTERN, PaymentRequest, RiskAssessment
from .service import RiskService

if TYPE_CHECKING:
    from .platform import PlatformDeps

_ID_RE = re.compile(ID_PATTERN.strip("^$"))
_LOC_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_MAX_ERRORS = 20
PRINCIPAL_KEY = "agentpal.principal"
V1_CALLER_KEY = "agentpal.v1_caller"  # same value as agentpal.platform.router
_DEV_PRINCIPAL = "dev-unauthenticated"
_CREDENTIAL_PREFIXES = ("apk_", "apo_", "apt_")
# documents the legacy scheme in OpenAPI; authentication itself is done by SecurityGate
_legacy_scheme = HTTPBearer(
    scheme_name="LegacyToken", bearerFormat="apt_...", auto_error=False,
    description="Legacy per-agent or operator token (TokenRegistry); legacy endpoints only.")
V1_DESCRIPTION = (
    "AgentPal: financial infrastructure for EXTERNAL AI agents. The /v1 developer API "
    "registers agents (one-time API keys), configures virtual wallets (a VIRTUAL ALLOWANCE "
    "derived from the ledger, never money), grants permissions, and accepts payment "
    "requests that are validated, budget-checked and risk-assessed, then need an "
    "authenticated owner approval bound to the exact terms. Execution is PayPal SANDBOX "
    "only. Errors are `{\"error\": \"<code>\"}`. Development preview: not production-ready. "
    "The legacy risk-assessment endpoints are documented as well.")


async def _send_json(send, status: int, payload: dict,
                     extra_headers: list[tuple[bytes, bytes]] | None = None) -> None:
    body = json.dumps(payload).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()),
                            (b"connection", b"close"), *(extra_headers or [])]})
    await send({"type": "http.response.body", "body": body})


class _JsonFormatter(logging.Formatter):
    _std = set(vars(logging.makeLogRecord({})))

    def format(self, record: logging.LogRecord) -> str:
        payload = {"level": record.levelname, "logger": record.name, "msg": record.getMessage()}
        payload.update({k: v for k, v in vars(record).items() if k not in self._std})
        return json.dumps(safe_log_value(payload), default=str)


class _SafeAccessFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        args = record.args
        if isinstance(args, tuple) and len(args) == 5:
            peer, method, target, version, status = args
            path = str(target).split("?", 1)[0]
            # Raw resource IDs may themselves be secrets, including encoded forms.
            # Keep route families/statuses for operations without logging opaque targets.
            if path.startswith("/v1/"):
                path = "/v1/[resource]"
            elif path.startswith("/assessments/"):
                path = "/assessments/[resource]"
            elif path not in {"/health", "/model-info", "/assess", "/docs", "/openapi.json", "/redoc"}:
                path = "[unrecognized]"
            return json.dumps(safe_log_value({"event": "http_access", "peer": peer,
                "method": method, "path": path, "http_version": version, "status": status}))
        return json.dumps({"event": "http_access", "status": "unavailable"})


def uvicorn_log_config() -> dict:
    from copy import deepcopy
    from uvicorn.config import LOGGING_CONFIG
    config = deepcopy(LOGGING_CONFIG)
    config["formatters"]["default"] = {"()": "agentpal.api._JsonFormatter"}
    config["formatters"]["access"] = {"()": "agentpal.api._SafeAccessFormatter"}
    return config


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger("agentpal")
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    root.propagate = False


class BodySizeLimit:
    """Pure-ASGI request-size limit enforced *while streaming* (P0.7b).

    * A declared Content-Length above the limit is rejected before any body is read;
      a malformed Content-Length is rejected with 400.
    * Otherwise the body is read chunk by chunk and abandoned as soon as the running
      total exceeds the limit, so at most `max_bytes` (+ one chunk) is ever buffered,
      with or without Content-Length (chunked transfer encoding).
    * The accepted body is replayed to the application unchanged.
    """

    _METHODS = {"POST", "PUT", "PATCH"}
    BODY_READ_TIMEOUT_S = 5.0

    def __init__(self, app, max_bytes: int, on_reject=None):
        self.app = app
        self.max_bytes = max_bytes
        self.on_reject = on_reject  # optional audit hook: (reason, scope) -> None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") not in self._METHODS:
            await self.app(scope, receive, send)
            return
        cap = min(self.max_bytes, 512 * 1024) if scope.get("agentpal.public_webhook") else self.max_bytes
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None:
            if not declared.isdigit():
                await self._reject(scope, send, 400, "invalid_content_length")
                return
            if int(declared) > cap:
                await self._reject(scope, send, 413, "request_too_large")
                return
        chunks, total, more = [], 0, True
        # E2E-H1-2: the whole body must arrive within BODY_READ_TIMEOUT_S; a stalled or
        # trickled body is refused (408) instead of holding a connection open indefinitely.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.BODY_READ_TIMEOUT_S
        while more:
            try:
                message = await asyncio.wait_for(receive(), timeout=max(0.0, deadline - loop.time()))
            except asyncio.TimeoutError:
                await self._reject(scope, send, 408, "request_timeout")
                return
            if message["type"] == "http.disconnect":
                return
            body = message.get("body", b"")
            total += len(body)
            if total > cap:
                await self._reject(scope, send, 413, "request_too_large")
                return
            chunks.append(body)
            more = message.get("more_body", False)
        buffered = b"".join(chunks)
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": buffered, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)

    async def _reject(self, scope, send, status: int, error: str) -> None:
        if self.on_reject is not None:
            try:
                self.on_reject(error, scope)
            except Exception:  # auditing must never turn a rejection into a 500
                logging.getLogger("agentpal.api").error("reject_audit_failed")
        body = json.dumps({"error": error}).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode()),
                                (b"connection", b"close")]})
        await send({"type": "http.response.body", "body": body})


# Starlette's in-process test transport reports this peer name; a network peer always has
# an IP address, so it cannot be presented by a real client.
_TEST_TRANSPORT_PEER = "testclient"


def is_loopback_peer(host: str | None) -> bool:
    if host == _TEST_TRANSPORT_PEER:
        return True
    try:
        ip = ipaddress.ip_address(host or "")
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


class LoopbackOnly:
    """Owner decision 10: until authentication, authorization, rate limiting and deployment
    security exist, only loopback clients are served, even if the server is (mis)bound to
    a public interface. Every refusal is audited."""

    def __init__(self, app, on_reject=None, public_webhook=None, hosted=False):
        self.app = app
        self.on_reject = on_reject
        self.public_webhook = public_webhook
        self.hosted = hosted

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            client = scope.get("client")
            if self.public_webhook is not None and self.public_webhook.matches(scope):
                if not await self.public_webhook.admit(scope, send):
                    return
                scope["agentpal.public_webhook"] = True
            elif self.hosted and scope.get("agentpal.hosted_remote") \
                    and hosted_remote_allowed(scope):
                pass  # hosted mode: authenticated /v1 + health; all other guards still apply
            elif not is_loopback_peer(client[0] if client else None):
                if self.on_reject is not None:
                    try:
                        self.on_reject("non_loopback_client", scope)
                    except Exception:
                        logging.getLogger("agentpal.api").error("reject_audit_failed")
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                body = json.dumps({"error": "forbidden_non_loopback_client"}).encode()
                await send({"type": "http.response.start", "status": 403,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"content-length", str(len(body)).encode()),
                                        (b"connection", b"close")]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


# ---------------------------------------------------------------- hosted (Render) mode
_HOSTED_REMOTE_EXACT = frozenset({"/health", "/v1/health"})


def hosted_remote_allowed(scope) -> bool:
    """In hosted mode, remote (proxied) clients may reach ONLY the authenticated /v1 API and
    the health checks. Docs, OpenAPI and the legacy /assess API stay loopback-only. The raw
    path must agree with the decoded path, so encoded variants cannot slip through."""
    path = scope.get("path") or ""
    raw = scope.get("raw_path")
    if raw is not None and raw.decode("latin-1") != path:
        return False
    if scope.get("root_path"):
        return False
    from .platform.router import WEBHOOK_PATH
    if path == WEBHOOK_PATH or path.startswith(WEBHOOK_PATH):
        # Review 2026-11-05: the PayPal webhook is reachable ONLY through PublicWebhookIngress
        # (its own opt-in, limits, Host set and 512 KiB cap), never via the generic /v1 rule.
        return False
    return (path in _HOSTED_REMOTE_EXACT or path.startswith("/v1/")
            or dashboard_matches(path))


class RenderFrontDoor:
    """Hosted mode only (AGENTPAL_HOSTED_PROXY=render, refused unless RENDER=true).

    Render's TLS-terminating proxy is the only network path into a web service, so here,
    and only here, its X-Forwarded-Proto / X-Forwarded-For are trusted:
    * exactly one X-Forwarded-Proto, equal to "https", or the request is refused (403);
      Render's own health probe (no forwarding headers) may reach the health paths only;
    * the client address is the RIGHT-MOST X-Forwarded-For entry (the one Render appends;
      earlier entries are client-controlled) and must be a valid IP, else 400;
    * the ASGI scope is rewritten to scheme https and that client, so every downstream
      guard (Host allow-list, auth, rate limits per real client, webhook gate) applies
      unchanged. Websockets are refused."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if (scope.get("path") == "/health" and scope.get("method") in ("GET", "HEAD")
                and not scope.get("query_string")):
            # Liveness for Render's health checker, whatever headers or Host it sends: a
            # fixed body with no data, answered here before any other layer.
            await _send_json(send, 200, {"status": "ok"})
            return
        headers = scope.get("headers") or []
        proto = [v for k, v in headers if k.lower() == b"x-forwarded-proto"]
        forwarded = [v for k, v in headers if k.lower() == b"x-forwarded-for"]
        if not proto and not forwarded and scope.get("path") in _HOSTED_REMOTE_EXACT \
                and scope.get("method") == "GET":
            # Render's internal health probe: plain HTTP, no forwarding headers, liveness only.
            # Its Host may be an internal address; health is read-only, so use a loopback Host.
            headers = [(k, v) for k, v in headers if k.lower() != b"host"] + [(b"host", b"127.0.0.1")]
            scope = dict(scope, client=("127.0.0.1", 0), headers=headers)
            await self.app(scope, receive, send)
            return
        if len(proto) != 1 or proto[0].strip().lower() != b"https":
            await _send_json(send, 403, {"error": "https_required"})
            return
        if len(forwarded) != 1:
            await _send_json(send, 400, {"error": "invalid_forwarded_client"})
            return
        last = forwarded[0].decode("latin-1").split(",")[-1].strip()
        try:
            ip = ipaddress.ip_address(last)  # refuses scoped addresses such as fe80::1%lo
        except ValueError:
            await _send_json(send, 400, {"error": "invalid_forwarded_client"})
            return
        if ip.is_unspecified or ip.is_link_local or ip.is_multicast:
            await _send_json(send, 400, {"error": "invalid_forwarded_client"})
            return
        client = str(ip)
        if is_loopback_peer(client):
            # A forwarded loopback client would unlock loopback-only routes: never accept it.
            await _send_json(send, 400, {"error": "invalid_forwarded_client"})
            return
        scope = dict(scope, scheme="https", client=(client, 0))
        scope["agentpal.hosted_remote"] = True
        await self.app(scope, receive, send)


def _host_name(headers: list[tuple[bytes, bytes]]) -> str | None:
    values = [v for k, v in headers if k.lower() == b"host"]
    if len(values) != 1:
        return None
    host = values[0].decode("latin-1").strip().lower()
    if host.startswith("["):  # [::1]:8000
        return host[1:host.find("]")] if "]" in host else None
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


class _UnavailableLimiter:
    def hit(self, key):
        raise LimiterUnavailable("limiter initialization failed; restart after restoring storage")


def _sqlite_limiter(*args, **kwargs):
    try:
        return SQLiteSlidingWindowLimiter(*args, **kwargs)
    except LimiterUnavailable:
        logging.getLogger("agentpal.api").error("rate_limit_store_unavailable")
        return _UnavailableLimiter()


class PublicWebhookIngress:
    """The one opt-in public exception. Admission always precedes body buffering."""

    def __init__(self, settings: Settings, on_reject):
        from .platform.router import WEBHOOK_PATH
        self.path, self.on_reject = WEBHOOK_PATH, on_reject
        self.hosts = {h.lower() for h in settings.public_webhook_hosts}
        path = settings.rate_limit_db_path or settings.db_path + ".ratelimit.db"
        self.global_limit = _sqlite_limiter(
            path, "webhook-global", settings.webhook_rate_limit_global_per_minute)
        self.peer_limit = _sqlite_limiter(
            path, "webhook-peer", settings.webhook_rate_limit_per_peer_per_minute)

    def matches(self, scope) -> bool:
        return (scope["type"] == "http" and scope.get("method") == "POST"
                and scope.get("path") == self.path and not scope.get("root_path")
                and scope.get("raw_path", self.path.encode()) == self.path.encode()
                and not scope.get("query_string"))

    async def admit(self, scope, send) -> bool:
        if scope.get("scheme") != "https":
            await _send_json(send, 403, {"error": "webhook_https_required"})
            return False
        if _host_name(scope.get("headers") or []) not in self.hosts:
            await _send_json(send, 400, {"error": "forbidden_host"})
            return False
        peer = scope.get("client")
        for limiter, key in ((self.global_limit, "global"),
                             (self.peer_limit, peer[0] if peer else "unknown")):
            try:
                allowed, retry = await run_in_threadpool(limiter.hit, key)
                if not allowed:
                    if limiter.should_audit(key):
                        self.on_reject("rate_limited", scope)
                    await _send_json(send, 429, {"error": "rate_limited"},
                                     [(b"retry-after", str(retry).encode())])
                    return False
            except LimiterUnavailable:
                await _send_json(send, 503, {"error": "rate_limit_unavailable"})
                return False
        return True


_V1_HEADERS = ((b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"))


def _v1_hardened(send):
    """UI-T2 INFO-1: every /v1 response (including errors) is never cached and never
    content-sniffed. Existing values of these two headers are replaced."""
    async def wrapped(message):
        if message.get("type") == "http.response.start":
            names = {n for n, _ in _V1_HEADERS}
            kept = [(k, v) for k, v in message.get("headers", []) if k.lower() not in names]
            message = {**message, "headers": kept + list(_V1_HEADERS)}
        await send(message)
    return wrapped


class SecurityGate:
    """Pure-ASGI gate inside LoopbackOnly and BodySizeLimit (SEC-01..SEC-04):

    1. Host allow-list (DNS-rebinding defence): a browser page on an attacker's domain
       that rebinds to 127.0.0.1 still sends its own Host header and is refused.
    2. Per-peer sliding-window rate limit (every path, including /health).
    3. Bearer-token authentication for every path except /health (and, in development,
       the OpenAPI docs). Missing/invalid tokens -> 401.
    4. Per-token sliding-window rate limit.
    The authenticated Principal is placed in the scope for the route-level authorization
    checks. Every refusal is audited without the token or request content; repeated 429s
    are audited at most once per key and window.

    /v1 (only when a `v1_authenticate` callable is given): steps 1-2 are identical; then
    every /v1 path except `v1_open_paths` needs an agent API key or an owner credential,
    ALWAYS, even in disabled_insecure_dev mode (identity is what /v1 is about). Legacy
    tokens are not accepted on /v1, and /v1 credentials are not accepted on legacy paths.
    The per-credential rate limit uses the same per-token budget."""

    def __init__(self, app, settings: Settings, registry: TokenRegistry, on_reject,
                 clock=None, v1_authenticate=None, v1_open_paths=frozenset(),
                 dashboard=None):
        import time

        self.app = app
        self.settings = settings
        self.registry = registry
        self.on_reject = on_reject
        if settings.rate_limit_backend == "memory":
            self.peer_limiter = SlidingWindowLimiter(settings.rate_limit_per_peer_per_minute,
                                                     60.0, clock or time.monotonic)
            self.token_limiter = SlidingWindowLimiter(settings.rate_limit_per_token_per_minute,
                                                      60.0, clock or time.monotonic)
        else:
            path = settings.rate_limit_db_path or settings.db_path + ".ratelimit.db"
            self.peer_limiter = _sqlite_limiter(
                path, "peer", settings.rate_limit_per_peer_per_minute, clock=clock or time.time)
            self.token_limiter = _sqlite_limiter(
                path, "token", settings.rate_limit_per_token_per_minute, clock=clock or time.time)
        self.allowed_hosts = {h.strip("[]").lower() for h in settings.allowed_hosts}
        self.open_paths = {"/health"}
        self.v1_authenticate = v1_authenticate
        self.v1_open_paths = frozenset(v1_open_paths)
        # Owner dashboard static files (agentpal.dashboard): public, secret-free, GET/HEAD
        # only, exact allow-listed names. Host allow-list and peer rate limit still apply.
        self.dashboard = dashboard
        if settings.environment == "development":
            self.open_paths |= {"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"}

    def _audit(self, reason: str, agent_id: str | None = None) -> None:
        try:
            self.on_reject(reason, agent_id)
        except Exception:
            logging.getLogger("agentpal.api").error("reject_audit_failed")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("agentpal.public_webhook"):
            # The exact route was checked/rate-limited by the outer ingress gate.
            # Bearers are deliberately ignored; the adapter verifies the signature.
            await self.app(scope, receive, send)
            return
        headers = scope.get("headers") or []
        client = scope.get("client")
        peer = client[0] if client else "unknown"
        if peer != _TEST_TRANSPORT_PEER and _host_name(headers) not in self.allowed_hosts:
            self._audit("forbidden_host")
            await _send_json(send, 400, {"error": "forbidden_host"})
            return
        if not await self._limit(self.peer_limiter, f"peer:{peer}", send):
            return
        path = scope.get("path") or ""
        if self.dashboard and dashboard_matches(path):
            await self.dashboard(scope, receive, send)
            return
        if self.v1_authenticate is not None and (path == "/v1" or path.startswith("/v1/")):
            await self._v1(scope, receive, _v1_hardened(send), headers, path)
            return
        if self.settings.auth_disabled:
            scope[PRINCIPAL_KEY] = None
            await self.app(scope, receive, self._mark_dev(send))
            return
        if scope.get("path") in self.open_paths:
            await self.app(scope, receive, send)
            return
        token = bearer_token(headers)
        principal = self.registry.authenticate(token)
        if principal is None:
            error = "authentication_required" if token is None else "invalid_token"
            self._audit(error)
            await _send_json(send, 401, {"error": error},
                             [(b"www-authenticate", b'Bearer realm="agentpal"')])
            return
        if not await self._limit(self.token_limiter, f"token:{principal.token_id}", send,
                                 principal.agent_id):
            return
        scope[PRINCIPAL_KEY] = principal
        await self.app(scope, receive, send)

    async def _v1(self, scope, receive, send, headers, path: str) -> None:
        if path in self.v1_open_paths:
            await self.app(scope, receive, send)
            return
        token = bearer_token(headers)
        # E2E-T4-2: credential lookups hit SQLite (identity/agent stores) and may wait on a
        # lock; never block the event loop (and /health) while they do.
        caller = await run_in_threadpool(self.v1_authenticate, token)
        if caller is None:
            error = "authentication_required" if token is None else "invalid_token"
            self._audit(f"v1_{error}")
            await _send_json(send, 401, {"error": error},
                             [(b"www-authenticate", b'Bearer realm="agentpal"')])
            return
        agent_id = getattr(getattr(caller, "ctx", None), "agent_id", None)
        if not await self._limit(self.token_limiter, caller.rate_key, send, agent_id):
            return
        scope[V1_CALLER_KEY] = caller
        await self.app(scope, receive, send)

    async def _limit(self, limiter: SlidingWindowLimiter, key: str, send,
                     agent_id: str | None = None) -> bool:
        try:
            allowed, retry_after = await run_in_threadpool(limiter.hit, key)
            if allowed:
                return True
            if limiter.should_audit(key):
                self._audit("rate_limited", agent_id)
        except LimiterUnavailable:
            await _send_json(send, 503, {"error": "rate_limit_unavailable"})
            return False
        await _send_json(send, 429, {"error": "rate_limited"},
                         [(b"retry-after", str(retry_after).encode())])
        return False

    @staticmethod
    def _mark_dev(send):
        async def wrapped(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []),
                                                  (b"x-agentpal-auth", b"disabled-insecure-dev")]}
            await send(message)
        return wrapped


class HealthResponse(BaseModel):
    status: str
    version: str
    rules_version: str
    active_backend: str
    model_backend_available: bool
    payment_execution_enabled: bool = False
    authentication: str = "required"
    environment: str = "development"


class BackendInfo(BaseModel):
    backend: str
    model_name: str
    model_version: str
    available: bool
    is_fallback: bool
    unavailable_reason: str = ""
    integrity_required: bool | None = None
    pinned_sha256: str | None = None


class ModelInfoResponse(BaseModel):
    active_backend: str
    backends: list[BackendInfo]
    rules_version: str
    note: str


def create_app(service: RiskService | None = None, settings: Settings | None = None,
               registry: TokenRegistry | None = None, *,
               platform: "PlatformDeps | None" = None) -> FastAPI:
    """`platform`: inject `agentpal.platform.PlatformDeps` to mount the /v1 developer API
    (owner authenticator, wallet store, approval and lifecycle gateways, sandbox provider,
    provider event sink). Without it the app is exactly the legacy risk API."""
    settings = settings or get_settings()
    if (settings.rate_limit_db_path is None and settings.environment == "development"
            and service is not None and service.store.path == ":memory:"):
        # An explicitly injected in-memory development service has no shared deployment.
        settings = settings.model_copy(update={"rate_limit_db_path": ":memory:"})
    if settings.public_webhook_enabled:
        if (platform is None or getattr(platform.provider, "environment", None) != "sandbox"
                or not callable(getattr(platform.provider, "verify_webhook", None))
                or not callable(getattr(platform.event_sink, "apply_provider_event", None))):
            raise UnsafeConfiguration("public webhooks require sandbox verification and event sink")
    configure_logging(settings.log_level)
    log = logging.getLogger("agentpal.api")
    # SEC-04: refuse unsafe combinations before anything is served
    if service is not None:
        spend_configuration = service.spend_policy.configuration
    else:
        from .spending import load_spend_policy
        spend_configuration = load_spend_policy(settings.spend_policy_path).configuration
    for warning in settings.check_runtime(spend_policy_configuration=spend_configuration):
        log.warning("insecure_configuration", extra={"warning": warning})
    registry = registry if registry is not None else TokenRegistry.load(settings.api_tokens_file)

    v1 = None
    if platform is not None:
        from .platform import Platform
        v1 = Platform(platform, lambda: app.state.service)
        if service is not None:
            v1.install(service)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if service is None:
            app.state.service = RiskService.from_settings(settings)
            if v1 is not None:
                v1.install(app.state.service)
            # decision 7: verify (and stage) every model before serving, so integrity work
            # never runs inside a request's model timeout
            log.info("model_warm_up", extra={"backends": app.state.service.warm_up()})
        if v1 is not None:
            from .platform.savers import SaverTicker
            app.state.saver_ticker = SaverTicker(v1.savers, SaverTicker.interval())
            await app.state.saver_ticker.start()
        try:
            yield
        finally:
            if v1 is not None:
                await app.state.saver_ticker.close()

    docs = {} if settings.environment == "development" else {
        "docs_url": None, "redoc_url": None, "openapi_url": None}
    if v1 is None:
        meta = {"title": "AgentPal Risk Intelligence (Phase 1)",
                "description": "Risk assessment for AI-agent payment requests. Assessment "
                               "only: this API does not authorize, execute or process "
                               "payments."}
    else:
        meta = {"title": "AgentPal API",
                "description": V1_DESCRIPTION}
    app = FastAPI(version=__version__, lifespan=lifespan, **meta, **docs)
    app.state.service = service
    app.state.platform = v1
    # Host allow-list, authentication and rate limits. Inside the body-size limit, so an
    # unauthenticated client can make the server buffer at most max_request_bytes.
    app.add_middleware(
        SecurityGate, settings=settings, registry=registry,
        on_reject=lambda reason, agent_id: _audit_rejection(reason, None, agent_id, [
            {"loc": ["request"], "type": reason}]),
        v1_authenticate=v1.authenticate if v1 is not None else None,
        v1_open_paths=_v1_open_paths() if v1 is not None else frozenset(),
        dashboard=Dashboard(load_site()) if v1 is not None else None)
    app.add_middleware(
        BodySizeLimit, max_bytes=settings.max_request_bytes,
        on_reject=lambda reason, scope: _audit_rejection(reason, None, None, [
            {"loc": ["body"], "type": reason}]))
    # added last = outermost: refuse non-loopback peers before anything else runs
    app.add_middleware(
        LoopbackOnly,
        public_webhook=PublicWebhookIngress(settings, lambda reason, scope: _audit_rejection(
            reason, None, None, [{"loc": ["request"], "type": reason}]))
            if settings.public_webhook_enabled else None,
        on_reject=lambda reason, scope: _audit_rejection(reason, None, None, [
            {"loc": ["client"], "type": reason}]),
        hosted=settings.hosted_proxy == "render")
    if settings.hosted_proxy == "render":
        # outermost of all: establish the real client and scheme from Render's proxy
        app.add_middleware(RenderFrontDoor)

    def svc(request: Request) -> RiskService:
        return request.app.state.service

    def _audit_rejection(reason: str, request_id: str | None, agent_id: str | None,
                         details: list[dict]) -> None:
        """Best-effort audit + structured security log; never raises (P0.7c)."""
        log.warning("request_rejected", extra={"reason": reason, "request_id": request_id,
                                               "error_types": [d.get("type") for d in details]})
        service = app.state.service
        try:
            service.store.record_rejection(reason, request_id, agent_id, details)
        except Exception as audit_exc:
            log.error("audit_write_failed", extra={"stage": "rejection",
                                                   "error_type": type(audit_exc).__name__})

    def _safe_id(body: object, key: str) -> str | None:
        value = body.get(key) if isinstance(body, dict) else None
        if isinstance(value, str) and value.startswith(_CREDENTIAL_PREFIXES):
            return None  # a credential pasted into an id field is never persisted
        return value if isinstance(value, str) and _ID_RE.fullmatch(value) else None

    def _safe_loc(part: object) -> object:
        # SEC-12: a location element can be an attacker-chosen key (extra_forbidden);
        # only identifier-like names and list indexes are reflected
        if isinstance(part, int):
            return str(part)
        if isinstance(part, str) and part.startswith(_CREDENTIAL_PREFIXES):
            return "<field>"
        return part if isinstance(part, str) and _LOC_RE.fullmatch(part) else "<field>"

    def _error(status: int, error: str, headers: dict | None = None) -> JSONResponse:
        return JSONResponse(status_code=status, content={"error": error}, headers=headers)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException):
        codes = {404: "not_found", 405: "method_not_allowed"}
        return _error(exc.status_code, codes.get(exc.status_code, "http_error"),
                      getattr(exc, "headers", None))

    if v1 is not None:
        from .platform.errors import PlatformError

        @app.exception_handler(PlatformError)
        async def _platform_error(request: Request, exc: PlatformError):
            if exc.audit:
                caller = request.scope.get(V1_CALLER_KEY)
                agent_id = getattr(getattr(caller, "ctx", None), "agent_id", None)
                _audit_rejection(f"v1_{exc.audit}", None, agent_id,
                                 [{"loc": ["request"], "type": exc.audit}])
            return _error(exc.status, exc.code, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        # Do not echo raw input back (it may contain sensitive data).
        errors = [{"loc": [_safe_loc(p) for p in e.get("loc", [])], "msg": redact_sensitive_text(e.get("msg", "")),
                   "type": e.get("type", "")} for e in exc.errors()[:_MAX_ERRORS]]
        body = getattr(exc, "body", None)
        if request.url.path.startswith("/v1/"):
            # /v1: ids in a rejected body are untrusted; record the authenticated agent only
            caller = request.scope.get(V1_CALLER_KEY)
            request_id = None
            agent_id = getattr(getattr(caller, "ctx", None), "agent_id", None)
        else:
            request_id, agent_id = _safe_id(body, "request_id"), _safe_id(body, "agent_id")
        _audit_rejection("validation", request_id, agent_id,
                         [{"loc": e["loc"], "type": e["type"]} for e in errors])
        return JSONResponse(status_code=422, content={"error": "invalid_request",
                                                      "details": errors})

    @app.exception_handler(RecursionError)
    async def _too_deep(_: Request, exc: RecursionError):
        # E2E-H1-1: absurdly nested JSON exhausts the parser's recursion; it is a client
        # error, not a server fault.
        return JSONResponse(status_code=422, content={"error": "invalid_request",
                                                      "details": [{"type": "json_too_deep"}]})

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception):
        log.error("unhandled_error", extra={"error_type": type(exc).__name__})
        return JSONResponse(status_code=500, content={"error": "internal_error"})

    def _active_backend(s: RiskService) -> tuple[str, bool]:
        for a in s.chain:
            if a.available():
                return a.name, not a.is_fallback
        return "heuristic", False

    @app.get("/health", response_model=HealthResponse, operation_id="legacyHealth",
             openapi_extra={"security": []})
    def health(request: Request) -> HealthResponse:
        s = svc(request)
        backend, model_ok = _active_backend(s)
        return HealthResponse(status="ok", version=__version__,
                              rules_version=s.policy.rules_version, active_backend=backend,
                              model_backend_available=model_ok,
                              authentication=settings.auth_mode,
                              environment=settings.environment)

    legacy = [Depends(_legacy_scheme)]

    @app.get("/model-info", response_model=ModelInfoResponse, dependencies=legacy,
             operation_id="legacyModelInfo")
    def model_info(request: Request) -> ModelInfoResponse:
        s = svc(request)
        backend, _ = _active_backend(s)
        return ModelInfoResponse(
            active_backend=backend,
            backends=[BackendInfo(**b) for b in s.backends_status()],
            rules_version=s.policy.rules_version,
            note="Backends are tried in order; the first available one is used and the "
                 "deterministic heuristic is the final fallback.",
        )

    def _principal(request: Request) -> Principal | None:
        """None only in disabled_insecure_dev mode (SecurityGate refuses everything else
        without a valid token before a route runs)."""
        return request.scope.get(PRINCIPAL_KEY)

    @app.post("/assess", response_model=RiskAssessment, dependencies=legacy,
              operation_id="legacyAssess",
              responses={401: {}, 403: {}, 429: {}})
    def assess_endpoint(payload: PaymentRequest, request: Request):
        principal = _principal(request)
        if principal is not None or not settings.auth_disabled:
            # SEC-02: an agent token authenticates exactly one agent_id. Checked BEFORE the
            # claim, so a caller cannot burn (squat) a request_id for another agent.
            if principal is None or principal.role != "agent":
                _audit_rejection("agent_token_required", payload.request_id, None,
                                 [{"loc": ["authorization"], "type": "agent_token_required"}])
                return _error(403, "agent_token_required")
            if principal.agent_id != payload.agent_id:
                _audit_rejection("agent_mismatch", payload.request_id, principal.agent_id,
                                 [{"loc": ["body", "agent_id"], "type": "agent_mismatch"}])
                return _error(403, "agent_mismatch")
        try:
            reject_credential_input(payload.model_dump(mode="json"))
        except ValueError:
            _audit_rejection("credential_in_metadata", None, None,
                             [{"loc": ["body"], "type": "credential_in_metadata"}])
            return _error(422, "invalid_request")
        return svc(request).assess(payload)

    @app.get("/assessments/{request_id}", response_model=RiskAssessment, dependencies=legacy,
             operation_id="legacyGetAssessment",
             responses={401: {}, 404: {}, 429: {}})
    def get_assessment(
        request_id: Annotated[str, Path(pattern=ID_PATTERN)], request: Request
    ):
        principal = _principal(request)
        agent_id = principal.agent_id if principal is not None and principal.role == "agent" else None
        found = svc(request).store.get_assessment(request_id, agent_id=agent_id)
        # Preserve the indistinguishable 404 and the existing cross-agent refusal audit.
        if found is None and agent_id is not None:
            other = svc(request).store.get_assessment(request_id)
            if other is not None and other.agent_id != agent_id:
                _audit_rejection("cross_agent_read", request_id, agent_id,
                                 [{"loc": ["path", "request_id"], "type": "cross_agent_read"}])
        if found is not None and (principal is not None or not settings.auth_disabled):
            # SEC-01 (BOLA): only the owning agent or an operator may read it. Another
            # agent gets the same 404 as for an unknown id (no existence oracle).
            if principal is None or not principal.may_read(found.agent_id):
                _audit_rejection("cross_agent_read", request_id,
                                 principal.agent_id if principal else None,
                                 [{"loc": ["path", "request_id"], "type": "cross_agent_read"}])
                found = None
        if found is None:
            return _error(404, "not_found")
        return found

    if v1 is not None:
        from .platform.router import build_router
        # These routes already carry their /v1 prefix and all their dependencies.
        # Keep concrete APIRoutes visible to route-table auditing on FastAPI versions
        # that otherwise retain included routers as deferred wrapper objects.
        app.router.routes.extend(build_router().routes)

    return app


def _v1_open_paths() -> frozenset[str]:
    from .platform.router import OPEN_PATHS
    return OPEN_PATHS
