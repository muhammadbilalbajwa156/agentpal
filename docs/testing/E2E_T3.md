# T3 backend acceptance: intelligence quality

IMPLEMENTED — TESTED. Tested base `db7d00870563cb55c6702787408e9bbcfbe65fec`
on 2026-11-03. Product code, SDK, existing tests, shared docs and model weights
are unchanged. This report and additions under `tests/e2e/t3/` are the deliverable.
Suggested product fixes below have **not** been implemented.

| Severity | Confirmed | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 1 | E2E-T3-1 |
| Medium | 4 | E2E-T3-2 through E2E-T3-5 |
| Low | 0 | — |

No approval bypass or completed payment was observed. These findings affect
the evidence and explanations on which an owner bases a decision.

The explicit T3 live runs made **zero PayPal orders, captures, refunds or payouts**.
They used real Channel3 and a real CLI HTTP server on `127.0.0.1:8103`, with
ledger, identity, wallet and rate-limit databases under `/tmp/e2e-t3`.
The SDK was imported from `sdk/python` and used over TCP for owner provisioning,
agent reads and owner refreshes. Credentials were captured in memory through
the documented identity CLI; server stdout/stderr were suppressed.

## Scenario design and evidence

`tests/e2e/t3/scenarios.json` contains **50 labelled requests**, ten per product:
Logitech MX Master 3S, Sony WH-1000XM5, Apple AirPods Pro 2, Samsung 990 PRO 2TB,
and Nintendo Switch OLED. Retailers and starting prices came from five real
Channel3 discovery searches, not invented retailer fixtures.

The expected owner outcome, market outcome and applicable signal codes were
written **before the first API run**. `labels_lock.json` records SHA-256
`bbbac13ed6a06b0c270959cb42eef9eebe2d2847a15272fb528acccaf03aae99`.
The first unpaced run completed 27 requests before the owner-token rate limit
returned 429. Its databases are retained under `/tmp/e2e-t3/attempt1`. The
completed run used fresh databases and three-second pacing, preserving all
default security controls. Repeated attempts did not change the labels.

| Kind | Count | Expected owner outcome | Observed risk score | Market result |
|---|---:|---|---:|---|
| Fair listed purchase | 5 | Routine review, no unsupported scam allegation | 25 | 4 no_concern; 1 no_match |
| Different currency, no local quote | 5 | Unknown comparison; never convert currencies | 25 | 4 no_concern; 1 no_match |
| Twice the listed price | 5 | Explicit price anomaly caution | 25 | 4 high_risk; 1 no_match |
| 20% of listed price | 5 | Too-cheap/scam caution | 25 | 4 high_risk; 1 no_match |
| Lookalike retailer domain | 5 | Merchant caution before approval | 25 | 4 review_recommended; 1 no_match |
| Wrong merchant/payments domain | 5 | Merchant and purpose caution | 88 | 4 review_recommended; 1 no_match |
| Gift-card/crypto urgency | 5 | Flag pressure and payment language | 99 | 4 no_concern; 1 no_match |
| Injection in purpose | 5 | Treat as data, flag injection | 80 | 4 no_concern; 1 no_match |
| Unexplained recurring hardware | 5 | Flag unexplained monthly charge | 53 | 4 no_concern; 1 no_match |
| Explained monthly equipment rental | 5 | Do not allege unexplained recurrence | 25 | 4 no_concern; 1 no_match |

All 50 HTTP creates returned **201** and `awaiting_approval`, with
`requires_owner_approval=true`, `is_real_money=false`, and `require_review`.
The four Nintendo price/merchant-anomaly cases are part of ten total Nintendo
requests; all ten initial checks were `no_match` because this query
has no model digit. Four price/merchant anomalies therefore lacked actionable
market caution. The absence of a comparison is honest; refresh promotion is
the separate E2E-T3-5 defect.

Raw redacted responses are in `live_results.json`; `metrics.json` contains
counts and latency. `catalogue.json` preserves normalized discovery offers,
retrieval timestamps and exact upstream response hashes; expiring buy links
were removed from that catalogue snapshot. `internal_risk_telemetry.json` is
explicitly **read-only SQLite diagnostic evidence**, not owner-visible API
output: it records reason codes and degraded status missing from `/v1`.

### Precision, recall and review burden

Labels: 35 crafted risky requests, 10 routine legitimate purchase/rental
requests, and five non-scam currency-unknown controls. The last five are not
claims of a verified fair EUR price. The following confusion matrices group
all 15 non-scam controls together; TP/FP/FN/TN are counts.

| Decision proxy | TP | FP | FN | TN | Precision | Recall | Non-scam flag rate |
|---|---:|---:|---:|---:|---:|---:|---:|
| Risk recommendation requires review | 35 | 15 | 0 | 0 | 70.0% | 100% | 100% |
| Risk score at least 50 (diagnostic only) | 20 | 0 | 15 | 15 | 100% | 57.1% | 0% |
| Market warning/high across all scenarios | 16 | 0 | 19 | 15 | 100% | 45.7% | 0% |
| Market warning/high, price/merchant scope only | 16 | 0 | 4 | 15 | 100% | 80.0% | 0% |
| Score at least 50 OR market warning/high (diagnostic) | 32 | 0 | 3 | 15 | 100% | 91.4% | 0% |

Review is an approval gate, **not an accusation of fraud**. The first row is
a burden proxy, not proof of a security defect: each new real retailer incurred
12 points for unapproved merchant and 13 for missing evidence. No routine
purchase was blocked. Raising the review threshold to 50 would lose 15/35 risky
cases in this sample, so these results do not justify weakening that control.

All 50 assessments were degraded because the configured ML artifact was
unavailable; deterministic rules still ran. The owner API did not disclose
that condition (E2E-T3-2). No model or policy configuration was changed.
Five product families, repeated perturbations and a single tester do not form
an independent estimate of population scam-detection performance.

Create latency, excluding provisioning, pacing, replay and refresh:
**p50 218.28 ms; p95 1,315.65 ms** (nearest rank); range 151.01–1,759.23 ms.

### Credits and caching

| Explicit T3 operation | Paid searches | Free product reads |
|---|---:|---:|
| Discovery | 5 | 0 |
| Aborted rate-limited attempt, three query families | 3 | Not included in final-run metrics |
| Completed 50-scenario run | 5 | 45 cache re-queries + 5 owner refreshes |
| Separate live accessory defect probe | 1 | 0 |
| Total explicit T3 paid usage | **14** | At least 50 |

Five scenario responses cite `/v1/search`; the other 45 cite freshly fetched
`/v1/products/…<last4>` evidence. Cache-hit rate for scenario creates is 90%.
Every idempotent replay returned 200 with `Idempotent-Replayed: true` and the
identical snapshot; none triggered a new search. The unchanged baseline and
first final full suites also include two credential-gated Channel3 search smoke
tests each. The final budget-conserving rerun retains those two tests as well:
at most six additional search credits, giving a **20-credit upper bound for
the complete validation history**, below the 60-credit limit. This is call
accounting, not a measured provider account balance or invoice.

## Confirmed defects and exact reproductions

From `agentpal/`, run any named check using:

```sh
python -m pytest -o addopts='' -q -p no:cacheprovider tests/e2e/t3/test_intelligence_acceptance.py --runxfail --tb=short -k <test_name>
```

Each confirmed defect has a strict xfail with its ID. `--runxfail` makes all
six checks fail today at the stated behavioral assertions; all other checks
pass. No product source was mutated to obtain those failures.

### E2E-T3-1 — high — accessory falsely matched as the requested product

**Reproduce:** `test_airpods_ear_tips_cannot_be_high_confidence_earbuds` and
`test_ear_tips_cannot_support_earbuds_overprice_and_deal_claims` use the saved
live AirPods catalogue. For a fresh live proof after the main run, execute
`python tests/e2e/t3/probe_accessory.py` (one paid search).

**Expected:** Ear tips cannot be a high-confidence match for AirPods earbuds;
accessory prices cannot support earbuds market medians or overpricing claims.

**Actual:** Query `Apple AirPods Pro 2`, merchant `apple.com`, amount `250.00 USD`
returned HTTP 201 with product **Airpods Pro 2 Ear Tips**, confidence **high**,
median **9.99 USD**, advisory **high_risk**, and `PRICE_ABOVE_MARKET`. The live
request ends **…5bbd**. `live_accessory_probe.json` contains the exact response;
new runs generate different IDs.

**Cause/minimal fix:** Merchant preference selects the apple.com accessory
because `ear`/`tips` are absent from the accessory exclusion rules. Add
category-aware accessory rejection (including multiword ear tips/ear cushions),
with directional rules so an explicit accessory query still works. Raising
the token overlap threshold alone cannot fix a 100%-overlap accessory title.

### E2E-T3-2 — medium — owner cannot see risk reasons or degraded assessment

**Reproduce:** `test_owner_can_see_risk_reasons_for_prompt_injection` provisions
real identity/wallet/risk/lifecycle services, prepares an injection request,
and reads it as its authenticated owner through `/v1`.

**Expected:** The owner sees the stored risk reason and whether the assessment
is degraded, so a high score is explainable before approving exact terms.

**Actual:** HTTP 201 then owner GET **200**, but `/v1` returns only score and
recommendation for risk. The internal assessment contains
`PROMPT_INJECTION_ATTEMPT`; the response contains no risk reasons. All 50 live
requests similarly omit risk explanation, degraded state and the risk
assessment disclaimer. For example injection requests score 80 while the
market advisory can say `no_concern`, leaving no structured owner-visible
explanation of the separate risk alarm. This is an API information omission,
not proof that the engine failed to detect the injection.

**Minimal fix:** Add sanitized, stored risk codes/messages, degraded status and
the risk disclaimer to the scoped payment response and approval queue.
Read the existing assessment; do not re-assess or alter terms, score or state.
Update the published schema and SDK contract through their owning workstreams.

### E2E-T3-3 — medium — absence of comparable offers labelled no_concern

**Reproduce:** `test_no_comparable_currency_is_unknown_instead_of_no_concern`.
Live `T3-01-fair_currency_unknown` also reproduces this through HTTP 201.

**Expected:** Unknown/no comparison when offers are USD and the request is EUR.
No currency conversion or positive price reassurance.

**Actual:** `market=null`, `better_deals=[]`, signal `NO_COMPARABLE_OFFERS`, but
top-level advisory **no_concern**. Four of five currency controls show this;
the fifth honestly returns no_match. Currency conversion did not occur.

**Minimal fix:** If the matched product has no same-currency, new, in-stock
comparison and no stronger caution signal, return `no_data`/an explicit
insufficient-evidence state, retaining the explanatory signal. Do not change
payment approval or risk as a consequence.

### E2E-T3-4 — medium — unbound variant/range produces high-risk mismatch

**Reproduce:** `test_real_aggregate_price_range_cannot_contradict_its_valid_upper_price`
loads the exact saved Logitech HTML and requests `99.99 USD` through the real
offline KernelPageChecker provider path.

**Expected:** A claim inside the published AggregateOffer range is compatible
unless the selected SKU/offer is independently bound to a contradictory price.
At minimum describe variant ambiguity rather than a confident price mismatch.

**Actual:** The page publishes **lowPrice 89.99 / highPrice 99.99 USD**; the checker
displays 89.99 and emits **PAGE_PRICE_MISMATCH / high**, advisory **high_risk**
for 99.99. Extractors retain `price_low`/`price_high`, but the checker compares
ordinary price facts (including page metadata) without applying those bounds
or binding a selected variant. Evidence: `pages/page_8.html.gz` and
`pages/extraction_results.json`, with the exact HTML SHA-256 in the manifest.

A second real page makes the variant problem explicit: Pimoroni lists Pico 2
offers at **4.8 GBP** and **5.7 GBP**. Claiming 4.8 produces observed price 4.8,
yet a **high** PAGE_PRICE_MISMATCH against the sibling 5.7 offer. The checker
chooses the furthest price across all offers. This separately reproduced
output is in `pages/variant_mismatch_evidence.json`; the same fix must preserve
the matching offer instead of interpreting sibling variants as contradictions.

**Minimal fix:** Preserve offer/variant context and aggregate bounds. Only use
the current >2% warning / >10% high thresholds for a comparable selected offer;
within-range ambiguous variants should not produce a high mismatch allegation.
Keep independent currency, stock and injection cautions.

### E2E-T3-5 — medium — free refresh upgrades uncertain identity to exact

**Reproduce:** `test_free_refresh_cannot_upgrade_ambiguous_search_to_exact_identity`
uses saved Sony offers with nonspecific query `Sony wireless headphones`.
The live `T3-05-fair` initial result is no_match; its authenticated free owner
refresh returns no_concern without new input identifying a SKU. Its request
ends **…5863**. `live_refresh_evidence.json` preserves the **read-only SQLite**
snapshot after the successful HTTP refresh: medium became exact for the same
stored product. It is labelled as database evidence rather than an HTTP log.

**Expected:** Refreshing offers preserves uncertainty about which product the
request intends. Exact identity requires a genuine URL lookup or explicit
product identity, not a product ID selected by an uncertain search.

**Actual:** Initial match confidence **medium**, no price comparison. Calling
the free product read for that internally selected ID changes confidence to
**exact** and enables market/price claims. The checker treats every `product`
endpoint response as exact, conflating refreshed catalogue identity with
identity of the agent's requested item. Live refresh HTTP **200**; financial
authority fields remain unchanged.

**Minimal fix:** Preserve match provenance/confidence with the stored snapshot
and revalidate against the original query on refresh. Only a user-bound product
URL or explicitly reviewed product identity may bypass query matching.

## What worked

- **50/50** real HTTP creates: 201, sandbox/virtual allowance labels, owner
  approval mandatory; SDK reads returned the same stored response.
- **50/50** identical idempotent replays: 200, replay header and same snapshot.
- **5/5** authenticated owner market refreshes: terms/digest, status, risk and
  recommendation unchanged. No automatic retailer switch or approval.
- **50/50** market snapshots: advisory_only true, authoritative false;
  commission disclosure, verification notice and new-approval switch notice
  present; discount_codes_available false; no invented coupon codes.
- Six better-deal offers across two scenario snapshots use the request
  currency, explicit stock and known
  condition. The real backmarket.com unknown-condition offers do not become
  new-condition deals. EUR controls did not compare or convert USD prices.
- Price anomalies were caught for all four families with strong matches;
  merchant warnings likewise appeared for four of five families.
- Purpose injections, urgency and unexplained recurrence increased deterministic
  risk (80, 99, 53 respectively); explained rental stayed at baseline 25.
- Five independently labelled retailer pages passed exact price/currency/stock/
  name extraction: Adafruit Pico ($4 USD, in stock), Pimoroni Pico 2 (£4.80 GBP,
  in stock), BOOX Palma ($199.99 USD, out of stock), Logitech MX Master 3S
  (89.99 USD displayed from metadata/range), Adafruit Qualia (19.9500 USD,
  in stock). Exact saved bytes are compressed losslessly with SHA-256 evidence.
- **5/5** saved retailer pages with appended, explicitly synthetic injection
  text produced `SUSPICIOUS_INSTRUCTIONS_ON_PAGE` and high_risk; original
  extracted price was preserved. These are modified offline captures, not
  claims that the real retailers served injection text.
- Saved Pimoroni GBP page versus USD claim: CURRENCY_MISMATCH, no numeric price
  comparison. A tampered HTML hash yields no_data and no observed claim.
- Real platform page-check route: owner 200 with observed $4, agent 403;
  stored snapshot visible afterward; all financial authority fields unchanged.

## Page fetches and blocked scope

`pages/manifest.json` records nine public httpx GET attempts. Six returned 200;
five are genuine retailer pages used for acceptance. The sixth is the public
Books to Scrape demo: it is supplemental unsupported-markup coverage, **not**
a real retailer or a sixth real-product acceptance claim. Raspberry Pi's two
public pages returned 403 and the selected SparkFun URL returned 404; these
responses were not used as successful product captures. `fetch_pages.py`
repeats the GETs into `/tmp/t3-pages-fresh` without overwriting frozen evidence.

**BLOCKED:** live Kernel capture is blocked by this container's network, per
the assignment and lead verification. No cloud sessions or control-host retries
were attempted. Saved HTTP HTML was injected as BrowserCapture evidence into
the production KernelPageChecker, then into a real platform route. There is
an injected public DNS answer in the offline route fixture; it makes no
additional network call or independent claim about DNS/SSRF protection. There is
no claim of a live browser screenshot, replay, rendered DOM or Kernel egress
verification. Screenshots/replays are null in these fixtures.

**SUSPECTED / not counted as confirmed defects:** Channel3 has fragmented
listings for the same model and titles differing in heatsink/colour/region.
Those facts can affect the representativeness of a median. No independently
verified GTIN/SKU identity was available to prove a particular additional
cross-variant mistake; these observations need investigation, not another
asserted defect. The five-product sample does not justify a new fraud rule.

## Improvements ranked by owner impact

1. Fix accessory/category identity before showing any market-price allegation
   (E2E-T3-1), and preserve confidence across refresh (E2E-T3-5).
2. Expose stored risk reasons and degraded status (E2E-T3-2). Label catalogue
   `no_concern` specifically as market scope, so it cannot visually erase a
   purpose injection alarm. Keep existing deterministic controls.
3. Distinguish missing comparable data from reassuring evidence (E2E-T3-3).
   The fair currency controls directly exercise this owner-facing ambiguity.
4. Bind price comparisons to offers/variants; respect aggregate ranges
   (E2E-T3-4). Retain 2%/10% thresholds for genuinely comparable single prices.
5. Present uncertainty-driven review separately from substantive scam signals.
   Keep owner approval mandatory and retain the current review threshold:
   raising 25 to 50 would miss 15/35 crafted risks in this sample. A future
   owner-maintained merchant allowlist can reduce uncertainty; never auto-trust
   a merchant merely because Channel3 listed it.
6. Add a link to the existing identity bootstrap CLI from DEVELOPER_API.md and
   SDK.md. Bootstrap was functional but required finding the architecture guide;
   this is a documentation improvement, not a counted security defect.

## Verification and delivery

Baseline: `python -m pytest -o addopts='' -q -p no:cacheprovider`:
**2,914 passed / 38 skipped / 0 failed**, 597.01 s. Baseline
`ruff check agentpal training tests`: **clean**.

New offline checks with `--runxfail --tb=short`: **6 failed / 14 passed**;
each failure is at its intended defect assertion. Normal mode keeps six
strict xfails; no inherited assertion or product implementation was edited.

Final T3-only suite: **14 passed / 6 strict xfailed / 0 failed**, 6.11 s.
The required `python -m pytest -q -p no:cacheprovider tests/e2e` returned **0**.
Final `ruff check agentpal tests` and `ruff check agentpal training tests`:
**clean**. A scan of all 23 owned files, including decompressed HTML, found no
configured credential values. The ownership check found only the T3 report
and T3 test/evidence additions.

Validation history is preserved rather than hiding an inherited failure: the
first final full run produced **2,927 passed / 38 skipped / 6 strict xfailed /
1 failed**, 474.01 s. The unchanged
`test_overall_deadline_and_eventual_cleanup[screenshot]` took 1.73 s instead of
less than 0.5 s. An isolated five-case rerun produced **3 passed / 2 failed**:
create/execute timed out within about 0.1 s before their injected handlers were
entered, failing the test's handler-entry assertion. This is a suspected
scheduling/cold-start-sensitive validation issue, not an independently proven
new product defect. No existing test, deadline or assertion was changed.
Running the existing successful-capture test before that five-case group in
the same process produced **6 passed**, 1.30 s. That supports, but does not
prove, a cold-start/scheduling explanation.

The subsequent full rerun removes PayPal sandbox credentials **only from the
test subprocess environment**, so inherited live money-movement checks take
their existing credential skips instead of spending another set of orders.
It retains Channel3 for the inherited live search smoke checks. This adds at
most two paid searches to the accounting above: the complete validation
history is bounded by **20 Channel3 search credits**. The live T3 intelligence
acceptance run remains the 50-request, 14-explicit-search evidence described
above; the verification rerun is not a claim of additional live PayPal coverage.

**BLOCKED — final complete-suite acceptance gate.** That full rerun also failed
the unchanged screenshot deadline assertion, now **2.25 s** versus less than
0.5 s: **2,913 passed / 52 skipped / 6 strict xfailed / 1 failed**, 459.53 s.
The 14 extra skips reflect the deliberately absent sandbox credentials.
T3 cannot fix the unowned Kernel implementation or edit its existing assertion.
The two full-run failures and mixed isolation outcomes need lead investigation;
neither a clean final full suite nor a proven root cause is claimed.
Running T3 together with the entire existing Kernel browser module produced
**119 passed / 6 strict xfailed / 0 failed**, 7.86 s. Thus the full-suite
failure is not reproduced by simply placing the T3 tests before that module.

The 20 T3 cases were also run with both HTTPX network transports replaced by
raising guards: **14 passed / 6 strict xfailed**, confirming that these saved
data regressions make no external HTTP requests.

Delivery is the verified `/tmp/m7-t3.bundle`, branch `m7-t3-review`, based on
the original commit above. The first two commits are `41d9023` (locked scenarios,
live evidence and fixtures) and `57c2c4f` (offline regressions and report); the
final validation commit records the complete-suite blocker. Shared Git
metadata was not written; the review repository is `/tmp/m7-t3-review`.

Reproduction commands from `agentpal/`:

```sh
python -m pytest -q -p no:cacheprovider tests/e2e
python -m pytest -o addopts='' -q -p no:cacheprovider tests/e2e/t3 --runxfail --tb=short
ruff check agentpal training tests
# Live: reserve fresh /tmp/e2e-t3 databases; exported Channel3 credential required.
python tests/e2e/t3/run_acceptance.py
python tests/e2e/t3/probe_accessory.py
python tests/e2e/t3/fetch_pages.py --out /tmp/t3-pages-fresh
```

**Requests to lead:** Review/apply the five product fixes in intelligence,
platform response models, and generated API/SDK contracts, all outside T3
ownership. Investigate the repeated full-suite Kernel screenshot deadline
failure before declaring the acceptance gate green; preserve the existing
security assertion while diagnosing the cause. Append the following short
entry to the shared team memory if
desired; the later testing assignment prohibits modifying that shared doc:

> 2026-11-03 T3 — intelligence acceptance: 50 live labelled requests; five
> confirmed decision-quality defects, six strict offline xfails; 14 explicit
> Channel3 searches, no explicit T3 PayPal operations; live Kernel blocked.
> Keep approval/risk/terms isolation; do not raise review thresholds from this
> small correlated sample. Review `docs/testing/E2E_T3.md` and T3 bundle.
