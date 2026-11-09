# Owner-approved PayPal sandbox payouts (P3)

AgentPal controls a single payout to an email recipient: an external agent
requests it, deterministic policy reserves its virtual allowance, an authenticated
human owner approves the exact terms, and only an authenticated PayPal status READ
can confirm completion. Sandbox only; `is_real_money` is always false.

## API and SDK

| Method and path | Credential | Operation ID |
|---|---|---|
| POST `/v1/payout-requests` | agent, `payout.request` | `createPayoutRequest` |
| GET `/v1/payout-requests` | owner | `listPayoutRequests` |
| GET `/v1/payout-requests/{request_id}` | owner | `getPayoutRequest` |
| POST `/v1/payout-requests/{request_id}/approve` | owner | `approvePayoutRequest` |
| POST `/v1/payout-requests/{request_id}/reject` | owner | `rejectPayoutRequest` |
| POST `/v1/payout-requests/{request_id}/execute` | recorded agent, `payment.execute_sandbox` | `executePayoutRequest` |
| POST `/v1/payout-requests/{request_id}/reconciliation` | owner or recorded agent with execution capability | `reconcilePayoutRequest` |

Create requires `Idempotency-Key` and this body (fake recipient):

```json
{"receiver_email":"freelancer@example.com","amount":"1.00","currency":"USD",
 "note":"Sandbox invoice 7","purpose":"Sandbox freelancer work"}
```

Amounts must be decimal strings with the currency's minor units; JSON numbers,
floats, exponents, nonfinite and nonpositive values are refused. Email validation
accepts bounded ASCII mailboxes with a public-style dotted domain and excludes
display names, whitespace, control characters, domain literals and malformed dot
or label syntax. Mailboxes are normalized to lowercase. Note and purpose are at
most 255 characters, and controls are refused. Unknown input fields are refused.

The create response includes `request_id`, `wallet_id`, the complete terms,
`terms_sha256`, status and sandbox flags. Same agent/key/canonical body replays the
original stored response with HTTP 200 and `Idempotent-Replayed: true`; the first
response is HTTP 201. A changed body returns 409 `idempotency_key_reused`.
Creation replay is a historical snapshot; owner GET reads current ledger status.

Approval accepts only `{"terms_sha256":"<digest from reviewed response>"}`.
An agent credential cannot approve or reject; an owner credential cannot execute.
Another owner's resource and an absent resource both return 404 `not_found`.
Owner listing returns at most the latest 100 successfully prepared requests.

The Python SDK adds `AgentClient.create_payout_request`,
`execute_payout_request`, `reconcile_payout_request`; `OwnerClient` adds
`list_payout_requests`, `get_payout_request`, `approve_payout_request`,
`reject_payout_request`, `reconcile_payout_request`. Creation generates an
idempotency key when one is not supplied and accepts only `str`/`Decimal` amounts.
Clients do not automatically retry money mutations.

## Shared policy, approval and ledger

Payouts reuse the existing `AssessmentStore` ledger, not a separate allowance.
Payment and payout holds therefore count against each other's monthly and rolling
limits. Wallet transaction views and allowance totals include payout principal.
The existing risk engine may add review or reject; it cannot grant approval.

Preparation takes the existing wallet-before-assessment lock order. It checks
current registry capabilities and active identity, active currency wallet, operator
caps, per-transaction limits and remaining monthly allowance. The existing
`RiskService.assess` performs the authoritative read/check/reserve in the ledger's
SQLite transaction, including rolling and split-transaction limits. A durable
payout claim preserves the same request ID across an interrupted projection;
completed assessment can be reprojected, while an interrupted assessment fails
closed. Policy refusals are recorded in `platform_payout_events`; permission
decisions also use the existing permission audit.

The outer SHA-256 digest covers receiver, amount, currency, note, purpose, request,
wallet, agent and owner. Its canonical JSON uses sorted keys and exact decimal
strings. That digest is embedded in the ledger's existing `ApprovalTerms.purpose`.
The independent ledger digest check therefore detects changed payout terms even
if a corrupted metadata row has a newly computed outer hash. The existing wallet
approval gateway revalidates the authenticated owner's session and records owner,
organization, session, authentication method, MFA, expiry and single-use consumption.
Configured MFA applies before approval. Approval expiry is the existing minimum
of request lifetime and approval validity; it is checked inside execution.

No agent-controlled field asserts the agent's identity email. Platform agent IDs
are not mailboxes. Self-payout checking additionally rejects a receiver matching
the owner-maintained profile name or `email`, `identity_email`, `receiver_email`
metadata aliases. An externally authenticated email association is not currently
part of the shared agent identity contract; binding one requires lead-owned
identity/registration changes.

Every financial transition uses the existing ledger APIs, approval/attempt tables,
provider event deduplication and append-only ledger history. Payout metadata and
observation audits are additive wallet tables. There is no new balance column.
Pending attempts are quarantined by existing startup recovery after a restart.

## Provider verification and recovery

The adapter uses only `https://api-m.sandbox.paypal.com`, OAuth client credentials,
configured TLS verification and the existing proxy-aware httpx client. It refuses
redirects, including with an injected redirect-following client. OAuth and API
responses are streamed with a 128 KiB limit and bounded configured timeouts plus
a response deadline; IDs, lists, amounts and header values are bounded. Errors
are constant codes and never include raw PayPal response bodies or credentials.

Execution consumes approval and persists an operation claim before provider I/O.
Only one item with `recipient_type: EMAIL` is created. Both
`sender_batch_id` (30 characters) and the distinct `PayPal-Request-Id` derive
deterministically from the request ID using separate operation prefixes.
Re-execution returns local state without issuing another create. Header acceptance,
batch status `SUCCESS`, and a successful create response do not confirm a payout.

The server reads the recorded batch through PayPal. Before applying an observation
it requires the exact batch, sender batch, one item, item batch, sender item,
recipient type, receiver, note, amount and currency. Multi-page/truncated results
fail closed. Only item `transaction_status: SUCCESS` becomes `provider_confirmed`.

| Verified item status | Behavior |
|---|---|
| SUCCESS | Completed principal in the shared ledger |
| PENDING / ONHOLD / UNCLAIMED | Retain pending hold; UNCLAIMED includes an explicit warning |
| FAILED / BLOCKED / DENIED | Failed; release hold once |
| RETURNED / REFUNDED / REVERSED | Failed if never completed; reversed after verified completion |
| Unknown, malformed or mismatched | Reconciliation required; retain hold |

Timeouts, connection failures during money calls, 5xx, redirects, HTTP 400/409
(including possible duplicate batch responses), malformed success and failure to
persist an accepted batch all retain the hold in `reconciliation_required`.
Definite provider refusal, such as an offline-tested HTTP 422, becomes failed.
No blind retry is made, even though PayPal provides duplicate detection.

Reconciliation performs only an authenticated READ of the stored batch. If the
create response was lost before its batch ID was persisted, the app refuses with
409 `payout_batch_unknown`; operator investigation is needed. No endpoint accepts
an arbitrary replacement batch ID or follows a provider-supplied URL. With a
known batch, a verified terminal read recovers the attempt without another POST.
The shared lifecycle conservatively retains reconciliation state for nonfinal
observations following an unknown outcome, until terminal evidence arrives.

Existing ledger conflict handling applies: contradictory final evidence is
quarantined; duplicate observations do not spend or release twice. Payout fees
are provider charges separate from the virtual principal allowance. No payout
webhook integration, item cancellation, batching, production funds or UI is added.

## Running and evidence

From `agentpal/`:

```sh
python -m agentpal.demo.payout_flow
python -m agentpal.demo.payout_flow --live
python -m pytest -o addopts="" -q -p no:cacheprovider tests/test_paypal_payouts*.py
```

Default demo mode mocks only PayPal HTTP and ignores sandbox environment secrets.
Live mode explicitly uses sandbox credentials from environment variables. Both
modes run the real `/v1` app, registry, policy, authenticated owner approval and
shared ledger. Polling is bounded to at most 300 seconds and all printed IDs are
redacted to their final four characters. Live tests skip cleanly if either sandbox
credential variable is absent. No buyer password is needed for Payouts.

Observed 2026-11-02, IMPLEMENTED — TESTED:

- Initial baseline: 2,192 passed / 39 skipped; required ruff clean.
- Offline demo: awaiting approval → approved → provider confirmed; one provider
  operation, one verified outcome, consistency check clean.
- Live $1 USD to Priya: awaiting approval → approved → provider pending → provider
  confirmed; item SUCCESS, batch `...PWMN`, request `...6e56`; one operation and
  one verified outcome. Re-execution returned the same batch.
- Live simulator receiver with note `ERRPYO002`: failed, request `...4380`, no batch,
  zero verified outcomes and no confirmation.

Initial fail-first evidence: `test_owner_approved_payout_settles_shared_ledger_once`
failed with HTTP 404 on the scaffold; the batch persistence failure regression
raised RuntimeError before the recovery fix; the HTTP 400 regression observed
`failed` instead of `reconciliation_required` before conservative duplicate
handling; the deadline regression failed before deadline enforcement existed.
Targeted boundary-removal mutation results and final suite totals are recorded in
the review commit bodies and final delivery report. Existing assertions were not
edited; only the explicitly owned route/operation maps were extended.

The lead regenerates and commits OpenAPI/Postman artifacts after integration.
P3 intentionally leaves those checked-in artifacts unchanged, so drift checks
against the old published spec will fail until that regeneration.

Final verification (2026-11-02):

- Final complete suite, isolated checkout with regenerated OpenAPI/Postman:
  **2,305 passed / 39 skipped / 0 failed**, including the live tests; 224.73 s.
- P3 offline group: **111 passed**; live group: **2 passed** initially, and ran
  again in the complete suites. Missing-credential gate: **2 clean skips**.
- Required ruff (`agentpal tests` and `agentpal training tests`) and SDK ruff:
  **clean**. Both inherited route-map test files retain every original assertion;
  wallet schema changes append exactly two CREATE TABLE statements.
- Worktree full suite before the final two text-control cases: **2,300 passed /
  39 skipped / 3 failed**. The three failures are the intentional old OpenAPI
  artifact and its two APIMatic drift fences. All 36 affected contract/workflow
  cases pass after regeneration in the isolated checkout.
- `openapi --write`, `postman.generate`, and both checks passed in that checkout.
  The worktree `openapi --check` still reports drift, as expected; generated files
  are excluded from the review commits and bundle.
- Changed-file scan found none of the configured PayPal credential values.
- Review commits: `c499451` (financial/provider/API boundaries), `e5eee9e`
  (SDK, demo and live tests), plus the final coverage/documentation/memory commit.

## Mutation verification (45 caught, zero survivors)

Each named boundary below was deliberately removed or weakened in an isolated
source variant. Its targeted regression failed, then the production source was
restored. The final two control-character cases protect note and purpose input.

- `sandbox_configuration`: CAUGHT
- `sandbox_at_io`: CAUGHT
- `redirect_refusal`: CAUGHT
- `response_size`: CAUGHT
- `response_deadline`: CAUGHT
- `oauth_header_validation`: CAUGHT
- `batch_path_validation`: CAUGHT
- `batch_identity`: CAUGHT
- `sender_batch_binding`: CAUGHT
- `one_item_only`: CAUGHT
- `total_items_bound`: CAUGHT
- `total_pages_bound`: CAUGHT
- `email_recipient_type`: CAUGHT
- `receiver_binding`: CAUGHT
- `sender_item_binding`: CAUGHT
- `note_binding`: CAUGHT
- `currency_binding`: CAUGHT
- `decimal_amount_binding`: CAUGHT
- `only_success_completes`: CAUGHT
- `owner_scope`: CAUGHT
- `agent_scope`: CAUGHT
- `payout_capability`: CAUGHT
- `execute_capability`: CAUGHT
- `wallet_frozen`: CAUGHT
- `self_receiver`: CAUGHT
- `idempotency_body_binding`: CAUGHT
- `hard_transaction_limit`: CAUGHT
- `remaining_monthly_budget`: CAUGHT
- `requested_digest_binding`: CAUGHT
- `mfa_gate`: CAUGHT
- `exact_ledger_terms`: CAUGHT
- `metadata_digest`: CAUGHT
- `sandbox_provider_gate`: CAUGHT
- `single_use_approval`: CAUGHT
- `execution_replay_fence`: CAUGHT
- `unknown_hold_retention`: CAUGHT
- `duplicate_400_hold`: CAUGHT
- `item_batch_binding`: CAUGHT
- `plain_decimal_read`: CAUGHT
- `strict_receiver_input`: CAUGHT
- `currency_minor_units`: CAUGHT
- `note_and_purpose_controls`: CAUGHT
- `note_and_purpose_length`: CAUGHT
- `positive_amount_input`: CAUGHT
- `known_item_status`: CAUGHT

## Official references

- [Payouts v1 API](https://developer.paypal.com/docs/api/payments.payouts-batch/v1/)
- [Batch status read](https://developer.paypal.com/api/payments.payouts-batch/v1/payouts-get/)
- [Payout item status definitions](https://developer.paypal.com/api/payments.payouts-batch/v1/definitions/transaction_enum/)
- [Payout customization and duplicate detection](https://developer.paypal.com/docs/payouts/standard/integrate-api/customize/)

The documented duplicate sender-batch window is finite; local single-use operation
claims prevent resubmission independently of that provider retention window.
Sandbox observations are not evidence of production readiness.
