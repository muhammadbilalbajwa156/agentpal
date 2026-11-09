"""Real-service offline story and honest live-mode gates."""
import asyncio

from agentpal.demo.browser_check import (
    FIXTURE_URL, OfflineBrowser, OfflinePageChecker, end_to_end, main,
)


def test_offline_demo_real_platform_preserves_terms_risk_and_state():
    out = asyncio.run(end_to_end(OfflineBrowser(), OfflinePageChecker(), FIXTURE_URL))
    for field in ("terms", "terms_sha256", "status", "risk_score", "recommendation"):
        assert out["before"][field] == out["after"][field]
    assert out["check"]["observed"]["price"] == "99.99"
    assert out["check"]["page"]["final_url"] != FIXTURE_URL
    assert out["after"]["page_check"] == out["check"]
    assert out["research"]["status"] == "ok" and out["research"]["html_sha256"]
    assert out["approval_queue"][0]["request_id"] == out["after"]["request_id"]
    assert out["after"]["status"] == "awaiting_approval"


def test_offline_demo_labels_synthetic_evidence_and_no_replay(capsys):
    assert main(["--end-to-end", "--offline"]) == 0
    text = capsys.readouterr().out
    assert "OFFLINE synthetic fixture (not live evidence)" in text
    assert "99.99 USD" in text and "250.00 USD" in text
    assert "PAGE_REDIRECT_OFF_MERCHANT" in text
    assert "fixture has no live replay" in text


def test_live_without_credentials_is_blocked(monkeypatch, capsys):
    monkeypatch.delenv("KERNEL_API_KEY", raising=False)
    assert main(["--end-to-end"]) == 2
    assert "BLOCKED (network/credentials)" in capsys.readouterr().out


def test_live_without_integrated_components_is_blocked(monkeypatch, capsys):
    monkeypatch.setenv("KERNEL_API_KEY", "fixture-key")
    monkeypatch.setattr("agentpal.demo.browser_check._kernel_tools", lambda env: None)
    assert main(["--end-to-end"]) == 2
    assert "BLOCKED (network/credentials)" in capsys.readouterr().out


def test_live_unreachable_browser_is_blocked_not_a_success(monkeypatch, capsys):
    from agentpal.demo import browser_check
    monkeypatch.setenv("KERNEL_API_KEY", "fixture-key")
    monkeypatch.setattr(browser_check, "_kernel_tools", lambda env: (OfflineBrowser(), OfflinePageChecker()))

    async def unreachable(*args):
        return {"research": {"status": "no_data"}, "check": {"advisory_level": "no_data"}}
    monkeypatch.setattr(browser_check, "end_to_end", unreachable)
    assert main(["--end-to-end"]) == 2
    assert "BLOCKED (network/credentials)" in capsys.readouterr().out
