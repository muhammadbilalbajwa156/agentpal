# H3 — upstream trust boundaries and supply-chain verification

2026-11-05. Start: `ab24447fd1af6013e5403264a1993c3bcac5c9e9`.

**IMPLEMENTED — TESTED:** 123 new offline/local cases: 113 pass and 10 strict
xfail cases reproduce three confirmed findings. No critical/high financial bypass
was confirmed. No product code or inherited test was edited. Findings remain
unfixed for the lead's review.

| ID | Severity | Status | Reproducible result |
| --- | --- | --- | --- |
| E2E-H3-1 | Medium | CONFIRMED; reproduction IMPLEMENTED — TESTED | Channel3 owner-facing deals retain unsafe/misattributed buy URLs under a claimed retailer name (7 cases). |
| E2E-H3-2 | Low | CONFIRMED; reproduction IMPLEMENTED — TESTED | An unhashable upstream PayPal link relation raises TypeError and returns HTTP 500 (1 case); hold remains intact. |
| E2E-H3-3 | Low | CONFIRMED; reproduction IMPLEMENTED — TESTED | Webhook certificate and postback HTTP responses are buffered without an enforced streaming byte cap (2 cases). |

## Scope and method

Only the H3-owned paths are added: `tests/e2e/h3/` and this report. Financial
integration tests use the real platform, authenticated identities, exact-term
approvals, deterministic policy, SQLite stores and lifecycle. Only provider HTTP
is replaced with `httpx.MockTransport`. Browser tests use an injected capture or
the **exact production CAPTURE_JS**, executed in Node with controlled browser/DNS
I/O; no network fetch occurs in that harness. Mock request records are assertions
about simulated provider traffic, not live PayPal evidence.

All H3 databases are private temporary directories under `/tmp/m8-h3` and are
removed after use. The one TCP smoke starts its own
`python -m agentpal serve --platform --host 127.0.0.1 --port 8203`, bootstraps its
own owner with `python -m agentpal new-owner h3_cli_owner`, and removes service
credentials from its child environment. Credentials remain in memory and command
output is withheld. It refuses to reuse an occupied port. H3 made **zero live
PayPal/Channel3/Kernel calls**, zero searches, zero orders, and no package-install
or advisory-service calls.

Read context: README, PROJECT_CONTEXT, PROJECT_MEMORY, DEV_INFRA_PLAN, the prior
acceptance summary/T1–T4 reports, DEVELOPER_API, SDK, SECURITY_AUDIT, PAYPAL_*
documents, KERNEL_INTEGRATION, CHANNEL3_INTEGRATION and the checked-in OpenAPI
operations/schemas. No `docs/handover/` directory exists. Historical docs have
stale implementation counts/limitations; assertions here use the owning source
at the start commit and actual executions.

## Confirmed findings and manual injected reproductions

### E2E-H3-1 — unsafe Channel3 links and false retailer attribution

**Medium; CONFIRMED; IMPLEMENTED — TESTED (reproduction).**

Owning code: `tools/channel3.py:_offer` accepts `domain` and `url` independently;
`intelligence/market_check.py:build_market_check` copies `o.url` directly to
`better_deals[].buy_url`; the API output model accepts a plain string. This
survives the real payment preparation and authenticated owner GET.

Exact offline/manual sequence:

1. Build the real platform using `tests/e2e/h3/conftest.py:h3_world`, with its
   private owner, external agent and USD 10 wallet, USD 5 per-transaction limit.
2. Inject `MarketChecker(Channel3Client(..., client=httpx.Client(transport=
   httpx.MockTransport(handler))))` into `deps.market_checker`. Have the handler
   return this search result (no upstream disclosure is needed):

   ```json
   {"products":[{"id":"h3-widget-7","title":"Acme Widget 7",
     "brands":[{"name":"Acme"}],"offers":[{"domain":"retailer.example",
     "price":{"price":"0.50","currency":"USD"},"condition":"new",
     "availability":"InStock","url":"https://retailer.example.evil.example/widget"}]}]}
   ```

3. As the agent, `POST /v1/payment-requests`, header
   `Idempotency-Key: h3-prepare`, with `amount: "5.00"`, `currency: "USD"`,
   `merchant_name: "H3 store"`, `merchant_domain: "example.com"`,
   `merchant_paypal_email: "merchant@example.com"`,
   `purpose: "H3 offline boundary verification"`,
   `product_query: "Acme Widget 7"`.
4. As its authenticated owner, `GET /v1/payment-requests/{returned_request_id}`.
   Actual: `market_check.better_deals[0].retailer` is `retailer.example`, while
   `buy_url` still names `retailer.example.evil.example`.
5. Repeat using `javascript:alert(1)`, `data:text/html,...`, `file:///etc/passwd`,
   `https://user:password@retailer.example/widget`,
   `https://127.0.0.1/private`, or a URL containing a newline: each reaches the
   owner's actionable buy URL unchanged.

Automated exact reproduction:

```sh
cd agentpal
python -m pytest -o addopts='' -q -p no:cacheprovider tests/e2e/h3/test_market_boundaries.py \
  --runxfail -k untrusted_deal_url
# 7 failures, at the final unsafe buy_url assertion
```

Expected: omit unsafe links/offers, or expose a validated destination with honest
retailer attribution. Actual: seven unsafe/misattributed links are retained.
Original payment terms/digest and awaiting-owner-approval state **do remain
unchanged**, and the local commission disclosure **is present**.

CVSS-like reasoning: medium integrity/phishing exposure if a malicious or
corrupted catalogue entry reaches an owner who follows the link. Exploitation
needs upstream influence and owner interaction. There is no frontend in scope:
this is **not** proof of JavaScript execution, server-side SSRF, credential theft,
or a financial approval bypass. JSON text itself is data.

Minimal suggested fix: validate offer URLs before emitting a buy link: supported
HTTP(S) scheme, bounded normalized URL, no controls/userinfo, standard port,
public-style host and domain-label comparison to the declared retailer. For
legitimate affiliate redirectors, use an explicit reviewed attribution rule and
identify the destination as unverified rather than trusting arbitrary hrefs.
Never fetch an arbitrary offer URL while validating it.

### E2E-H3-2 — malformed PayPal link relation crashes the request handler

**Low; CONFIRMED; IMPLEMENTED — TESTED (reproduction).**

Owning code: `providers/paypal_sandbox.py:PayPalSandboxProvider._payer_link` builds
a dictionary keyed by an unvalidated `link.get("rel")`. A JSON list is unhashable.

Exact offline/manual sequence:

1. In `h3_world`, prepare and authenticate owner approval of a USD 1 payment,
   with the default exact merchant/payee/purpose terms from that fixture.
2. Replace only the MockTransport create-order response with HTTP 201 and:

   ```json
   {"id":"ORDER-H3","links":[{"rel":[],"href":"https://evil.example"}]}
   ```

3. As the recorded agent, `POST /v1/payment-requests/{id}/execute`.
   Actual: `_payer_link` raises `TypeError`; lifecycle quarantines the attempt
   and rethrows; the generic API exception handler returns
   HTTP 500 `{"error":"internal_error"}`.
4. `GET /v1/health` still returns 200. Duplicate execute returns 409. Ledger state
   is `reconciliation_required`, no payment outcome exists, and only one
   create-order operation was sent. This is a **request failure**, not process
   termination or a money bypass.

```sh
cd agentpal
python -m pytest -o addopts='' -q -p no:cacheprovider tests/e2e/h3/test_paypal_boundaries.py \
  --runxfail -k malformed_create
# 1 failure: 500 not in (200, 502, 503), after the hold/replay/health assertions pass
```

CVSS-like reasoning: low availability impact; requires malformed authenticated
upstream data, affects one execution response and leaves the server responsive.
No secret/body is reflected, hold released or operation retried.

Minimal suggested fix: accept only string allowlisted relations before building
link mappings, and map malformed provider representations to a sanitized
`ProviderError(outcome_unknown=True)`. Preserve quarantine and the one-shot claim.

### E2E-H3-3 — verification HTTP bodies are not bounded while streaming

**Low; CONFIRMED; IMPLEMENTED — TESTED (reproduction).**

Owning code: `providers/paypal_webhooks.py:PayPalWebhookVerifier._certificate`
uses `.get()` (which buffers the response) before checking `MAX_CERT_BYTES`.
`_postback` similarly calls `.post()` and `.json()` with no response byte cap.
The regular Orders/OAuth/payout/refund adapters have independent streaming limits;
those do not protect these verifier HTTP calls.

Exact offline/manual certificate sequence:

1. Make a provider with configured fake webhook ID using the existing test helper;
   generate a signing key/certificate entirely in memory and sign a small capture
   event using `signed_headers`.
2. For its pinned `CERT_URL`, inject HTTP 200 with `Stream([b'x' * (65536 + 1),
   b'must-not-read-tail'])`. Count iterator reads; this is only about 64 KiB.
3. Call `provider.verify_webhook(headers, raw)`. It ultimately raises
   `WebhookVerificationError`, but **both** chunks have already been consumed.
   A streaming cap must stop before the tail. Certificate rejection still holds.

Exact offline/manual postback sequence:

1. Use `make_provider(webhook_verification='postback')`, with mocked OAuth.
2. Return three chunks from `/v1/notifications/verify-webhook-signature`:
   `{"verification_status":"SUCCESS","padding":"`, 1,048,577 `x` bytes, and
   `"}`. No real credential or external endpoint is involved.
3. Call `verify_webhook` with the signed small event. Actual: all chunks are read
   and the oversized SUCCESS is accepted as a verified event. The regression
   expects bounded rejection. It does not pass this event to a financial sink.

```sh
cd agentpal
python -m pytest -o addopts='' -q -p no:cacheprovider \
  tests/e2e/h3/test_provider_resource_bounds.py --runxfail \
  -k 'certificate_size or postback_verification_body'
# 2 failures: stream.reads is 2, and oversized postback was not refused
```

CVSS-like reasoning: low robustness/availability risk under a faulty or compromised
trusted upstream, since response size can grow independently of the incoming
webhook body. Arbitrary attacker certificate hosts remain blocked. We did not
attempt memory exhaustion, a slow-drip load or a process kill; those impacts are
potential, not measured. Postback success still comes from the authenticated
configured API; no signature forgery was proved.

Minimal suggested fix: stream and cap decoded certificate data at 64 KiB and
verification JSON at a small explicit bound; close at the first excess byte,
reject redirects, preserve timeouts and return constant verification errors.
Do not catch a bounds failure and fall back to unverified acceptance.

## Guards that held

All rows below: **IMPLEMENTED — TESTED** for the specified offline/local evidence.
These are regression observations, not new product fixes or exhaustive proofs.

| Guard | Evidence in `tests/e2e/h3/` |
| --- | --- |
| Unsigned/wrong signatures, wrong configured webhook ID, stale/future timestamps, attacker/userinfo/lookalike certificate hosts, self-signed/expired/wrong-SAN chains and duplicate PayPal headers cannot mutate state | 12 parametrized real-API rejection cases in `test_paypal_boundaries.py`; HTTP 400, pending ledger, no provider event/outcome; rejected origins trigger no cert fetch. |
| Duplicate/replayed/out-of-order verified deliveries settle once | Three identical signed completed deliveries, followed by signed pending and order-approved events; final completed, three stored event IDs, one outcome, zero capture POSTs. |
| Capture evidence must bind exact payee, merchant ID, request references, single unit, gross amount, currency, capture ID and status | 12 substitutions including unusual Unicode; real owner capture goes to reconciliation; duplicate capture 409, one POST, USD 1 retained, no outcome. |
| Bad/oversized/unknown/nested create outcomes never double-submit or free exposure | Six invalid-success cases, including a >1 MiB body and a roughly 12 KiB/6,000-level JSON body without an order identity; one create, replay 409, USD 1 retained. This tests those shapes, not a universal JSON depth limit. |
| Refund evidence must match approved amount/currency, original capture link, refund ID and eligible status | Five verifier substitutions rejected; five noncanonical money values rejected (negative/nonfinite/exponent/full-width digit/excess precision). |
| Unknown refunds reserve their exact amount without credit or re-POST | Seven real-platform cases: wrong amount/currency/capture/status, oversized/nested body and lost response; first 201/replay 200 remain reconciliation-required, one POST, USD 1 still spent, additional USD 0.61 refund refused. |
| Payout reads bind receiver, amount, currency, sender item, batch, note, item ID, status and page count | Nine provider substitution cases reject; deeply nested create is a controlled unknown error. |
| Unknown payouts cannot double-move money or falsely confirm | Five real-platform cases: wrong receiver/amount/status, response timeout, missing batch ID; two execute calls, one payout POST, USD 1 held, no completed outcome. |
| PayPal payer links refuse non-sandbox, file, userinfo and lookalike destinations | Four cases drop the URL without any HTTP request. Malformed link object shapes are separately E2E-H3-2. |
| Page instructions are data and cannot grant consent | Four real-platform checks: visible instructions, hidden HTML, CSS-hidden content and comments flag suspicious instructions; digest/awaiting-approval state unchanged; execute 409; no approval row/provider call. |
| Conflicting prices and excessive page structures do not become an all-clear | Multiple offers, excessive open-element depth, deeply nested JSON-LD and >2,000,000-byte JSON-LD become warning/no_data with advisory-only flags. |
| Unsafe schemes, URL credentials, numeric obfuscations, IPv6-mapped/NAT64/private/mixed DNS are rejected before I/O | Ten netguard URL cases plus three DNS answer shapes. These do not prove protection against a DNS answer changing between check and connect. |
| Private redirects are vetted before fetching their target | Six cases execute the exact production browser program in Node; only the original public URL is fetched; private/file/data/userinfo destinations return blocked_target. |
| Embedded recognizable credentials do not leave the browse API | A generated agent credential hidden in captured HTML yields no_data, no raw HTML and no credential in response. |
| Fake similar products and invalid catalogue prices do not become better deals | Four accessory/model/variant mismatches; seven negative/huge/nonfinite/overprecise/bool prices; no price comparison/deal. |
| Lookalike catalogue domains do not become the claimed merchant; local disclosures survive missing upstream disclosures | Two domains remain distinct and produce MERCHANT_NOT_IN_MARKET; constant commission, verification and new-approval notices are present. This is not a universal retailer reputation check. |
| Application-owned log formatter redacts secrets and contains newline injection | Fake configured client secret, generated-format owner token and Authorization extra are redacted; a newline remains inside one JSON log record. External logging handlers are outside this assertion. |
| Real CLI bootstrap works on the owned port; forged JSON is not a webhook signature | `test_local_cli.py`: own new-owner/serve, register agent 201, agent self-read 200, unsigned forged webhook 400, health 200. |

Certificate verification uses a frozen offline PKI/freshness clock; financial
approval and authenticated order-read clocks remain real. No test disables a
financial, authentication, URL or certificate guard.

## Supply chain and repository/history secret inventory

**IMPLEMENTED — TESTED (offline hygiene).** `test_supply_chain.py` verifies all
24 runtime lock entries use exact versions and SHA-256 hashes, every direct
runtime dependency is covered at a compatible version, and the Postman/APIMatic
npm locks use HTTPS registry URLs and integrity fields. There are 148 Postman
and 611 APIMatic package records; in-bundle nested npm records inherit their
parent archive's integrity rather than having separate resolved URLs.

**DESIGN ONLY — follow-up hardening:** `requirements-train.lock` is a 61-pin
historical installed-environment inventory without hashes. CI first installs
unlocked development requirements/tools and uses mutable action version tags,
then tests a separate hash-locked runtime installation. This does not make its
initial test environment fully reproducible. Neither observation proves a
malicious dependency or a current exploitable advisory. Training/model files
were not changed. A fresh vulnerability-database audit is **BLOCKED in this
no-third-party-test-traffic round**; no claim of zero known vulnerabilities is made.

Run the non-network scanner:

```sh
python agentpal/tests/e2e/h3/audit_repository.py
```

It scans tracked bytes and **`git log --all -p`**, including historical deleted
lines, for private-key headers, recognizable GitHub/AWS/Slack/AgentPal formats,
long literal secret assignments and exact configured credential values. It emits
only file/line or commit/type/name metadata, never matched lines or credentials.
The final review scan covers 647 tracked files and five configured values.

Redacted triage:

| Match class | Location | Assessment |
| --- | --- | --- |
| Long literal access-token assignment | `tests/test_provider_paypal_support.py:21`; history commit `e141cfb47da6` | Existing explicit offline fake fixture, not a live token; value withheld. |
| Configured buyer email exact match | Public sandbox personas JSON; history commit `99ee911637a2` | Intentional sandbox persona identifier, not its password; value withheld. |
| Private keys, recognizable provider/AgentPal credentials, configured live secrets/passwords/keys | No additional source/history match | No detected secret in the scanned forms. |

This is a bounded-format/content inventory, **not proof that every possible
unknown secret is absent**. Old unrecognized/rotated credentials may evade exact
current-value matching. No repository history was rewritten. Scan results are
at `/tmp/m8-h3/repository-final.json`; raw git diffs were consumed in memory.

## Suspected boundary and limits

**SUSPECTED — IMPLEMENTED — UNVERIFIED: Kernel DNS check/connect rebinding.**
The fixed program resolves a hostname with `dns.lookup` inside `publicURL`, then
calls `route.fetch` with the original URL. It does not pin the checked IP to that
connection. A changing DNS answer could therefore reach a private address unless
Kernel's independent egress policy prevents it. This is also a documented K1
limitation. Our tests prove private/mixed answers and unsafe redirects are
blocked; they do **not** prove an actual DNS flip in the cloud browser. No live
rebinding attempt or private-network request was made. Potential SSRF impact
would be high if demonstrated, but it is not counted as a confirmed finding and
has no misleading fail-today xfail. Lead should verify provider-enforced egress
or implement connection-level pinning while preserving TLS hostname validation.

**BLOCKED — live signed webhooks and live Kernel behavior:** no public webhook
listener or Kernel session was created in this offline task. PKI/signature,
local lifecycle and fixed-JS evidence does not establish real subscription
compatibility or cloud egress behavior.

## Verification and delivery

- Required untouched baseline, from `agentpal/`:
  `python -m pytest -o addopts='' -q -p no:cacheprovider`:
  **2,984 passed / 42 skipped / 9 failed / 46 errors**, 490.03 seconds.
  All failures/errors were in inherited T2/T4 TCP acceptance harnesses, including
  invalid_token during bootstrap, occupied ports, transport interruption and a
  wallet table absent after the failed server setup. These preceded H3 changes.
- Baseline `ruff check agentpal training tests`: **passed**.
- Final H3 gate:
  `python -m pytest -o addopts='' -q -p no:cacheprovider tests/e2e/h3`:
  **113 passed / 10 strict xfailed**, 24.12 seconds, no failures/errors. All xfails belong to
  this workstream, use the corresponding exact E2E-H3 ID and restrict expected
  exceptions to AssertionError. No inherited assertion changed.
- Delivery checkout `/tmp/m8-h3-review/agentpal` independently reran the owned
  suite: **113 passed / 10 strict xfailed**, 51.22 seconds; both required Ruff
  commands also passed there.
- Fail-first, unchanged product, `--runxfail` selecting the three findings:
  **10 failed / 113 deselected**, 2.37 seconds. Failures are at the intended
  unsafe URL, HTTP status, stream consumption and oversized acceptance assertions.
- Final broader gate: **3,068 passed / 56 skipped / 10 strict xfailed /
  3 failed**, 370.261 seconds; zero collection/setup errors. Command:
  `python -m pytest -q -p no:cacheprovider --ignore=tests/e2e/t2
  --basetemp=/tmp/m8-h3/pytest-after --junitxml=/tmp/m8-h3/after.xml --tb=short`.
  It removes PayPal/Channel3/Kernel credentials from the test child only and
  excludes 67 inherited T2 cases because port 8102 is occupied by an unowned
  listener. All H3 cases passed or strictly xfailed. Remaining failures are
  inherited T4 `test_newer_wallet_schema_refuses_startup_without_rewriting_it`
  (missing wallet_meta), `test_corrupt_ledger_fails_closed_on_restart`
  (bootstrap invalid_token) and `test_sqlite_integrity_on_persistent_files`
  (server disconnect). These are unresolved acceptance-harness/startup failures,
  not H3 finding reproductions. Their exact root causes were not established.
  The requested complete green full-suite gate remains **BLOCKED**; excluded
  tests are not represented as passes. H3 did not stop any unowned listener.
- Final required `ruff check agentpal tests` and
  `ruff check agentpal training tests`: **passed**.
- Evidence: `/tmp/m8-h3/focused-final.xml`, `fail-first.xml`, `fail-first.log`,
  `after.xml`, `after.log`, `repository-final.json`.

Shared Git metadata refuses `index.lock` with read-only filesystem, so delivery
uses an isolated local review checkout, `/tmp/m8-h3-review`, branch
`m8-h3-review`, based exactly on the start commit. Test commits:
`5877805` (PayPal evidence and one-shot operations),
`e999b69` (advisory/browser/CLI/supply-chain verification), followed by the report
and deterministic webhook-test-clock commit at the branch tip.
Bundle: `/tmp/m8-h3.bundle`; `git bundle verify /tmp/m8-h3.bundle` **verified**.
The bundle requires the start commit already present in the lead repository.
No push, PR, rebase, amend, force operation, inherited file edit or model change.

Files added (11): this report; `tests/e2e/h3/__init__.py`, `conftest.py`,
`audit_repository.py`, `test_browser_boundaries.py`, `test_local_cli.py`,
`test_market_boundaries.py`, `test_paypal_boundaries.py`,
`test_provider_resource_bounds.py`, `test_refund_payout_replays.py`,
`test_supply_chain.py`.

Requests to the lead: fix the three owning product boundaries above; investigate
and safely isolate the inherited T2/T4 TCP harnesses before claiming a green full
suite; validate Kernel egress/pinning; review training/dev dependency hygiene.
The later hard scope permits only additions in the two H3-owned paths, so the
shared team memory was **not edited**. Suggested entry for the lead to append:

```md
### 2026-11-05 H3 — offline upstream trust-boundary verification
- Done: 123 local/offline cases, 113 pass / 10 strict xfail; review commits
  5877805, e999b69 plus report tip; docs/testing/E2E_H3.md; m8-h3-review bundle.
- Decisions/traps: unsafe Channel3 buy links (medium), malformed PayPal rel
  request 500 and unbounded verifier responses (low); financial holds/replays held.
  Full gate blocked by inherited TCP acceptance failures; Kernel rebinding unverified.
- Next: lead reviews/fixes three boundaries, resolves TCP isolation and validates egress.
```
