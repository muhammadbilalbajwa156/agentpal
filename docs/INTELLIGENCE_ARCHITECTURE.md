# Intelligence engine architecture

Status labels used here: **IMPLEMENTED — TESTED**, **IMPLEMENTED — UNVERIFIED**, **DESIGN ONLY**.

The intelligence engine (`agentpal/intelligence/`) turns a payment request, the rule results, the optional advisory model output and any retrieved web pages into a structured **`FinancialIntelligenceReport`**.

It is **advisory only**:
* It never approves, authorizes, executes or unblocks a payment.
* The single authority stays the deterministic rules plus the orchestrator's `RiskAssessment.recommendation`, followed by an authenticated human decision.

## 1. Where it sits

```
 owner policy ──► rules.py (authority) ─┐
                                        ├─► orchestrator.assess() ─► RiskAssessment ─┐
 pinned model ─► ModelAnalysis (frozen) ┘                                            │
                                                                                     ▼
 workforce (Agent 2)                                   intelligence/adapter.py
   search ─► fetch (allowed origins, sandbox) ─►         report_from_assessment()
   (SourceEvidence, bytes) ──► research.assess_sources ──► observations + discrepancies
                                 │ extractors.py                         │
                                 │ compare.py                            ▼
                                                     FinancialIntelligenceReport ─► owner
                                                     dashboard / approval UI / audit log
```

The report is computed **after** the orchestrator, and nothing feeds back into the rules or the score. The adapter also accepts raw `rule_signals` plus a `ModelAnalysis` (`build_report`), for use before an orchestrator run exists.

## 2. Modules

| Module | What it does | Status |
|---|---|---|
| `report.py` | The output schema: `FinancialIntelligenceReport`, `Claim`, `Discrepancy`, `EvidenceQuality`, `VerificationStep`, `IntelSignal`, `RequestSummary`. Its validators enforce the advisory invariants (§4). | IMPLEMENTED — TESTED |
| `extractors.py` | `JsonLdProductExtractor`, `OpenGraphProductExtractor` and `MicrodataProductExtractor`, all implementing `contracts.Extractor`. `CompositeProductExtractor` runs all three. Uses the standard library only. | IMPLEMENTED — TESTED (local synthetic fixtures) |
| `compare.py` | Compares claimed amount, currency and merchant domain against observed facts and returns discrepancies. Also `claims_from_request`. | IMPLEMENTED — TESTED |
| `evidence.py` | Evidence-quality grading and deterministic recommended verification steps. | IMPLEMENTED — TESTED |
| `adapter.py` | Rule signals, `ModelAnalysis`/`RiskAssessment` and observations become the report. Contains the deterministic advisory derivation. | IMPLEMENTED — TESTED |
| `research.py` | `assess_sources(req, pages, extractor=None)`: the I/O-free seam with the workforce. | IMPLEMENTED — TESTED (with fixtures, not with Agent 2's live tools) |
| Wiring into `api.py`, `service.py` and the dashboard | Not done. Outside this package's ownership. | DESIGN ONLY |

## 3. Capabilities

| Capability | How | Status |
|---|---|---|
| Payment request understanding | `RequestSummary`: amount, currency, merchant, recurring, interval, purpose categories via `features.infer_categories` and evidence types. All labelled `claimed`. | IMPLEMENTED — TESTED |
| Financial anomaly identification | Rule risk indicators restated as `IntelSignal(origin="rule")`, plus `OBSERVED_*` comparison signals. No new scoring. | IMPLEMENTED — TESTED |
| Merchant information assessment | `MERCHANT_DOMAIN_MISMATCH` and `REDIRECTED_OFF_DOMAIN` (PSL registrable domain via `features.base_domain`), plus rule lookalike/confusable signals. | IMPLEMENTED — TESTED |
| Product and price discrepancies | `PRICE_MISMATCH`, `PRICE_OUTSIDE_RANGE`, `CURRENCY_MISMATCH`, `CURRENCY_UNAVAILABLE`, `PRICE_UNAVAILABLE`, `MULTIPLE_CONFLICTING_PRICES` and `OUT_OF_STOCK`. | IMPLEMENTED — TESTED |
| Evidence quality | `none`, `claimed_only`, `observed_inconsistent` or `observed_consistent`. There is deliberately **no "verified" level**. | IMPLEMENTED — TESTED |
| Missing information | Rule uncertainty messages, "terms are claimed only" when nothing was observed, unavailable price or currency, and model `missing_information` prefixed `(model)`. | IMPLEMENTED — TESTED |
| Suspicious communication | `communication_flags` combines the rule codes (`PROMPT_INJECTION_ATTEMPT`, `PRESSURE_LANGUAGE`, `HIGH_RISK_PAYMENT_METHOD_LANGUAGE`) with model findings whose codes name injection, pressure, impersonation or payment-detail changes. Email and WhatsApp message *ingestion* does not exist yet. | IMPLEMENTED — TESTED (request text only); message channels DESIGN ONLY |
| Unverified claims | `unverified_claims` holds every claim not *consistent with* an observation. Purpose, justification and every evidence document always stay unverified. | IMPLEMENTED — TESTED |
| Recommended verification steps | A deterministic table (`evidence._STEPS`), for example `CONFIRM_FINAL_PRICE`, `VERIFY_MERCHANT_IDENTITY`, `CONFIRM_OUT_OF_BAND`, `OBTAIN_INVOICE_FROM_KNOWN_CHANNEL` and `OWNER_POLICY_DECISION`. Steps tell a human to use channels the owner already trusts, never contact details from the request or message. | IMPLEMENTED — TESTED |

## 4. Advisory, and its mapping to the existing authority

`advisory` takes one of four values: `no_concern_found`, `verify_before_proceeding`, `recommend_review` or `blocked_by_rules`. None of them can approve.

| Advisory | Maps to `Recommendation` | When it is set |
|---|---|---|
| `blocked_by_rules` | `block` | **Only** when a deterministic rule hard violation is present (including an enabled combination block, which is rule-derived). It *reports* the rules' block; the intelligence layer cannot block anything itself. |
| `recommend_review` | `require_review` | Any of these: a review-severity discrepancy; the orchestrator requires review; suspicious communication; the advisory model flagged the request. |
| `verify_before_proceeding` | `require_review` | Any of these: rule risk indicators; rule uncertainty; evidence quality below `observed_consistent`; an incomplete or ambiguous observation; the model lists missing information. |
| `no_concern_found` | `allow_for_review` | None of the above. This means "may go to the owner's normal approval", which is exactly what `allow_for_review` already means. It is **not** an approval. |

Invariants, enforced by validators and tests:
1. `blocked_by_rules` requires a rule-origin hard violation. Conversely, a rule hard violation forces `blocked_by_rules`. `IntelSignal` refuses `hard_violation` from `model` or `comparison` origins.
2. `no_concern_found` is impossible when any of these is present: a review-severity discrepancy, a hard violation, `PRICE_UNAVAILABLE`, or an unavailable `price` fact.
3. The advisory's mapped recommendation is **never less strict** than `orchestrator_recommendation`.
4. Model output can raise the advisory to `recommend_review` at most, and never lowers it (random-output property test). Unverifiable model claims ("verified legitimate", "good reputation") are dropped with the same pattern `parse_model_output` uses.
5. `authoritative` and `payment_executed` are the constant `False`.

There is **no exception** to "every input only raises". An earlier draft let a consistent page from the merchant's own domain clear `MISSING_EVIDENCE` / `EVIDENCE_UNVERIFIED`. The lead removed that on 2026-10-31: an observed page shows what was served to our fetcher, not that the agent's invoice is genuine or the purchase justified (owner decision F2; security rule 8). Test: `test_consistent_observation_does_not_clear_missing_evidence`.

## 5. Extractors (the workforce seam)

`contracts.Extractor.extract(source: SourceEvidence, content: bytes) -> list[ObservedFact]`.

* **Hash check first.** If the bytes do not match `source.content_sha256`, only `unavailable` facts are returned. The same happens for content above 2 MB.
* **Vocabularies:**
  * schema.org JSON-LD `Product`/`ProductGroup` with `offers`: `Offer`, `AggregateOffer` (`lowPrice`/`highPrice`), `priceSpecification` (`valueAddedTaxIncluded`), `shippingDetails.shippingRate` and `seller`. `@graph` and nesting are handled.
  * OpenGraph and product meta tags (`og:price:*`, `product:price:*`, `product:availability`, `product:shipping_cost:*`).
  * schema.org microdata (`itemscope`, `itemtype`, `itemprop`).
* **Fact names:**
  * `product_name`, `brand_name`, `seller_name`;
  * `price`, `price_low`, `price_high` (each with `unit` = observed currency, or `None`);
  * `price_currency`, `availability`, `shipping_price`, `tax_included`;
  * `page_domain` (from `final_url`, else `url`; composite extractor only).
* **Locator.** Where a value was found is kept in `note`, e.g. `jsonld[0].Product[0].offers[1].price`. The lead declined a `locator` contract field for now.
* **Never guessed:**
  * prices must be schema.org decimals: `"1,299.00"`, `"1.234,50"` and `"$49"` are reported as unavailable with a note;
  * currency is never inferred from a symbol, locale or domain;
  * visible page text and non-JSON-LD scripts are never read for prices;
  * no conversion between currencies.
* **Bounds:** at most 20 JSON-LD blocks of 200 KB each, a 2,000-node walk budget, depth ≤ 12, 5 products × 10 offers, 200-character strings and at most 150 facts. These fit `ToolResult`'s 200-fact limit.
* **Page text is data.** An injection string in a product name is kept verbatim, length-capped, as an observation. It is not a signal, an instruction or an input to the advisory (tested).

**Comparison defaults:**
* Price tolerance is `max(2% of observed, 1 minor unit)`, inclusive. JPY has 0 minor units.
* `price + observed shipping` in the same currency is also accepted.
* An observed `low..high` range is accepted with tolerance at both ends.
* The domain check compares PSL registrable domains, so `store.eu.lumen.example` matches `lumen.example`.

**Research flow (design; the fetch and search side is Agent 2's):**
1. The research agent, holding `research.search`, finds candidate URLs. The merchant's claimed domain is preferred, and search results are leads only.
2. The research agent, holding `research.fetch`, fetches only allowed origins inside the workforce sandbox. It records `SourceEvidence` (url, `final_url`, UTC time, sha256, method).
3. `assess_sources(req, pages)` runs here and performs no I/O. It processes at most 10 pages.
4. `report_from_assessment(req, assessment, observations=..., discrepancies=...)` builds the report.
5. The report is shown to the owner next to the `RiskAssessment`. Verification steps marked `requires_human` are for the owner. `OBSERVE_MERCHANT_PAGE` and `OBTAIN_PRICE_FROM_MERCHANT` can be delegated to the research agent.

## 6. Security properties (mapping to the non-negotiable rules)

| Rule | How this package respects it |
|---|---|
| 1, 12: models are advisory; model failure never bypasses rules | The model can only raise the advisory, to `recommend_review` at most. With no model, the report is built from rules and observations alone. |
| 2: deterministic limits | No scoring, no thresholds and no blocks are created here. `blocked_by_rules` only restates rules. |
| 3, 4, 5, 6: approvals | The report contains no approval object. Approval binding stays in `spending.ApprovalTerms`. |
| 7: verified provider outcomes | Not touched. `payment_executed` is always False. |
| 8: browser evidence is not trustworthy | `observed` is never presented as `verified`. The disclaimer and the evidence-quality notes say so. Hash-checked bytes only. |
| 9: external messages are not consent | Message text is a `claimed` input and raises `CONFIRM_OUT_OF_BAND`. |
| 11: auditable | The report is a frozen, JSON-serializable pydantic model. Every observation carries URL, time and hash. |

## 7. Limitations

* **Pages can lie.** Extraction shows what a page served to *our* fetcher. Cloaking, A/B prices, geo-pricing, logged-in prices and pages changed after retrieval are not detected.
* **Charset.** Non-UTF-8 pages are decoded with replacement characters. Prices stay intact because they are ASCII in every supported vocabulary, but names may be garbled.
* **Multi-offer pages.** Shipping is not linked to a specific offer, and variant prices produce `MULTIPLE_CONFLICTING_PRICES` (info). An unparseable offer price (e.g. `"1,299.00"`) on a page that also has a parseable price is dropped without a note, because the composite extractor reports a fact name as unavailable only when nothing for it was observed.
* **JSON round trip.** `ObservedFact.value` (`str | Decimal | bool | None`) re-reads a Decimal from JSON as a string. `compare.py` re-parses strict decimal strings, so comparisons still work, but other consumers must do the same.
* **Not wired yet.** The report is not yet served by the API or stored in the audit ledger. The package is not yet in `pyproject.toml` `packages`; the lead will add it at integration.
* **No real-world evaluation.** All tests use synthetic fixtures.
