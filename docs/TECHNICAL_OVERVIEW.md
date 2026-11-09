# AgentPal technical overview

_Full engineering reference (moved from the original README). Start with the [main README](../README.md)._


AI agents **request** payments; AgentPal decides what an agent may do and a human owner decides
what is paid:

* deterministic policy (budgets, per-transaction limits, capabilities, risk rules) runs first,
  and an AI model is **advisory only**;
* an **authenticated owner** approves the **exact terms** (amount, currency, merchant, PayPal payee,
  purpose); any change invalidates the approval, and an agent can never approve itself;
* payments run through **PayPal Sandbox** (orders with buyer approval, merchant payee binding,
  refunds, payouts, reconciliation), and only a **verified PayPal outcome** counts as completed;
* advisory evidence for the owner: **Channel3** market check and better deals, **Kernel**
  cloud-browser page checks; neither can approve, change terms or skip approval;
* everything important is audited. Sandbox only: `is_real_money` is always false.

Security in place: owner tokens (apo_), agent keys (apk_), optional TOTP MFA for approvals,
per-agent capabilities, rate limits (shared SQLite), loopback by default with optional TLS and a
separate public webhook listener, signature-verified PayPal webhooks, hash-locked dependencies.
It is a hackathon sandbox build, **not production-ready** (no live PayPal, single-node SQLite).

### Run it yourself

`pip install -r requirements.txt && python -m agentpal start`, then open the printed dashboard link and enter the one-time setup code. Full guide: [`docs/SELF_HOSTING.md`](SELF_HOSTING.md).

### Quick start (/v1 developer platform)

```sh
pip install -r requirements-dev.txt
export AGENTPAL_DB_PATH=$PWD/agentpal.db            # identity/wallet DBs are created beside it
python -m agentpal new-owner alice --name "Alice"   # prints ONE owner token (apo_...) once
python -m agentpal serve --platform                 # http://127.0.0.1:8000, docs at /docs
```

Then follow [`docs/DEVELOPER_API.md`](DEVELOPER_API.md) or the Python SDK
([`docs/SDK.md`](SDK.md)): the owner registers agents (`POST /v1/agents` returns an
`apk_` key once), creates wallets, and approves requests. PayPal, Channel3 and Kernel turn on
when their environment variables are set (`PAYPAL_SANDBOX_CLIENT_ID/SECRET`,
`CHANNEL3_API_KEY`, `KERNEL_API_KEY`).

### History

This project grew from a Phase 1 risk-assessment service (assessment only). Its P0
remediation and owner decisions are recorded in
[`docs/P0_REMEDIATION_REPORT.md`](P0_REMEDIATION_REPORT.md) and
[`docs/OWNER_DECISIONS.md`](OWNER_DECISIONS.md); the original `/assess` API still exists.
`allow_for_review` means *no configured blocking condition was found*. It is **not** an
authorization, and a risk score is not proof of fraud.

## Architecture

See [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) (decision policy, Phase 2 design) and [`docs/TRUST_AND_LEDGER.md`](TRUST_AND_LEDGER.md) (trust states, ledger, currency, time).

```
request JSON ─▶ schema validate/normalize ─▶ atomic claim + spend snapshot + reservation (one DB transaction)
             ─▶ features (normalized text/domains, server time, operator budgets, verified history)
             ─▶ deterministic rules ─┐
  analyzer chain, each call bounded ─┴─▶ orchestrator ─▶ RiskAssessment ─▶ SQLite (requests, ledger, trust records, audit)
  (llamacpp | ollama | ml | heuristic; model artifacts verified against pinned digests)
```

* **Rules** (`agentpal/rules.py`) emit reason codes with evidence, in three kinds:
  * `hard_violation` (always `block`; no model can override). These are: `DUPLICATE_REQUEST_ID`, `CURRENCY_NOT_ALLOWED`, `NO_SPENDING_POLICY`, `AMOUNT_ABOVE_HARD_LIMIT`, `BUDGET_EXCEEDED`, `CUMULATIVE_LIMIT_EXCEEDED`, `SPLIT_TRANSACTION_EXCEEDS_HARD_LIMIT`, `REQUEST_EXPIRED`, `REQUEST_LIFETIME_EXCEEDS_MAX` and `BLOCKED_MERCHANT_DOMAIN`, plus any **enabled** `combination_blocks` entry (`COMBINATION_BLOCK:<name>`).
  * `risk_indicator`: lookalike or confusable merchant, duplicate intent, purpose/merchant mismatch, injection, and so on.
  * `uncertainty`: missing or unverifiable information. It is never fraud evidence, and its total is capped at 30 points.
* **`block` comes only from deterministic policy violations** (owner decision 2).
  * Soft indicators, however many, produce `require_review` (rule score ≥ 25).
  * A combination of soft signals can block only through an explicit, enabled entry in `recommendation.combination_blocks`. The one shipped entry (`impersonation_lookalike`) is **disabled**.
  * A model can never cause or remove `block`.
* **Trust boundaries (P0):**
  * Budgets and limits come only from the operator file [`config/spend_policy.json`](../config/spend_policy.json), per agent and per currency. Agent fields such as `monthly_budget` are ignored for enforcement.
  * The shipped file is a **demo configuration** (`"configuration": "demo"`); its amounts are placeholders, not approved limits.
  * An `operator` configuration (template: [`config/spend_policy.operator.example.json`](../config/spend_policy.operator.example.json)) cannot define a default budget, so an agent it does not list is rejected (`NO_SPENDING_POLICY`).
  * In the demo, an unlisted agent gets placeholder limits plus `DEMO_DEFAULT_BUDGET_APPLIED`, which always requires review.
  * Every time window uses **server receipt time**.
  * Only **verified completed payments of the same agent** count as trusted history. An owner may opt in to sharing verified history across *their own* agents (`owners.<id>.share_verified_merchant_history_across_agents`, default `false`).
  * Approvals hold budget for `approval_validity_minutes` (15). Reserved, approved, attempted, completed and failed amounts are tracked separately (`store.spend_breakdown`).
* **Analyzers** sit behind one provider-independent interface: a local LLM (llama.cpp or Ollama), the JSON logistic-regression baseline, and the heuristic fallback.
  * Every backend call is bounded by `AGENTPAL_MODEL_TIMEOUT_SECONDS`.
  * Artifacts are verified against SHA-256 digests in [`config/model_manifest.json`](../config/model_manifest.json); see [`docs/MODEL_INTEGRITY.md`](MODEL_INTEGRITY.md).
* **Orchestrator policy:**
  * **Deterministic rules are the security authority. Models, including the ML baseline, are advisory**: they can add a review but never block, unblock or relax. No model here is a proven fraud detector.
  * A model may move the rule score by −10 / +25 at most.
  * A model escalates only through that bounded score (`model_escalation: "score_only"`); its label alone is ignored. It can never relax the rules' recommendation or cause `block`. LLM uncertainty ≥ 0.7 requires review.
  * A model timeout or failure falls back to the rules and marks the assessment **`degraded`** (`degraded_reason`, signal `MODEL_UNAVAILABLE_OR_TIMED_OUT`). Any rule points then require review (`degraded_mode.review_if_rule_score_at_or_above: 1`). Failure is never treated as evidence of safety.
  * Agent-claimed evidence never lowers risk (`evidence_policy.claimed_evidence_reduces_risk: false`; signal `EVIDENCE_UNVERIFIED`).
* **Policy files:** risk rules are in [`config/risk_policy.json`](../config/risk_policy.json) (`rules_version` `2026.11.2-final`); budgets, limits, approval validity and owner sharing are in `config/spend_policy.json` (`2026.11.2-final-demo`).

## Setup

Requires Python ≥ 3.11 (verified on 3.13, Linux, 4 CPUs, no GPU).

```bash
cd agentpal
python3 -m pip install -r requirements-dev.txt   # fastapi, pydantic, uvicorn, httpx, publicsuffixlist, scikit-learn, numpy, pytest
```

For a reproducible runtime install (deployment), use the hash-locked set: `python3 -m pip install --require-hashes -r requirements.lock`. Regenerate it with `pip-compile --generate-hashes --strip-extras -o requirements.lock requirements.txt` and review the diff.

Configuration is read from environment variables; see [`.env.example`](../.env.example) (placeholders only). No API keys are required.

Notes:
* Development SQLite files created before P0 are refused with a clear error. Move them aside.
* The ML artifact is git-ignored. After `train`, the backend stays unavailable (and the rules are used) until you review the artifact and pin it (`make pin-ml`), because model integrity is required by default.

## Commands (all verified in this branch)

| Task | Command |
|---|---|
| Run all tests | `python3 -m pytest` |
| Generate synthetic dataset (seed 20260 → `data/`) | `python3 -m agentpal data` |
| Fit ML baseline (→ `artifacts/baseline_classifier.json`) | `python3 -m agentpal train` |
| Pin a model artifact's digest (operator action) | `python3 -m agentpal pin-model ml artifacts/baseline_classifier.json --source "..."` |
| Synthetic evaluation, validation split | `python3 -m agentpal evaluate --split validation` |
| Synthetic test split (**retired for selection**, regression check only) | `python3 -m agentpal evaluate --split test` |
| AgentPal-Bench seed dev set (single cases + API episodes) | `python3 -m agentpal bench` |
| Lock a benchmark before selection | `python3 -m agentpal bench-lock --note "..."` |
| Assess example files | `python3 -m agentpal assess examples/requests/*.json` |
| Start the API (http://127.0.0.1:8000, docs at `/docs`; **loopback only**, other hosts are refused) | `python3 -m agentpal serve` |

### API

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Status, rules version, active backend, `payment_execution_enabled: false` |
| POST | `/assess` | Validate and assess a request (see status codes below) |
| GET | `/assessments/{request_id}` | Stored assessment (404 if unknown) |
| GET | `/model-info` | Backends, model names and versions, availability, integrity status |

`/assess` status codes:
* **403** client is not on loopback (every endpoint; audited as `rejected:non_loopback_client`).
* **422** invalid input; the response never echoes submitted values.
* **413** over 32 KB, also enforced on chunked or streamed bodies.
* **400** malformed `Content-Length`.

Behaviour covered by tests, including real uvicorn processes:
* Concurrent submissions of one `request_id` (threads, processes, two uvicorn workers): exactly one is assessed, and every other gets a deterministic `block` (`DUPLICATE_REQUEST_ID`).
* Several workers starting together on a new database file all start (a journal-mode race that could stop a multi-worker server was fixed during the owner-decision work).
* Rejected requests (422/413/400) are audited with error codes only. A failing audit write never changes the response.
* The assessment includes the server-computed `effective_expires_at`.

### Example requests and outputs (re-measured on `agentpal-owner-decisions`)

Default chain (`ml,heuristic`, ML artifact pinned), fresh in-memory database, operator budgets from `config/spend_policy.json`:

| File | Score | Category | Recommendation | Key rule findings |
|---|---|---|---|---|
| 01_legit_hosting | 19 | low | allow_for_review | MISSING_EVIDENCE |
| 02_legit_with_invoice | 14 | low | allow_for_review | EVIDENCE_UNVERIFIED (claimed invoice does not lower risk) |
| 10_lookalike_domain | 100 | critical | require_review | LOOKALIKE_DOMAIN, MERCHANT_DOMAIN_CHANGED, MERCHANT_NAME_DOMAIN_MISMATCH, UNAPPROVED_MERCHANT |
| 11_prompt_injection | 93 | critical | require_review | PROMPT_INJECTION_ATTEMPT |
| 12_budget_exceeded | 100 | critical | block | BUDGET_EXCEEDED, CUMULATIVE_LIMIT_EXCEEDED (hard) |
| 13_unexpected_recurring | 80 | critical | require_review | RECURRING_NOT_EXPLAINED, UNUSUAL_BILLING_INTERVAL |
| 14_purpose_mismatch_giftcards | 100 | critical | require_review | PURPOSE_MERCHANT_MISMATCH, PRESSURE_LANGUAGE, HIGH_RISK_PAYMENT_METHOD_LANGUAGE |
| 15_missing_info | 89 | critical | require_review | UNAPPROVED_MERCHANT, LARGE_SHARE_OF_BUDGET, MISSING_JUSTIFICATION / EVIDENCE / DOMAIN |
| 16_evidence_mismatch | 98 | critical | require_review | EVIDENCE_AMOUNT_MISMATCH, EVIDENCE_DOMAIN_MISMATCH, EVIDENCE_UNVERIFIED |
| 90_invalid_negative_amount | — | — | HTTP 422 | "amount must be greater than zero" |

Changes from P0:
* **10** and **14** were `block` purely from soft signals. They are now `require_review` (owner decision 2). Enabling the `impersonation_lookalike` combination would block **10** again, explicitly.
* **02** and **16**: a claimed invoice is unverified, so it no longer lowers risk (owner decision 9).

Changes from Phase 1:
* **12** also trips `CUMULATIVE_LIMIT_EXCEEDED`.
* **15** now has an operator budget (USD 100), where previously there was none because it was agent-supplied and absent. It therefore gets `LARGE_SHARE_OF_BUDGET`.

**Why `01` and `02` are no longer flagged (ML-03, fixed 2026-10-29).**
* **What went wrong.** Earlier versions of this table showed both legitimate requests at 38 / `require_review`, escalated by the ML baseline.
  * Owner decision F2 made the ML feature `missing_evidence` 1.0 for **every** request (claimed evidence counts as missing).
  * The pinned artifact had been trained while that feature still varied, so it added a constant **+1.72 logit** to every assessment.
* **The fix.** The model was retrained with the unchanged procedure (fit on train, C/threshold on validation) and re-pinned (`lr-20261029-C1.0`). The constant feature now contributes 0.
* **The consequence.** On the validation split and on the bench v2 dev set, rules + ML now gives **the same results as rules alone**. The hybrid's earlier "extra recall" came from the stale offset. See `docs/ML_AUDIT.md` (ML-03) and `docs/AUDIT_LEDGER.md`.

## Models

| Backend | Status | Details |
|---|---|---|
| `ml` (default chain) | Trained in Phase 1; **pinned digest**; **advisory only**: it can add a review but never blocks or relaxes. Not a proven fraud detector. Whether its extra review burden is worthwhile is to be decided on the independent benchmark. | Logistic regression on 34 rule-derived features (it never reads text). Retrained 2026-10-29 as `lr-20261029-C1.0` (C=1.0, threshold 0.30 chosen on validation, 1,110 synthetic training rows) to fix ML-03. **On current data it adds nothing over the rules.** |
| `heuristic` | Always available | Deterministic fallback mirroring the rules. |
| `llamacpp` | **Fine-tuned AgentPal model (run 2)**, opt-in and advisory. Qwen3-1.7B + LoRA, trained 2026-10-30 on Modal and exported to GGUF Q4_K_M; pinned digest `23bd2fde…`; served without rule findings, with thinking disabled and on all CPU cores. | **Development evidence only.** On bench v2 dev, rules + model miss 8/137 risky units vs 40/137 for rules alone, with false positives 20 vs 19 of 73; 0 invalid outputs. CPU latency p50/p95 is 6.9/10.7 s against the 30 s timeout. See `docs/training_runs/2026-10-30-modal-run2/`. |
| `ollama` | Implemented; tested only with a mocked server | Integrity check compares the server-reported digest (limitation documented). |

One language model has been fine-tuned: the opt-in `llamacpp` Qwen3-1.7B + LoRA run 2 above (development evidence only, advisory, not part of the hosted default). The logistic-regression baseline was retrained once (ML-03 fix, same procedure). The demo AI agents (Shopper, the two companies) use hosted open-weight models through Groq and OpenRouter; those are separate from the risk analyzer.

## Evaluation: what the numbers do and do not mean

**1. Synthetic, in-distribution (historical).** These are from the Phase 1 run at commit `c87a509`, rules `2026.10.1`, test-split SHA `bfb5c3d7…66f8`. They are preserved unchanged in `docs/evaluation_report_synthetic.json` and `docs/validation_report_synthetic.json`.

| Approach (synthetic test, n=370) | Precision | Recall [95% CI] | FPR [95% CI] |
|---|---|---|---|
| Rules only | 1.000 | 0.950 [0.915, 0.971] | 0.000 [0, 0.029] |
| ML only | 0.979 | 0.954 [0.920, 0.974] | 0.038 [0.017, 0.087] |
| Rules + ML | 0.979 | 0.958 [0.925, 0.977] | 0.038 [0.017, 0.087] |
| Rules + Qwen2.5-1.5B (60-item sample) | 0.800 | 1.000 [0.926, 1] | 1.000 [0.757, 1] |

* These approaches are **statistically indistinguishable**.
* The synthetic generator and the rules were written by the same person, and text templates repeat across splits. These numbers show the pipeline matches its generator. They are **not** evidence of generalization or of real-world accuracy.
* The test split was inspected during the review, so it is now **retired for model or threshold selection**. `evaluate --split test` marks the report `retired_for_model_selection` and prints a warning.
* **Regression check after P0:** the validation-split metrics under rules `2026.11.0-p0` are identical to Phase 1 (rules P 0.958 / R 0.946 / FPR 0.077; hybrid P 0.929 / R 0.979 / FPR 0.139). Every report now includes Wilson 95% intervals and the evaluated artifact's SHA-256.
* **After the owner decisions** (rules `2026.11.1-od`; re-measured unchanged under `2026.11.2-final`; development evidence, not real-world performance; validation split, n = 370: 240 risky, 130 legitimate). These were measured, **not** used to tune anything. Missed-risky and false-positive rates are reported separately, as decision 1 requires:

  | Approach | Missed risky [95% CI] | Legitimate flagged [95% CI] | Legit. blocked | Review rate | Block rate | Latency p50 / p95 |
  |---|---|---|---|---|---|---|
  | Rules only | 12 = 0.050 [0.029, 0.085] | 25 = 0.192 [0.134, 0.269] | 0 | 0.616 | 0.068 | 1.0 / 2.3 ms |
  | Rules + ML, stale artifact (ML-03; superseded) | 5 = 0.021 [0.009, 0.048] | 45 = 0.346 [0.270, 0.431] | 0 | 0.689 | 0.068 | 0.9 / 1.6 ms |
  | **Rules + ML, retrained `lr-20261029-C1.0`** | 12 = 0.050 [0.029, 0.085] | 25 = 0.192 [0.134, 0.269] | 0 | 0.616 | 0.068 | 0.9 / 1.6 ms |

  Recall is essentially unchanged from P0, but false positives roughly doubled. This comes mainly from unverified evidence no longer lowering risk. It is an open owner decision (see `docs/OWNER_DECISIONS.md`, "Unresolved gaps").

**2. Hand-written out-of-distribution pilot (16 cases, review).** On wording the generator never produced:
* Rules caught **5 of 10** risky cases (recall 95% CI 0.24–0.76), and flagged 1 of 6 legitimate cases.
* The hybrid caught the same 5.
* After P0, the same 16 cases in the benchmark seed give the same result. P0 fixed controls; it did not improve semantic detection of paraphrased attacks (P1 work).

**3. AgentPal-Bench v1 (new; [`benchmarks/agentpal-bench-v1`](../benchmarks/agentpal-bench-v1/README.md)).**
* **What exists:** the harness, the lock mechanism and API episode replay are implemented, plus a **seed `dev` split**: the 16 pilot cases and 13 episodes reproducing review probes and owner decisions (approval hold, reservation TTL, attempted spend, unverified outcome). The independent, locked v2 benchmark is designed in [`docs/BENCHMARK_ANNOTATION_PROTOCOL.md`](BENCHMARK_ANNOTATION_PROTOCOL.md) but not yet built.
* **Why the seed proves little:** it was written by the rule author (`independent: false`), so it is a harness and regression check only. No independent `locked` split exists yet, and no claim of generalization is made.
* **Seed results:**
  * **Single cases, re-measured after the ML-03 fix.** Rules and the hybrid both miss 5 of 10 risky cases (recall 0.50 [0.24, 0.76]) and flag 3 of 6 legitimate ones. The classifier alone misses 6 and flags 2.
  * **Earlier result (superseded).** The hybrid's "4 missed" came from the stale constant offset.
  * **Episodes.** Rules-only and the hybrid both pass 33 of 33 steps; the earlier legitimate-renewal false positive is gone. The P04 near-duplicate gap is detected.

## Security notes

* **Localhost only** (owner decision 10 / F8). `serve` refuses non-loopback bind addresses, and the app returns 403 to non-loopback peers.
  * Client identity comes from the socket peer only. `X-Forwarded-For`, `X-Real-IP` and `Forwarded` are ignored, and `serve` disables uvicorn's proxy-header handling.
  * Do not expose the API until authentication, authorization, rate limiting, TLS and multi-tenant isolation exist and are tested.

* Secrets are never in source. `.env`, local DBs, model weights, generated data and reports are git-ignored.
* Request bodies are limited to 32 KB, enforced while streaming. Unknown fields are rejected, and timestamps must carry a timezone.
* Text is canonicalized before every detector and identity comparison: NFKC, ignorable characters removed, confusables folded. Domains use the Public Suffix List; approval is exact-host only. Nothing is fetched from merchant URLs.
* Agent text is always treated as data. Hard rules always apply, model influence is bounded, and every model call has a timeout.
* Model artifacts are loaded only if they match pinned digests. A match proves byte-identity, not safety. GGUF models must sit on read-only storage; llama.cpp loads a private copy that was hashed while copying, after a startup warm-up.
* The audit log stores codes and statuses, not raw content. Rejections are audited, and secrets are redacted.

## Known limitations and open items

See also `docs/OWNER_DECISIONS.md` ("Policy assumptions", "Unresolved gaps").

* **Review load is high.** On the validation split, 62–69% of requests are routed to review, and 19–35% of legitimate requests are flagged. The evidence weight needs an owner decision on an independent benchmark.
* **The detectors are still heuristics.** Not detected:
  * paraphrased, non-English and obfuscated injections;
  * paraphrased purpose mismatches;
  * internally consistent fabricated evidence.

  Tests show these cannot *relax* an outcome, but they are missed.
* **Near-name evasion.** Appending words to an approved merchant's name drops `MERCHANT_DOMAIN_CHANGED`. The outcome still stays review because of `LOOKALIKE_DOMAIN`.
* **The ML baseline was retrained only once (ML-03 fix) on synthetic data.** It still escalates on synthetic artifacts (`missing_evidence`, `recurring`).
* Script detection is approximated from Unicode character names. Confusable data is Unicode 18.0.0 (vendored).
* **The SQLite store is development-only.** Multi-worker safety relies on SQLite file locking on one host.
* **The trusted clock is the host clock**, which must be NTP-synchronized.
* **Pending provider attempts are not released automatically.** Expired or ambiguous attempts keep their allowance reserved as `reconciliation_required` until a verified PayPal read or webhook settles them (Phase 2 reconciliation, SEC-16).
* **All spend amounts in the shipped demo configuration are placeholders**, including the default-agent USD 100 / GBP 80 / JPY 15000. They are not approved spending limits.
* **Model integrity checks do not protect against a privileged (root) process.** A cold first hash of the GGUF took about 117 s before the API started serving. Set `AGENTPAL_MODEL_STAGING_DIR` to a dedicated path (see `docs/MODEL_INTEGRITY.md`).
* **Fabricated documents are not detected.** Unverified evidence simply never lowers risk.
* **There are no real-world labels**, so no real-world accuracy claim is possible. No independent benchmark exists yet.

## Phase 1 hand-off (historical)

_This section is kept for history. Phase 2 is built: owner decisions, payment execution, PayPal verification and reconciliation are exposed through the `/v1` API and the owner dashboard (see `docs/DEVELOPER_API.md`)._

`RiskService.assess()` is the integration point.

* **Owner decisions, payment attempts and verified payment outcomes** are recorded through `store.record_owner_decision`, `record_payment_attempt` and `record_payment_outcome`. (Phase 1 note: these were internal only; in Phase 2 they are driven by the `/v1` approve, execute and capture routes.)
* **Approvals and attempts must carry `ApprovalTerms`** (request, agent, amount, currency, merchant, purpose). Their digest must match the one stored when the request was assessed; otherwise they are refused. The approval UI must show the owner exactly these terms.
* **Before retries are allowed, Phase 2 must implement:**
  * provider reconciliation of every attempted payment;
  * explicit handling of pending, failed and uncertain outcomes.

  An attempted payment is never completed spending.
* **Payment idempotency:** PayPal's `PayPal-Request-Id` header is per API call (create, authorize, capture), optional, and has no documented retention window for Orders. Phase 2 should derive one key per (`request_id`, operation), persist it before the call, and resend it only when retrying that same call ([PayPal idempotency](https://developer.paypal.com/api/rest/reference/idempotency/)).
* **Webhooks:** must be verified before `record_payment_outcome(verified=True)` is called.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#how-phase-2-connects).
