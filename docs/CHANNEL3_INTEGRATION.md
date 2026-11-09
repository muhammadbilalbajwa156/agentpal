# Channel3 × AgentPal: market check and better deals for AI-agent purchases

**Sponsor prize:** "Best Use of Channel3", PayPal AI Hackathon (https://paypalaihackathon.devpost.com/details/channel3).

**Status:**
- IMPLEMENTED
- TESTED OFFLINE
- **VERIFIED LIVE** against the Channel3 API on 2026-11-01

## The problem

AgentPal is the financial control layer for AI agents that developers bring themselves. When an agent asks to spend money ("buy this mouse for $250 at shady-deals.example"), the owner has to decide whether to approve. AI agents make mistakes. They hallucinate prices, pick the wrong merchant, overpay, or get tricked by a scam listing.

## What Channel3 adds

The owner approves with **real market evidence**:

| The owner sees, before approving | From Channel3 |
|---|---|
| **Overpricing:** "the claimed price is 2.30× the market median" | live offers for the same product across retailers |
| **Too good to be true:** "only 0.4× the median (counterfeit or scam risk?)" | the same |
| **Unknown merchant:** "shady-deals.example is not among the retailers listing this product" | the retailers on the product's offers |
| **Price padding:** "the agent claims $130, bestbuy.com lists $108.62" | the merchant's own offer |
| **Out of stock** or **used/refurbished** at the agent's merchant | offer `availability` and `condition` |
| **Better deals:** the same product cheaper elsewhere, with savings and sale prices | offers plus `compare_at_price` |

**Guarantees that never change:**
- **Advisory only.** The market check never approves, unblocks, lowers risk, changes terms or skips owner approval (AgentPal rules 1, 8 and 12). Every response carries `advisory_only: true` and `authoritative: false`.
- **No silent merchant switching.** Buying from a better-deal retailer means a **new payment request and a new owner approval** of its exact terms.
- **Failures never block payments.** A Channel3 outage, a 401/402/429 or a checker bug is recorded as `MARKET_DATA_UNAVAILABLE`, and the payment request is still created.
- **No invented discount codes.** Channel3's data has sale prices (`compare_at_price`) but no coupon codes. The response says so (`discount_codes_available: false`).
- **Honest labels.** Every deal carries Channel3's required commission disclosure, *"We may earn a commission when you buy through links on this page."* ([Commissions](https://channel3.mintlify.dev/docs/commissions.md)), and a verify-yourself notice.

## How Channel3 is used (official API, per the docs)

| Call | Credits | When AgentPal calls it |
|---|---|---|
| `POST /v1/search` `{"query"}` ([Search](https://channel3.mintlify.dev/docs/api-reference/v1/search.md)) | 1 | the agent names the product (`product_query`) |
| `POST /v1/lookup` `{"url"}` ([URL Lookup](https://channel3.mintlify.dev/docs/guides/url-lookup.md)) | 1 | the agent gives the merchant's product page (`product_url`); this gives an **exact** match |
| `GET /v1/products/{id}` ([Product Detail](https://channel3.mintlify.dev/docs/guides/product-detail.md)) | **free** | the owner refreshes before approving (`POST /v1/payment-requests/{id}/market-check`) |

- **Auth:** an `x-api-key` header, read from the env var `CHANNEL3_API_KEY`. It is never put in URLs, logs, reprs, errors or the repository.
- **Caching:** this follows [Caching Best Practices](https://channel3.mintlify.dev/docs/guides/caching.md).
  - Product IDs are kept.
  - Only identical *search* queries are cached, for 10 minutes, to save credits. Concurrent identical searches share one paid call.
  - A cache hit **never shows cached prices**: the matched product's live offers are re-read through the free `GET /v1/products/{id}`.
- **Robustness:**
  - The whole call has an 8-second deadline (connect, read and slow-drip bodies included).
  - Bodies are streamed and cut off at 4 MiB.
  - Error bodies are never read.
  - Absurd prices (`1e100`, NaN, negative, ≥10¹²) are dropped.
  - Analysis errors degrade to `MARKET_DATA_UNAVAILABLE`.

**Matching and analysis** (`agentpal/intelligence/market_check.py`, deterministic and fully unit-tested):
- **Same product.** One Channel3 product is one canonical item, and its offers are listings at different retailers ([Offer](https://channel3.mintlify.dev/docs/guides/offer.md)). Without a URL, a candidate must pass all of these:
  - it shares at least 60% of the query's words;
  - it contains **every model token**, single digits included ("Pixel 8" never matches "Pixel 9", "3S" never matches "3");
  - its **variant words** (Pro, Max, Mini, Plus, Ultra…) are exactly the query's (15 Pro is not 15 Pro Max);
  - it is **not an accessory** the query didn't ask for (a case for the mouse is not the mouse);
  - it contains any **capacity** the query names (256GB is not 512GB);
  - it carries the product's **brand** whenever Channel3 lists one (an Apple keyboard query never matches a Dell keyboard).

  Only **exact** (URL lookup) or **high-confidence** matches are used for price comparisons and deal claims. An approximate match only says "name the exact product or pass its URL". A product the claimed merchant actually sells is preferred.
- **Like for like.** Market statistics use only offers that are **explicitly `InStock`**, in the **same currency** and **explicitly `new`**. A real offer with no condition, from a refurbished marketplace, is not assumed new, and unknown availability is not assumed in stock. Deals must be explicitly in stock and of explicitly known condition: new, or the same second-hand condition as the agent's purchase. A deal's condition is reported exactly as listed and never defaulted. Currencies are never converted.
- **Thresholds:**

  | Ratio to the market median | Signal |
  |---|---|
  | ≥ 1.25 | warning |
  | ≥ 1.5 | high |
  | ≤ 0.5 | high (too good to be true) |

- **Merchant price mismatch:** more than ±2%.
- **Deals:** at least max(1.00, 3%) cheaper, at most 3, cheapest first.
- **Merchant matching** uses registrable domains (Public Suffix List), so `www.officedepot.com` matches `officedepot.com`.
- **Evidence:** every check records the Channel3 endpoint, `retrieved_at` and the SHA-256 of the exact response bytes, using AgentPal's provenance model.

## Where it sits in the real product flow

1. An agent creates a payment request: `POST /v1/payment-requests` with an optional `product_query` or `product_url`.
2. AgentPal runs the market check once per request. It runs **outside** the wallet's database write lock, and an idempotent replay returns the stored check without spending a credit.
3. The owner sees `market_check` in `GET /v1/payment-requests/{id}` and in the **approval queue** `GET /v1/approvals`.
4. Optionally, the owner refreshes with live prices: `POST /v1/payment-requests/{id}/market-check`. This is free, owner-only, and changes no payment state.
5. The owner approves the exact terms, rejects, or asks the agent for a new request at the better merchant.

The integration is enabled automatically when `CHANNEL3_API_KEY` is set (`platform/wiring.py`). The OpenAPI spec, the Python SDK and the Postman collections include the new fields and route.

## Independent review (Codex, GPT-6.1-Sol, read-only, 2026-11-01)

A separate AI reviewer audited the integration. It reported 1 high, 5 medium and 3 low findings. The lead verified **all nine as real** and fixed each one with a test that **fails on the previous code** (8 + 10 + 1 failing assertions recorded before the fixes):

| # | Finding | Fix |
|---|---|---|
| 1 (high) | A slow-dripping response could delay payment creation indefinitely (httpx timeouts apply per read) | 8 s total deadline |
| 2 | Size limit enforced only after downloading the whole body | streamed cap; error bodies unread |
| 3 | Loose matching: Pixel 8→9, a mouse→its case, 15 Pro Max→Pro, Apple→Dell keyboard | identity rules above; approximate matches make no price or deal claims |
| 4 | Unknown availability treated as in stock | explicit `InStock` only |
| 5 | A second-hand comparison could label an unknown-condition offer "new" | explicit, compatible condition; reported as listed |
| 6 | Cached search could show stale prices | free product re-read on cache hits |
| 7 (low) | A retry after a failed preparation spent another credit | reuse only for the same agent, idempotency key and body |
| 8 (low) | Concurrent identical searches each paid | in-flight coalescing |
| 9 (low) | `1e100` price crashed the owner's refresh | price bounds and a guarded analysis |

**Cleared by the reviewer:**
- API-key handling;
- isolation of approval, risk and terms;
- refresh authorization (another owner and an unknown id both get an identical 404);
- currency filtering, even-count medians, savings and sale arithmetic;
- merchant self-exclusion and the disclosure.

## Live evidence (2026-11-01, real API, key not shown)

`python -m agentpal.demo.market_check --end-to-end` (LIVE) runs through the real `/v1` app, identity service, wallet and risk engine:

```
Agent asks to pay 250.00 USD at shady-deals.example for: Logitech MX Master 3S wireless mouse
--- owner sees, BEFORE approving: advisory_level = HIGH_RISK
    product: Logitech MX Master 3S Performance Wireless Mouse (Channel3 id nLZKmNT, match high)
    market (3 new in-stock offers): low 99.99, median 108.62, high 113.99 USD; claimed is 2.30x the median
    [high] PRICE_ABOVE_MARKET: The claimed price is 2.30x the market median (108.62 USD).
    [warning] MERCHANT_NOT_IN_MARKET: shady-deals.example is not among the retailers Channel3 lists ...
    [info] BETTER_PRICE_AVAILABLE: The same product is listed for 99.99 USD at officedepot.com (save 150.01 USD) ...
    deal: officedepot.com 99.99 USD, save 150.01 (60.0%)
    deal: bestbuy.com 108.62 USD, save 141.38 (56.6%)
    deal: 6ave.com 113.99 USD, save 136.01 (54.4%)
    We may earn a commission when you buy through links on this page.
Agent asks to pay 99.99 USD at officedepot.com for the same product
--- owner sees: advisory_level = NO_CONCERN
Owner refreshed the risky request via Channel3's free product endpoint: high_risk
Both requests remain awaiting the owner's decision: 2 in queue; payment status awaiting_approval.
Channel3 calls: {'search': 1, 'lookup': 0, 'product': 2}
```

**What the live run used and showed:**
- **Credits:** one paid search for two payment requests. The second request re-read live offers through the free product endpoint, and the owner's refresh was free too.
- **Real-data finding:** backmarket.com listed the item at 123.00 with **no condition**. It is excluded from the "new" market statistics rather than assumed new.

## Reproduce

```sh
cd agentpal
pip install -r requirements-dev.txt
python -m agentpal.demo.market_check --end-to-end --offline   # recorded real response
export CHANNEL3_API_KEY=...                                    # your key (env/secret only)
python -m agentpal.demo.market_check --end-to-end              # LIVE
python -m agentpal.demo.market_check "Sony WH-1000XM5 headphones" --amount 600 --merchant-domain example.com
python -m pytest -q tests/test_channel3_*.py                   # live tests run when the key is set
```

## Tests

All files are in `tests/test_channel3_*.py`: 86 tests, 2 of them live. Coverage:
- **Client:** the key only in its header; error mapping (401/402/422/429/5xx/302/timeout) with no retry and no redirect; the cache; parsing of the documented and live price shapes; path-injection guards; size limits.
- **Analysis:** every signal and threshold boundary; model-token matching; merchant preference; condition, currency and stock rules for deals; sale prices; the advisory-only contract.
- **API:** visible in the approval queue; no effect on risk, terms or status; idempotent replay costs no credit; Channel3 failure or checker crash never blocks a payment; product-URL lookup; owner-only free refresh; input validation.
- **Demo:** the end-to-end story; no key gives BLOCKED.
- **Mutation-checked by the lead.** Seven deliberate breakages were each caught:
  1. merchant's own offer shown as a deal;
  2. unknown condition treated as new;
  3. model tokens ignored;
  4. a checker crash propagating;
  5. re-checking on a crash retry;
  6. following redirects;
  7. the key in the URL.

## Limitations

- Channel3 data is third-party. Prices and stock change quickly, which is why the free refresh exists and why every deal says "verify yourself".
- No currency conversion: offers in other currencies are ignored.
- Shipping and tax are not in the comparison; a deal can cost more once shipping is added.
- Offers with unknown condition are excluded from comparisons. This is conservative and can hide valid new offers.
- The 20,000 hackathon credits are finite. A 402 response degrades to `MARKET_DATA_UNAVAILABLE` and never blocks payments.
- Channel3 webhooks (price and availability updates) are not used yet. They need a public HTTPS endpoint, planned with hosting.
