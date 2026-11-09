# PayPal Sandbox payment completion

**IMPLEMENTED — TESTED OFFLINE.** Backend only, sandbox only, no real money.
Live OAuth, buyer-approved capture and app webhook delivery are **BLOCKED BY
CREDENTIALS** in this worktree. Mocked results below are not live PayPal results.

## Official references

Checked 2026-10-31:

- OAuth: https://developer.paypal.com/api/rest/authentication/
- Orders flow (capture follows buyer approval): https://developer.paypal.com/api/rest/integration/orders-api
- Orders v2 schema: https://developer.paypal.com/api/orders/v2/schema.json
- Operation-specific idempotency: https://developer.paypal.com/api/rest/reference/idempotency/
- Webhook signatures, raw bytes, registration and retries: https://developer.paypal.com/api/rest/webhooks/rest/
- Event names: https://developer.paypal.com/api/rest/webhooks/event-names/
- Certificate path validation: https://cryptography.io/en/42.0.8/x509/verification/
- Issuer certificate repository: https://knowledge.digicert.com/general-information/digicert-trusted-root-authority-certificates

The PayPal guide requires a public HTTPS listener on port 443 and documents
non-2xx delivery retries up to 25 times over three days. Its simulator events use
`WEBHOOK_ID`; they do not prove delivery for an actual app. AgentPal refuses that
placeholder as its configured subscription ID.

## Configuration

All credentials come from environment secrets; never put them in source, command
arguments, demo output, collections or committed environment files.

| Variable | Meaning |
|---|---|
| `PAYPAL_SANDBOX_CLIENT_ID` | Sandbox REST app client ID |
| `PAYPAL_SANDBOX_CLIENT_SECRET` | Sandbox REST app secret |
| `PAYPAL_SANDBOX_SECRET` | Accepted alias for the same secret |
| `PAYPAL_SANDBOX_WEBHOOK_ID` | Registered sandbox app webhook subscription ID; required for webhook verification |
| `PAYPAL_SANDBOX_SIGNING_IDENTITY` | Expected literal signing-certificate DNS SAN; default `messageverificationcerts.sandbox.paypal.com` |
| `PAYPAL_SANDBOX_CERT_INTERMEDIATES_FILE` | Optional local PEM bundle of additional **untrusted intermediates**, for certificate rotation |

Either secret spelling works. Blank values are treated as absent; equal nonempty
values work; conflicting nonempty values fail closed without displaying either.
Partial sandbox configuration fails closed rather than selecting the simulator.
The normal platform factory selects its existing local simulator when no sandbox
settings exist; the demo's default mode explicitly uses a mocked PayPal adapter.

The API base is fixed to `https://api-m.sandbox.paypal.com`; no environment base
URL override exists. OAuth, order, capture, status, certificate and postback calls
explicitly refuse redirects even with a redirect-following injected HTTP client.
A payer approval URL must be HTTPS on `sandbox.paypal.com` or a subdomain, on
port 443/default, without userinfo, whitespace or controls. Production/lookalike
links are discarded. This URL is a provider reference, never proof of approval.

## HTTP payment lifecycle

1. The external agent creates a payment request with its agent key and
   `Idempotency-Key`. Deterministic policy and the virtual allowance hold apply.
2. An authenticated owner approves the exact `terms_sha256`. The approval is
   time-limited and single-use; an agent cannot approve itself.
3. The agent calls `POST /v1/payment-requests/{id}/execute`. One transaction consumes
   approval and persists distinct create/capture keys. Provider I/O follows commit.
4. The owner reads `GET /v1/payment-requests/{id}/reconciliation`. It is **local
   only** and returns stored provider references, including the persisted sandbox
   `payer_action_url`. Existing v4 databases gain an additive link table.
5. The buyer opens that URL and approves using a sandbox buyer account.
6. The owner calls `POST /v1/payment-requests/{id}/capture`. An agent credential is
   refused. Ownership is checked before provider I/O; current execution permission,
   agent status and wallet status still apply.
7. Capture first reads the stored PayPal order through authenticated Orders v2.
   The observation must be verified, `approved_by_payer`, and for the exact stored
   order, amount and currency. A webhook/browser claim is insufficient.
8. Before capture I/O, the SQLite write transaction independently rechecks that
   observation against both assessed and ledger amounts/currencies, the bound terms
   digest, consumed owner approval, provider/order, pending state and attempt
   deadline. It accepts a read at most 30 seconds old (up to 5 seconds clock skew),
   records the proof hash and claims the one capture invocation atomically.
9. Only a verified capture/status/webhook outcome with matching terms settles.
   An order-level `COMPLETED` without a single completed capture is unknown.

The capture response uses `ExecutionOut`, with `payment_environment: sandbox` and
`is_real_money: false`. It may report `provider_pending` or
`reconciliation_required`; an HTTP 200 is not by itself payment completion.
An observed already-completed order is reconciled without another capture POST.
An ineligible/replayed capture is 409; wrong credential type is 403; another
owner's or unknown request is 404; missing authentication is 401.

Owner approval expiry is enforced when execution consumes it. After that, the
attempt's reconciliation deadline governs the pending hold; the consumed approval
cannot be reused for another attempt. Browser return parameters, including
`PayerID`, never establish approval.

## Verified status and reconciliation

`GET /v1/payment-requests/{id}` and reconciliation GET read local lifecycle facts.
`POST /v1/payment-requests/{id}/reconciliation` is owner-only and invokes the real
provider status read, then returns `ReconciliationOut`. Its operation ID is
`reconcilePaymentRequest`; capture is `capturePaymentRequest`.

PayPal status reads become verified `ORDER_STATUS_READ` events. Their stable IDs
include order/capture status, capture identity, amount **and currency**. Repeated
reads of the same observation deduplicate. All observations use the same
transactional event sink as webhooks.

Unknown outcomes retain committed allowance. Capture errors, post-send timeouts,
5xx, unexpected redirects, malformed success, conflicting evidence and crashes
require reconciliation; they never cause a blind create/capture retry or release.
A definite create refusal before an order exists retains the existing safe
cancellation behavior. OAuth failures are pre-send failures.

If create may have succeeded but no order ID was recorded, automated
reconciliation cannot discover it by arbitrary browser/caller input. The hold
stays in `reconciliation_required`, with no resubmission; operator investigation
is required. An authenticated status read that remains non-final does not release
that hold or authorize a retry of a claimed operation. Startup recovery also
quarantines pending attempts; it does not invent provider outcomes.

The durable per-operation keys remain unchanged across restarts. The adapter
sends a deterministic `PayPal-Request-Id` derived from operation plus stored key,
so create/capture headers differ across processes. No change was made to that
completed implementation. Do not rely on indefinite provider key retention.

## Webhook trust and delivery

`POST /v1/providers/paypal/webhooks` needs a verified PayPal signature, not a bearer
token. `WEBHOOK_PATH` remains importable from `platform.router` for A3's middleware.
The handler rejects duplicate PayPal headers, preserves raw body bytes and checks
at most 512 KiB. Verification and persistence run in the thread pool.

Self-verification remains the default:

- Require our configured subscription ID, required headers and `SHA256withRSA`.
- Require transmission time within plus/minus 300 seconds (AgentPal policy).
- Require an HTTPS certificate URL at an exact sandbox API host and certificate
  path; no redirects, userinfo, nonstandard port, query or fragment.
- Read at most the existing 64 KiB certificate response limit and build a path to
  **certifi.where() trust anchors** with cryptography's verifier. Host OS trust
  paths and environment SSL-root overrides are not used for this signing path.
- Downloaded, bundled and operator-supplied intermediates are **untrusted chain
  material only**. Nothing fetches AIA URLs; a supplied root never becomes trusted.
- Validate chain signatures, CA constraints, dates and usage, an RSA key of at
  least 2048 bits, digital-signature usage, and the literal configured DNS SAN.
- Verify the RSA PKCS#1 v1.5 SHA-256 signature over
  `transmission_id|transmission_time|configured_webhook_id|decimal_crc32(raw_body)`.
- Only then parse `ProviderEvent(verified=True)` and pass it to the event sink.

The signing identity pin is **AgentPal policy, not a PayPal guarantee**. It is
separate from the certificate-download API hostname. A SAN rotation/mismatch or
missing chain fails closed; recover legitimate outcomes using authenticated
status reconciliation. Configure a deliberate identity/intermediate update after
review, rather than disabling verification.

The certificate cache holds at most 32 URL entries. Cached chain material is
revalidated at each delivery time, including intermediate expiry. The code works
with the declared cryptography 42.0.0 minimum and the installed version.

Optional `webhook_verification="postback"` retains the existing alternative:
PayPal's authenticated sandbox verification API must return `SUCCESS`, with raw
event bytes preserved. PayPal performs signature verification in that mode; no
local certificate is downloaded. URL/freshness checks and sandbox/no-redirect
restrictions still apply. There is no fallback from failed self-verification to
postback, and no unverified event is accepted.

Delivery outcomes:

| Situation | Response/financial behavior |
|---|---|
| Invalid signature/header/chain/freshness | 400; never sent to the sink |
| Signature-valid unknown order | Audited 503 `webhook_retry_required`; retry after order persistence |
| Persistence failure | 503 `webhook_retry_required`; transaction rolls back |
| Valid duplicate | 200; no second settlement |
| Amount/currency mismatch or conflict | Sink refuses/quarantines and audits; never a false completion |

Unknown-order event retention is globally capped at 1000 rows, including linked
capture metadata. Eviction preserves known financial evidence and append-only
audit records. An evicted event can be retried once the order exists.

A3 owns public exposure: default-off, exact HTTP POST path only, HTTPS, configured
Host validation, shared peer rate limits and a body cap of the smaller of the
operator limit and 512 KiB. Other paths/methods remain protected. This A1 branch
does not change `api.py`; public reachability requires A3 integration.

## Bundled public intermediate

`agentpal/providers/certs/paypal_sandbox_intermediates.pem` contains the public
DigiCert SHA2 Extended Validation Server CA certificate, downloaded over HTTPS:

https://cacerts.digicert.com/DigiCertSHA2ExtendedValidationServerCA.crt

DER SHA-256:
`403e062a2653059113285baf80a0d4ae422c848c9f78fad01fc94bc5b87fef1a`.
Valid until 2028-10-22 12:00 UTC. This is not a trust anchor. PayPal's documented
example certificate chains through that issuer, but its example leaf expired in
2025. Fetching that example is not verification of the app's current live chain.
The lead registers this package data and `agentpal.demo` in `pyproject.toml`.

**Added 2026-11-06 (live finding).** PayPal rotated the sandbox webhook signing
certificate on 2026-10-21 (`CERT-360caa42-fca2a594-9eaebfdf`, valid to 2027-05-07). It is
issued by **DigiCert EV RSA CA G2**, which chains to DigiCert Global Root G2 (in certifi).
With only the old intermediate bundled, every real delivery to the hosted service failed
chain validation. The bundle now also holds that public intermediate, downloaded from
https://cacerts.digicert.com/DigiCertEVRSACAG2.crt, DER SHA-256
`9588ef74199e45acefcccfc0c47010e9f2a37a1dd44c61a4e1c6b334da5af614`, valid to
2030-07-02. It is still only a path-building hint, never a trust anchor. A regression
test validates PayPal's real current leaf (`tests/fixtures/paypal/`) against it.

Verified PayPal events whose type AgentPal does not consume (for example
`PAYMENT.PAYOUTS-ITEM.SUCCEEDED`) are acknowledged `{"received": true, "status":
"ignored"}` with nothing applied, so PayPal stops retrying them. Unverified deliveries
of any type are still rejected with 400.

## Reproducible demo and tests

From `agentpal/`:

```sh
python -m agentpal.demo.paypal_flow
python -m agentpal.demo.paypal_flow --simulate-capture-timeout
python -m agentpal.demo.paypal_flow --live
python -m pytest -o addopts="" -q -p no:cacheprovider
ruff check agentpal training tests
```

The default command stays offline even when sandbox variables exist. Only PayPal
HTTP is mocked; the real API, owner/agent credentials, policy, approvals, ledger,
lifecycle, adapter and certificate/signature verifier run. Demo private keys and
credentials are ephemeral and never written. Its private trust CA is injected
only in explicit offline code; live mode always uses certifi.

Observed offline CLI results: one capture POST, one verified outcome, duplicate
signed webhook responses `[200, 200]`, final `provider_confirmed`. The injected
lost-response scenario first reports `reconciliation_required`, then recovers by
verified status read with no second capture POST. Output labels these results
`TESTED OFFLINE` and `offline_mocked_paypal`.

Live mode is explicit. It requires sandbox environment credentials, displays the
sandbox payer URL and waits for the operator to type `APPROVED` after the buyer
step. That text only requests a provider read; it never establishes approval.
Missing/invalid configuration returns exit 2 and `BLOCKED BY CREDENTIALS`.
Actual live webhook delivery requires a registered public HTTPS listener and
subscription ID; the demo never fabricates it.

Offline regression files cover provider configuration/orders/redirects/trust,
`test_lifecycle_capture.py`, `test_lifecycle_execution.py`,
`test_lifecycle_events_bounded.py` and `test_paypal_flow_{api,recovery,demo}.py`.
Fail-first evidence and validation commands are recorded in the implementation
commit bodies. Existing test assertions were preserved.

## Remaining limits

- **BLOCKED BY CREDENTIALS:** real OAuth, order creation, approved capture and app
  webhooks. Successful capture also needs a sandbox buyer action; actual delivery
  needs A3's HTTPS exposure and app webhook registration.
- **IMPLEMENTED — UNVERIFIED LIVE:** signing-identity compatibility and real
  refund/reversal payload shapes. Partial refunds remain refused for full-budget
  reversal; authorize-then-capture, initiated refunds, disputes and payouts are
  outside this workstream.
- The lead/A2 must update the old exact route/operation maps and regenerate the
  OpenAPI/Postman artifacts. A1 does not commit those cross-owned files.
- Sandbox-only work is not a production-readiness claim.

## A1 verification record (2026-10-31)

- Baseline full suite: **1872 passed, 38 skipped**; required ruff passed.
- Final full suite: **1950 passed, 38 skipped, 4 failed**. All four failures are
  unchanged cross-owned contracts: the exact route table, the former prohibition
  on any capture route, checked-in OpenAPI drift, and the exact operation-ID map.
  They conflict with the explicitly approved new surface and require lead/A2
  integration; no inherited assertion was changed to hide these failures.
- Focused provider/lifecycle/PayPal-flow group: **336 passed, 3 skipped**.
- Minimum cryptography 42.0.0 config/webhook/certificate group: **121 passed**.
- Required `ruff check agentpal training tests`: **passed**.
- AST comparison of inherited assertions in all five changed baseline test files:
  **zero removed or changed assertions**.

Complete command output is preserved in `/tmp/m3-a1-baseline.log`,
`/tmp/m3-a1-final-full.log`, `/tmp/m3-a1-final-focused.log`,
`/tmp/m3-a1-crypto42-final.log` and `/tmp/m3-a1-final-ruff.log`.
The concrete proposed contract-only patch is `/tmp/m3-a1-contract-update.patch`;
a freshly generated spec for review is `/tmp/m3-a1-openapi.json`. Neither artifact
is committed into cross-owned repository files. Public webhook exposure remains
A3's separate integration responsibility.

## P1 core order completion (2026-11-02)

**IMPLEMENTED — TESTED:** merchant payee binding, exact capture/read evidence,
verified declines, sandbox negative testing and buyer-approval deadlines. Sandbox only;
`is_real_money` remains false. The earlier credential-blocked notes above are historical.

`PaymentCreate.merchant_paypal_email` is optional, ASCII, at most 127 characters,
strictly validated and normalized to lowercase. It is exposed on `PaymentOut` next to
`terms_sha256` (the existing `TermsOut` model is outside P1 ownership). When present it
is part of `ApprovalTerms` and its digest. When absent it is omitted from canonical
terms and from the create-body idempotency digest. The unmodified `43b3d3a` digest for
the existing `make_terms()` fixture is
`091d2a65b0b43c7202c1b2ef02acb3a89f189ed1c011c877e826610f3fcfbc4f`;
the regression test pins that value.

Preparation binds the additional term in a ledger transaction before any approval
can exist; the wallet preparation transaction then saves the same terms/digest.
Bindings cannot be introduced or changed after approval. The additive lifecycle-owned
`paypal_approved_terms` table stores the exact terms, digest, returned merchant email/id
and buyer deadline, so a new lifecycle instance retains the binding. Existing wallet,
approval, execution-key and ledger tables retain their existing controls. Cross-store
crashes fail closed through the existing startup consistency check.

The order sends `purchase_units[0].payee.email_address` when requested. With no explicit
payee the request omits `payee`, letting PayPal select the app account; AgentPal records
the identity returned by the authenticated create response. Every preflight, capture
response and reconciliation read must then match the pinned identity, single purchase
unit, both emitted request references, and exact amount/currency. A matching merchant
id can identify an omitted email, but cannot hide a contradictory email. Missing or
contradictory evidence never settles. Missing capture identity, malformed capture lists,
and an order-level COMPLETED without a completed capture remain unknown. Observation
fingerprints include the identity fields, preventing deduplication from concealing a
changed payee.

A signed capture webhook may omit purchase-unit identity. The lifecycle obtains it by
an authenticated order read before settlement and audits the read hash. If the provider
is unavailable, the read is inexact, or no provider was registered with the lifecycle,
the event requires reconciliation. A restarted service can receive its provider through
`PaymentLifecycleService(..., paypal_provider=provider)`; normal submit/capture/reconcile
also register it. Persisted legacy orders without a binding are deliberately quarantined,
not silently assigned a newly observed recipient.

Capture COMPLETED settles; PENDING retains the hold; DECLINED and FAILED settle as failed.
An authenticated 4xx with exactly one of `INSTRUMENT_DECLINED`, `PAYEE_ACCOUNT_INVALID`,
`PAYEE_ACCOUNT_RESTRICTED`, `PAYEE_ACCOUNT_LOCKED_OR_CLOSED`, `TRANSACTION_REFUSED`, or
`MAX_NUMBER_OF_PAYMENT_ATTEMPTS_EXCEEDED` is definitive. Capture failure and the single
failed outcome are committed with an audit record, releasing the hold once. Stable
`provider_error_code` values are available on owner/agent payment reads. Errors never
include PayPal's raw name, message, issue text outside the allowlist, or debug identifier.
`ORDER_NOT_APPROVED` and `PAYER_ACTION_REQUIRED` keep the request pending and produce
specific 409 codes; their verified no-capture response permits another explicit attempt
using the persisted capture key. `ORDER_ALREADY_CAPTURED`, generic UNPROCESSABLE_ENTITY,
mixed/unrecognized issues, 5xx, and lost responses remain unknown and retain their holds.
Recovery is by verified read, without resubmitting an uncertain capture. Existing
operation-specific PayPal-Request-Id derivation remains unchanged.

`PAYPAL_SANDBOX_NEGATIVE_TESTING=1` enables the default-off provider-level
`mock_response=` parameter. Only allowlisted application codes, the exact sandbox API
base, and create/capture operations can send `PayPal-Mock-Response`. An API body cannot
supply that parameter, and incoming HTTP headers are never forwarded to PayPal. The
live test enables the flag explicitly in its in-process provider configuration. OAuth
and order responses are streamed with a 1 MiB bound; token, amount and evidence strings
are bounded. TLS verification and proxy support remain enabled.

`PAYPAL_SANDBOX_BUYER_APPROVAL_TIMEOUT_S` defaults to 10800 (3 hours), bounded to
60–86400 seconds. An expired capture request first verifies the order and cancels an
uncaptured pending request once. The deadline is checked again after its durable capture
claim and before POST, closing the preflight/claim race. Never-captured cancellations
are reported as `expired`. Maintenance can call
`expire_and_reconcile(now, provider=provider)` to inspect up to 100 due orders. Without
a provider, with an unavailable read, with a pending/uncertain capture, or after startup
has quarantined an unknown attempt, no hold is released on time alone. Wire the provider
into periodic maintenance; local GET endpoints remain reads without provider I/O.

Offline verification uses real owner authentication, approval, policy, wallet, lifecycle
and MockTransport HTTP. The two inherited API/recovery fixture files receive additive
identity fields derived from their submitted requests; no inherited assertion changes.
New tests are `test_paypal_core_boundaries.py` and `test_paypal_core_live.py`.

Fail-first evidence: the initial 54-case file had 39 failures/15 passes on the starting
code. The final 97-case file has 80 failures/17 passes on an isolated checkout of
`43b3d3a`. Additional incremental proofs reproduced the post-claim deadline race (false
provider_confirmed), missing specific create/payer-action codes (2 failures), and malformed
capture identity falsely confirming (2 failures). Commands and test names are recorded
in review commit bodies; local logs are `/tmp/m6-p1-fail-first*.log`.

Live evidence (all $1.00 USD sandbox orders, identifiers redacted): OAuth token reuse;
Northwind payee read `…712U`; unapproved capture `…880L` → order_not_approved; forced
negative capture `…901X` → instrument_declined; bogus payee `…645H` never confirmed;
real owner-approved/buyer-approved forced decline `…583S` → failed and hold released once.
The combined live group passed **6 tests**. The labelled Playwright buyer is independent
of owner approval; only the subsequent authenticated provider read establishes buyer
approval. Its test provider requests PAY_NOW plus a sandbox return URL to avoid the
CONTINUE checkout loop. No browser result is accepted as payment evidence.

Official shape/behavior references:

- [Orders v2 schema](https://developer.paypal.com/api/orders/v2/schema.json)
- [Orders error definitions](https://developer.paypal.com/serversdk/php/api-endpoints/orders/errors)
- [Sandbox negative-testing example](https://github.com/paypaldev/getting-started-with-paypal-sandbox/blob/main/README.md)

Integration requests: the lead regenerates OpenAPI/Postman/APIMatic fingerprints; wires
`paypal_provider` on startup and provider-aware expiry maintenance; applies the separate
`/tmp/m6-p1-lead-demo.patch` to the unowned offline demo; and approves replacement of four
inherited assertions that explicitly require raw `debug_id=d1` in exceptions. P1 preserves
those assertions, so their expected conflict remains visible instead of leaking raw data.

P1 final validation: baseline **2192 passed / 39 skipped**, lint clean. Final full
`python -m pytest -q --tb=short -p no:cacheprovider`: **2285 passed / 38 skipped /
10 failed** (quiet progress counted in `/tmp/m6-p1-final-counts.json`). Focused
core/API/recovery: **123 passed**; live: **6 passed**; required ruff: clean.
The 10 full-suite failures are exactly 3 drift-dependent tests, 3 unowned demo
fixture failures, and 4 preserved raw-debug-text assertions. Applying the proposed
demo patch and regenerating OpenAPI/Postman in an isolated checkout yields
**23 passed** for the affected contract/demo/APIMatic group; no unowned changes are
committed. Startup wiring patch: `/tmp/m6-p1-lead-wiring.patch`.

Mutation controls: **26 caught / 0 survived** on the final rerun. The tested
controls (each disabled and restored) are:

late_capture_deadline, exact_authenticated_order, decline_claim, observation_identity, malformed_capture, capture_identifier, strict_email, legacy_digest, payee_in_digest, outbound_payee, negative_flag, negative_host, negative_payload, binding_on_status, binding_on_preflight, email_check, merchant_id_check, reference_check, custom_check, unit_amount, unit_count, capture_amount, expiry, definitive_decline, ambiguous_decline, size_bound.
