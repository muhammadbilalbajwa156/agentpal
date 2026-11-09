"""Bounded, read-only Kernel REST browser evidence; never financial authority.

Kernel is optional at import time. No browser is allocated until netguard accepts
the target. See docs/KERNEL_BROWSER.md for the remote-DNS/egress trust boundary.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import queue
import re
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote

import httpx

from .browser_contracts import (
    MAX_HTML_BYTES, MAX_SCREENSHOT_BYTES, MAX_TEXT_CHARS,
    BrowserCapture, BrowserError, BrowserErrorKind,
)
from .netguard import MAX_URL_LENGTH, BlockedTarget, Resolver, system_resolver, vet_url

# The ONLY variable part of the program is a JSON data declaration preceding this
# constant. Never interpolate a URL, page text, or caller-provided code into JS.
CAPTURE_JS = r"""const readContext = await browser.newContext({serviceWorkers: "block", acceptDownloads: false});
const readPage = await readContext.newPage();
await readPage.bringToFront();
const {default: dns} = await import("node:dns/promises");
const {BlockList, isIP} = await import("node:net");
const denied = new BlockList();
for (const [ip, bits] of [
  ["0.0.0.0",8],["10.0.0.0",8],["100.64.0.0",10],["127.0.0.0",8],
  ["169.254.0.0",16],["172.16.0.0",12],["192.0.0.0",24],["192.0.2.0",24],
  ["192.168.0.0",16],["198.18.0.0",15],["198.51.100.0",24],
  ["203.0.113.0",24],["224.0.0.0",4],["240.0.0.0",4]
]) denied.addSubnet(ip, bits, "ipv4");
for (const [ip, bits] of [["2001::",23],["2001:db8::",32],["2002::",16],["3fff::",20]])
  denied.addSubnet(ip, bits, "ipv6");
const globalV6 = new BlockList();
globalV6.addSubnet("2000::", 3, "ipv6");
const end = Date.now() + input.timeout_ms;
const remaining = () => {
  const left = end - Date.now();
  if (left <= 0) { const e = new Error(); e.name = "TimeoutError"; throw e; }
  return left;
};
async function publicURL(raw) {
  if (raw.length > 2048 || /[\\\x00-\x20\x7f]/.test(raw)) return false;
  let u;
  try { u = new URL(raw); } catch { return false; }
  if (!["http:","https:"].includes(u.protocol) || u.username || u.password || u.port)
    return false;
  const host = u.hostname.replace(/^\[|\]$/g, "").replace(/\.$/, "").toLowerCase();
  if (!isIP(host) && (!host.includes(".") || /(^|\.)(localhost|local|internal|lan|corp|intranet|localdomain)$/.test(host)
      || host.endsWith(".home.arpa"))) return false;
  const addresses = isIP(host) ? [{address: host}] : await dns.lookup(host, {all: true});
  if (!addresses.length) return false;
  return addresses.every(({address}) => {
    const family = isIP(address);
    return family === 4 ? !denied.check(address, "ipv4") :
      family === 6 && globalV6.check(address, "ipv6") && !denied.check(address, "ipv6");
  });
}
let redirect = null;
let blocked = false;
let routeFailure = null;
// Fetch with maxRedirects:0 is essential: Playwright routing alone does NOT
// intercept redirected requests. A redirect is followed by a NEW vetted goto.
await readContext.route("**/*", async route => {
  const request = route.request();
  try {
    remaining();
    if (request.method() !== "GET" || !await publicURL(request.url())) {
      if (request.isNavigationRequest()) blocked = true;
      return await route.abort();
    }
    const headers = {...request.headers()};
    for (const key of Object.keys(headers))
      if (["cookie","authorization","proxy-authorization"].includes(key.toLowerCase())) delete headers[key];
    // An explicit empty Cookie prevents APIRequestContext adding its cookie jar.
    headers.cookie = "";
    const response = await route.fetch({maxRedirects: 0, timeout: remaining(), headers});
    const responseHeaders = {...response.headers()};
    for (const key of Object.keys(responseHeaders))
      if (key.toLowerCase() === "set-cookie") delete responseHeaders[key];
    const location = responseHeaders.location;
    if (response.status() >= 300 && response.status() < 400 && location) {
      if (request.isNavigationRequest() && request.frame() === readPage.mainFrame()) {
        redirect = new URL(location, request.url()).href;
        // Never send a 3xx to Chromium, where it would bypass the route guard.
        return await route.fulfill({status: 200, contentType: "text/html", body: ""});
      }
      return await route.abort();
    }
    return await route.fulfill({response, headers: responseHeaders});
  } catch (error) {
    routeFailure = error && error.name === "TimeoutError" ? "timeout" : "unavailable";
    return await route.abort();
  }
});
await readContext.routeWebSocket("**/*", socket => socket.close());
try {
  const chain = [];
  let target = input.url;
  let response;
  for (let hop = 0; ; hop++) {
    if (hop >= 20) return {failure: "invalid_response"};
    if (!await publicURL(target)) return {failure: "blocked_target"};
    redirect = null;
    chain.push(target);
    response = await readPage.goto(target, {waitUntil: "domcontentloaded", timeout: remaining()});
    if (blocked) return {failure: "blocked_target"};
    if (routeFailure) return {failure: routeFailure};
    if (!redirect) break;
    target = redirect;
  }
  const final_url = readPage.url();
  if (chain[chain.length - 1] !== final_url) chain.push(final_url);
  const data = await readPage.evaluate(() => {
    const raw = document.documentElement ? document.documentElement.outerHTML : "";
    const bytes = new TextEncoder().encode(raw);
    // Streaming decode drops an incomplete trailing UTF-8 codepoint.
    const html = new TextDecoder().decode(bytes.subarray(0, 2097152), {stream: true});
    return {
      html, html_truncated: bytes.length > 2097152,
      visible_text: (document.body ? document.body.innerText : "").slice(0, 8000),
      title: document.title.slice(0, 300)
    };
  });
  if (blocked) return {failure: "blocked_target"};
  if (routeFailure) return {failure: routeFailure};
  return {...data, final_url, redirect_chain: chain,
          http_status: response ? response.status() : null};
} catch (error) {
  return {failure: blocked ? "blocked_target" : routeFailure ||
    (error && error.name === "TimeoutError" ? "timeout" : "unavailable")};
}
"""

_PROCESS_SLOTS = threading.BoundedSemaphore(2)
_IO = threading.local()
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_PNG = b"\x89PNG\r\n\x1a\n"
# JSON can escape each HTML byte as six ASCII bytes, plus envelope/text overhead.
_MAX_JSON_BYTES = 6 * MAX_HTML_BYTES + 256 * 1024
_MAX_CREATE_BYTES = 64 * 1024


class _QuietBrowserIO(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # SDK debug output includes JWT URLs, response headers and tracebacks.
        # Affect only this provider's worker, never other requests/threads.
        return not getattr(_IO, "active", False)


_QUIET = _QuietBrowserIO()


def _install_log_filters() -> None:
    for name in ("kernel", "kernel._base_client", "kernel._response", "httpx",
                 "httpcore.connection", "httpcore.http11", "httpcore.http2",
                 "httpcore.proxy", "httpcore.socks"):
        logger = logging.getLogger(name)
        if _QUIET not in logger.filters:
            logger.addFilter(_QUIET)


def _error(kind: BrowserErrorKind) -> BrowserError:
    # No input, upstream message, URL, credential, or exception chain is reflected.
    return BrowserError(kind, f"Kernel browser capture: {kind}")


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise _error("timeout")
    return left


def _status_kind(status: int) -> BrowserErrorKind:
    if status in (401, 403):
        return "auth"
    if status in (402, 429):
        return "quota"
    return "unavailable"


def _map_error(exc: Exception) -> BrowserError:
    # The SDK wraps exceptions raised by HTTP hooks. Walk types, never messages.
    current: BaseException | None = exc
    for _ in range(8):
        if isinstance(current, BrowserError):
            return _error(current.kind)
        if isinstance(current, (TimeoutError, httpx.TimeoutException)):
            return _error("timeout")
        status = getattr(current, "status_code", None)
        if isinstance(status, int):
            return _error(_status_kind(status))
        current = current.__cause__ if current is not None else None
        if current is None:
            break
    if type(exc).__name__ == "APITimeoutError":
        return _error("timeout")
    if isinstance(exc, (ValueError, TypeError, AttributeError)):
        return _error("invalid_response")
    return _error("unavailable")


def _bounded_response(response: httpx.Response) -> None:
    """SDK hook: cap decoded bodies BEFORE SDK parsing, including error bodies."""
    if not getattr(_IO, "active", False):
        return
    try:
        # A late create response must still be decoded so its OWN session can
        # be deleted. Caller deadline is enforced independently by capture().
        read_deadline = (max(_IO.deadline, time.monotonic() + 2.0)
                         if getattr(_IO, "creating", False) else _IO.deadline)
        _remaining(read_deadline)
        if not 200 <= response.status_code < 300:
            # Do not download errors, including redirects, and do not let httpx
            # follow a redirect carrying an Authorization header or session JWT.
            raise _error(_status_kind(response.status_code))
        path = response.request.url.path
        cap = (MAX_SCREENSHOT_BYTES if path.endswith("/screenshot") else
               _MAX_JSON_BYTES if path.endswith("/execute") else _MAX_CREATE_BYTES)
        parts, size = [], 0
        for part in response.iter_bytes():
            _remaining(read_deadline)
            size += len(part)
            if size > cap:
                raise _error("invalid_response")
            parts.append(part)
        # httpx response hooks support reading before SDK parsing. Its public
        # read() has no size cap, so cache our bounded decoded read in the same
        # slot read() uses. This small httpx integration is covered by SDK tests.
        response._content = b"".join(parts)
    finally:
        response.close()


class KernelBrowserProvider:
    name = "kernel"

    def __init__(self, api_key: str | None = None, *, client: Any = None,
                 http_client: httpx.Client | None = None,
                 resolver: Resolver = system_resolver,
                 max_concurrent_sessions: int = 2):
        if type(max_concurrent_sessions) is not int or not 1 <= max_concurrent_sessions <= 2:
            raise ValueError("max_concurrent_sessions must be 1 or 2")
        if client is not None and http_client is not None:
            raise ValueError("supply client or http_client")
        key = api_key if api_key is not None else os.environ.get("KERNEL_API_KEY", "")
        self._key = key.strip()
        if client is not None and not self._key:
            self._key = getattr(client, "api_key", "") or ""
        self._client = client
        self._http_client = http_client
        self._resolver = resolver
        self._slots = threading.BoundedSemaphore(max_concurrent_sessions)
        self._client_lock = threading.Lock()
        _install_log_filters()

    def __repr__(self) -> str:
        return f"KernelBrowserProvider(configured={self.configured()})"

    def configured(self) -> bool:
        return self._client is not None or bool(self._key)

    def _sdk(self):
        with self._client_lock:
            try:
                from kernel import Kernel
            except ImportError:
                raise _error("not_configured") from None
            if self._client is None:
                self._client = Kernel(api_key=self._key, max_retries=0,
                                      base_url="https://api.onkernel.com",
                                      http_client=self._http_client)
            if isinstance(self._client, Kernel):
                # Copy retains injected transport and routing cache; no SDK
                # retry may extend the deadline or allocate another session.
                sdk = self._client.with_options(max_retries=0)
                hooks = sdk._client.event_hooks["response"]
                if _bounded_response not in hooks:
                    hooks.insert(0, _bounded_response)
                return sdk
            return self._client  # trusted injectable SDK-shaped test double

    def _contains_key(self, value: str | bytes) -> bool:
        if not self._key:
            return False
        return (self._key.encode() in value if isinstance(value, bytes) else
                self._key in unquote(value))

    def _vet(self, url: str) -> None:
        # Reject WHATWG/httpx parser differences and control normalization.
        if (not isinstance(url, str) or len(url) > MAX_URL_LENGTH or "\\" in url
                or any(ord(c) < 32 or ord(c) == 127 for c in url)
                or self._contains_key(url)):
            raise _error("blocked_target")
        try:
            vet_url(url, resolver=self._resolver, allow_http=True)
        except (BlockedTarget, ValueError, TypeError):
            raise _error("blocked_target") from None

    def capture(self, url: str, *, timeout_s: float = 30.0) -> BrowserCapture:
        start = time.monotonic()
        try:
            valid_timeout = (type(timeout_s) in (int, float)
                             and math.isfinite(timeout_s) and timeout_s > 0)
        except OverflowError:
            valid_timeout = False
        if not valid_timeout:
            raise _error("timeout")
        # Operator-facing upper bound; callers may only request less time.
        deadline = start + min(timeout_s, 60.0)
        if not self.configured():
            raise _error("not_configured")
        if not self._slots.acquire(timeout=_remaining(deadline)):
            raise _error("timeout")
        try:
            if not _PROCESS_SLOTS.acquire(timeout=_remaining(deadline)):
                raise _error("timeout")
        except BaseException:
            self._slots.release()
            raise
        result: queue.Queue[BrowserCapture | BrowserError] = queue.Queue(maxsize=1)

        def work() -> None:
            _IO.active, _IO.deadline = True, deadline
            try:
                try:
                    value = self._capture(url, start, deadline)
                except Exception as exc:
                    value = _map_error(exc)
                result.put(value)
            finally:
                _IO.active = False
                _PROCESS_SLOTS.release()
                self._slots.release()

        try:
            threading.Thread(target=work, name="kernel-capture", daemon=True).start()
        except BaseException:
            _PROCESS_SLOTS.release()
            self._slots.release()
            raise
        try:
            value = result.get(timeout=_remaining(deadline))
        except queue.Empty:
            raise _error("timeout") from None
        if isinstance(value, BrowserError):
            raise value
        _remaining(deadline)
        return value

    def _capture(self, url: str, start: float, deadline: float) -> BrowserCapture:
        self._vet(url)
        _remaining(deadline)
        sdk = self._sdk()
        session_id = None
        try:
            _IO.creating = True
            try:
                session = sdk.browsers.create(timeout_seconds=30, timeout=_remaining(deadline))
            finally:
                _IO.creating = False
            candidate = getattr(session, "session_id", None)
            if (not isinstance(candidate, str) or not _SESSION_ID.fullmatch(candidate)
                    or self._contains_key(candidate)):
                raise _error("invalid_response")
            session_id = candidate
            left = _remaining(deadline)
            code = "const input = " + json.dumps({
                "url": url, "timeout_ms": max(1, min(10000, int(left * 1000)))
            }, ensure_ascii=True) + ";\n" + CAPTURE_JS
            execution = sdk.browsers.playwright.execute(
                session_id, code=code, timeout_sec=max(1, math.ceil(left)), timeout=left)
            data = self._parse(execution, url)
            _remaining(deadline)
            screenshot = sdk.browsers.computer.capture_screenshot(
                session_id, timeout=_remaining(deadline))
            image = screenshot if isinstance(screenshot, bytes) else screenshot.read()
            if (not isinstance(image, bytes) or not image.startswith(_PNG)
                    or len(image) > MAX_SCREENSHOT_BYTES or self._contains_key(image)):
                raise _error("invalid_response")
            _remaining(deadline)
            return BrowserCapture(
                provider=self.name, session_id=session_id, requested_url=url,
                **data, screenshot_png=image, screenshot_sha256=hashlib.sha256(image).hexdigest(),
                captured_at=datetime.now(timezone.utc), replay_url=None,
                timings_ms={"capture": int((time.monotonic() - start) * 1000)})
        finally:
            if session_id is not None:
                # Cleanup is owned by this worker even if its caller timed out.
                # It keeps its credit slot until this single delete attempt ends.
                _IO.deadline = max(deadline, time.monotonic() + 2.0)
                had_failure = sys.exc_info()[0] is not None
                try:
                    sdk.browsers.delete_by_id(session_id, timeout=min(2.0, _remaining(_IO.deadline)))
                except Exception:
                    # Preserve the original sanitized classification. A cleanup
                    # failure after an otherwise successful visit still fails it.
                    if not had_failure:
                        raise

    def _parse(self, execution: Any, requested_url: str) -> dict:
        success = getattr(execution, "success", None)
        if success is not True:
            message = getattr(execution, "error", None)
            if (success is False and isinstance(message, str)
                    and (message.startswith("TimeoutError:") or "timed out" in message.lower())):
                raise _error("timeout")
            raise _error("invalid_response")
        data = getattr(execution, "result", None)
        if not isinstance(data, dict):
            raise _error("invalid_response")
        if data.get("failure") in ("timeout", "unavailable", "invalid_response", "blocked_target"):
            raise _error(data["failure"])
        for field in ("final_url", "title", "html", "visible_text"):
            if not isinstance(data.get(field), str) or self._contains_key(data[field]):
                raise _error("invalid_response")
        status, chain = data.get("http_status"), data.get("redirect_chain")
        if (status is not None and (type(status) is not int or not 100 <= status <= 599)
                or type(data.get("html_truncated")) is not bool
                or not isinstance(chain, list) or not 1 <= len(chain) <= 21):
            raise _error("invalid_response")
        for target in [*chain, data["final_url"]]:
            try:
                self._vet(target)
            except BrowserError:
                raise _error("invalid_response") from None
        if chain[-1] != data["final_url"]:
            raise _error("invalid_response")
        if chain[0] != requested_url:
            chain = [requested_url, *chain]
        raw_html = data["html"].encode("utf-8")
        html = raw_html[:MAX_HTML_BYTES]
        return dict(final_url=data["final_url"], redirect_chain=tuple(chain), http_status=status,
                    title=data["title"][:300], html=html,
                    html_truncated=data["html_truncated"] or len(raw_html) > MAX_HTML_BYTES,
                    html_sha256=hashlib.sha256(html).hexdigest(),
                    visible_text=data["visible_text"][:MAX_TEXT_CHARS])
