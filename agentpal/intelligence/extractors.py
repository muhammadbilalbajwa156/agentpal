"""Deterministic product-page extractors implementing `contracts.Extractor`.

Input: a retrieved source (`SourceEvidence`) and the exact bytes it served. Output:
provenance-tagged facts. Rules every extractor here follows:
  * every value found is `provenance=observed` and cites the given source;
  * a fact that was looked for and not found is `provenance=unavailable` (value None);
  * nothing is guessed: an unparseable price is reported unavailable with a note, never
    "repaired"; currency is never inferred from a symbol, locale or domain;
  * page text is data, never instructions; strings are length-capped;
  * the bytes must match `source.content_sha256`, otherwise nothing is extracted;
  * no I/O: extractors never fetch, follow links or execute scripts.

Standard library only (`html.parser`, `json`). Supported vocabularies:
schema.org JSON-LD Product / Offer / AggregateOffer, OpenGraph and product meta tags
(`og:price:*`, `product:price:*`, `product:availability`), and schema.org microdata.
Where a value sits in the page is recorded in `note` (the "locator").
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Iterable, Iterator

from ..contracts import ObservedFact, Provenance, SourceEvidence
from ..schemas import normalize_domain

MAX_CONTENT_BYTES = 2_000_000
MAX_JSONLD_BLOCKS = 20
MAX_JSONLD_BLOCK_CHARS = 200_000
MAX_NODES = 2_000           # JSON-LD nodes visited per page
MAX_PRODUCTS = 5
MAX_OFFERS_PER_PRODUCT = 10
MAX_FACTS = 80              # per extractor call; ToolResult allows 200
MAX_TEXT = 200

# Facts an extractor reports as unavailable when it finds no value for them.
CORE_FACTS = ("product_name", "price", "price_currency", "availability", "shipping_price",
              "tax_included")

_PRICE = re.compile(r"^\d{1,12}(\.\d{1,8})?$")      # schema.org: '.' decimal, no grouping
_CURRENCY = re.compile(r"^[A-Za-z]{3}$")
_AVAILABILITY = {
    "instock": "InStock", "outofstock": "OutOfStock", "preorder": "PreOrder",
    "presale": "PreSale", "backorder": "BackOrder", "discontinued": "Discontinued",
    "soldout": "SoldOut", "limitedavailability": "LimitedAvailability",
    "onlineonly": "OnlineOnly", "instoreonly": "InStoreOnly",
    "madetoorder": "MadeToOrder", "reserved": "Reserved",
    # OpenGraph product:availability values
    "in stock": "InStock", "out of stock": "OutOfStock", "pending": "PreOrder",
    "available for order": "BackOrder",
}
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


# ---------------------------------------------------------------- value parsing

def parse_price(value: object) -> Decimal | None:
    """Strict schema.org price parsing. Accepts a JSON number or a plain decimal string
    ("49", "49.00"). Rejects thousands separators, symbols, ranges, negatives, NaN."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return Decimal(value) if 0 <= value < 10**12 else None
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")) or value < 0:
            return None
        value = repr(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not _PRICE.match(text):
        return None
    try:
        return Decimal(text)
    except InvalidOperation:  # pragma: no cover - the regex already guarantees validity
        return None


def parse_currency(value: object) -> str | None:
    if not isinstance(value, str) or not _CURRENCY.match(value.strip()):
        return None
    return value.strip().upper()


def parse_availability(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    key = value.strip().rsplit("/", 1)[-1].strip().lower()
    return _AVAILABILITY.get(key)


def parse_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return None


def clean_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(_CONTROL.sub(" ", value).split())
    return text[:MAX_TEXT] or None


def page_domain(source: SourceEvidence) -> str | None:
    """Domain the content was actually served from (after redirects)."""
    url = source.final_url or source.url
    host = re.sub(r"^https?://", "", url, flags=re.IGNORECASE).split("/", 1)[0]
    host = host.split("?", 1)[0].split("#", 1)[0]
    try:
        return normalize_domain(host)
    except ValueError:
        return None


def _decode(content: bytes) -> str:
    # UTF-8 with replacement: a page in another charset yields replacement characters in
    # text fields, never altered digits (prices are ASCII in every supported vocabulary).
    return content.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- HTML scanning

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
         "source", "track", "wbr"}


class _Scanner(HTMLParser):
    """One pass collecting JSON-LD script bodies, meta tags and microdata properties."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.jsonld: list[str] = []
        self.meta: list[tuple[str, str]] = []          # (property/name lowercased, content)
        self.items: list[dict] = []                   # microdata scopes in document order
        self._script: list[str] | None = None
        self._raw_text = False                         # inside another <script>/<style>
        # open elements: (tag, scope opened here, itemprop, owning scope, text buffer)
        self._stack: list[tuple[str, dict | None, str | None, dict | None,
                                list[str] | None]] = []
        self._scopes: list[dict] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag in ("script", "style"):
            if (tag == "script" and a.get("type", "").strip().lower() == "application/ld+json"
                    and len(self.jsonld) < MAX_JSONLD_BLOCKS):
                self._script = []
            else:
                self._raw_text = True
            return
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").strip().lower()
            if key and "content" in a and len(self.meta) < 200:
                self.meta.append((key, a["content"]))
        prop = a.get("itemprop", "").strip() or None
        owner = self._scopes[-1] if self._scopes else None
        scope: dict | None = None
        buf: list[str] | None = None
        if "itemscope" in a and len(self.items) < 50:
            scope = {"type": a.get("itemtype", "").strip().rsplit("/", 1)[-1],
                     "props": {}, "index": len(self.items)}
            if owner is not None and prop:
                owner["props"].setdefault(prop, scope)
            self.items.append(scope)
        elif prop and owner is not None:
            direct = a.get("content")
            if direct is None and tag in ("a", "link"):
                direct = a.get("href")
            if direct is not None:
                owner["props"].setdefault(prop, direct)
            elif tag not in _VOID:
                buf = []
        if tag in _VOID:
            return
        if scope is not None:
            self._scopes.append(scope)
        self._stack.append((tag, scope, prop, owner, buf))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID and tag not in ("script", "style"):
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._script is not None:
            if sum(map(len, self._script)) <= MAX_JSONLD_BLOCK_CHARS:
                self._script.append(data)
            return
        if self._raw_text:
            return
        for *_, buf in self._stack:
            if buf is not None and sum(map(len, buf)) < 2 * MAX_TEXT:
                buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            if self._script is not None:
                self.jsonld.append("".join(self._script))
                self._script = None
            self._raw_text = False
            return
        if not any(t == tag for t, *_ in self._stack):
            return  # stray end tag
        while self._stack:
            t, scope, prop, owner, buf = self._stack.pop()
            if buf is not None and owner is not None and prop:
                owner["props"].setdefault(prop, "".join(buf))
            if scope is not None and self._scopes and self._scopes[-1] is scope:
                self._scopes.pop()
            if t == tag:
                break


def _scan(text: str) -> _Scanner:
    scanner = _Scanner()
    try:
        scanner.feed(text)
        scanner.close()
    except Exception:  # noqa: BLE001 - html.parser on hostile input: keep what was read
        pass
    return scanner


# ---------------------------------------------------------------- base class

class _ProductExtractorBase:
    name = "product_base"

    def extract(self, source: SourceEvidence, content: bytes) -> list[ObservedFact]:
        problem = _precheck(source, content)
        if problem:
            return unavailable_facts(CORE_FACTS, self.name, problem)
        found = list(self._found(source, _scan(_decode(content))))[:MAX_FACTS]
        have = {f.name for f in found}
        missing = [n for n in CORE_FACTS if n not in have]
        return found + unavailable_facts(missing, self.name, "not found in this vocabulary")

    def _found(self, source: SourceEvidence, scan: _Scanner) -> Iterator[ObservedFact]:
        raise NotImplementedError

    def _fact(self, source: SourceEvidence, name: str, value, locator: str,
              unit: str | None = None) -> ObservedFact:
        return ObservedFact(name=name, value=value, unit=unit, provenance=Provenance.observed,
                            source=source, extracted_by=self.name, note=locator[:300])


def _precheck(source: SourceEvidence, content: bytes) -> str | None:
    if not isinstance(content, (bytes, bytearray)):
        return "content is not bytes; nothing extracted"
    if len(content) > MAX_CONTENT_BYTES:
        return "content exceeds the extractor size limit; nothing extracted"
    if SourceEvidence.digest(bytes(content)) != source.content_sha256:
        return "content does not match the source's sha256; nothing extracted"
    return None


def unavailable_facts(names: Iterable[str], extracted_by: str, note: str) -> list[ObservedFact]:
    return [ObservedFact(name=n, value=None, provenance=Provenance.unavailable,
                         extracted_by=extracted_by, note=note[:300]) for n in names]


# ---------------------------------------------------------------- JSON-LD

def _types(node: dict) -> set[str]:
    t = node.get("@type")
    values = t if isinstance(t, list) else [t]
    return {v.rsplit("/", 1)[-1] for v in values if isinstance(v, str)}


def _walk(value, budget: list[int], depth: int = 0) -> Iterator[dict]:
    """Every dict in a JSON-LD document (@graph and nesting included), bounded."""
    if budget[0] <= 0 or depth > 12:
        return
    if isinstance(value, dict):
        budget[0] -= 1
        yield value
        for v in value.values():
            if isinstance(v, (dict, list)):
                yield from _walk(v, budget, depth + 1)
    elif isinstance(value, list):
        for v in value[:200]:
            yield from _walk(v, budget, depth + 1)


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _reject_constant(name: str):
    raise ValueError(f"non-finite number {name}")


class JsonLdProductExtractor(_ProductExtractorBase):
    """schema.org Product/Offer/AggregateOffer in `<script type="application/ld+json">`."""

    name = "jsonld_product"

    def _found(self, source: SourceEvidence, scan: _Scanner) -> Iterator[ObservedFact]:
        products = 0
        loose_offers: list[tuple[str, dict]] = []
        seen_offer_ids: set[int] = set()
        for b, block in enumerate(scan.jsonld):
            if len(block) > MAX_JSONLD_BLOCK_CHARS:
                continue
            try:
                doc = json.loads(block, parse_constant=_reject_constant)
            except (ValueError, RecursionError):
                continue
            budget = [MAX_NODES]
            for node in _walk(doc, budget):
                types = _types(node)
                if "Product" in types or "ProductGroup" in types:
                    if products >= MAX_PRODUCTS:
                        continue
                    loc = f"jsonld[{b}].Product[{products}]"
                    products += 1
                    name = clean_text(node.get("name"))
                    if name:
                        yield self._fact(source, "product_name", name, loc + ".name")
                    brand = node.get("brand")
                    brand_name = clean_text(brand.get("name") if isinstance(brand, dict)
                                            else brand)
                    if brand_name:
                        yield self._fact(source, "brand_name", brand_name, loc + ".brand")
                    for i, offer in enumerate(_as_list(node.get("offers"))
                                              [:MAX_OFFERS_PER_PRODUCT]):
                        if isinstance(offer, dict):
                            seen_offer_ids.add(id(offer))
                            yield from self._offer(source, offer, f"{loc}.offers[{i}]")
                elif types & {"Offer", "AggregateOffer"}:
                    loose_offers.append((f"jsonld[{b}].Offer", node))
        for i, (loc, offer) in enumerate(loose_offers[:MAX_OFFERS_PER_PRODUCT]):
            if id(offer) not in seen_offer_ids:
                yield from self._offer(source, offer, f"{loc}[{i}]")

    def _offer(self, source: SourceEvidence, offer: dict, loc: str) -> Iterator[ObservedFact]:
        currency = parse_currency(offer.get("priceCurrency"))
        price_raw = offer.get("price")
        spec_tax = None
        if price_raw is None:  # price may live in a priceSpecification
            for spec in _as_list(offer.get("priceSpecification"))[:5]:
                if isinstance(spec, dict) and spec.get("price") is not None:
                    price_raw = spec.get("price")
                    currency = currency or parse_currency(spec.get("priceCurrency"))
                    spec_tax = parse_bool(spec.get("valueAddedTaxIncluded"))
                    loc += ".priceSpecification"
                    break
        for spec in _as_list(offer.get("priceSpecification"))[:5]:
            if spec_tax is None and isinstance(spec, dict):
                spec_tax = parse_bool(spec.get("valueAddedTaxIncluded"))
        if currency:
            yield self._fact(source, "price_currency", currency, loc + ".priceCurrency")
        if price_raw is not None:
            price = parse_price(price_raw)
            if price is not None:
                yield self._fact(source, "price", price, loc + ".price", unit=currency)
            else:
                yield from unavailable_facts(
                    ["price"], self.name,
                    f"{loc}.price present but not a schema.org decimal; value not guessed")
        if "AggregateOffer" in _types(offer):
            for key, name in (("lowPrice", "price_low"), ("highPrice", "price_high")):
                value = parse_price(offer.get(key))
                if value is not None:
                    yield self._fact(source, name, value, f"{loc}.{key}", unit=currency)
        availability = parse_availability(offer.get("availability"))
        if availability:
            yield self._fact(source, "availability", availability, loc + ".availability")
        if spec_tax is not None:
            yield self._fact(source, "tax_included", spec_tax,
                             loc + ".priceSpecification.valueAddedTaxIncluded")
        for s, details in enumerate(_as_list(offer.get("shippingDetails"))[:3]):
            rate = details.get("shippingRate") if isinstance(details, dict) else None
            if isinstance(rate, dict):
                value = parse_price(rate.get("value"))
                rate_currency = parse_currency(rate.get("currency"))
                if value is not None:
                    yield self._fact(source, "shipping_price", value,
                                     f"{loc}.shippingDetails[{s}].shippingRate",
                                     unit=rate_currency)
        seller = offer.get("seller")
        seller_name = clean_text(seller.get("name") if isinstance(seller, dict) else None)
        if seller_name:
            yield self._fact(source, "seller_name", seller_name, loc + ".seller.name")


# ---------------------------------------------------------------- meta tags

_META_PRICE = ("product:price:amount", "og:price:amount", "product:sale_price:amount")
_META_CURRENCY = ("product:price:currency", "og:price:currency",
                  "product:sale_price:currency")
_META_AVAIL = ("product:availability", "og:availability")
_META_SHIP = ("product:shipping_cost:amount", "og:shipping_cost:amount")
_META_SHIP_CUR = ("product:shipping_cost:currency", "og:shipping_cost:currency")


class OpenGraphProductExtractor(_ProductExtractorBase):
    """OpenGraph / Facebook product meta tags (`og:*`, `product:*`)."""

    name = "meta_product"

    def _found(self, source: SourceEvidence, scan: _Scanner) -> Iterator[ObservedFact]:
        meta = scan.meta
        currencies = [parse_currency(v) for k, v in meta if k in _META_CURRENCY]
        currency = next((c for c in currencies if c), None)
        if currency:
            yield self._fact(source, "price_currency", currency, "meta:price:currency")
        n = 0
        for k, v in meta:
            if k in _META_PRICE and n < MAX_OFFERS_PER_PRODUCT:
                price = parse_price(v)
                if price is not None:
                    n += 1
                    yield self._fact(source, "price", price, f"meta:{k}", unit=currency)
        title = next((clean_text(v) for k, v in meta if k == "og:title"), None)
        if title and any(k.startswith(("product:", "og:price")) for k, _ in meta):
            yield self._fact(source, "product_name", title, "meta:og:title")
        avail = next((a for k, v in meta if k in _META_AVAIL
                      and (a := parse_availability(v))), None)
        if avail:
            yield self._fact(source, "availability", avail, "meta:product:availability")
        ship_cur = next((c for k, v in meta if k in _META_SHIP_CUR
                         and (c := parse_currency(v))), None)
        ship = next((p for k, v in meta if k in _META_SHIP
                     and (p := parse_price(v)) is not None), None)
        if ship is not None:
            yield self._fact(source, "shipping_price", ship, "meta:shipping_cost:amount",
                             unit=ship_cur)


# ---------------------------------------------------------------- microdata

class MicrodataProductExtractor(_ProductExtractorBase):
    """schema.org microdata (`itemscope` / `itemtype` / `itemprop`)."""

    name = "microdata_product"

    def _found(self, source: SourceEvidence, scan: _Scanner) -> Iterator[ObservedFact]:
        products = [i for i in scan.items if i["type"] in ("Product", "ProductGroup")]
        offers_done: set[int] = set()
        for p, product in enumerate(products[:MAX_PRODUCTS]):
            loc = f"microdata:Product[{p}]"
            name = clean_text(product["props"].get("name"))
            if name:
                yield self._fact(source, "product_name", name, loc + ".name")
            offer = product["props"].get("offers")
            if isinstance(offer, dict):
                offers_done.add(offer["index"])
                yield from self._offer(source, offer, loc + ".offers")
        loose = [i for i in scan.items if i["type"] in ("Offer", "AggregateOffer")
                 and i["index"] not in offers_done]
        for o, offer in enumerate(loose[:MAX_OFFERS_PER_PRODUCT]):
            yield from self._offer(source, offer, f"microdata:Offer[{o}]")

    def _offer(self, source: SourceEvidence, offer: dict, loc: str) -> Iterator[ObservedFact]:
        props = offer["props"]
        currency = parse_currency(props.get("priceCurrency"))
        if currency:
            yield self._fact(source, "price_currency", currency, loc + ".priceCurrency")
        price = parse_price(props.get("price"))
        if price is not None:
            yield self._fact(source, "price", price, loc + ".price", unit=currency)
        if offer["type"] == "AggregateOffer":
            for key, name in (("lowPrice", "price_low"), ("highPrice", "price_high")):
                value = parse_price(props.get(key))
                if value is not None:
                    yield self._fact(source, name, value, f"{loc}.{key}", unit=currency)
        avail = parse_availability(props.get("availability"))
        if avail:
            yield self._fact(source, "availability", avail, loc + ".availability")


# ---------------------------------------------------------------- composite

class CompositeProductExtractor:
    """Runs every vocabulary and keeps each fact with its own `extracted_by`. It does not
    merge or choose between conflicting values: the comparison step sees all of them.
    Adds `page_domain`, observed from the source metadata (final URL after redirects)."""

    name = "product_page"

    def __init__(self, extractors: list | None = None) -> None:
        self.extractors = extractors or [JsonLdProductExtractor(), OpenGraphProductExtractor(),
                                         MicrodataProductExtractor()]

    def extract(self, source: SourceEvidence, content: bytes) -> list[ObservedFact]:
        problem = _precheck(source, content)
        if problem:
            return unavailable_facts((*CORE_FACTS, "page_domain"), self.name, problem)
        facts: list[ObservedFact] = []
        domain = page_domain(source)
        if domain:
            facts.append(ObservedFact(
                name="page_domain", value=domain, provenance=Provenance.observed,
                source=source, extracted_by=self.name,
                note="source.final_url" if source.final_url else "source.url"))
        specific: dict[str, ObservedFact] = {}   # informative "found but unparseable" notes
        for ex in self.extractors:
            for f in ex.extract(source, content):
                if f.provenance == Provenance.observed:
                    facts.append(f)
                elif "not found in this vocabulary" not in f.note:
                    specific.setdefault(f.name, f)
        have = {f.name for f in facts}
        for name in (*CORE_FACTS, "page_domain"):
            if name not in have:
                facts.append(specific.get(name) or unavailable_facts(
                    [name], self.name, "not found by any extractor")[0])
        return facts[:150]
