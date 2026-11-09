"""Channel3 client: key handling, error mapping, no retry/redirect, cache, parsing (offline)."""

import hashlib
import json
import logging
from pathlib import Path

import httpx
import pytest

from agentpal.tools.channel3 import Channel3Client, Channel3Error, strip_tracking
from agentpal.tools.search import SearchHit, SearchNotConfigured

KEY = "c3_test_" + "k" * 32
FIXTURE = Path(__file__).parent / "fixtures" / "channel3" / "search_mx_master_3s.json"
RAW = FIXTURE.read_bytes()


def client(handler, key=KEY):
    calls = []

    def wrapped(request):
        calls.append(request)
        return handler(request)
    return Channel3Client(key, client=httpx.Client(transport=httpx.MockTransport(wrapped))), calls


def ok(request):
    return httpx.Response(200, content=RAW)


def test_key_only_in_header_never_in_url_body_repr_or_logs(caplog):
    caplog.set_level(logging.DEBUG)
    c, calls = client(ok)
    result = c.search_products("Logitech MX Master 3S")
    req = calls[0]
    assert req.method == "POST" and str(req.url) == "https://api.trychannel3.com/v1/search"
    assert req.headers["x-api-key"] == KEY
    assert KEY not in str(req.url) and KEY.encode() not in req.content
    assert json.loads(req.content) == {"query": "Logitech MX Master 3S"}
    assert KEY not in repr(c) and KEY not in caplog.text
    assert result.endpoint == "search" and len(result.products) == 5


@pytest.mark.parametrize("status,kind", [(401, "auth"), (402, "credits_exhausted"),
                                         (429, "rate_limited"), (422, "invalid_request"),
                                         (500, "unavailable"), (503, "unavailable"),
                                         (302, "unavailable")])
def test_error_mapping_without_retry_or_redirect(status, kind):
    c, calls = client(lambda r: httpx.Response(status, headers={"location": "https://evil.example/"},
                                               json={"detail": "x"}))
    with pytest.raises(Channel3Error) as exc:
        c.search_products("mouse")
    assert exc.value.kind == kind
    assert len(calls) == 1                     # no retry, redirect never followed
    assert KEY not in str(exc.value)


def test_timeout_and_transport_errors_are_unavailable():
    def boom(request):
        raise httpx.ReadTimeout("slow", request=request)
    c, _ = client(boom)
    with pytest.raises(Channel3Error) as exc:
        c.search_products("mouse")
    assert exc.value.kind == "unavailable"


def test_not_configured_makes_no_call():
    c, calls = client(ok, key="  ")
    assert not c.configured()
    with pytest.raises(Channel3Error) as exc:
        c.search_products("mouse")
    assert exc.value.kind == "not_configured" and calls == []
    with pytest.raises(SearchNotConfigured):
        c.search("mouse", 5)


def test_identical_searches_are_cached_to_save_credits():
    c, calls = client(ok)
    c.search_products("Logitech  MX Master 3S")
    c.search_products("logitech mx master 3s")
    assert len(calls) == 1 and c.calls["search"] == 1


def test_evidence_records_endpoint_time_and_exact_hash():
    c, _ = client(ok)
    ev = c.search_products("mouse").evidence
    assert ev.content_sha256 == hashlib.sha256(RAW).hexdigest()
    assert ev.method == "search_result" and ev.url.endswith("/v1/search")
    assert ev.retrieved_at.tzinfo is not None


def test_parsing_accepts_documented_and_live_price_shapes_and_skips_garbage():
    body = {"products": [
        {"id": "a1", "title": "Thing", "brands": [{"name": "B"}], "offers": [
            {"domain": "Shop.com", "price": {"price": 10, "currency": "usd"}},
            {"domain": "doc.com", "price": {"amount": "12.50", "currency": "USD"}},
            {"domain": "neg.com", "price": {"price": -1, "currency": "USD"}},
            {"price": {"price": 5, "currency": "USD"}},              # no domain: skipped
            "garbage"]},
        {"title": "no id: skipped"}]}
    c, _ = client(lambda r: httpx.Response(200, json=body))
    (p,) = c.search_products("thing").products
    assert [(o.domain, o.price, o.currency) for o in p.offers] == [
        ("shop.com", 10, "USD"), ("doc.com", 12.5, "USD"), ("neg.com", None, "USD")]


def test_malformed_and_oversized_responses_are_rejected():
    c, _ = client(lambda r: httpx.Response(200, content=b"not json"))
    with pytest.raises(Channel3Error, match="not JSON"):
        c.search_products("x")
    c, _ = client(lambda r: httpx.Response(200, json={"unexpected": True}))
    with pytest.raises(Channel3Error) as exc:
        c.search_products("y")
    assert exc.value.kind == "invalid_response"


def test_lookup_and_product_endpoints_validate_input_before_any_call():
    c, calls = client(lambda r: httpx.Response(200, json={"product": json.loads(RAW)["products"][0]}))
    for bad in ("ftp://shop.com/x", "javascript:alert(1)", "not a url"):
        with pytest.raises(Channel3Error):
            c.lookup_url(bad)
    for bad in ("../admin", "a/b", "x?y=1", ""):
        with pytest.raises(Channel3Error):
            c.get_product(bad)
    assert calls == []
    r = c.lookup_url("https://www.officedepot.com/a/products/123/")
    assert r.endpoint == "lookup" and r.products[0].id == "nLZKmNT"
    assert json.loads(calls[0].content) == {"url": "https://www.officedepot.com/a/products/123/"}


def test_product_refresh_uses_free_get_endpoint():
    c, calls = client(lambda r: httpx.Response(200, json=json.loads(RAW)["products"][0]))
    r = c.get_product("nLZKmNT")
    assert calls[0].method == "GET" and calls[0].url.path == "/v1/products/nLZKmNT"
    assert r.endpoint == "product" and c.calls["product"] == 1


def test_workforce_search_provider_returns_hits():
    c, _ = client(ok)
    hits = c.search("Logitech MX Master 3S", 3)
    assert hits and all(isinstance(h, SearchHit) for h in hits)
    assert "offers:" in hits[0].snippet


def test_strip_tracking_removes_affiliate_parameters():
    assert strip_tracking("https://buy.trychannel3.com/abc?o=1&aid=2#x") == \
        "https://buy.trychannel3.com/abc"


# --------------------------------------------------- Codex review fixes (2026-11-01)
class _Stream(httpx.SyncByteStream):
    def __init__(self, chunks, delay=0.0):
        self.chunks, self.delay, self.yielded = chunks, delay, 0

    def __iter__(self):
        import time as _t
        for c in self.chunks:
            if self.delay:
                _t.sleep(self.delay)
            self.yielded += 1
            yield c


def test_slow_drip_body_hits_the_total_deadline():
    stream = _Stream([b"{"] + [b" "] * 50, delay=0.05)
    c = Channel3Client(KEY, total_deadline_s=0.3, client=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=stream))))
    import time as _t
    started = _t.monotonic()
    with pytest.raises(Channel3Error) as exc:
        c.search_products("mouse")
    assert exc.value.kind == "unavailable" and _t.monotonic() - started < 1.5
    assert stream.yielded < 51


def test_oversized_body_is_rejected_while_streaming_not_after_download():
    stream = _Stream([b"x" * (1024 * 1024)] * 8)
    c = Channel3Client(KEY, client=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=stream))))
    with pytest.raises(Channel3Error) as exc:
        c.search_products("mouse")
    assert exc.value.kind == "invalid_response" and stream.yielded <= 5


def test_error_bodies_are_not_read():
    stream = _Stream([b"x" * (1024 * 1024)] * 8)
    c = Channel3Client(KEY, client=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(500, stream=stream))))
    with pytest.raises(Channel3Error):
        c.search_products("mouse")
    assert stream.yielded == 0


def test_cache_hits_are_flagged_so_callers_refresh_prices():
    c, _ = client(ok)
    assert c.search_products("mouse").cached is False
    assert c.search_products("mouse").cached is True


def test_concurrent_identical_searches_spend_one_credit():
    import threading
    import time as _t
    barrier = threading.Barrier(4)

    def slow(request):
        _t.sleep(0.2)
        return httpx.Response(200, content=RAW)
    c, calls = client(slow)

    def go():
        barrier.wait()
        c.search_products("Logitech MX Master 3S")
    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1


@pytest.mark.parametrize("bad", ["1e100", "1e12", "0.00001", "NaN", "Infinity", "-5"])
def test_absurd_prices_are_dropped_not_crashing(bad):
    body = {"products": [{"id": "a1", "title": "T", "offers": [
        {"domain": "s.com", "price": {"price": bad, "currency": "USD"}}]}]}
    c, _ = client(lambda r: httpx.Response(200, json=body))
    (p,) = c.search_products("t").products
    assert p.offers[0].price is None
