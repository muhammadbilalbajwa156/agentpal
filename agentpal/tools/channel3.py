"""Channel3 product-catalog client (PayPal AI Hackathon sponsor integration).

Channel3 returns normalized product data (titles, offers from many retailers, prices,
availability, condition, buy links) for 100M+ products. AgentPal uses it as THIRD-PARTY
EVIDENCE for an advisory market check on agent purchase requests. It never authorizes
anything.

Official API (verified 2026-11-01; https://docs.trychannel3.com/llms.txt):
  POST /v1/search                 {"query": ...}          1 credit
  POST /v1/lookup                 {"url": <product page>} 1 credit
  GET  /v1/products/{product_id}                          free (refresh before display)
  auth: header ``x-api-key``; 401 bad key, 402 credits exhausted, 422 validation.
Caching rules (https://channel3.mintlify.dev/docs/guides/caching.md): cache product IDs;
never serve cached prices/offer URLs without refreshing. Accordingly only *search
results* are cached briefly (to avoid spending credits on identical queries); displays
refresh through the free product endpoint.

Safety: the key is sent only in ``x-api-key``; never logged, never in reprs or errors.
No redirects, no automatic retries, bounded response size.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from ..contracts import SourceEvidence
from .search import SearchHit, SearchNotConfigured

logger = logging.getLogger("agentpal.channel3")

API_BASE = "https://api.trychannel3.com"
ENV_KEY = "CHANNEL3_API_KEY"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_PRODUCT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class Channel3Error(RuntimeError):
    """`kind` is one of: not_configured, auth, credits_exhausted, rate_limited,
    unavailable, invalid_request, invalid_response. Messages never contain the key."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class Offer:
    domain: str
    price: Decimal | None
    currency: str | None
    availability: str | None          # InStock / OutOfStock / None
    condition: str | None             # new / used / refurbished / None
    compare_at_price: Decimal | None  # merchant's reference ("was") price, if any
    url: str | None                   # short-lived buy link (may be affiliate-attributed)
    max_commission_rate: Decimal | None


@dataclass(frozen=True)
class Product:
    id: str
    title: str
    brands: tuple[str, ...]
    offers: tuple[Offer, ...]


@dataclass(frozen=True)
class Channel3Result:
    products: tuple[Product, ...]
    evidence: SourceEvidence          # what was retrieved, when, and its hash
    endpoint: str                     # search / lookup / product
    cached: bool = False              # served from the short search cache: offers may be
    #                                   stale, so callers refresh via the free product read


def _dec(value) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    # bounded: finite, non-negative, below 10^12, at most 4 decimal places of precision
    if not d.is_finite() or d < 0 or d >= Decimal(10) ** 12 or d.as_tuple().exponent < -4:
        return None
    return d


def _offer(raw: dict) -> Offer | None:
    if not isinstance(raw, dict):
        return None
    price = raw.get("price")
    amount = currency = compare = None
    if isinstance(price, dict):
        # The live API returns {"price": ..}; the docs show {"amount": ..}. Accept both.
        amount = _dec(price.get("price", price.get("amount")))
        currency = price.get("currency") if isinstance(price.get("currency"), str) else None
        compare = _dec(price.get("compare_at_price"))
    domain = raw.get("domain")
    if not isinstance(domain, str) or not domain:
        return None
    def s(key):
        v = raw.get(key)
        return v if isinstance(v, str) else None
    return Offer(domain=domain.lower(), price=amount,
                 currency=currency.upper() if currency else None,
                 availability=s("availability"), condition=s("condition"),
                 compare_at_price=compare, url=s("url"),
                 max_commission_rate=_dec(raw.get("max_commission_rate")))


def _product(raw: dict) -> Product | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
        return None
    brands = raw.get("brands") or []
    names = tuple(b.get("name") if isinstance(b, dict) else str(b) for b in brands
                  if (isinstance(b, dict) and isinstance(b.get("name"), str))
                  or isinstance(b, str))
    offers = tuple(o for o in (_offer(x) for x in raw.get("offers") or []) if o)
    return Product(id=raw["id"], title=str(raw.get("title") or "")[:300], brands=names,
                   offers=offers)


class Channel3Client:
    name = "channel3"

    def __init__(self, api_key: str | None = None, *, client: httpx.Client | None = None,
                 timeout: float = 4.0, total_deadline_s: float = 8.0,
                 search_cache_ttl_s: float = 600.0,
                 cache_size: int = 256, clock: Callable[[], datetime] | None = None):
        key = api_key if api_key is not None else os.environ.get(ENV_KEY, "")
        self._key = key.strip()
        # trust_env stays default so an egress proxy configured for the host is honoured;
        # redirects are refused per request below.
        self._http = client or httpx.Client(timeout=timeout)
        self._timeout = timeout
        self._deadline = total_deadline_s     # whole call, including a slow-drip body
        self._inflight: dict[str, threading.Lock] = {}
        self._ttl = search_cache_ttl_s
        self._cache: OrderedDict[str, tuple[float, Channel3Result]] = OrderedDict()
        self._cache_size = cache_size
        self._lock = threading.Lock()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.calls = {"search": 0, "lookup": 0, "product": 0}  # observable credit usage

    def __repr__(self) -> str:  # never reveal the key
        return f"Channel3Client(configured={self.configured()})"

    def configured(self) -> bool:
        return bool(self._key)

    # ------------------------------------------------------------------ endpoints
    def search_products(self, query: str, *, max_results: int = 20) -> Channel3Result:
        q = " ".join(str(query).split())[:200]
        if not q:
            raise Channel3Error("invalid_request", "empty product query")
        cache_key = q.lower()
        with self._lock:
            key_lock = self._inflight.setdefault(cache_key, threading.Lock())
        # concurrent identical searches wait for one paid call instead of each paying
        with key_lock:
            with self._lock:
                hit = self._cache.get(cache_key)
                if hit and hit[0] > time.monotonic():
                    self._cache.move_to_end(cache_key)
                    r = hit[1]
                    return Channel3Result(r.products, r.evidence, r.endpoint, cached=True)
            result = self._call("POST", "/v1/search", "search", json_body={"query": q})
            trimmed = Channel3Result(result.products[:max_results], result.evidence, "search")
            with self._lock:
                self._cache[cache_key] = (time.monotonic() + self._ttl, trimmed)
                while len(self._cache) > self._cache_size:
                    evicted, _ = self._cache.popitem(last=False)
                    self._inflight.pop(evicted, None)
            return trimmed

    def lookup_url(self, url: str) -> Channel3Result:
        parts = urlsplit(str(url))
        if parts.scheme not in ("http", "https") or not parts.hostname or len(url) > 2048:
            raise Channel3Error("invalid_request", "product URL must be an http(s) URL")
        return self._call("POST", "/v1/lookup", "lookup", json_body={"url": url})

    def get_product(self, product_id: str) -> Channel3Result:
        if not _PRODUCT_ID.fullmatch(str(product_id)):
            raise Channel3Error("invalid_request", "invalid product id")
        return self._call("GET", f"/v1/products/{quote(product_id, safe='')}", "product")

    # --------------------------------------------- SearchProvider (workforce tool)
    def search(self, query: str, max_results: int) -> Sequence[SearchHit]:
        if not self.configured():
            raise SearchNotConfigured("CHANNEL3_API_KEY is not set")
        result = self.search_products(query, max_results=max_results)
        hits = []
        for p in result.products:
            offer = next((o for o in p.offers if o.url), None)
            snippet = "; ".join(f"{o.domain} {o.price} {o.currency or ''}".strip()
                                for o in p.offers[:5] if o.price is not None)
            if offer and offer.url and offer.url.startswith(("https://", "http://")):
                hits.append(SearchHit(url=offer.url[:2048], title=p.title[:300],
                                      snippet=("offers: " + snippet)[:1000]))
        return hits

    # ------------------------------------------------------------------ transport
    def _call(self, method: str, path: str, kind: str, json_body: dict | None = None
              ) -> Channel3Result:
        if not self.configured():
            raise Channel3Error("not_configured", "CHANNEL3_API_KEY is not set")
        url = API_BASE + path
        self.calls[kind] += 1
        deadline = time.monotonic() + self._deadline
        try:
            with self._http.stream(method, url, json=json_body, timeout=self._timeout,
                                   follow_redirects=False,
                                   headers={"x-api-key": self._key,
                                            "Accept": "application/json"}) as resp:
                status = resp.status_code
                if status != 200:           # decide on the status; never read error bodies
                    raw = b""
                else:
                    chunks, size = [], 0
                    for chunk in resp.iter_bytes():
                        size += len(chunk)
                        if size > MAX_RESPONSE_BYTES:
                            raise Channel3Error("invalid_response", "Channel3 response too large")
                        if time.monotonic() > deadline:
                            raise Channel3Error("unavailable", "Channel3 response too slow")
                        chunks.append(chunk)
                    raw = b"".join(chunks)
        except httpx.TimeoutException:
            raise Channel3Error("unavailable", "Channel3 request timed out") from None
        except httpx.HTTPError as exc:
            raise Channel3Error("unavailable",
                                f"Channel3 transport error: {type(exc).__name__}") from None
        if status == 401:
            raise Channel3Error("auth", "Channel3 rejected the API key (401)")
        if status == 402:
            raise Channel3Error("credits_exhausted", "Channel3 credits exhausted (402)")
        if status == 429:
            raise Channel3Error("rate_limited", "Channel3 rate limit (429)")
        if status in (400, 404, 422):
            raise Channel3Error("invalid_request", f"Channel3 rejected the request ({status})")
        if status != 200:
            raise Channel3Error("unavailable", f"Channel3 HTTP {status}")
        try:
            data = json.loads(raw)
        except ValueError:
            raise Channel3Error("invalid_response", "Channel3 response is not JSON") from None
        if kind == "search":
            items = data.get("products") if isinstance(data, dict) else None
        elif kind == "lookup":
            items = [data.get("product")] if isinstance(data, dict) else None
        else:
            items = [data] if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise Channel3Error("invalid_response", "unexpected Channel3 response shape")
        products = tuple(p for p in (_product(x) for x in items) if p)
        evidence = SourceEvidence(url=url, retrieved_at=self._clock(),
                                  content_sha256=hashlib.sha256(raw).hexdigest(),
                                  http_status=status, method="search_result")
        logger.info("channel3 %s ok products=%d", kind, len(products))
        return Channel3Result(products, evidence, kind)


def strip_tracking(url: str | None) -> str | None:
    """Drop query/fragment (affiliate/session parameters) for storing as evidence."""
    if not url:
        return None
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path, "", ""))
