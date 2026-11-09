"""The offline demo runs the full owner/agent story through the real /v1 app."""

import asyncio

from agentpal.demo.market_check import QUERY, _offline_client, end_to_end, main
from agentpal.intelligence.market_check import MarketChecker


def test_offline_end_to_end_story():
    out = asyncio.run(end_to_end(MarketChecker(_offline_client())))
    assert out["risky"]["market_check"]["advisory_level"] == "high_risk"
    assert out["fair"]["market_check"]["advisory_level"] == "no_concern"
    assert out["refreshed"]["evidence"]["endpoint"] == "product"
    assert out["risky"]["status"] == "awaiting_approval"
    # one paid search; the cached second request and the owner refresh re-read live
    # offers through Channel3's FREE product endpoint (never cached prices)
    assert out["credits"] == {"search": 1, "lookup": 0, "product": 2}
    assert len(out["approval_queue"]) == 2
    assert QUERY in out["risky"]["market_check"]["input"]["product_query"]


def test_demo_without_key_is_blocked_not_faked(monkeypatch, capsys):
    monkeypatch.delenv("CHANNEL3_API_KEY", raising=False)
    assert main([]) == 2
    assert "BLOCKED BY CREDENTIALS" in capsys.readouterr().err
