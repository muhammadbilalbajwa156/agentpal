# Product-owner decisions: implementation record

Branch `agentpal-owner-decisions` (from P0 commit `3877a25`). Rules version `2026.11.2-final`;
spend policy `2026.11.2-final-demo` (a **demo** configuration).

There were two rounds:
* **Round 1** (sections 1–10 below): ten product-owner decisions, implemented with tests.
* **Round 2** ("Final product-owner decisions", next section): the owner reviewed round 1 and settled the open assumptions. Where the two differ, round 2 wins.

This document maps each of the ten product-owner decisions to:
* its configuration (where it is explicit, documented configuration),
* the code that enforces it,
* the tests that prove it.

It then lists the policy assumptions made where the decisions left a detail open, and the gaps that are still unresolved.

**This is not a production-readiness claim.** The API is deliberately localhost-only, and there is no authentication, authorization or rate limiting.

**Method.** For every decision, the tests were written first and run against the previous code. They failed where behaviour had to change. Pre-fix and post-fix results are summarised under each decision. Thresholds were **not** changed from validation results, and the synthetic test split was not used.

---

## Final product-owner decisions (round 2, 2026-10-29)

| # | Decision | Status | Config / code | Tests |
|---|---|---|---|---|
| F1 | **Degraded mode stays conservative.** A model timeout or failure, with any rule points remaining, requires review. Zero rule points is never presented as safe. | Confirmed; configurable | `recommendation.degraded_mode.review_if_rule_score_at_or_above: 1`. Setting `0` makes every degraded assessment require review. | `test_od_decisions_2_5_9.py`: `test_degraded_with_zero_rule_points_is_never_presented_as_safe`, `test_threshold_zero_makes_every_degraded_assessment_require_review`, `test_shipped_degraded_policy_is_the_conservative_one` |
| F2 | **Unverified evidence costs the same as missing evidence** (13 points). Claimed invoices or justifications never reduce risk unless independently verified. | Confirmed | `evidence_policy.claimed_evidence_reduces_risk: false`; `EVIDENCE_UNVERIFIED` | `test_od_decision_9_gaps.py` |
| F3 | **Near-duplicates:** within max(2%, one minor unit), same agent and merchant, inside the duplicate window → review, never block | Confirmed; new tests | `limits.near_duplicate_tolerance_ratio: 0.02`, `duplicate_intent_window_hours: 72` | `test_od_final_3_near_duplicate.py` (19): rounding and inclusive boundaries, the one-cent and one-yen floors, JPY, the 72 h window boundary, currency separation, never blocks, 8 concurrent requests |
| F4 | **Default budgets are demo placeholders.** They are allowed only in a configuration labelled demo. Outside it, unlisted agents are rejected. Agents never set budgets. | **Changed** | `SpendPolicy.configuration: "operator"` (default) or `"demo"`. A validator rejects `default_agent` unless `demo`. The shipped `spend_policy.json` is labelled `demo`. Any request using the demo default gets `DEMO_DEFAULT_BUDGET_APPLIED` (25 points, uncertainty), so it always requires review. New template `config/spend_policy.operator.example.json`. | `test_od_final_4_demo_budgets.py` (5; all failed before the change) |
| F5 | **An approval is bound to the exact request, agent, amount, currency, recipient and purpose.** An attempt must present the same terms. Attempted ≠ completed. | **Changed** | `spending.ApprovalTerms` (canonical, exact amount per currency). The store writes its SHA-256 to `request_terms` in the claim transaction; the terms text is not stored. `record_owner_decision(..., terms=)` is required for approvals; `record_payment_attempt(..., terms=)` is keyword-only and required. A mismatch raises `ApprovalTermsMismatch` and changes nothing. SQLite schema v3. | `test_od_final_5_approval_binding.py` (18; failed before the change) |
| F6 | **Sharing stays off by default.** It is opt-in, same owner only, verified completed payments only. It never raises another agent's budget or replaces its checks. | Confirmed; new test | `owners.<id>.share_verified_merchant_history_across_agents: false` | `test_od_decision_8.py::test_sharing_never_raises_another_agents_budget_or_replaces_its_checks` |
| F7 | **`impersonation_lookalike` stays disabled** until it has been evaluated on an independent hand-labelled benchmark. Lookalikes → review unless a separate hard violation applies. | Confirmed | `combination_blocks[0].enabled: false` | `test_od_decisions_2_5_9.py::test_shipped_combination_policy_is_disabled`, `::test_soft_signals_alone_never_block` |
| F8 | **Localhost only.** Forwarded headers never establish client identity. Authentication, authorization, rate limiting, TLS and multi-tenant isolation are required before any public deployment. | **Changed** (hardening) | `serve` passes `proxy_headers=False` (uvicorn would otherwise honour `X-Forwarded-For` from 127.0.0.1) and `server_header=False`. `LoopbackOnly` uses the socket peer only. | `test_od_decision_10.py`: forwarded headers cannot make a remote client local or a local client remote; `serve` disables proxy headers |
| F9 | **Rules are the security authority; ML is advisory.** It is not a blocking authority and not a proven fraud detector. Rules-only vs. rules + ML is to be decided on an independent benchmark. | Confirmed; new test | The orchestrator: a model can add review only; it never blocks, unblocks or relaxes | `test_od_final_9_ml_advisory.py` (25: the real pinned artifact on every shipped example and seed case; skipped if the git-ignored artifact is absent); `test_od_decision_9_adversarial.py` (10,000 random outputs) |
| F10 | **Build the independent benchmark** per the protocol, with reviewers who did not write the rules. The test set is locked; metrics are reported separately; attack families are listed. | **Not done (needs people)** | `BENCHMARK_ANNOTATION_PROTOCOL.md` updated with the required attack families and the rules-only vs. rules + ML question | — |

**Notes on F4.** The demo file still holds the demo agents (`agent_demo_001`…`010`). Their amounts are placeholders too, and the file's `note` says so.

**Notes on F5.** An approval or attempt for a request assessed before schema v3 has no stored terms digest, so it is refused (fail closed).

**Required before payments are wired up (Phase 2, not done):**
* **Provider reconciliation.** Every `attempted` (`pending`) entry must be resolved from the provider's verified records, not from the agent.
* **Explicit handling of outcomes.** `pending`, `failed` and **uncertain** outcomes (timeouts, ambiguous provider responses) each need their own handling.
* **No retries until resolved.** A retry is allowed only after the previous attempt is verified failed or cancelled, and it must reuse the same idempotency key.
* **Attempts never count as completed.** An attempted payment is never treated as completed spending (`test_exact_terms_approve_then_attempt_but_never_complete`).

**Not claimed:**
* **Fabricated documents are not detected.** Unverified evidence simply does not lower risk.
* **No model is a proven fraud detector**, and the validation-split results are development evidence only, not real-world performance.
* **Model integrity checks do not protect against a privileged (root) process**; see section 7.

---

## 1. Avoid missed risky payments; report metrics separately

| | |
|---|---|
| Config | None: thresholds unchanged (`review_at_or_above` stays 25; no value was tuned from validation results). |
| Code | `evaluation.operational_metrics`; an `operational` section in the `evaluate` and `bench` reports. The CLI prints missed-risky, legitimate-flagged, review and block rates on their own line, and p50/p95 latency on another. |
| Tests | `test_p0_8_evaluation.py::test_operational_metrics_report_misses_false_positives_and_rates_separately` |

**Reported separately:**
* missed risky (count, rate, Wilson 95% CI)
* legitimate flagged (count, rate, CI)
* legitimate blocked
* allow, review and block rates
* degraded rate
* latency (p50/p95)

**Consequence.** Decisions 2 and 9 move the operating point towards fewer misses and more reviews (see "Measured effect" at the end). That is the trade-off decision 1 asked for. The thresholds themselves are unchanged.

## 2. Hard-block only deterministic policy violations

| | |
|---|---|
| Config | `risk_policy.json → recommendation`: <ul><li>`review_at_or_above: 25`</li><li>`combination_blocks` (each has `name`, `all_of` ≥ 2 rule codes, `enabled`, `rationale`)</li></ul> `block_at_or_above` and `model_may_trigger_block` were removed. |
| Code | `orchestrator.recommendation_for` returns `block` only when there is a hard violation. `orchestrator.matched_combinations` matches **rule-sourced** signals only. A match adds a `COMBINATION_BLOCK:<name>` hard violation, with its evidence and rationale in the explanation. |
| Tests | `test_od_decisions_2_5_9.py`: <ul><li>soft signals alone never block</li><li>a score of 100 maps to review</li><li>hard rules still block</li><li>the shipped combination is disabled</li><li>an enabled combination blocks and is explained</li><li>model findings cannot satisfy a combination</li><li>config validation rejects an unknown rule code, a single-code combination, a malformed name or a missing rationale (each tested on its own)</li></ul> 10,000 random model outputs never block alone (`test_od_decision_9_adversarial.py`). |

* **Hard violations (block):** `DUPLICATE_REQUEST_ID`, `CURRENCY_NOT_ALLOWED`, `NO_SPENDING_POLICY`, `AMOUNT_ABOVE_HARD_LIMIT`, `BUDGET_EXCEEDED`, `CUMULATIVE_LIMIT_EXCEEDED`, `SPLIT_TRANSACTION_EXCEEDS_HARD_LIMIT`, `REQUEST_EXPIRED`, `REQUEST_LIFETIME_EXCEEDS_MAX`, `BLOCKED_MERCHANT_DOMAIN`, plus any enabled `COMBINATION_BLOCK:*`.
* **Changed:** soft signals scoring 75 or more previously produced `block`. They now produce `require_review`. Examples 10 (lookalike) and 14 (gift-card mismatch) therefore change from `block` to `require_review`.
* **One combination is shipped, disabled:** `impersonation_lookalike` (`LOOKALIKE_DOMAIN` + `MERCHANT_DOMAIN_CHANGED`). It is tested, but stays inactive until the owner enables it.

## 3. Time policy

| | |
|---|---|
| Config | `risk_policy.json → time_policy`: <ul><li>`default_ttl_minutes 60`</li><li>`max_lifetime_hours 48`</li><li>`max_client_clock_skew_minutes 10`</li><li>`max_client_age_hours 24`</li></ul> |
| Code | `features.effective_expiry`; server receipt time from the injected trusted clock, which is shared by the service and the store. Timestamps without an offset are rejected with 422. |
| Tests | `test_od_decision_3.py`: owner defaults, nine exact-boundary cases, default expiry, and hard vs. soft outcomes. Also `test_p0_3_time.py` and the untrusted-timestamp manipulation tests in `test_od_decision_9_adversarial.py`. |

**Exact boundaries** (T = server receipt time):

| Condition | Result |
|---|---|
| `expires_at ≤ T` | `REQUEST_EXPIRED`, block |
| `expires_at > T + 48 h` | `REQUEST_LIFETIME_EXCEEDS_MAX`, block (exactly 48 h is allowed) |
| `created_at > T + 10 min` | `TIMESTAMP_IN_FUTURE`, review signal (exactly 10 min is allowed) |
| `created_at < T − 24 h` | `STALE_REQUEST`, review signal (exactly 24 h is allowed) |
| No `expires_at` | effective expiry = T + 60 min |
| Any `expires_at` | effective expiry = `min(expires_at, T + 48 h)` |

These tests passed on the P0 code unchanged. This decision confirmed existing values; no code change was needed.

## 4. Spending

| | |
|---|---|
| Config | `config/spend_policy.json` (operator file): <ul><li>per agent, per currency `monthly_budget`, `rolling_window_limit`, `per_transaction_hard_limit`, `per_transaction_review_limit`</li><li>`rolling_window_hours: 24`</li><li>`default_agent` (conservative: USD 100 / GBP 80 / JPY 15000)</li></ul> Removing `default_agent` blocks every unlisted agent with `NO_SPENDING_POLICY`. |
| Code | `spending.py`; `store.claim_and_reserve` (one `BEGIN IMMEDIATE` transaction); `store.spend_breakdown`. |
| Tests | `test_p0_2_spending.py` (agent budget fields ignored; concurrent overspend); `test_p0_5_currency.py`; `test_od_decisions_4_6.py::test_amounts_are_reported_separately_by_state` and `::test_unverified_outcome_never_counts_as_completed`; the agent-field manipulation tests in `test_od_decision_9_adversarial.py`. |

* **Budgets come only from the operator file.** Request fields `monthly_budget` and `budget_spent_this_period` are ignored for enforcement.
* **Every amount is an exact `Decimal` in its own currency.** No exchange rates exist. A currency with no entry for the agent produces `NO_SPENDING_POLICY` (block).
* **Separated amounts:** `spend_breakdown` reports `reserved`, `approved`, `attempted` (ledger state `pending`), `completed`, `failed`, `cancelled`, `released` and `committed`, per agent and currency.
* **Only a verified outcome reaches `completed`.** An unverified outcome is audited and leaves the entry in `attempted`.
* Budget exposure (`committed`) = reserved + approved + attempted + completed.

## 5. Model timeout or failure → degraded, rules apply

| | |
|---|---|
| Config | `recommendation.degraded_mode.review_if_rule_score_at_or_above: 1`; `AGENTPAL_MODEL_TIMEOUT_SECONDS`. |
| Code | `orchestrator.assess`: an assessment is `degraded` when a configured model backend was skipped and the deterministic fallback ran. It then has: <ul><li>`degraded_reason` (the backend error codes)</li><li>a `MODEL_UNAVAILABLE_OR_TIMED_OUT` uncertainty signal</li><li>an explanation stating that this is not evidence of safety</li></ul> Falling back to a *second model* is not degraded. |
| Tests | `test_od_decisions_2_5_9.py`: degraded flag, reason and review for an unavailable, raising, crashing or invalid-output backend; never less strict than rules-only; configurable threshold; a rules-only configuration is not degraded; a fallback to a second model is not degraded. Also `test_p0_7a_timeout.py` (a hanging backend returns at the deadline, degraded, `require_review`). |

## 6. Approval holds for 15 minutes

| | |
|---|---|
| Config | `spend_policy.json → approval_validity_minutes: 15` |
| Code | `store.record_owner_decision` sets `expires_at = approval time + validity`. `store.expire_holds` releases expired `reserved` holds (reason `reservation_expired`) and `approved` holds (reason `approval_expired`) in its own committed transaction. It runs before every decision, attempt and transition, so an expired hold can never be approved or attempted. |
| Tests | `test_od_decisions_4_6.py`: <ul><li>15-minute default</li><li>the hold lasts the window, then releases</li><li>an attempt after expiry is refused</li><li>approving an expired reservation is refused</li><li>a late approval cannot reclaim budget others took</li><li>concurrent approvals never exceed the budget (threads, file DB)</li><li>a cancelled approval releases</li></ul> Episodes `ep-approval-hold-expiry`, `ep-unapproved-reservation-ttl`, `ep-attempted-payment-keeps-hold` and `ep-unverified-outcome-not-completed` are replayed through the API in `test_od_multi_request_episodes.py`. |

The three stages are distinct ledger states:
* **Approval** = `approved`, an expiring hold.
* **Execution** = `pending`, reported as `attempted`. It does not expire; it counts against the budget until a verified outcome arrives.
* **Provider-confirmed completion** = `completed`, which requires `verified=True`.

## 7. Model integrity

| | |
|---|---|
| Config | <ul><li>`AGENTPAL_REQUIRE_MODEL_INTEGRITY=true` (default)</li><li>`AGENTPAL_REQUIRE_READ_ONLY_MODEL_STORAGE=true` (default)</li><li>`AGENTPAL_MODEL_STAGING_DIR` (default `<tmp>/agentpal-models-<uid>`)</li><li>`config/model_manifest.json`</li></ul> |
| Code | `integrity.storage_is_read_only`, `integrity.stage_verified_copy` and `RiskService.warm_up`, used by `LlamaCppAnalyzer`. See [`MODEL_INTEGRITY.md`](MODEL_INTEGRITY.md). |
| Tests | `test_od_decision_7.py`: <ul><li>defaults</li><li>writable storage refused</li><li>read-only storage loads a verified 0400 private copy in a 0700 directory</li><li>a source rewrite after staging cannot affect the loaded bytes</li><li>a swapped stream is never loaded</li><li>an insecure staging directory is refused</li><li>warm-up</li></ul> Also `test_p0_7d_integrity.py`. |

* An unpinned model is unavailable, and the rules apply.
* llama.cpp loads only the staged copy. That copy is hashed while it is being copied and is published only if the hash matches.
* **Not protected: a privileged root process.** The read-only requirement checks permission bits, which root ignores. A root process, or anything else running as the service user, can still alter files or the staging directory. Use read-only mounts or image layers and process isolation.
* **Staging directory:** set `AGENTPAL_MODEL_STAGING_DIR` to a dedicated local path owned by the service user. The default is `<system tmp>/agentpal-models-<uid>`.
* **Start-up delay:** the first cold-cache hash of the 1.1 GB GGUF took **about 117 s** in this environment (3.5 s warm). The warm-up runs before the API serves requests, so start-up blocks for that long. Without the warm-up, the first requests would time out and be degraded.

## 8. Merchant history isolation

| | |
|---|---|
| Config | `spend_policy.json → owners.<owner_id>.share_verified_merchant_history_across_agents` (default and shipped value: `false`). |
| Code | `store.shared_trusted_history_for` (same owner, other agents, **verified completed** outcomes only). `service` passes it only when the owner policy enables sharing. `features` uses shared history only for merchant familiarity (`UNAPPROVED_MERCHANT`, `MERCHANT_DOMAIN_CHANGED`), never for the agent's spend baseline. |
| Tests | `test_od_decision_8.py`: <ul><li>isolated by default</li><li>explicit owner sharing works for the same owner only</li><li>unverified or merely approved history is never shared</li><li>shared history never defines another agent's spend baseline</li></ul> Also `test_p0_4_trust.py` and the episode `ep-unverified-outcome-not-completed`. |

## 9. Detection gaps, adversarial and multi-request tests, benchmark

**Addressed in code:**

| Gap | Change | Config | Tests |
|---|---|---|---|
| Near-duplicate amounts (review P04) | `NEAR_DUPLICATE_PAYMENT_INTENT` (30 points): same agent and merchant, inside the duplicate window, amount within max(2%, one minor unit) | `limits.near_duplicate_tolerance_ratio: 0.02` | `test_od_decision_9_gaps.py`; seed episode `ep-near-duplicate` now passes |
| Fabricated evidence influence (P13/P16). This removes the *influence*; it does **not detect** fabricated documents. | Claimed evidence no longer removes risk: `EVIDENCE_UNVERIFIED` (13 points, uncertainty) replaces `MISSING_EVIDENCE`. The ML vector treats claimed evidence as missing. Mismatch rules still fire. | `evidence_policy.claimed_evidence_reduces_risk: false` | `test_od_decision_9_gaps.py` |
| Model escalation by label alone | The model escalates only through its bounded score (−10/+25). Its label is ignored. LLM uncertainty ≥ 0.7 → review. | `recommendation.model_escalation: "score_only"` (alternative `"label_or_score"`) | `test_od_decisions_2_5_9.py` |

**Adversarial and property tests** (`test_od_decision_9_adversarial.py`, 160 cases):
* Six known and five undetected injections, in three text fields, on four base requests. None relaxes the outcome or removes a hard violation, and free text never lowers the rule score.
* 10,000 seeded random model outputs (score, uncertainty, label, findings) on four base requests. None edits rule signals or hard violations, blocks alone, unblocks, relaxes the rule floor, or leaves the blending bounds.
* Agent-controlled budget, timestamp and evidence fields never relax the service outcome. The effective expiry stays ≤ T + 48 h, and `assessed_at` is always server time.

**Multi-request tests:**
* `test_od_multi_request_episodes.py` replays **all 13** seed episodes (33 steps) through the API, rules-only. It covers replay, structuring, near-duplicates, approval expiry, reservation TTL, attempted spend and unverified outcomes.
* The concurrency tests are listed under "Test results" below.

**Documented limitations (tested, not fixed):**
* **Prompt injection is pattern-based.** Paraphrased ("let this through without the usual checks"), non-English (Spanish, German and Arabic tested) and obfuscated ("1gn0re") injections are **not** flagged. Because text can only add points, they cannot relax an outcome, but they are not detected either. Pinned by `test_documented_limitation_paraphrased_and_non_english_injection`.
* **Near-name evasion.** Merchant identity uses the exact normalized name. "Example Hosting Billing Dept" on a lookalike domain loses `MERCHANT_DOMAIN_CHANGED` (40 points). `LOOKALIKE_DOMAIN` still fires and the outcome stays review. Pinned by `test_documented_limitation_near_name_evades_domain_changed_rule`.
* **Fabricated but internally consistent evidence** is neither detected nor rewarded. Nothing is verified against a provider.
* The ML baseline was **not** retrained. It still learned synthetic artifacts (see the README).

**New benchmark:** a locked, independently annotated benchmark is *designed*, not built. See [`BENCHMARK_ANNOTATION_PROTOCOL.md`](BENCHMARK_ANNOTATION_PROTOCOL.md). Nothing was trained or tuned on the existing test split.

## 10. Localhost only

| | |
|---|---|
| Config | None to loosen. `serve` accepts only `127.0.0.1`, `::1` or `localhost`, and exits for any other host. |
| Code | `__main__.LOOPBACK_HOSTS`; the outermost `api.LoopbackOnly` ASGI middleware returns 403 to non-loopback peers and audits `rejected:non_loopback_client`. IPv4-mapped loopback is accepted. Proxy headers (`X-Forwarded-For`) are not trusted. |
| Tests | `test_od_decision_10.py`: <ul><li>six peer addresses</li><li>four refused bind hosts</li><li>three allowed bind hosts</li></ul> |

There is no payment execution and no WhatsApp integration. `payment_executed` is always `false`.

---

## Defect found and fixed during final validation (pre-existing, P0)

**What failed.** One full-suite run failed `test_p0_1_replay.py::test_concurrent_across_real_uvicorn_workers` with `Connection reset by peer`.

**Root cause.** It reproduced in isolation as a real defect, not a test flake: when several workers open the **same new** SQLite file at once, `PRAGMA journal_mode=WAL` raised `database is locked` without waiting for the busy timeout. A uvicorn worker that fails at startup makes the uvicorn supervisor stop the whole server.

**Before the fix:** 50 of 120 racing opens failed.

**Fix.** `AssessmentStore._enable_wal` retries the switch until the store's busy timeout expires, then re-raises.

**After the fix:**
* 0 of 120 racing opens fail.
* New regression test `test_concurrent_first_open_of_a_fresh_database`: 4 processes × 10 fresh files, lock-stepped by a barrier. It failed before the fix.
* 40 consecutive loops of the timing-sensitive suites: all passed.

## Policy assumptions (round 1)

Round 2 confirmed assumptions 1, 2, 3, 5 (approval-window start), 6 and 8. It replaced assumption 4 with F4, a demo-only default budget.

1. **"Material unresolved risk" in degraded mode** means any rule points (`review_if_rule_score_at_or_above: 1`).
   * A degraded request with a rule score of 0 stays `allow_for_review`, but is marked degraded.
   * In practice almost every request carries evidence-related points, so degraded requests nearly always go to review.
2. **Claimed evidence costs the same as missing evidence** (13 points). The decision said fabricated evidence must not lower risk; the exact weight is my choice. It is the main cause of the higher false-positive rate.
3. **Near-duplicate tolerance:** 2% of the larger amount or one minor unit, whichever is larger, inside the existing duplicate window, for the same agent and merchant identity (name **or** domain). It scores 30 points, so it always reaches review but never blocks.
4. *(Superseded by F4.)* The `default_agent` values (USD 100, GBP 80, JPY 15000) are demo placeholders. They are allowed only in a configuration labelled `demo`, and their use always requires review. An operator configuration cannot contain them.
5. **The approval window runs from the approval time**, not from the original reservation.
   * A payment attempt inside the window moves the entry to `attempted`, which does not expire.
   * Releasing a stuck attempt needs a verified provider outcome (Phase 2 reconciliation).
6. **Sharing is owner-level, opt-in and same-owner only.** Shared history counts only verified completed payments, and is used only for merchant familiarity, never for another agent's spend baseline.
7. **The read-only check uses permission bits** on the file and its directory. It does not detect a root-owned process, read-only bind mounts or a filesystem mounted read-only by other means. See `MODEL_INTEGRITY.md`.
8. **The combination block ships disabled.** The decision allows combination blocks; it does not say which to enable.
9. **The loopback check** trusts the ASGI peer address. Starlette's in-process `testclient` peer is accepted so that tests can run.
10. **The seed episode `ep-near-duplicate`** was updated to expect the new rule (it was a recorded `known_gap`). The seed is author-written dev data, not a measurement.

## Unresolved gaps

Round 2 left all of these open except one: the evidence weight is now an owner decision (F2), not an assumption.

* **Review load and false positives rose sharply** (validation split, measured *after* the changes and not used to tune anything):

  | Approach | Missed risky | Legitimate flagged | Review rate | Block rate |
  |---|---|---|---|---|
  | Rules | 12/240 = 0.050 [0.029, 0.085] | 25/130 = 0.192 [0.134, 0.269] | 0.616 | 0.068 |
  | Rules + ML | 5/240 = 0.021 [0.009, 0.048] | 45/130 = 0.346 [0.270, 0.431] | 0.689 | 0.068 |

  For comparison, at P0 the legitimate-flagged rate was 0.077 (rules) and 0.139 (hybrid), with recall 0.946 / 0.979. Recall is essentially unchanged, while false positives roughly doubled. The evidence weight is now owner-confirmed (F2). Whether the ML's extra review burden is worthwhile is to be decided on the independent benchmark (F9). These synthetic results are development evidence only. Re-measured after round 2: unchanged.
* **Semantic detection is weak.** On the seed out-of-distribution cases, rules miss 5 of 10 risky cases (recall 0.50, CI 0.24–0.76). Injection detection does not cover paraphrases or other languages.
* **No independent benchmark exists yet.** There is only a protocol.
* **The ML baseline is not retrained** and relies on synthetic artifacts. One seed episode (a legitimate renewal) is escalated by the hybrid.
* **Deployment security is missing:** no authentication, authorization, rate limiting, TLS or multi-tenant isolation, and SQLite (one host). This is why the API is localhost-only (F8).
* **Approval binding is enforced in the store only.** No approval service, signed approval token or user interface exists yet. Phase 2 must show the owner exactly the terms it binds.
* **No provider reconciliation exists yet** (F5, Phase 2). Uncertain outcomes have no handling path.
* **The ML advisory test needs the git-ignored artifact.** On a clean checkout without the pinned artifact it is skipped, not failed.
* **`attempted` (pending) holds never expire.** They need provider reconciliation (Phase 2).
* **Model staging defaults to the system temp directory.** Production should set `AGENTPAL_MODEL_STAGING_DIR` to a dedicated path. A cold-cache first hash of the 1.1 GB GGUF took 117 s, which delays startup (warm-up runs before serving). Integrity checks do not protect against a root process.
* **Ollama integrity** relies on the digest the server reports.
* **The host clock is trusted** and must be NTP-synchronised.
