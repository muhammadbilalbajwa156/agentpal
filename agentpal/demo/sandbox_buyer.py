"""TEST HARNESS: plays the sandbox BUYER. This is NOT AgentPal owner approval.

A browser click is never evidence of payment. The app must verify Orders v2 itself.
Credentials come only from environment; no Playwright traces, videos or raw errors.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import time
from typing import Literal
from urllib.parse import urlsplit


@dataclass(frozen=True)
class BuyerResult:
    status: Literal['approved', 'failed']
    reason: str
    screenshot: str | None = None


def safe_buyer_url(url: str) -> bool:
    try:
        p = urlsplit(url)
        return (len(url) <= 2048 and not any(ord(c) < 33 for c in url)
                and p.scheme == 'https' and p.hostname in {'www.sandbox.paypal.com', 'sandbox.paypal.com'}
                and p.port in (None, 443) and p.username is None and p.password is None)
    except (ValueError, TypeError):
        return False


def approve_sandbox_buyer(url: str, *, report_dir: str | Path | None = None,
                          timeout_s: int = 90) -> BuyerResult:
    if not safe_buyer_url(url):
        return BuyerResult('failed', 'unsafe_sandbox_url')
    email = os.environ.get('PAYPAL_SANDBOX_BUYER_EMAIL', '')
    password = os.environ.get('PAYPAL_SANDBOX_BUYER_PASSWORD', '')
    if not email or not password:
        return BuyerResult('failed', 'buyer_credentials_missing')
    if type(timeout_s) is not int or not 1 <= timeout_s <= 180:
        return BuyerResult('failed', 'invalid_timeout')
    deadline = time.monotonic() + timeout_s
    blocked = False
    stage = 'launch'
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            options = {'headless': True}
            if os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE'):
                options['executable_path'] = os.environ['PLAYWRIGHT_CHROMIUM_EXECUTABLE']
            if os.environ.get('HTTPS_PROXY'):
                options['proxy'] = {'server': os.environ['HTTPS_PROXY']}
            browser = playwright.chromium.launch(**options, timeout=min(timeout_s * 1000, 30000))
            try:
                context = browser.new_context(service_workers='block')
                page = context.new_page()
                def guard(route):
                    nonlocal blocked
                    req = route.request
                    if req.is_navigation_request() and req.frame == page.main_frame and not safe_buyer_url(req.url):
                        blocked = True
                        route.abort()
                    else:
                        route.continue_()
                context.route('**/*', guard)
                def remaining():
                    if blocked or not safe_buyer_url(page.url):
                        raise ValueError('unsafe_navigation')
                    ms = int((deadline - time.monotonic()) * 1000)
                    if ms <= 0:
                        raise TimeoutError
                    page.set_default_timeout(ms)
                    return ms
                stage = 'navigation'
                page.goto(url, wait_until='domcontentloaded', timeout=timeout_s * 1000)
                remaining()
                stage = 'email'
                page.locator('#email').fill(email)
                next_button = page.locator('#btnNext')
                if next_button.count() and next_button.is_visible():
                    remaining()
                    next_button.click()
                remaining()
                stage = 'password'
                page.locator('#password').fill(password)
                remaining()
                stage = 'login'
                page.locator('#btnLogin').click()
                remaining()
                stage = 'submit'
                page.locator('#payment-submit-btn, [data-testid="submit-button-initial"]').first.click()
                remaining()
                screenshot = None
                if report_dir is not None:
                    directory = Path(report_dir)
                    directory.mkdir(parents=True, exist_ok=True)
                    # Remove credential text/values before any screenshot. Failure captures
                    # are deliberately omitted: login forms may contain sensitive input.
                    page.evaluate('''([email, password]) => {
                        for (const el of document.querySelectorAll('input')) el.value = '';
                        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                        while (walker.nextNode()) for (const secret of [email, password])
                            walker.currentNode.textContent = walker.currentNode.textContent.split(secret).join('[redacted]');
                    }''', [email, password])
                    screenshot = str(directory / 'sandbox-buyer-approved.png')
                    page.screenshot(path=screenshot, full_page=True, timeout=remaining())
                return BuyerResult('approved', 'buyer_submit_clicked_not_payment_evidence', screenshot)
            finally:
                browser.close()
    except Exception as exc:
        reason = 'buyer_' + stage + '_failed'
        if 'net::ERR_CERT_AUTHORITY_INVALID' in str(exc):
            reason = 'browser_tls_untrusted'
        if "Executable doesn't exist" in str(exc):
            reason = 'chromium_unavailable'
        return BuyerResult('failed', 'unsafe_sandbox_navigation' if blocked else reason)
