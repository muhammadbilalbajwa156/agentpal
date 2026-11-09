# PayPal Sandbox owner refunds and dispute visibility (P2)

IMPLEMENTED — TESTED: authenticated owner refund routes, bounded PayPal adapter,
concurrent reservations, verified partial/full refunds, exactly-once spending credits,
read reconciliation, dispute filtering and owner SDK methods. Sandbox only;
`is_real_money` is always false.

IMPLEMENTED — TESTED (lead-approved follow-up): `check_consistency` now validates
completed net spending against distinct, bounded verified refund credits and their
matching local claims. Provider observations compare against immutable gross
request amounts. The three approved refund route/method exceptions are explicit.
Every inherited assertion remains unchanged.

BLOCKED — platform startup forwarding: `RiskService.__init__` performs an early
recovery check **without the wallet store**, before `build_platform` supplies it.
The approved claim-matching rule correctly refuses that wallet-less partial-net
check. Direct reopened-store recovery with the explicit wallet passes, including
both cross-DB crash windows. Four real factory restart cases remain failing until
the separate two-file forwarding patch is authorized. See the follow-up section.
Fully reversed payments still preserve gross amount and pass legacy startup.

## API and authorization

| Method/path | Operation ID | Behavior |
|---|---|---|
| POST `/v1/payment-requests/{request_id}/refunds` | `refundPayment` | Owner only, required `Idempotency-Key`; 201 first response, 200 replay |
| GET `/v1/payment-requests/{request_id}/refunds` | `listPaymentRefunds` | Local owner-only list, captured/refunded/reserved totals and payment status |
| POST `/v1/payment-requests/{request_id}/refunds/reconciliation` | `reconcilePaymentRefunds` | Owner-only verified reads; never issues a refund POST |
| GET `/v1/disputes` | `listDisputes` | Owner-only bounded provider page mapped to owned verified captures |

Refund body: `{ "amount": "0.40", "reason": "Sandbox test refund" }`.
Both fields are optional. Omitted amount means the unreserved remainder. Numeric
JSON amounts, nonpositive amounts, unsupported precision, over-refunds, unknown
fields, credentials in supported credential formats and reasons over 255 characters
are refused. Currency comes from the verified capture, never from caller input.

Authentication uses the same router owner dependency as approvals. Agent keys get
403, even for reads or replays. Unknown and another owner's payment both get the
same 404. Refund creation and reconciliation use `require_mfa_for_approval` and
`mfa_satisfied` exactly as owner approval does. MFA is checked even on replay.

The original payment must be `provider_confirmed`. A verified, applied PayPal
completion event must name a capture (an order ID fallback is insufficient), match
the immutable request's gross amount/currency and have a matching verified completed
outcome. Immediately before refund POST, an authenticated capture read must again
match capture ID, gross amount, currency and eligible capture status.

## Persistence and unknown outcomes

Two additive wallet tables record refund claims and append-only refund audits.
The claim contains the owner/session, SHA-256 of the client key and body, exact
amount/currency, original order/capture, and the provider operation key. Neither
the key's plaintext nor the reason is persisted. Claim status starts as
`reconciliation_required` **before** network I/O; there is no stale-claim retry.

`PayPal-Request-Id` is deterministically derived from the JSON tuple
`(request_id, refund key, "refund")`. It is distinct from create/capture keys and
other requests/refunds. Same key/body replays the same refund locally, including
when the provider is disconnected. Changed body with that key returns 409.

SQLite `BEGIN IMMEDIATE` serializes all claims, including independent connections.
COMPLETED, PENDING and unknown refunds reserve their exact amounts; FAILED and
CANCELLED refunds reserve nothing. At most 100 refunds are permitted per payment.
I/O runs outside the claim transaction. A timeout, reset, 5xx, redirect, malformed
or inexact success remains unknown, reserves allowance and never blindly retries.

Only COMPLETED/PENDING/FAILED/CANCELLED evidence with exact amount/currency and
one exact `up` capture reference is accepted. Known refund IDs are also bound on
read. Real sandbox responses use `https://api.sandbox.paypal.com` reference links;
that exact alias and `https://api-m.sandbox.paypal.com` are accepted for comparison.
**No response link is ever followed. Every request still goes to
`https://api-m.sandbox.paypal.com`.** Production, lookalike, userinfo, query and
fragment variants are refused.

Reconciliation reads the known refund ID. When its POST response was lost, it reads
the stored order's bounded `purchase_units[].payments.refunds`, then reads listed
refund IDs and matches our unique `invoice_id`. Missing/ambiguous evidence remains
unknown. No caller can inject a refund ID. Discovery may inspect at most 100 refunds.

The adapter streams at most 256 KiB per response, refuses redirects, bounds lists
and identifiers, uses Decimal exclusively, sets 15-second HTTP timeouts (5-second
connect), and never exposes provider bodies/errors to callers. OAuth uses the
existing provider and configured TLS trust; TLS verification is never disabled.

## Ledger credits and recovery

A verified COMPLETED refund reduces the completed entry's **net counted spending**
in an assessment-store transaction. Original request amount, approval terms/digest,
provider events and verified capture outcomes remain unchanged. Partial refunds
keep state `completed` (`provider_confirmed`) and reduce counted exposure. A full
refund changes state to `reversed`; reversed entries retain gross amount and are
excluded from spending. Consequently all existing allowance/budget calculations
see the returned allowance, rather than just an API-side display adjustment.

The same transaction appends a `verified:paypal_refund` ledger event whose reason is
`paypal_refund_credit:<local refund id>:<exact amount>` and writes the assessment
audit. Its marker is checked before crediting again. The wallet transaction then
persists verified status/provider ID/evidence digest and audit. Lock order is
always wallet first, assessment ledger second, consistent with preparation.

The databases do not commit atomically. A crash after the ledger commit can leave
the refund projection unknown. The durable ledger marker prevents double credit
when an owner reconciles it through provider reads. The offline crash-injection
test proves this boundary. Pending and unknown evidence never credits allowance.

## Disputes

PayPal's default list consists of summaries, which may omit transaction IDs. The
adapter reads at most 20 summaries and fetches missing details through authenticated,
fixed-host GETs, binding each detail to its requested dispute ID. Only a single
`seller_transaction_id` matching the owner's verified capture is projected. Buyer
information, raw details, other disputes, provider links and global counts are
never returned. Up to 1,000 owned requests are considered. `page_complete: false`
explicitly says this bounded first page is not a complete dispute history.

## Original lead requests (approved follow-up implementation)

Items 1–3 below were approved in the 2026-11-03 lead decision and implemented in
`7fbb59e`; the original requirements are retained for review. Item 4 remains the
shared post-merge artifact step. The additional startup dependency is described
in the follow-up section below.

1. `agentpal/lifecycle/recovery.py`, `check_consistency`: preserve the existing
   gross check, but add support for audited net completed entries. Extend its
   ledger/request query with `l.state AS ledger_state`. Where gross and ledger
   amounts differ, accept **only** state `completed`, unchanged currency, positive
   net amount, and `request_amount - ledger_amount` equal to the sum of distinct
   positive refund credits from that payment's ledger events. Require each such
   event's actor to be exactly `verified:paypal_refund`, its reason to match
   `paypal_refund_credit:rfd-[0-9a-f]{32}:<bounded positive decimal>`, and its local
   refund claim to match agent, request, owner, amount and currency. Reject missing,
   duplicate or inconsistent credits. Keep the ordinary equality check for all
   other states. Read ledger markers as the authority across a cross-DB crash;
   a still-unknown wallet projection must be recoverable by verified refund reads.
   Add restart cases for partial credit, missing/forged/duplicate credit and the
   ledger-commit/wallet-rollback window. **Do not simply disable the check.**
2. `agentpal/store.py`, `_apply_event`: its amount comparison currently uses the
   mutable net `ledger_entries.amount`. Fetch the immutable `requests.amount` for
   this agent/request and compare provider observations to that gross value.
   Keep the exact currency, verified-event and capture-identity gates. This prevents
   legitimate later gross capture reads from being refused after a partial credit.
   Refund webhooks must not use an individual partial amount to settle the whole
   payment; integrate cumulative refund accounting before broadening that behavior.
3. `tests/test_api_v1_invariants.py`, the inherited route prohibition: approve and
   add a narrow exception for GET/POST `/v1/payment-requests/{request_id}/refunds`
   and POST `/v1/payment-requests/{request_id}/refunds/reconciliation` before its
   `refund` regex assertion. P2 changed only the explicitly owned route map, not
   this existing assertion. It consequently fails on the approved new routes.
4. Regenerate and commit `docs/api/openapi.json` and `postman/*` after all PayPal
   workstreams merge. P2 supplies generated review artifacts but does not commit
   those shared files. Ensure new modules are included in the lead's distribution
   packaging if the explicit package configuration requires it.

## Official response contracts

Checked 2026-11-02 against primary provider specifications:

- [Payments v2](https://developer.paypal.com/api/payments/v2/schema.json)
- [Orders v2](https://developer.paypal.com/api/orders/v2/schema.json)
- [Customer disputes v1](https://developer.paypal.com/api/customer-disputes/v1/schema.json)

## Verification record

Baseline: **2,192 passed, 39 skipped**, required ruff clean. The first refund suite
ran against the scaffold before implementation: **26 failed, 1 passed**. Important
failing cases included owner/agent gates, MFA, amount/precision/remaining limits,
verified capture, replay/body binding, evidence mismatches, concurrent reservations,
unknown recovery, wallet credit and dispute scoping. Cross-owner 404 alone already
passed on the missing route; removing ownership after implementation was caught.

Additional fail-first cases: dispute-summary detail mapping (one failure), real
sandbox reference alias (one failure), full-refund startup consistency (one failure),
and replay while disconnected from the provider (one failure). All were fixed
without altering an inherited assertion. Evidence logs live under `/tmp/m6-p2-*`.

Mutation checks remove ownership, MFA, verified capture, confirmed state, remaining
limit, minor-unit precision, pending reservation, body binding, durable replay,
capture-read equality, refund amount/currency, refund capture link, refund status,
read refund identity, credit deduplication, dispute scoping, response size, sandbox
pinning, operation-key binding, detail identity and dispute page bounds. The initial
response-size mutation survived because whitespace was invalid JSON anyway. The
new oversized **valid JSON** regression catches it; the restored check is retained.

LIVE SANDBOX: buyer-approved USD 1.00 capture, USD 0.40 partial refund then USD 0.60
full refund, separate refund GET verification, payment `reversed`, spent allowance
zero. Refund suffixes **821Y / 8306**. Live disputes GET also succeeded and excluded
all unrelated records. Both live tests passed; no credentials or raw bodies are
stored as fixtures. A sandbox-only reference-host mismatch was discovered and fixed
without retrying its mutation. Browser navigation remains on `www.sandbox.paypal.com`.

This read-only container required Chromium installation under `/tmp/m6-p2-browsers`
and read-only access to its already configured NSS CA database. The temporary
`/tmp/m6-p2-nss-readonly.so` maps NSS writable initialization to NSS read-only
initialization; it does **not** bypass certificate validation. Browser TLS navigation
was verified before buyer login. Normal environments need Playwright Chromium and
a correctly trusted CA database; no TLS bypass flags are used.

## Delivery files

New:

- `agentpal/agentpal/providers/paypal_refunds.py`
- `agentpal/agentpal/lifecycle/refunds.py`
- `agentpal/agentpal/platform/refunds.py`
- `agentpal/agentpal/platform/refund_models.py`
- `agentpal/tests/test_paypal_refunds.py`
- `agentpal/tests/test_paypal_refunds_sdk.py`
- `agentpal/tests/test_paypal_refunds_live.py`
- `agentpal/docs/PAYPAL_REFUNDS.md`

Changed:

- `agentpal/agentpal/platform/paypal_refunds_router.py`
- `agentpal/agentpal/wallet/store.py` (new CREATE TABLE statements only)
- `agentpal/sdk/python/agentpal_sdk/client.py` (new OwnerClient methods only)
- `agentpal/tests/test_api_v1_invariants.py` (route map additions only)
- `agentpal/tests/test_api_v1_openapi.py` (operation map additions only)
- `agentpal/docs/memory/PROJECT_MEMORY.md` (append only)

The two inherited test files' assertion ASTs were compared with the starting
commit: every assertion is unchanged. Environment-secret scanning of all changed
files passed. Review commits are based on `43b3d3acb9f409bf92f0de324139500c411698ce`
and are delivered through `/tmp/m6-p2.bundle`, branch `m6-p2-review`; shared Git
metadata remains untouched.

## Final gate results

- Full suite with the intentionally unchanged shared OpenAPI: **2,253 passed,
  39 skipped, 4 failed**. Failures were the inherited refund-route prohibition,
  OpenAPI drift and two APIMatic workflow tests stopped by that same drift.
- Ran `python -m agentpal.platform.openapi --write`, `python -m postman.generate`
  and `python -m agentpal.platform.openapi --check`: all succeeded. Three drift
  failures then passed, while the inherited prohibition still failed.
- Full suite with freshly generated artifacts: **2,256 passed, 39 skipped,
  1 failed**, 194.66 seconds. JUnit confirms **all 65 new tests passed**, including
  both LIVE tests. The only failure is
  `test_api_v1_invariants.py::test_no_route_completes_captures_or_records_outcomes`.
- Generated shared artifacts were restored byte-for-byte and are not in the
  commits. Review outputs: `/tmp/m6-p2-openapi.json`, `/tmp/m6-p2-postman/`.
- Required ruff (`agentpal tests`, also `training` and SDK): **all checks passed**.
- Final mutations: **21 caught / 0 survived**. Consolidated evidence:
  `/tmp/m6-p2-mutation-report.json`; detailed logs `/tmp/m6-p2-mutation-*.log`.
- Final full output and JUnit: `/tmp/m6-p2-full-generated.log` and
  `/tmp/m6-p2-full-generated.xml`. Unregenerated full output:
  `/tmp/m6-p2-full-final.log`. Baseline output was observed directly in the session.
- An earlier live verifier-refused USD 0.40 refund was also recovered by authenticated
  order/refund reads, without resubmitting it; a distinct USD 0.60 remainder refund
  then completed and reversed that payment (remainder suffix **193D**).

Review commits: `c1f5ad0` (provider/lifecycle/routes/schema/offline regressions) and
`371a21f` (SDK and live harness), followed by this documentation/memory commit.
The bundle requires starting commit `43b3d3acb9f409bf92f0de324139500c411698ce`.

## Lead-approved follow-up verification

Commit `7fbb59e` changes only `check_consistency` in recovery.py, `_apply_event` in
store.py, the three approved route exceptions, and a new restart regression file.
AST comparisons prove that no other function in those two owning modules changed
and every inherited test assertion is identical to the previously delivered branch.

The exception applies only to completed entries. Positive net amount, unchanged
currency and exact Decimal gross-minus-net equality are required. Credits use an
exact actor, a full bounded marker match and distinct local refund IDs. Each local
claim must match agent/request/owner/amount/currency and must not be terminal failed
or cancelled. A matching older PENDING or reconciliation_required projection is
allowed only because its verified ledger credit already committed. Reads are
bounded to 101 markers so a 101st record cannot be silently omitted. Malformed,
missing, forged, duplicate or inconsistent markers/claims fail. Unapplied markers
on an otherwise gross-equal completed entry also fail. Other ledger states retain
the original equality rule.

New file `tests/test_paypal_refunds_restart.py`: **64 cases**, of which **60 pass**
and four correctly expose the startup forwarding dependency. Tests cover direct
reopened-store recovery; multiple credits; malformed/missing/duplicate markers;
claim bindings; positive net and credit amounts; every other ledger state; bounded
count and lookahead; both ledger-commit/wallet-rollback projections; immutable-gross
and request-key joins; retained provider verification/currency/capture gates; and
rejection of unapproved refund route or method variants.

Fail-first against the previous delivery: **10 failed, 38 passed**. The failures
included the restart and crash-window success cases, gross-read acceptance,
net-sized observation refusal and retained verification/capture behavior. An extra
unrelated audit-reason test failed with SQL LIKE underscore wildcards; the selector
now uses a literal GLOB prefix. No inherited assertion was edited to obtain a pass.

Mutation checks: **38 caught / zero survivors**. Covered completed-only scope,
unapplied marker inspection, actor/reason selectors and literal prefix, wallet
claim requirement, positive net, count bound and lookahead, exact actor/full marker
and ID/integer/fraction bounds, positive/distinct credits, missing claim and every
claim binding/status, accumulation and exact delta, gross and credit queries scoped
to both request keys, inherited provider gates, and route path/method narrowness.
All mutations were restored. Evidence: `/tmp/m6-p2-followup-mutation-report.json`
and `/tmp/m6-p2-followup-mutation-*.log`.

- Fresh follow-up baseline: **2,252 passed / 39 skipped / 5 failed** (the prior four
  artifact/invariant failures plus the default Chromium launch configuration).
- Full suite after OpenAPI/Postman regeneration and with the existing secure browser
  environment: **2,317 passed / 39 skipped / 4 failed**, 193.93 seconds. Both LIVE
  tests passed again. The only failures are the four new factory restart cases.
- OpenAPI write, Postman generation and OpenAPI check succeeded. Shared generated
  files were restored byte-for-byte and excluded from commits.
- Ruff (`agentpal training tests`, also the SDK) passes.
- Full output/JUnit: `/tmp/m6-p2-followup-full-generated.log` and
  `/tmp/m6-p2-followup-full-generated.xml`. Baseline and fail-first output:
  `/tmp/m6-p2-followup-baseline.log`, `/tmp/m6-p2-followup-fail-first.log`.

### Additional ownership request: startup wallet forwarding

The lead's expanded ownership explicitly covers only the three named functions.
The approved validator cannot accept net entries without checking the matching
local wallet claim. `RiskService.__init__` currently calls `startup_recovery(store)`
without the wallet before the platform gets to `check_consistency(store, wallets)`.
Skipping claim validation on that first call would weaken the approved rule.

A concrete patch is ready at `/tmp/m6-p2-startup-wallet-wiring.patch` and passes
`git apply --check`. It is **not applied** because these files are outside that
expanded ownership:

1. `agentpal/agentpal/service.py`, `RiskService.__init__`: append an optional,
   keyword-only `recovery_wallet_store=None` and pass it into the existing
   `startup_recovery` call. Existing standalone callers retain the current default.
2. `agentpal/agentpal/platform/wiring.py`, `build_platform`: pass its already-open
   `wallets` as `recovery_wallet_store` when constructing RiskService.

The four factory restart tests are the fail-first evidence for that exact wiring.
No consistency check is removed, deferred or disabled. Authorization was requested
asynchronously; elapsed time has not been treated as approval. Durable partial
refund platform restarts remain BLOCKED until this patch can be implemented and
verified. This dependency does not require any P1/P3/P4 or model changes.

Follow-up delivery additionally changes `agentpal/agentpal/lifecycle/recovery.py`
and `agentpal/agentpal/store.py` in only their approved functions, and creates
`agentpal/tests/test_paypal_refunds_restart.py`. The route invariant, this document
and append-only team memory are updated. The two proposed startup-wiring files
remain unchanged.
