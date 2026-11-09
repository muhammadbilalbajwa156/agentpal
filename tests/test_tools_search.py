"""Search tool: not configured by default (says what is needed); permission-gated; hits are
leads recorded as `search_result` evidence, never facts."""

from __future__ import annotations

from agentpal.contracts import SourceEvidence, Tool, ToolError
from agentpal.tools import EvidenceStore, SearchHit, WebSearchTool

from test_wallet_fixtures import build_world


class FakeProvider:
    name = "fake"

    def __init__(self, hits=None, exc=None):
        self.hits, self.exc, self.calls = hits or [], exc, []

    def configured(self):
        return True

    def search(self, query, max_results):
        self.calls.append((query, max_results))
        if self.exc:
            raise self.exc
        return self.hits


def test_default_provider_is_not_configured_and_explains_what_is_needed():
    w = build_world()
    tool = WebSearchTool(w.checker)
    assert isinstance(tool, Tool)
    r = tool.run(w.ctx("alice_research"), query="usb-c dock")
    assert not r.ok and r.error == ToolError.not_configured
    assert "API key" in r.error_detail and r.evidence == []


def test_search_needs_research_search():
    w = build_world()
    provider = FakeProvider()
    tool = WebSearchTool(w.checker, provider)
    for agent in ("alice_payment", "alice_verification", "alice_finance"):
        assert tool.run(w.ctx(agent), query="x").error == ToolError.not_permitted
    assert provider.calls == []


def test_hits_become_search_result_evidence_and_no_facts(tmp_path):
    w = build_world()
    hits = [SearchHit(url="https://shop.example.com/dock", title="Dock", snippet="$49"),
            SearchHit(url="http://insecure.example.com/", title="plain http"),
            SearchHit(url="https://other.example.org/", title="Other")]
    store = EvidenceStore(tmp_path)
    tool = WebSearchTool(w.checker, FakeProvider(hits), evidence_store=store, clock=w.clock)
    r = tool.run(w.ctx("alice_research"), query="usb-c dock", max_results=50)
    assert r.ok and r.facts == []
    assert [e.url for e in r.evidence] == ["https://shop.example.com/dock",
                                           "https://other.example.org/"]
    assert all(e.method == "search_result" for e in r.evidence)
    assert r.evidence[0].content_sha256 == SourceEvidence.digest(hits[0].canonical_bytes())
    assert store.get(r.evidence[0].content_sha256) == hits[0].canonical_bytes()


def test_result_count_is_capped():
    w = build_world()
    provider = FakeProvider([SearchHit(url=f"https://s{i}.example.com/") for i in range(30)])
    r = WebSearchTool(w.checker, provider).run(w.ctx("alice_research"), query="q",
                                              max_results=50)
    assert provider.calls == [("q", 10)] and len(r.evidence) == 10


def test_provider_failure_and_bad_query():
    w = build_world()
    ctx = w.ctx("alice_research")
    r = WebSearchTool(w.checker, FakeProvider(exc=RuntimeError("boom"))).run(ctx, query="q")
    assert r.error == ToolError.http_error and "boom" not in r.error_detail
    tool = WebSearchTool(w.checker, FakeProvider())
    for bad in ("", "   ", "x" * 301, None, 7):
        assert tool.run(ctx, query=bad).error == ToolError.parse_error
