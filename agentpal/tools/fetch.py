"""Sandboxed HTTP fetch tool (`research.fetch`).

Sandbox:
* capability check through the injected authority (never `ctx.capabilities`);
* https only (http only with `allow_http=True`); default ports only; no userinfo;
* SSRF guard (`netguard.vet_url`) before the first request AND for every redirect hop;
* IP PINNING: the request is sent to one of the vetted addresses, with the original
  `Host` header and TLS SNI/certificate name (`sni_hostname` extension), so a second DNS
  answer (DNS rebinding) cannot redirect the connection to a private address;
* redirects followed manually, at most `max_redirects`;
* per-operation timeout plus an overall deadline; at most `max_bytes` of (decoded) body;
* no cookies (a fresh client per hop, so nothing set by one hop is sent to the next), no
  credentials, no proxy or environment configuration (`trust_env=False`);
* optional per-owner origin allow-list, checked on every hop.

Output: a `ToolResult` with one `SourceEvidence` (original URL, final URL, UTC retrieval
time, HTTP status, SHA-256 of the body). The body itself goes to the `EvidenceStore` under
that SHA-256. The tool emits no facts: extraction is the intelligence engine's job, through
the `contracts.Extractor` protocol. Retrieved content is untrusted data, never instructions.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime

import httpx

from ..contracts import AgentContext, Capability, SourceEvidence, ToolError, ToolResult
from .base import PermissionAuthority, failed, gate, utc_now
from .evidence_store import EvidenceError, EvidenceStore
from .netguard import BlockedTarget, Resolver, VettedTarget, system_resolver, vet_url

OriginAllowlist = Callable[[str], "frozenset[str] | set[str] | None"]
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
USER_AGENT = "AgentPal-Research/0.1 (sandboxed fetch; no cookies)"


class _Abort(Exception):
    def __init__(self, error: ToolError, detail: str):
        super().__init__(detail)
        self.error, self.detail = error, detail


class HttpFetchTool:
    name = "http_fetch"
    required_capability = Capability.research_fetch

    def __init__(self, checker: PermissionAuthority, evidence_store: EvidenceStore, *,
                 transport: httpx.BaseTransport | None = None,
                 resolver: Resolver = system_resolver, allow_http: bool = False,
                 max_redirects: int = 5, timeout_s: float = 10.0,
                 max_bytes: int = 2_000_000,
                 origin_allowlist: OriginAllowlist | None = None,
                 clock: Callable[[], datetime] = utc_now,
                 monotonic: Callable[[], float] = time.monotonic):
        self.checker = checker
        self.evidence_store = evidence_store
        self.transport = transport
        self.resolver = resolver
        self.allow_http = allow_http
        self.max_redirects = max_redirects
        self.timeout_s = timeout_s
        self.max_bytes = max_bytes
        self.origin_allowlist = origin_allowlist
        self.clock = clock
        self.monotonic = monotonic

    # contracts.Tool
    def run(self, ctx: AgentContext, **params: object) -> ToolResult:
        started = self.clock()
        denied = gate(self.name, self.required_capability, ctx, self.checker, started,
                      self.clock)
        if denied:
            return denied
        url = params.get("url")
        if not isinstance(url, str):
            return failed(self.name, ToolError.parse_error, "url (string) required", started,
                          self.clock)
        try:
            status, final_url, body = self._fetch(ctx, url)
        except _Abort as exc:
            return failed(self.name, exc.error, exc.detail, started, self.clock)
        except BlockedTarget as exc:
            return failed(self.name, ToolError.blocked_target, exc.reason, started,
                          self.clock)
        except httpx.TimeoutException:
            return failed(self.name, ToolError.timeout, "request timed out", started,
                          self.clock)
        except httpx.HTTPError as exc:
            return failed(self.name, ToolError.http_error,
                          f"transport error ({type(exc).__name__})", started, self.clock)
        if status >= 400:
            return failed(self.name, ToolError.http_error, f"HTTP {status}", started,
                          self.clock)
        try:
            sha = self.evidence_store.put(body)
        except EvidenceError as exc:
            return failed(self.name, ToolError.internal, f"evidence store: {exc}", started,
                          self.clock)
        evidence = SourceEvidence(url=url, retrieved_at=self.clock(), content_sha256=sha,
                                  http_status=status, method="http_fetch",
                                  final_url=final_url)
        return ToolResult(tool=self.name, ok=True, evidence=[evidence], started_at=started,
                          finished_at=self.clock())

    # ------------------------------------------------------------------ internals
    def _allowed_origin(self, ctx: AgentContext, target: VettedTarget) -> None:
        if self.origin_allowlist is None:
            return
        allowed = self.origin_allowlist(ctx.owner_id)
        if allowed is not None and target.origin not in allowed:
            raise _Abort(ToolError.not_permitted, f"origin {target.origin} not allow-listed")

    def _fetch(self, ctx: AgentContext, url: str) -> tuple[int, str, bytes]:
        deadline = self.monotonic() + self.timeout_s
        current = url
        for _hop in range(self.max_redirects + 1):
            target = vet_url(current, resolver=self.resolver, allow_http=self.allow_http)
            self._allowed_origin(ctx, target)
            status, location, body = self._one_hop(target, deadline)
            if status in REDIRECT_STATUSES and location is not None:
                current = str(target.url.join(location))
                continue
            return status, str(target.url), body
        raise _Abort(ToolError.http_error, f"more than {self.max_redirects} redirects")

    def _one_hop(self, target: VettedTarget, deadline: float
                 ) -> tuple[int, str | None, bytes]:
        pinned = target.url.copy_with(host=target.addresses[0])
        headers = {"Host": target.host, "User-Agent": USER_AGENT,
                   "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,"
                             "*/*;q=0.5"}
        extensions = {"sni_hostname": target.host} if target.scheme == "https" else {}
        remaining = deadline - self.monotonic()
        if remaining <= 0:
            raise _Abort(ToolError.timeout, "overall deadline exceeded")
        timeout = httpx.Timeout(min(self.timeout_s, remaining))
        # fresh client per hop: no cookie jar carries over, no env proxies or .netrc
        with httpx.Client(transport=self.transport, follow_redirects=False,
                          trust_env=False, timeout=timeout) as client:
            request = client.build_request("GET", pinned, headers=headers,
                                           extensions=extensions)
            response = client.send(request, stream=True)
            try:
                if response.status_code in REDIRECT_STATUSES:
                    return response.status_code, response.headers.get("location"), b""
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > self.max_bytes:
                    raise _Abort(ToolError.too_large, f"body larger than {self.max_bytes}")
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise _Abort(ToolError.too_large,
                                     f"body larger than {self.max_bytes}")
                    if self.monotonic() > deadline:
                        raise _Abort(ToolError.timeout, "overall deadline exceeded")
                    chunks.append(chunk)
                return response.status_code, None, b"".join(chunks)
            finally:
                response.close()
