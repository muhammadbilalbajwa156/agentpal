"""Read-only KERNEL browser evidence through the real /v1 payment platform.

python -m agentpal.demo.browser_check --end-to-end --offline
python -m agentpal.demo.browser_check --end-to-end --url https://merchant.example/product

Offline mode is a synthetic fixture, never a claim of a live merchant visit.
Live mode requires KERNEL_API_KEY, the integrated K1/K2 components and reachable
Kernel control hosts. No payment is approved or executed by this demo.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from ..api import create_app
from ..auth import TokenRegistry
from ..config import Settings
from ..platform.models import UNTRUSTED_CONTENT_NOTICE
from ..platform.wiring import _kernel_tools, build_platform
from ..tools.browser_contracts import BrowserCapture, BrowserProvider, PageChecker

# Public IP literals keep this synthetic fixture entirely offline, including DNS.
FIXTURE_URL = "https://93.184.216.34/office-mouse"
FIXTURE_FINAL = "https://1.1.1.1/another-shop/office-mouse"
FIXTURE_HTML = b'''<html><head><title>OFFLINE office mouse fixture</title>
<script type="application/ld+json">{"@type":"Product","name":"Office mouse",
"offers":{"@type":"Offer","price":"99.99","priceCurrency":"USD",
"availability":"https://schema.org/InStock"}}</script></head>
<body>OFFLINE synthetic fixture: Office mouse 99.99 USD</body></html>'''
FIXTURE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")


class OfflineBrowser:
    """Synthetic bounded capture; never opens a socket or creates a Kernel session."""
    name = "kernel"

    def configured(self) -> bool:
        return True

    def capture(self, url: str, *, timeout_s: float = 30) -> BrowserCapture:
        if url != FIXTURE_URL:
            raise ValueError("offline fixture supports only its fixture URL")
        return BrowserCapture(provider="kernel", session_id="OFFLINE_fixture", requested_url=url,
            final_url=FIXTURE_FINAL, redirect_chain=(url, FIXTURE_FINAL), http_status=200,
            title="OFFLINE office mouse fixture", html=FIXTURE_HTML, html_truncated=False,
            html_sha256=hashlib.sha256(FIXTURE_HTML).hexdigest(),
            visible_text="OFFLINE fixture: Office mouse 99.99 USD", screenshot_png=FIXTURE_PNG,
            screenshot_sha256=hashlib.sha256(FIXTURE_PNG).hexdigest(),
            captured_at=datetime(2026, 11, 2, tzinfo=timezone.utc))


class OfflinePageChecker:
    """Precomputed fixture analysis implementing PageChecker; not a production checker."""
    def check(self, *, url: str, amount, currency: str, merchant_domain: str | None) -> dict:
        shot = OfflineBrowser().capture(url)
        return {"provider": "kernel", "checked_at": shot.captured_at.isoformat(),
            "advisory_only": True, "authoritative": False, "advisory_level": "high_risk",
            "input": {"url": url, "claimed_amount": str(amount), "currency": currency,
                      "merchant_domain": merchant_domain},
            "page": {"requested_url": url, "final_url": shot.final_url,
                     "redirect_chain": list(shot.redirect_chain), "http_status": 200,
                     "title": shot.title, "https": True},
            "observed": {"price": "99.99", "currency": "USD", "availability": "InStock",
                         "product_name": "Office mouse", "provenance": "observed",
                         "extracted_by": "OFFLINE_fixture"},
            "signals": [{"code": "PAGE_PRICE_MISMATCH", "severity": "high",
                         "message": "OFFLINE: claimed 250.00 USD; page shows 99.99 USD."},
                        {"code": "PAGE_REDIRECT_OFF_MERCHANT", "severity": "high",
                         "message": "OFFLINE: page redirected off the requested merchant."}],
            "evidence": {"html_sha256": shot.html_sha256,
                         "screenshot_sha256": shot.screenshot_sha256, "replay_url": None,
                         "session_id": shot.session_id, "captured_at": shot.captured_at.isoformat()},
            "untrusted_content_notice": "OFFLINE synthetic fixture. " + UNTRUSTED_CONTENT_NOTICE}


async def end_to_end(browser: BrowserProvider, checker: PageChecker, url: str) -> dict:
    with TemporaryDirectory(prefix="agentpal-kernel-demo-") as directory:
        settings = Settings(db_path=str(Path(directory) / "risk.db"), auth_mode="required",
                            analyzer_chain=["heuristic"], allowed_hosts=["127.0.0.1"],
                            log_level="ERROR")
        deps = build_platform(settings=settings, environ={})
        deps.browser, deps.page_checker = browser, checker
        try:
            identity = deps.owner_authenticator
            identity.create_owner("browser_demo_owner")
            token, _ = identity.issue_token("browser_demo_owner")
            owner_h = {"Authorization": "Bearer " + token}
            app = create_app(service=deps.risk_service, settings=settings,
                             registry=TokenRegistry(), platform=deps)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                         base_url="http://127.0.0.1", timeout=50) as client:
                async def call(method, path, headers=owner_h, expected=200, **kw):
                    response = await client.request(method, path, headers=headers, **kw)
                    if response.status_code != expected:
                        raise RuntimeError("demo API request failed")  # no payload/credential echo
                    return response.json()
                registered = await call("POST", "/v1/agents", expected=201, json={
                    "name": "Merchant research agent", "capabilities": ["wallet.read",
                        "payment.prepare", "owner.request_decision", "research.fetch"]})
                agent_h = {"Authorization": "Bearer " + registered["api_key"]}
                await call("POST", "/v1/wallets", expected=201, json={
                    "agent_id": registered["agent"]["agent_id"], "currency": "USD",
                    "monthly_budget": "1000.00", "per_transaction_limit": "500.00"})
                payment = await call("POST", "/v1/payment-requests", expected=201,
                    headers={**agent_h, "Idempotency-Key": "browser-demo-request-0001"}, json={
                        "amount": "250.00", "currency": "USD", "merchant_name": "Office shop",
                        "merchant_domain": httpx.URL(url).host, "purpose": "Office mouse",
                        "product_url": url})
                rid = payment["request_id"]
                check = await call("POST", f"/v1/payment-requests/{rid}/page-check")
                research = await call("POST", "/v1/research/browse", headers=agent_h,
                                      json={"url": url})
                after = await call("GET", f"/v1/payment-requests/{rid}")
                queue = await call("GET", "/v1/approvals")
                return {"before": payment, "after": after, "check": check, "research": research,
                        "approval_queue": queue["payment_requests"]}
        finally:
            deps.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--end-to-end", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--url", default="https://example.com/")
    args = parser.parse_args(argv)
    if args.offline:
        browser, checker, url = OfflineBrowser(), OfflinePageChecker(), FIXTURE_URL
        mode = "OFFLINE synthetic fixture (not live evidence)"
    else:
        if not os.environ.get("KERNEL_API_KEY", "").strip():
            print("BLOCKED (network/credentials): set KERNEL_API_KEY or use --offline")
            return 2
        try:
            components = _kernel_tools(os.environ)
            if components is None:
                print("BLOCKED (network/credentials): K1/K2 components are not integrated")
                return 2
            browser, checker = components
        except Exception:
            print("BLOCKED (network/credentials): Kernel cannot be configured")
            return 2
        url, mode = args.url, "LIVE attempt"
    try:
        out = asyncio.run(end_to_end(browser, checker, url))
    except Exception:
        print("BLOCKED (network/credentials): browser demo could not complete")
        return 2
    if out["research"]["status"] == "no_data" or out["check"]["advisory_level"] == "no_data":
        print("BLOCKED (network/credentials): remote browser control unavailable; payments unchanged")
        return 2
    check, research = out["check"], out["research"]
    print(f"AgentPal x KERNEL [{mode}] — PayPal sandbox only, no money moves")
    print("Agent requested 250.00 USD for an office mouse at " + httpx.URL(url).host)
    observed = check.get("observed") or {}
    print(f"Owner sees page price {observed.get('price')} {observed.get('currency')}; "
          f"advisory level {check['advisory_level']}")
    print("Final page URL: " + (check.get("page") or {}).get("final_url", "unavailable"))
    for signal in check["signals"]:
        print(f"  {signal['code']}: {signal['message']}")
    print(f"Agent research.browse: {research['status']}; {len(research['facts'])} provenance-tagged facts")
    print("Screenshot SHA-256: " + str(research["screenshot_sha256"]))
    print("Replay: " + (research["replay_url"] or "unavailable (fixture has no live replay)"))
    print(check["untrusted_content_notice"])
    print(f"Payment state unchanged: {out['before']['status']} -> {out['after']['status']}; "
          f"{len(out['approval_queue'])} request(s) await an owner decision.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
