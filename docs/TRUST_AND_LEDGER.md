# Trust states, spending ledger and time policy (P0.2–P0.5, owner decisions 3, 4, 6, 8)

This document describes how the live service behaves. The offline synthetic evaluation uses explicit *fixtures* for the same inputs (see the end of this document).

## 1. Record types (all separate, all auditable)

| Record | Table | Written by | Mutable? |
|---|---|---|---|
| Observed request | `requests` | `POST /assess` (atomic claim on `request_id`) | Status only: `claimed` → `assessed` or `assessment_error` |
| Risk assessment | `requests.assessment_json` + `audit_log` | the risk engine | No |
| Owner decision | `owner_decisions` | Phase 2 approval service (`store.record_owner_decision`) | Append-only |
| Payment attempt | `payment_attempts` | Phase 2 payment service (`store.record_payment_attempt`) | Append-only |
| Payment outcome | `payment_outcomes` (with `verified` 0/1) | Phase 2 verified webhook handler (`store.record_payment_outcome`) | Append-only |
| Budget hold / spend | `ledger_entries` + append-only `ledger_events` | service and the methods above | State changes only via the validated transition table |
| Security events | `audit_log` (`rejected:*`, `outcome_rejected:*`, `outcome_duplicate_ignored`, `recovered_stale_claim`) | API gate and store | Append-only; never contains tokens or request text |

None of the Phase-2 writer methods are exposed over HTTP in Phase 1. Over HTTP, an assessment can be read only by the agent whose token submitted it, or by an operator token (see `docs/SECURITY_AUDIT.md`).

## 2. What each baseline may use

| Baseline / check | Allowed inputs | Never used |
|---|---|---|
| Replay (`DUPLICATE_REQUEST_ID`) | `requests` primary key `(agent_id, request_id)` (any status, same agent) | – |
| Duplicate intent | This agent's **observed** requests in the window, measured by server receipt time. These can only *raise* risk. | Client timestamps |
| Known merchant (removes `UNAPPROVED_MERCHANT`) | The operator's approved list, or **this agent's** payments with a **verified completed** outcome. Other agents' verified completed payments count **only** if the owner enabled sharing (same owner only). | Our own recommendations, owner approval alone, unverified outcomes, other owners' history, other agents' history when sharing is off |
| Merchant domain baseline (`MERCHANT_DOMAIN_CHANGED`) | The operator's approved list, plus this agent's verified completed payments (plus same-owner shared ones, if enabled) | Flagged, blocked or unverified requests |
| Spend baseline (`AMOUNT_SPIKE_VS_HISTORY`) | This agent's verified completed payments **in the same currency** | Flagged or unverified requests; other agents (even when sharing is on); other currencies |
| Budget exposure (`BUDGET_EXCEEDED`, `CUMULATIVE_LIMIT_EXCEEDED`, split rules) | The ledger: `reserved` + `approved` + `attempted` (`pending`) + `completed` for this agent and currency | Agent-supplied `monthly_budget` or `budget_spent_this_period` |
| Completed spending | Ledger `completed`, which requires a **verified** outcome | Assessments, approvals, attempts |

**Budgets (final decision F4).** Budgets come only from the operator file.
* A configuration has `"configuration": "operator"` (the default) or `"demo"`.
* Only a `demo` configuration may contain `default_agent` placeholder limits, and a request that uses them gets `DEMO_DEFAULT_BUDGET_APPLIED` and always requires review.
* In an operator configuration, an unlisted agent is rejected with `NO_SPENDING_POLICY`.
* The shipped `config/spend_policy.json` is the demo configuration. `config/spend_policy.operator.example.json` is the operator template.

**Isolation (owner decision 8).** Trusted history is queried per `agent_id` *and* the `owner_id` from the operator spend policy, and it is re-filtered by agent inside feature extraction (defence in depth). By default, agent-level isolation applies even within the same owner, because one agent must not vouch for a merchant on another agent's behalf.

**Explicit sharing.** An owner can set `owners.<owner_id>.share_verified_merchant_history_across_agents: true` in `spend_policy.json`. Then other agents' **verified completed** payments *of the same owner* count for merchant familiarity only. Sharing never crosses owners, never includes approvals or unverified outcomes, and never feeds another agent's spend baseline. The shipped value is `false` for every owner.

**Owner approval is not trust.** An approval moves the ledger to `approved` (still a hold). It does not make the merchant "known", even with sharing on, and it does not count as completed spending. The same holds for `allow_for_review`.

## 3. Ledger state machine

```
          (claim within limits)
 request ───────────────────────▶ reserved ──owner approved──▶ approved ──payment submitted──▶ pending
                                     │                            │                              │
           assessment blocked,       │  owner declined,           │ cancelled before             ├─verified completed─▶ completed (spend)
           assessment error,         │  expired (server TTL)      │ attempt                      ├─verified failed────▶ failed
                                     ▼                            ▼                              └─verified cancelled─▶ cancelled
                                  released                     released
                                                   (approval expired after approval_validity_minutes)
```

Reporting names: `pending` is reported as **attempted**. `store.spend_breakdown(agent, currency)` returns `reserved`, `approved`, `attempted`, `completed`, `failed`, `cancelled`, `released` and `committed` (= reserved + approved + attempted + completed) as exact decimals.

* A reservation is created **in the same `BEGIN IMMEDIATE` transaction** that claims the `request_id` and reads the exposure. Concurrent requests therefore cannot overspend the remaining budget.
* Only requests that pass every financial check get a reservation. Requests blocked for any reason (including non-financial ones) are released when the assessment is stored.
* An unapproved reservation expires at the request's server-computed `effective_expires_at`, and its release is recorded with reason `reservation_expired`.
* **Approval validity (owner decision 6).** An approval holds the reservation for `approval_validity_minutes`: 15 by default, in `spend_policy.json`, counted from the approval time.
  * After that, the hold is released with reason `approval_expired`.
  * Expiry runs in its own committed transaction before every owner decision, payment attempt and ledger transition. So an expired hold can never be approved, attempted or revived, and a late approval cannot reclaim budget that others reserved in the meantime.
  * Concurrent approvals are serialised by `BEGIN IMMEDIATE` and cannot exceed the budget (`test_concurrent_approvals_never_exceed_budget`).
* **Execution vs. completion.** A payment attempt inside the window moves the entry to `attempted`. It does not expire, and it counts against the budget until a **verified** provider outcome arrives. Phase 2 must reconcile stuck attempts with the provider.
* **Approval binding (final decision F5).** At claim time, the store records the SHA-256 of the canonical `ApprovalTerms`: request ID, agent, exact amount, currency, merchant name and domain, and purpose. The terms text itself is not stored.
  * `record_owner_decision(..., "approved", terms=...)` and `record_payment_attempt(..., terms=...)` must present terms with the same digest. Otherwise they raise `ApprovalTermsMismatch` and change nothing.
  * So an approval cannot be reused for a different amount, currency, recipient or purpose, and an attempt cannot execute anything other than what was approved.
  * A decline needs no terms.
* **Approval gating (security audit SEC-05, SEC-06).**
  * An approval requires a **finished, non-blocking assessment** (`requests.status = 'assessed'`, recommendation not `block`). The reservation exists from the claim onwards, while the rules are still running. Before this check, an approval in that window survived a later `block`. A premature approval now raises `RequestNotApprovable`, a subclass of `InvalidLedgerTransition`.
  * The approved hold expires at `min(approval time + approval_validity_minutes, effective_expires_at)`. An approval never outlives the request it approves.
* **Time (SEC-07).** Every ledger timestamp is converted to UTC, and naive timestamps are refused (`ValueError`). Timestamps are compared as ISO strings inside SQLite, so before this fix a `now` with a negative offset could revive an expired approval.
* **Outcomes (SEC-08).** `record_payment_outcome`:
  * refuses an unknown request (`InvalidLedgerTransition`; nothing is stored);
  * treats a **redelivered** verified outcome as an idempotent no-op if it has the same outcome and provider reference. It is audited as `outcome_duplicate_ignored`;
  * refuses any other verified outcome for a settled entry (`completed`, `failed` or `cancelled`) with `OutcomeConflict`;
  * checks the keyword-only `amount`, `currency` and `attempt_reference`, when given, against the ledger entry and the recorded attempts. A mismatch raises `OutcomeMismatch`. **Phase 2 must always pass all three.** They are optional only so that the Phase-1 benchmark harnesses keep working;
  * audits every refusal as `outcome_rejected:<reason>`, and the refusal changes nothing else.
* **Crash recovery (SEC-09).** When a worker dies between claim and completion, the request stays `claimed` and its reservation holds budget until the request expires (up to 48 h). At start-up, `RiskService` calls `store.recover_stale_claims`. Claims older than `max(5 min, 4 × model timeout)` become `assessment_error`, and their hold is released (reason `stale_claim_recovered`, audit `recovered_stale_claim`). The id stays claimed (fail closed).
* **Known gap (SEC-14, open).** `store.transition_ledger` is a public method that moves an entry along the transition table *without* terms, assessment or verification checks. It is used by `tests/test_p0_2_spending.py`. It is not reachable over HTTP, but Phase 2 must not call it for approvals, attempts or outcomes. It should be restricted to cancellations (`→ released`).
* **Phase 2 requirements before payments are wired up (not implemented):**
  * provider reconciliation of every `attempted` entry;
  * explicit handling of `pending`, `failed` and uncertain outcomes (timeout, ambiguous response);
  * no retry until the previous attempt is verified `failed` or `cancelled`. PayPal's `PayPal-Request-Id` is a **per-API-call** idempotency key: Orders v2 accepts it on create, authorize and capture. So use one stored key per (request, operation), persist it *before* the call, and resend the same key on a retry of that same call. PayPal does not document how long keys are retained for Orders, so do not rely on a late retry being deduplicated. A simultaneous duplicate "might fail", and a call without the header is processed as new. See `docs/SECURITY_AUDIT.md`, "PayPal claims".
* Unverified outcomes are recorded for audit, but leave the entry `pending`.
* Invalid transitions (for example `completed` → `released`) raise `InvalidLedgerTransition`.

## 4. Currency (P0.5)

* Every limit is configured **per currency, in that currency's units** in `config/spend_policy.json`. Config amounts are decimal strings that respect ISO 4217 minor units; floats are rejected.
* Each ledger entry records its own currency. Sums are exact `Decimal` per currency.
* **No exchange rates are used or invented.**
* A currency that is globally allowed but not configured for the agent produces `NO_SPENDING_POLICY` (block).
* A currency outside `allowed_currencies` produces `CURRENCY_NOT_ALLOWED` (block). Unknown ISO codes are rejected with 422.

## 5. Time (P0.3)

* **Trusted time source:** the host's UTC wall clock (`datetime.now(timezone.utc)`), injectable for tests. Operators must keep it NTP-synchronized; the engine has no protection against a wrong host clock.
* All windows use the server receipt time. The client's `created_at` and `expires_at` are untrusted metadata and must include a timezone offset.
* `effective_expires_at = min(client expires_at, receipt + max_lifetime)`, or `receipt + default_ttl` when no expiry is supplied.
* Boundaries:
  * `expires_at` at or before receipt time → `REQUEST_EXPIRED` (block).
  * `expires_at` later than receipt + 48 h → `REQUEST_LIFETIME_EXCEEDS_MAX` (block); exactly 48 h is allowed.
  * `created_at` more than 10 min ahead of receipt → `TIMESTAMP_IN_FUTURE` (risk indicator).
  * `created_at` more than 24 h old → `STALE_REQUEST` (risk indicator).
* The defaults (TTL 60 min, maximum lifetime 48 h, skew 10 min, staleness 24 h) are in `config/risk_policy.json` under `time_policy`. They were **approved by the product owner** (decision 3) and each boundary is tested in `tests/test_od_decision_3.py`.

## 6. Offline fixtures (synthetic evaluation and pure unit tests only)

The synthetic dataset predates operator-owned budgets and verified outcomes. When it is evaluated offline:

* `SpendSnapshot.from_synthetic_fixture` maps each scenario's budget fields to a fixture snapshot, marked `source="synthetic_fixture"`.
* The scenario's prior rows for the same agent are treated as verified completed payments.

The live service never does either. `tests/test_p0_2_spending.py` and `tests/test_p0_4_trust.py` verify this.
