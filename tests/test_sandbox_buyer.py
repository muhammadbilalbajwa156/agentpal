"""The browser is a sandbox-only test actor, never payment evidence."""
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_browser_environment(monkeypatch):
    """Even broken URL guards must never send real credentials or open a browser."""
    import sys
    from types import SimpleNamespace
    monkeypatch.setenv('PAYPAL_SANDBOX_BUYER_EMAIL', 'fake-buyer@example.test')
    monkeypatch.setenv('PAYPAL_SANDBOX_BUYER_PASSWORD', 'fake-password')
    def no_browser():
        raise AssertionError('unexpected browser launch in an offline test')
    monkeypatch.setitem(sys.modules, 'playwright.sync_api',
                        SimpleNamespace(sync_playwright=no_browser))


@pytest.mark.parametrize('url', ['http://www.sandbox.paypal.com/', 'https://www.paypal.com/',
    'https://evil.sandbox.paypal.com/', 'https://www.sandbox.paypal.com.evil/',
    'https://user@www.sandbox.paypal.com/', 'https://www.sandbox.paypal.com:444/',
    'https://www.sandbox.paypal.com/\nsecret'])
def test_buyer_refuses_unsafe_url(url):
    from agentpal.demo.sandbox_buyer import approve_sandbox_buyer
    result = approve_sandbox_buyer(url)
    assert result.status == 'failed' and result.reason == 'unsafe_sandbox_url'


def test_buyer_credentials_environment_only(monkeypatch):
    from agentpal.demo.sandbox_buyer import approve_sandbox_buyer
    monkeypatch.delenv('PAYPAL_SANDBOX_BUYER_EMAIL', raising=False)
    monkeypatch.delenv('PAYPAL_SANDBOX_BUYER_PASSWORD', raising=False)
    assert approve_sandbox_buyer('https://www.sandbox.paypal.com/').reason == 'buyer_credentials_missing'


def test_production_platform_never_imports_buyer_harness():
    root = Path(__file__).resolve().parents[1] / 'agentpal'
    for path in root.rglob('*.py'):
        if 'demo' not in path.parts:
            assert 'sandbox_buyer' not in path.read_text(), path


def test_buyer_browser_flow_proxy_scrub_and_failure_redaction(monkeypatch, tmp_path):
    import sys
    from types import SimpleNamespace
    from agentpal.demo.sandbox_buyer import approve_sandbox_buyer
    monkeypatch.setenv('PAYPAL_SANDBOX_BUYER_EMAIL', 'fake-buyer@example.test')
    monkeypatch.setenv('PAYPAL_SANDBOX_BUYER_PASSWORD', 'fake-password')
    monkeypatch.setenv('HTTPS_PROXY', 'http://proxy.example.test:443')
    events = []
    class Locator:
        @property
        def first(self):
            return self
        def fill(self, value):
            events.append(('fill', value))
        def click(self):
            events.append(('click',))
        def count(self):
            return 1
        def is_visible(self):
            return True
    class Page:
        url = 'https://www.sandbox.paypal.com/'
        main_frame = object()
        def goto(self, url, **kw):
            self.url = url
        def locator(self, selector):
            events.append(('selector', selector))
            return Locator()
        def set_default_timeout(self, timeout):
            assert 0 < timeout <= 90000
        def evaluate(self, script, args):
            assert 'input' in script and '[redacted]' in script
            events.append(('scrub',))
        def screenshot(self, **kw):
            assert events[-1] == ('scrub',)
            events.append(('screenshot',))
    page = Page()
    class Context:
        def new_page(self):
            return page
        def route(self, pattern, handler):
            self.guard = handler
    context = Context()
    class Browser:
        def new_context(self, **kw):
            assert kw['service_workers'] == 'block'
            return context
        def close(self):
            events.append(('close',))
    class Playwright:
        chromium = None
        def __init__(self):
            self.chromium = self
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def launch(self, **kw):
            assert kw['proxy'] == {'server': 'http://proxy.example.test:443'}
            assert 'ignore_https_errors' not in kw
            return Browser()
    monkeypatch.setitem(sys.modules, 'playwright.sync_api', SimpleNamespace(sync_playwright=Playwright))
    result = approve_sandbox_buyer(page.url, report_dir=tmp_path)
    assert result.status == 'approved'
    assert 'fake-password' not in repr(result) and 'fake-buyer' not in repr(result)
    assert ('screenshot',) in events and events[-1] == ('close',)
    route = SimpleNamespace(request=SimpleNamespace(is_navigation_request=lambda: True,
        frame=page.main_frame, url='https://www.paypal.com/'), abort=lambda: events.append(('abort',)),
        continue_=lambda: events.append(('continue',)))
    context.guard(route)
    assert events[-1] == ('abort',)
    def broken(*a, **k):
        raise RuntimeError('fake-password fake-buyer@example.test')
    monkeypatch.setattr(page, 'goto', broken)
    failed = approve_sandbox_buyer(page.url, report_dir=tmp_path)
    assert failed.status == 'failed'
    assert 'fake-password' not in repr(failed) and 'fake-buyer' not in repr(failed)
