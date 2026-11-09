"""Sandboxed fetch tool, fully offline (httpx.MockTransport + fake resolver)."""

from __future__ import annotations

from datetime import timezone

import httpx
import pytest

from agentpal.contracts import AgentContext, Capability, SourceEvidence, Tool, ToolError
from agentpal.tools import EvidenceStore, HttpFetchTool

from test_wallet_fixtures import build_world

PAGE = b"<html><body><h1>Widget</h1><span class=price>$49.00</span></body></html>"


class DNS:
    """Fake resolver: host -> list of answers (successive calls pop the next answer)."""

    def __init__(self, table):
        self.table = {h: (list(v) if isinstance(v[0], list) else [v]) for h, v in table.items()}
        self.calls = []

    def __call__(self, host, port):
        self.calls.append(host)
        answers = self.table.get(host)
        if not answers:
            raise OSError("nxdomain")
        return answers.pop(0) if len(answers) > 1 else answers[0]


class Site:
    """Mock web: routes on the Host header and path; records every request."""

    def __init__(self, routes):
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.headers["host"], request.url.path)
        route = self.routes.get(key)
        if route is None:
            return httpx.Response(404, content=b"not found")
        return route(request) if callable(route) else route


def make(tmp_path, routes, dns=None, **kw):
    w = build_world()
    site = Site(routes)
    dns = dns or DNS({"shop.example.com": ["93.184.215.14"]})
    tool = HttpFetchTool(w.checker, EvidenceStore(tmp_path / "evidence"),
                         transport=httpx.MockTransport(site), resolver=dns,
                         clock=w.clock, **kw)
    return w, tool, site, dns


def test_fetch_records_evidence_and_stores_bytes(tmp_path):
    w, tool, site, _ = make(tmp_path, {("shop.example.com", "/item"):
                                       httpx.Response(200, content=PAGE)})
    assert isinstance(tool, Tool)
    r = tool.run(w.ctx("alice_research"), url="https://shop.example.com/item")
    assert r.ok and r.error is None and r.facts == []
    (ev,) = r.evidence
    assert ev.content_sha256 == SourceEvidence.digest(PAGE)
    assert ev.url == ev.final_url == "https://shop.example.com/item"
    assert ev.http_status == 200 and ev.method == "http_fetch"
    assert ev.retrieved_at.tzinfo == timezone.utc
    assert tool.evidence_store.get(ev.content_sha256) == PAGE


def test_connection_is_pinned_to_the_vetted_ip_with_host_and_sni(tmp_path):
    w, tool, site, _ = make(tmp_path, {("shop.example.com", "/item"):
                                       httpx.Response(200, content=PAGE)})
    tool.run(w.ctx("alice_research"), url="https://shop.example.com/item")
    (req,) = site.requests
    assert req.url.host == "93.184.215.14"
    assert req.headers["host"] == "shop.example.com"
    assert req.extensions.get("sni_hostname") == "shop.example.com"


def test_no_cookies_credentials_or_proxy_headers_are_sent(tmp_path):
    def first(req):
        return httpx.Response(302, headers={"location": "/final",
                                            "set-cookie": "session=abc; Path=/"})

    w, tool, site, _ = make(tmp_path, {("shop.example.com", "/start"): first,
                                       ("shop.example.com", "/final"):
                                       httpx.Response(200, content=PAGE)})
    r = tool.run(w.ctx("alice_research"), url="https://shop.example.com/start")
    assert r.ok and r.evidence[0].final_url == "https://shop.example.com/final"
    for req in site.requests:
        for h in ("cookie", "authorization", "proxy-authorization"):
            assert h not in req.headers
        assert req.headers["user-agent"].startswith("AgentPal-Research")


# ------------------------------------------------------------------ permissions
@pytest.mark.parametrize("agent", ["alice_payment", "alice_finance", "alice_communication"])
def test_agents_without_research_fetch_are_refused_before_any_io(tmp_path, agent):
    w, tool, site, dns = make(tmp_path, {})
    r = tool.run(w.ctx(agent), url="https://shop.example.com/item")
    assert not r.ok and r.error == ToolError.not_permitted
    assert site.requests == [] and dns.calls == []


def test_forged_context_capabilities_do_not_open_the_tool(tmp_path):
    w, tool, site, _ = make(tmp_path, {})
    real = w.ctx("alice_payment")
    forged = AgentContext(agent_id=real.agent_id, owner_id=real.owner_id, role=real.role,
                          capabilities=frozenset(Capability))
    r = tool.run(forged, url="https://shop.example.com/item")
    assert r.error == ToolError.not_permitted and site.requests == []


def test_tool_without_an_authority_fails_closed(tmp_path):
    tool = HttpFetchTool(None, EvidenceStore(tmp_path))
    r = tool.run(AgentContext(agent_id="any_agent", owner_id="any_owner", role="research",
                              capabilities=frozenset(Capability)),
                 url="https://shop.example.com/")
    assert r.error == ToolError.not_permitted


def test_tool_use_is_audited(tmp_path):
    w, tool, _, _ = make(tmp_path, {("shop.example.com", "/item"):
                                    httpx.Response(200, content=PAGE)})
    tool.run(w.ctx("alice_research"), url="https://shop.example.com/item")
    rows = w.wallet_store.permission_audit("alice_research")
    assert rows[-1]["action"] == "tool:http_fetch" and rows[-1]["allowed"] == 1


# ------------------------------------------------------------------ SSRF
def test_redirect_to_a_private_host_is_blocked(tmp_path):
    dns = DNS({"shop.example.com": ["93.184.215.14"], "evil.example.net": ["10.0.0.7"]})
    w, tool, site, _ = make(tmp_path, {("shop.example.com", "/go"): httpx.Response(
        302, headers={"location": "https://evil.example.net/admin"})}, dns=dns)
    r = tool.run(w.ctx("alice_research"), url="https://shop.example.com/go")
    assert r.error == ToolError.blocked_target and r.evidence == []
    assert [q.headers["host"] for q in site.requests] == ["shop.example.com"]


@pytest.mark.parametrize("location", ["https://169.254.169.254/latest/meta-data/",
                                      "https://127.0.0.1/", "http://shop.example.com/x",
                                      "https://shop.example.com:8443/x", "file:///etc/passwd"])
def test_redirect_to_metadata_loopback_http_or_odd_port_is_blocked(tmp_path, location):
    w, tool, site, _ = make(tmp_path, {("shop.example.com", "/go"): httpx.Response(
        302, headers={"location": location})})
    r = tool.run(w.ctx("alice_research"), url="https://shop.example.com/go")
    assert r.error == ToolError.blocked_target
    assert len(site.requests) == 1


def test_dns_rebinding_on_a_later_hop_is_caught(tmp_path):
    dns = DNS({"shop.example.com": [["93.184.215.14"], ["127.0.0.1"]]})
    w, tool, site, _ = make(tmp_path, {("shop.example.com", "/a"): httpx.Response(
        302, headers={"location": "/b"})}, dns=dns)
    r = tool.run(w.ctx("alice_research"), url="https://shop.example.com/a")
    assert r.error == ToolError.blocked_target and len(site.requests) == 1


def test_initial_private_target_never_reaches_the_network(tmp_path):
    dns = DNS({"internal.example.com": ["192.168.0.10"]})
    w, tool, site, _ = make(tmp_path, {}, dns=dns)
    r = tool.run(w.ctx("alice_research"), url="https://internal.example.com/")
    assert r.error == ToolError.blocked_target and site.requests == []


def test_http_needs_explicit_opt_in(tmp_path):
    routes = {("shop.example.com", "/"): httpx.Response(200, content=b"ok")}
    w, tool, _, _ = make(tmp_path, routes)
    assert tool.run(w.ctx("alice_research"), url="http://shop.example.com/").error == \
        ToolError.blocked_target
    w, tool, _, _ = make(tmp_path, routes, allow_http=True)
    assert tool.run(w.ctx("alice_research"), url="http://shop.example.com/").ok


# ------------------------------------------------------------------ limits
def test_redirect_limit(tmp_path):
    w, tool, site, _ = make(tmp_path, {("shop.example.com", "/loop"): httpx.Response(
        302, headers={"location": "/loop"})}, max_redirects=2)
    r = tool.run(w.ctx("alice_research"), url="https://shop.example.com/loop")
    assert r.error == ToolError.http_error and "redirects" in r.error_detail
    assert len(site.requests) == 3


def test_declared_oversize_body_is_refused(tmp_path):
    w, tool, _, _ = make(tmp_path, {("shop.example.com", "/big"):
                                    httpx.Response(200, content=b"x" * 5000)}, max_bytes=1000)
    r = tool.run(w.ctx("alice_research"), url="https://shop.example.com/big")
    assert r.error == ToolError.too_large and r.evidence == []


class Endless(httpx.SyncByteStream):
    def __iter__(self):
        while True:
            yield b"y" * 512


def test_streamed_oversize_body_without_length_is_cut_off(tmp_path):
    w, tool, _, _ = make(tmp_path, {("shop.example.com", "/stream"):
                                    httpx.Response(200, stream=Endless())}, max_bytes=4096)
    r = tool.run(w.ctx("alice_research"), url="https://shop.example.com/stream")
    assert r.error == ToolError.too_large
    assert list((tmp_path / "evidence").rglob("*")) == []


def test_timeout_is_reported(tmp_path):
    def slow(req):
        raise httpx.ReadTimeout("slow", request=req)

    w, tool, _, _ = make(tmp_path, {("shop.example.com", "/slow"): slow})
    r = tool.run(w.ctx("alice_research"), url="https://shop.example.com/slow")
    assert not r.ok and r.error == ToolError.timeout


def test_overall_deadline_spans_redirect_hops(tmp_path):
    ticks = iter([0.0, 0.0, 4.0, 11.0, 11.0, 11.0])
    w, tool, site, _ = make(tmp_path, {
        ("shop.example.com", "/a"): httpx.Response(302, headers={"location": "/b"}),
        ("shop.example.com", "/b"): httpx.Response(302, headers={"location": "/c"}),
        ("shop.example.com", "/c"): httpx.Response(200, content=b"late")},
        timeout_s=10.0, monotonic=lambda: next(ticks, 99.0))
    r = tool.run(w.ctx("alice_research"), url="https://shop.example.com/a")
    assert r.error == ToolError.timeout


def test_http_error_status_is_a_failure_without_evidence(tmp_path):
    w, tool, _, _ = make(tmp_path, {})
    r = tool.run(w.ctx("alice_research"), url="https://shop.example.com/missing")
    assert r.error == ToolError.http_error and r.error_detail == "HTTP 404"
    assert r.evidence == []


def test_missing_or_bad_url_parameter(tmp_path):
    w, tool, _, _ = make(tmp_path, {})
    assert tool.run(w.ctx("alice_research")).error == ToolError.parse_error
    assert tool.run(w.ctx("alice_research"), url=42).error == ToolError.parse_error


# ------------------------------------------------------------------ allow-list
def test_per_owner_origin_allowlist_applies_to_every_hop(tmp_path):
    dns = DNS({"shop.example.com": ["93.184.215.14"], "other.example.org": ["8.8.8.8"]})
    lists = {"owner_alice": frozenset({"https://shop.example.com"})}
    w, tool, site, _ = make(tmp_path, {
        ("shop.example.com", "/ok"): httpx.Response(200, content=b"fine"),
        ("shop.example.com", "/out"): httpx.Response(
            302, headers={"location": "https://other.example.org/"}),
        ("other.example.org", "/"): httpx.Response(200, content=b"elsewhere")},
        dns=dns, origin_allowlist=lists.get)
    ctx = w.ctx("alice_research")
    assert tool.run(ctx, url="https://shop.example.com/ok").ok
    r = tool.run(ctx, url="https://other.example.org/")
    assert r.error == ToolError.not_permitted and "allow-listed" in r.error_detail
    r = tool.run(ctx, url="https://shop.example.com/out")
    assert r.error == ToolError.not_permitted
    assert all(q.headers["host"] == "shop.example.com" for q in site.requests)
