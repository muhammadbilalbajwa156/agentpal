"""SDK transport boundaries and lazy optional Kernel wiring."""
import asyncio
import importlib
import sys
import types
from pathlib import Path

import httpx
import pytest

from test_kernel_api import URL, payment, world  # noqa: F401
from test_platform_fixtures import OWNER_A

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk" / "python"))
from agentpal_sdk import AgentClient, OwnerClient  # noqa: E402


def test_sdk_owner_check_and_agent_browse_use_real_app(world):  # noqa: F811
    w = world
    aid, key, p = payment(w)
    w.grant(aid, ["research.fetch"])

    async def run():
        transport = httpx.ASGITransport(app=w.app)
        async with OwnerClient(base_url="http://127.0.0.1", credential=OWNER_A,
                               transport=transport) as owner:
            saved = await owner.check_merchant_page(p["request_id"])
            supplied = await owner.check_merchant_page(p["request_id"], url=URL + "?variant=1")
        async with AgentClient(base_url="http://127.0.0.1", credential=key,
                               transport=transport) as agent:
            capture = await agent.browse(URL)
        return saved, supplied, capture
    saved, supplied, capture = asyncio.run(run())
    assert saved["input"]["url"] == URL
    assert supplied["input"]["url"] == URL + "?variant=1"
    assert capture["status"] == "ok" and capture["html_sha256"]


def test_sdk_paths_are_escaped_and_url_is_data_only():
    calls = []

    async def handler(request):
        calls.append((request.method, request.url.raw_path, request.read()))
        return httpx.Response(200, json={"ok": True})

    async def run():
        transport = httpx.MockTransport(handler)
        async with OwnerClient(base_url="http://localhost", credential="apo_fixture",
                               transport=transport) as owner:
            await owner.check_merchant_page("a/b?c", URL)
        async with AgentClient(base_url="http://localhost", credential="apk_fixture",
                               transport=transport) as agent:
            await agent.browse(URL)
    asyncio.run(run())
    assert calls == [("POST", b"/v1/payment-requests/a%2Fb%3Fc/page-check", b'{"url":"https://example.com/product"}'),
                     ("POST", b"/v1/research/browse", b'{"url":"https://example.com/product"}')]


@pytest.mark.parametrize("environment", [{}, {"KERNEL_API_KEY": "   "}])
def test_kernel_wiring_disabled_without_key(monkeypatch, environment):
    from agentpal.platform.wiring import _kernel_tools
    original = importlib.import_module
    assert _kernel_tools(environment) is None
    assert importlib.import_module is original


def test_kernel_wiring_missing_optional_modules_is_safe(monkeypatch):
    from agentpal.platform.wiring import _kernel_tools
    monkeypatch.setitem(sys.modules, "agentpal.tools.kernel_browser", None)
    assert _kernel_tools({"KERNEL_API_KEY": "fixture-key"}) is None


def test_kernel_wiring_builds_provider_and_checker_lazily(monkeypatch):
    from agentpal.platform.wiring import _kernel_tools
    browser_module = types.ModuleType("agentpal.tools.kernel_browser")
    checker_module = types.ModuleType("agentpal.intelligence.page_check")
    calls = []

    class Provider:
        def __init__(self, *, api_key):
            calls.append(api_key)

    class Checker:
        def __init__(self, browser):
            self.browser = browser
    browser_module.KernelBrowserProvider = Provider
    checker_module.KernelPageChecker = Checker
    monkeypatch.setitem(sys.modules, browser_module.__name__, browser_module)
    monkeypatch.setitem(sys.modules, checker_module.__name__, checker_module)
    browser, checker = _kernel_tools({"KERNEL_API_KEY": "fixture-key"})
    assert checker.browser is browser
    assert calls == ["fixture-key"]


def test_build_platform_enables_both_components_only_with_key(monkeypatch):
    from agentpal.config import Settings
    from agentpal.platform import wiring
    browser, checker = object(), object()
    calls = []

    def components(env):
        calls.append(dict(env))
        return (browser, checker) if env.get("KERNEL_API_KEY") else None
    monkeypatch.setattr(wiring, "_kernel_tools", components)
    for env in ({}, {"KERNEL_API_KEY": "fixture-key"}):
        deps = wiring.build_platform(settings=Settings(db_path=":memory:", analyzer_chain=["heuristic"]),
                                     environ=env)
        try:
            assert deps.browser is (browser if env else None)
            assert deps.page_checker is (checker if env else None)
        finally:
            deps.close()
    assert calls == [{}, {"KERNEL_API_KEY": "fixture-key"}]


def test_optional_kernel_constructor_failure_does_not_disable_payments(monkeypatch):
    from agentpal.platform.wiring import _kernel_tools
    browser_module = types.ModuleType("agentpal.tools.kernel_browser")
    checker_module = types.ModuleType("agentpal.intelligence.page_check")

    class BrokenProvider:
        def __init__(self, **kw):
            raise RuntimeError("provider initialization contains sensitive configuration")
    browser_module.KernelBrowserProvider = BrokenProvider
    checker_module.KernelPageChecker = object
    monkeypatch.setitem(sys.modules, browser_module.__name__, browser_module)
    monkeypatch.setitem(sys.modules, checker_module.__name__, checker_module)
    assert _kernel_tools({"KERNEL_API_KEY": "fixture-key"}) is None


def test_unavailable_browser_admission_database_does_not_disable_payments(monkeypatch):
    from agentpal.ratelimit import LimiterUnavailable
    from test_platform_fixtures import ALL_PAY, build_world

    def unavailable(*args):
        raise LimiterUnavailable("auxiliary file unavailable")
    monkeypatch.setattr("agentpal.platform.payments.SQLiteSlidingWindowLimiter", unavailable)
    w = build_world()
    _, key, _ = w.funded_agent(caps=ALL_PAY + ["research.fetch"])
    assert w.pay(key).status_code == 201
    r = w.client.post("/v1/research/browse", json={"url": URL}, headers=w.h(key))
    assert r.status_code == 503 and r.json() == {"error": "browser_admission_unavailable"}
