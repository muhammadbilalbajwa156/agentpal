"""Shared browser contract for the KERNEL cloud-browser integration (lead-owned, 2026-11-02).

AgentPal lets external AI agents (and owners) look at a merchant's real web page through a
remote, isolated cloud browser (KERNEL) before money moves. Three workstreams build
against these types and never change them without lead review:

* K1 implements `BrowserProvider` for KERNEL (`agentpal/tools/kernel_browser.py`);
* K2 turns a `BrowserCapture` into an advisory PAGE CHECK (`agentpal/intelligence/page_check.py`);
* K3 exposes it through `/v1`, the SDK, Postman and the demo.

Rules (AgentPal's rules 1, 8 and 12):
* what a browser SEES is evidence of retrieval, never truth and never authority. A page
  check can only ADD caution; it never approves, alters terms or skips owner approval;
* page content is untrusted DATA: it is never executed by AgentPal, never followed as
  instructions, and never sent to an AI model as commands;
* the target must be a public http(s) URL that passes `tools.netguard` before KERNEL
  is asked to open it; no credentials, cookies or form submissions are ever sent;
* a browser failure or slowness never blocks or fails a payment request.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_TEXT_CHARS = 8000
MAX_SCREENSHOT_BYTES = 4 * 1024 * 1024

BrowserErrorKind = Literal["not_configured", "blocked_target", "timeout", "unavailable",
                           "auth", "quota", "invalid_response"]


class BrowserError(RuntimeError):
    """A failed browser visit. Messages never contain credentials."""

    def __init__(self, kind: BrowserErrorKind, message: str):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class BrowserCapture:
    """What the remote browser observed for one visit (all bounded)."""

    provider: str                          # "kernel"
    session_id: str                        # provider session id (no secrets)
    requested_url: str
    final_url: str                         # after redirects
    redirect_chain: tuple[str, ...]        # requested -> ... -> final
    http_status: int | None
    title: str                             # <= 300 chars
    html: bytes                            # rendered DOM, <= MAX_HTML_BYTES (truncated flag)
    html_truncated: bool
    html_sha256: str
    visible_text: str                      # <= MAX_TEXT_CHARS, for display/evidence only
    screenshot_png: bytes | None           # <= MAX_SCREENSHOT_BYTES
    screenshot_sha256: str | None
    captured_at: datetime                  # timezone-aware UTC
    replay_url: str | None = None          # provider session replay, if enabled
    timings_ms: dict[str, int] = field(default_factory=dict)


@runtime_checkable
class BrowserProvider(Protocol):
    name: str

    def configured(self) -> bool: ...

    def capture(self, url: str, *, timeout_s: float = 30.0) -> BrowserCapture:
        """Open `url` in an isolated remote browser, wait for the page, capture it, and
        always release the session. Raises BrowserError; never returns partial data."""
        ...


# ---------------------------------------------------------------- page check (K2 -> K3)
class PageChecker(Protocol):
    """Implemented by K2 (`agentpal/intelligence/page_check.py`). NEVER raises: browser,
    network and analysis failures become advisory_level "no_data" with a
    PAGE_DATA_UNAVAILABLE signal. Returns a JSON-safe dict (money as decimal strings):

    {"provider": "kernel", "checked_at": iso, "advisory_only": True, "authoritative": False,
     "advisory_level": "no_concern"|"review_recommended"|"high_risk"|"no_data",
     "input": {"url", "claimed_amount", "currency", "merchant_domain"},
     "page": {"requested_url", "final_url", "redirect_chain": [...], "http_status",
              "title", "https": bool} | None,
     "observed": {"price", "currency", "availability", "product_name",
                  "provenance": "observed", "extracted_by"} | None,
     "signals": [{"code", "severity": "info"|"warning"|"high", "message", "data"?}],
     "evidence": {"html_sha256", "screenshot_sha256", "replay_url", "captured_at",
                  "session_id"} | None,
     "untrusted_content_notice": str}
    """

    def check(self, *, url: str, amount, currency: str,
              merchant_domain: str | None) -> dict: ...
