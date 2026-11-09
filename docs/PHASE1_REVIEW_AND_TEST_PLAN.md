# AgentPal Phase 1: Independent Review, Research and Test Plan

| | |
|---|---|
| Reviewed revision | branch `agentpal-phase1`, commit `c87a509`, clean working tree |
| Review date | 2026-10-28 |
| Scope | `agentpal/` only. The storefront code was not inspected or changed. |
| What changed in this review | Only documentation was added: this file, `docs/TEST_MATRIX.json` and `docs/review/*`. The policy, thresholds, trained model, source and tests are unchanged. |
| Held-out test split | Not re-scored. All new measurements use ad-hoc inputs, the train and validation splits, or the counts already saved in `docs/evaluation_report_synthetic.json`. |

---

## 0. Executive summary

**The software skeleton is sound.** Specifically:

* Schema validation, reason-coded rules and the provider-independent analyzer interface work.
* Schema-validated model output works.
* The orchestrator never lets a model relax a hard block. A randomized property test is still recommended (§5).
* The persistence and audit trail work, and the codebase has no payment or messaging code paths.
* All 108 tests pass.

**The risk engine is not yet a trustworthy input to payment authorization.** Seventeen targeted probes found several ways around its controls; every probe confirmed its hypothesis (see §3). The most serious:

| ID | Finding | Severity |
|---|---|---|
| P09 | **Replay protection fails under concurrency.** 8 simultaneous submissions of one `request_id` were *all* assessed as new, with no `DUPLICATE_REQUEST_ID`. | P0 |
| P01, P02 | **Budget and limit controls trust the agent.** Omitting `monthly_budget` removes the `BUDGET_EXCEEDED` hard block. Splitting a purchase into near-limit parts (structuring) is not detected; there is no cumulative spend or velocity control. | P0 |
| P03, P06 | **Time-based controls use agent-supplied timestamps.** Back- or forward-dating `created_at` evades duplicate detection. Omitting `expires_at` means a request can never expire. | P0 |
| P07, P08 | **History trust is based on our own recommendation, not on human approval.** A single `allow_for_review` result marks an unapproved domain as "known" *for every agent*. Flagged requests still count toward an agent's spend baseline. | P0 |
| P05 | **Limits ignore currency.** JPY 6,000 (≈ USD 40) is hard-blocked, while GBP 4,999 passes the "5,000" hard limit. | P0 |
| P13, P16 | **Unverifiable evidence lowers risk.** Attaching any fabricated invoice removes `MISSING_EVIDENCE`. That flips the demo request to `allow_for_review` under the default ML chain. | P1 (design) |

**The evaluation shows the pipeline matches its own generator. It does not show generalization.**

* The three approaches cannot be told apart. Their test 95% confidence intervals for recall and FPR overlap almost entirely (228, 229 and 230 of 240 positives caught).
* Text templates leak across splits: in 7 of 10 categories, 100% of validation free-text strings appear verbatim in train.
* On a 16-case hand-written out-of-distribution pilot, the rules caught **5 of 10** risky requests, against **0.95** recall on synthetic test. The ML hybrid caught the same 5.
* The Qwen result (FPR 1.0 on 12 negatives, 95% CI 0.76–1.0) is partly an *integration-policy artifact*: any model `require_review` escalates, whatever its score (P17).

**A correction to the README.** It attributes the demo escalation to "no history and no evidence". The actual drivers are `missing_evidence` (contribution +1.72) and `recurring` (+0.77); `no_history` contributes only +0.33 (P15). The README should be corrected.

**Recommendation:** do **not** open a PR yet. Fix the P0 items first: atomic replay protection, server-side budgets and ledger, server time, history trust, currency-aware limits and text normalization. That is about 4–6 engineering days (§7), and each fix needs a regression test from §5. Then open a draft PR scoped as an "assessment prototype". Model-quality work (P1) should wait for the new benchmark in §4.

---

## 1. Verified architecture and implementation findings

### 1.1 Method
* I read every source module.
* I ran the full test suite: `python3 -m pytest` → **108 passed** from 70 test functions.
* I wrote three read-only analysis scripts, all in `docs/review/`. Their raw outputs are committed next to them.
  * `probes.py` (17 behavioural probes)
  * `eval_analysis.py` (methodology checks on train/validation plus saved test counts)
  * `ood_pilot.py` (16 hand-written cases)

### 1.2 Claims checked against the source code

| Claim (README / ARCHITECTURE) | Verified? | Evidence |
|---|---|---|
| Hard violations always produce `block`, and the score is at least 75 | ✅ | `orchestrator.py:111-114`; `rules.py` sets points to 100 for the hard kind |
| A model cannot relax the rules' recommendation | ✅ | `orchestrator.py:116` (`_stricter` against the rules-only recommendation) |
| A model cannot cause `block` by itself | ✅ (with nuance) | `orchestrator.py:128-131`. **Nuance:** "rules block" includes *soft indicators summing to 75 or more* (P14), not only hard violations. The documentation should say "deterministic block", not "hard-rule block". |
| A model moves the score by −10 / +25 at most | ✅ for the score | `orchestrator.py:107-110`. **Gap:** the *recommendation* is not bounded by the score. Any model `recommendation != allow_for_review` escalates (`:117-118`), and so does LLM uncertainty ≥ 0.7. This is the mechanism behind the Qwen FPR (P17). |
| Missing information is capped at 30 points | ✅ | `rules.py` (`max_uncertainty_points`) |
| Fallback to the heuristic on unavailable, malformed or crashing backends | ✅ | `orchestrator.run_analyzers`; tested |
| LLM timeout (`AGENTPAL_MODEL_TIMEOUT_SECONDS`) | ⚠️ **Partially false** | Applied only to Ollama (`llm.py:76-100`). llama.cpp inference has **no timeout**, and a global lock serializes all LLM calls. |
| Replayed `request_id` is blocked and the original assessment kept | ⚠️ **True sequentially, false concurrently** | `service.py` reads history and then saves in a separate step, with no reservation in between (check-then-insert). P09: 8/8 concurrent submissions were "assessed". |
| Duplicate detection | ⚠️ Narrow | Requires the *exact* amount and same merchant, inside a window measured from the **agent-supplied** `created_at` (`features.py`, `ref_time`). |
| History contamination fixed | ⚠️ Partially | Flagged requests no longer redefine a merchant's domain. But "trusted" means `recommendation ∈ {None, allow_for_review}`, which is a model/rule output, not an approval (P07). The spend baseline ignores trust (P08). |
| Request size limit of 32 KB | ✅ functionally | `api.py BodySizeLimit`. **Note (code inspection only):** when no `Content-Length` header is sent, it buffers the *entire* body before checking the size. Use an ASGI-level streaming limit or a reverse-proxy limit. |
| Default analyzer chain is `ml,heuristic` | ✅ | `config.py Settings.analyzer_chain` |
| No payment execution, no messaging, no unsafe deserialization | ✅ | `grep` found no PayPal, WhatsApp, pickle, eval, exec or subprocess usage. The ML artifact is JSON. The only outbound HTTP is the operator-configured Ollama URL; `merchant_domain` is never fetched, so there's no SSRF path. |
| Model artifact integrity | ❌ Not implemented | The GGUF and JSON artifacts are loaded without a hash or signature check. |
| Secrets redacted in the audit log and stored assessment | ✅ (pattern-based) | `store.redact`; tested. Regex redaction can miss novel token formats. |
| Rejected (422) requests are audited | ❌ | Validation failures happen before the service is called, so they leave no audit row. |
| Evaluation report matches the dataset | ✅ | Report SHA `bfb5c3d7…66f8` matches `data/manifest.json` |

### 1.3 Structural observations
* **Agent-supplied context is trusted as fact.** `monthly_budget`, `budget_spent_this_period`, `created_at`, `expires_at` and `evidence` all come from the untrusted agent. Only `approved_merchants`, `blocked_domains` and the hard limits come from the operator.
* **"Known merchant" is global.** It applies across agents (`known_domains` uses the history of all agents), so one agent can launder a merchant's reputation for every other agent.
* **Approval extends to subdomains.** It uses a two-label `base_domain` heuristic, so `attacker-tenant.cloudnimbus.io` inherits approval (P12). The Public Suffix List's private section exists precisely for hosts that issue subdomains to mutually untrusting parties [PSL].
* **No Unicode normalization.** Fullwidth, homoglyph and paraphrased injections evade the regexes (P10), and confusable merchant names are not matched (P11). UTS #39 defines the standard confusable "skeleton" algorithm, which first strips default-ignorable characters [UTS39].

---

## 2. Behavioural probe results (confirmed by execution)

Reproduce with `PYTHONPATH=. python3 docs/review/probes.py`. Every probe ran against the in-memory store with ad-hoc requests. Ⓒ = hypothesis confirmed.

| ID | Hypothesis | Observed | Sev. |
|---|---|---|---|
| P01 | Omitting `monthly_budget` removes the budget hard block | USD 4,900 with budget 100 → `block`. Same request without a budget → `require_review` (43). | P0 |
| P02 | Structuring evades hard limits | 3 requests of ≈4,999 each → all `require_review`, no hard violation, no cumulative signal | P0 |
| P03 | Agent timestamps evade duplicate detection | A duplicate backdated 100 h → `STALE_REQUEST` only; forward-dated → `TIMESTAMP_IN_FUTURE` only | P0 |
| P04 | A near-duplicate (+0.01) is not a duplicate | `allow_for_review`, score 13 | P1 |
| P05 | Limits ignore currency | JPY 6,000 → hard `block`; GBP 4,999 → no hard violation | P0 |
| P06 | No server-side TTL | Without `created_at` or `expires_at`, a request never expires | P0 |
| P07 | One allow-recommendation whitelists a domain for all agents | Attacker: `UNAPPROVED_MERCHANT` (12) → victim agent: score 0 | P0 |
| P08 | Flagged history inflates the spend baseline | A 2,500 spike after 4 flagged ≈900 requests → no `AMOUNT_SPIKE_VS_HISTORY` | P1 |
| P09 | Concurrent replay race | 8 threads, same id → 8 × `assessed`, 8 × `allow_for_review` | **P0** |
| P10 | Injection detector evasion | plain ✔, zero-width ✔ (caught by a second pattern), fullwidth ✘, Cyrillic homoglyph ✘, paraphrase ✘ | P0 (normalization) / P1 (semantic) |
| P11 | Confusable merchant name | "Exаmple Hosting" (Cyrillic а): `MERCHANT_DOMAIN_CHANGED` not raised. The lookalike domain still caught it. | P1 |
| P12 | Approval inherited by subdomains | `attacker-tenant.cloudnimbus.io` → treated as approved | P1 |
| P13 | Fabricated evidence lowers the score | 13 → 0 | P1 (design) |
| P14 | Soft indicators alone can block | lookalike + changed + mismatch = 100 → `block`, no hard violation | Doc / policy decision |
| P15 | Demo escalation drivers | p = 0.53 against threshold 0.30. Top contributions: `missing_evidence` 1.72, `recurring` 0.77, `budget_ratio` 0.33, `no_history` 0.33 | Doc fix + P1 |
| P16 | What lets the demo request pass | An invoice alone → allow (9). History alone → review (35). | P1 |
| P17 | Model recommendation escalation ignores its own score | A constant model output (score 5, `require_review`) → `require_review` | P1 |

---

## 3. Critical assessment of the current evaluation

| Question | Finding | What it means |
|---|---|---|
| Are the metric definitions and positive class correct? | **Yes.** Positive = label 1 ("needs review or block"); predicted positive = `recommendation != allow_for_review`. Precision, recall, FPR and FNR are computed correctly (unit-tested). | The label is "needs review", **not** fraud. None of the numbers say anything about fraud detection. |
| Were all approaches evaluated on comparable examples? | Rules, ML and hybrid: **yes**, the same 370 test rows. Qwen: **no**, only a 60-row stratified subset (6 per category), compared against rules on the *same* 60 rows. | The rules-vs-Qwen comparison is paired and fair. Qwen is not comparable with the 370-row numbers. |
| Is the Qwen sample large enough? | It had 12 negatives and 48 positives. FPR 12/12 has a 95% Wilson CI of [0.76, 1.00]; recall 48/48 has [0.93, 1.00]. | It is enough to conclude the model is *not usable as configured* (FPR at least 0.76 with 95% confidence). It is **not** enough to compare recall or to judge per-category behaviour. |
| Is rules-only performance inflated? | **Very likely.** 7 of the 10 categories have a 1:1 "signature rule" that fires on 67–100% of their examples (`eval_analysis.json`). A trivial "any non-trivial indicator fired" classifier gets 0.946 recall on validation. On the OOD pilot, rule recall drops to 5/10. | Synthetic recall measures *rule–generator agreement*, not detection ability. |
| Does scenario-family splitting prevent leakage? | It prevents **near-duplicate** leakage only: variations of a group stay in one split. It does **not** prevent **template** leakage. There are 41 distinct purpose strings, shared pools, and the same 5 obvious injection strings. In 7/10 categories, 100% of validation free text appears verbatim in train; only 8.1% of validation merchant domains are unseen in train. | Group splitting is necessary but not sufficient. Kapoor & Narayanan identify this kind of train/test dependence as a main source of over-optimism [KN23]. |
| Do features, generator and rules encode the labels? | **Yes, by construction.** The same author wrote the generator templates, the keyword lists and the regexes. The LR's top coefficients are exactly the signature features (`purpose_merchant_mismatch` 1.94, `injection_hits` 1.77, `duplicate_intent` 1.59). It also learned a **spurious** `recurring` weight (+0.77), because every `unexpected_recurring` example is recurring while only some legitimate ones are. | The classifier mostly re-learns rule outputs plus generator artifacts. |
| Was validation used consistently? | It was used for C, the threshold, **and** for three policy fixes and the choice of default chain. | Validation is now "consumed". Its metrics are optimistic and should not be used for further selection without a fresh split. |
| Was the test set kept untouched? | The final report came from a single completed run on frozen policy `2026.10.1` (two earlier runs were killed before writing any report). After that, the test rows were *inspected* for diagnosis (per-category numbers, LLM outputs on 4 negatives), but nothing was tuned on them. | Treat the current test split as **retired for model selection**. Use it only as a regression check. |
| Is there real improvement over rules-only? | **No.** Test: rules 228/240 recall, 0 FP; hybrid 230/240, 5 FP. Overlapping 95% CIs; a 2-example recall difference. On validation the hybrid had 18 FPs, *more than the union* of rules FPs and ML FPs (16), so the blending itself creates false positives. | The ML component's added value is not established. |
| Are a 3.8% FPR and a ~14% validation review rate acceptable? | Unknown. It depends on owner review capacity and the cost of delay, which are not specified. Fraud-detection practice works with very small review budgets: the Worldline/ULB handbook argues an FPR above 0.001 is too high when investigators can check about 0.1% of transactions [FDH-Metrics]. AgentPal's human-approval design is different, because every payment may need approval anyway. | First define the **review budget** (open question Q1). Then select thresholds by cost or budget, not by F1. |
| Calibration | LR on validation: Brier 0.035, ECE 0.056. Reliability is poor in the mid-range: the 0.3–0.4 bin has predicted 0.32 vs observed 0.86, and the 0.4–0.5 bin has 0.49 vs 0.00 (only 5–7 samples per bin). `class_weight='balanced'` and 65% synthetic prevalence mean the probabilities do **not** transfer to real prevalence. | The `probability_to_score` mapping is a rescaling, not a calibration. |

**What the results *do* establish:** the pipeline runs end to end, rules fire on the scenarios they were designed for, the plumbing is reproducible, and the 1.5B LLM as integrated is unusable.
**What they do *not* establish:** generalization, real-world accuracy, any advantage of ML or the hybrid over rules, calibrated probabilities, or acceptable operational review rates.

---

## 4. Proposed independent benchmark: AgentPal-Bench v1

**Goals**
* Measure generalization rather than agreement with the generator.
* Measure stateful attacks.
* Give every approach (rules, LR, GBDT, any LLM) the *identical* examples.

**Design principles** (sources: [KN23], [FDH-Valid], [SR15], [CheckList], [Datasheets]):

1. **Independence of authorship.**
   * The benchmark generator and gold set are written *before* any further rule or feature changes, and frozen with a SHA-256 manifest.
   * Templates, vocabularies and merchant pools must be disjoint from `dataset.py`. This is enforced by a test that asserts zero verbatim overlap of free-text strings and merchant domains.
   * Ideally a second person writes the risky-scenario templates.
2. **Slices**, each reported separately with 95% CIs:
   * `in_dist`: regenerated with new templates but the same scenario types.
   * `paraphrase`: the same intents with unseen wording, including CheckList-style invariance cases [CheckList].
   * `unseen_merchants`: new names, domains and categories (legal, GPU, translation…).
   * `multilingual`: at least Spanish and French justifications.
   * `unicode_adversarial`: NFKC, zero-width, confusables, RTL overrides.
   * `injection_holdout`: injection phrasings never seen by rule authors, including indirect ones in evidence descriptions.
   * `episodes`: **stateful sequences** of 2–10 requests replayed through the real API. These cover structuring, near-duplicates, history poisoning, baseline inflation, concurrent replays and time manipulation.
3. **Hand-labelled gold set.** About 300 requests, written and labelled by two annotators against a written label guide that defines "needs review", "block" and "allow". Report Cohen's κ, adjudicate disagreements, and publish a datasheet [Datasheets].
4. **Locked test.** Split into `dev` (tunable) and `locked`.
   * The `locked` files live outside the repo or are encrypted. They are evaluated only by `make bench-locked`, which logs every access.
   * Run it at most once per model-promotion decision.
5. **Prevalence realism.** Report metrics at the generator's prevalence *and* re-weighted to assumed operational prevalences (for example 5% and 1% positives). Under imbalance, report precision–recall and AP rather than ROC [SR15], [FDH-Metrics].
6. **Temporal validation.** Once real traffic exists, use time-ordered hold-out or prequential validation with a label-delay gap [FDH-Valid]. Episodes get synthetic timestamps now so that the code path exists.
7. **Size rationale.** A 95% Wilson half-width of about ±1.7 points on a 3% FPR needs ≈400 negatives. Detecting a 5-point recall difference between paired systems needs a few hundred positives per compared slice (McNemar). Target ≈1,500 single requests (≥400 negatives) plus ≈200 episodes plus the 300-item gold set.
8. **Reporting.** For each slice:
   * Confusion matrix, precision, recall, FPR, FNR and AP, with bootstrap 95% CIs.
   * Paired comparisons against rules-only (McNemar or paired bootstrap).
   * Review rate on legitimate requests, cost-weighted loss under a stated cost matrix [Elkan01], and calibration (reliability, ECE, Brier) for probabilistic models [SKL-Cal].
   * Latency p50/p95 and peak RSS.

---

## 5. Testing matrix

The full machine-readable matrix is in [`docs/TEST_MATRIX.json`](TEST_MATRIX.json): 74 cases, each with id, area, case, expected outcome, severity, status and how to run it. Status counts:

| Status | Meaning | Count |
|---|---|---|
| `existing` | Covered by the current suite, so it passed in this review | 29 |
| `probe-confirmed-gap` | Executed in this review, and it *failed* the secure expectation | 16 |
| `code-inspection-gap` | Found by reading code; not executed | 4 |
| `verified-by-inspection` | Property holds by code reading, but has no automated test | 4 |
| `proposed` | Not yet written or run | 21 |

**No security test is claimed to pass unless it is marked `existing`.**

The summary below lists the key cases per area.

### A. Unit and schema tests
| Case | Expected | Sev | Status |
|---|---|---|---|
| Zero, negative, NaN, inf, bool, `1e12`, excess decimals | 422 | High | existing |
| JPY 0-decimal and per-currency minor units | Quantized or rejected | Med | existing |
| Invalid, IP and IDN domains; `user@host` | Rejected or flagged | Med | existing |
| Domain with a trailing dot, uppercase IDN, or mixed-script label | Normalized and flagged | Med | proposed |
| Naive vs aware timestamps; expiry before creation | UTC / 422 | Med | existing |
| `created_at` far in the past or future | Server time governs the windows | High | **gap (P03)** |
| Unknown fields, oversized body, >20 evidence items | 422 / 413 | Med | existing |
| Chunked body > limit without `Content-Length` | 413 *without* full buffering | Med | proposed |
| Malformed model output (7 variants) | Fallback, error recorded | High | existing |
| Score boundaries 24/25, 49/50, 74/75 | Correct band and recommendation | High | existing |
| llama.cpp timeout | Fallback within the timeout | High | **gap (code)** |

### B. Rule and policy tests
| Case | Expected | Sev | Status |
|---|---|---|---|
| Each hard rule independently (6) | `block`, code present | Critical | existing |
| Multiple simultaneous hard violations | All listed, single `block` | High | proposed |
| Each indicator rule independently (~20) | Code and evidence present | High | mostly existing |
| Missing-information cap | Missing-only requests ≤ medium | High | existing |
| Budget boundary: amount == remaining, remaining + 0.01 | Allow / hard block | High | proposed |
| Budget omitted, or agent-claimed budget > operator budget | Operator budget governs | **Critical** | **gap (P01)** |
| Cumulative spend over the window > limit (structuring) | Block or review | **Critical** | **gap (P02)** |
| Currency-aware limits | Per-currency limits | High | **gap (P05)** |
| Model cannot relax hard blocks (randomized property, 10k outputs) | Never below the rules' recommendation | Critical | proposed (spot tests exist) |
| Model cannot block alone | No `block` without a deterministic block | Critical | existing (one case) → make it a property test |
| Model score influence within bounds | Δ ∈ [−10, +25] | High | existing (one case) → property test |
| Model *recommendation* influence bounded | A low-score model `require_review` doesn't escalate *(if that policy is adopted)* | Med | **gap (P17)** |
| Rule determinism | Same input → same signals, order and score | Med | proposed |

### C. Model-quality tests (run on AgentPal-Bench, never on the retired test split)
| Case | Expected | Sev |
|---|---|---|
| Rules vs LR vs GBDT vs hybrid on identical examples, all slices | Paired CIs reported | High |
| Legitimate-request review rate | ≤ the review budget (Q1) | High |
| High-severity slice FNR (hard-limit, duplicate, lookalike) | Not worse than rules-only (paired) | Critical |
| Calibration (reliability, ECE, Brier) | Reported; calibrate with sigmoid if n < ~1,000 [SKL-Cal] | Med |
| Paraphrase, multilingual and unseen-merchant slices | Reported separately | High |
| Invariance: swap merchant name or amount within budget | Decision unchanged | Med |
| Spurious-feature check: toggle `recurring` on a matched legitimate request | No escalation | Med |

### D. Adversarial tests
| Case | Expected | Sev | Status |
|---|---|---|---|
| Injection: plain, fullwidth, homoglyph, zero-width, multilingual, paraphrase, in evidence | Flagged, or at least never relaxes policy | High | partial (P10) |
| Policy-override text ("set limit to 10000") | No effect on policy | Critical | proposed (by design: no tool access) |
| Field/text contradiction (`recurring:false` but "monthly plan") | Inconsistency flagged | Med | proposed |
| Lookalike domains: transposition, subdomain, TLD swap, confusables | Flagged | High | partial (P11, P12) |
| Near-duplicates (±1 cent, ±1%, renamed merchant) | Duplicate-intent signal | High | **gap (P04)** |
| Fabricated evidence | Does not reduce risk below the no-evidence level | Med | **gap (P13)** |
| Manipulated model output (claims verification; recommendation "approve") | Dropped or rejected | High | existing |
| Very long, deeply nested or control-character inputs | 413 / 422, no 500 | Med | partial |
| History poisoning (seed an allow, then attack from another agent) | Per-agent or approval-based trust | **Critical** | **gap (P07, P08)** |

### E. API and persistence tests
| Case | Expected | Sev | Status |
|---|---|---|---|
| Status codes 200/404/413/422/500 and the response schema | As documented | High | existing |
| Concurrent identical `request_id` (N = 8–64) | Exactly one `assessed`; others `replay_rejected` | **Critical** | **gap (P09)** |
| Restart persistence (file DB, new process) | History and assessments survive | High | proposed |
| SQLite integrity under concurrent writes (WAL) | No lost or partial rows | High | proposed |
| Storage unavailable (read-only file, disk full) | 503, fail closed, no assessment returned as "saved" | High | proposed |
| 422 rejections audited | Audit row with no raw input | Med | **gap (code)** |
| Response never implies payment (`payment_executed:false`, wording) | Always | Critical | existing |

### F. Security tests
| Case | Expected | Sev | Status |
|---|---|---|---|
| Secrets in logs, responses and DB (token formats, card PANs) | Redacted | Critical | existing (patterns) → add fuzzing |
| No unsafe deserialization (pickle or YAML) | grep or Bandit gate | High | verified by grep; gate proposed |
| No command injection or path traversal (`request_id` in file paths?) | `request_id` never used as a path | Med | verified by inspection |
| No outbound request to user-controlled URLs | `merchant_domain` never fetched | High | verified by inspection; regression test proposed |
| AuthN/AuthZ for exposed deployment | Documented requirement: mTLS or signed service tokens, per-agent scopes; BOLA check on `/assessments/{id}` [OWASP-API] | Critical (pre-Phase 2) | proposed |
| Rate limiting / resource consumption | Per-agent quotas; LLM concurrency limits [OWASP-API API4] | High | proposed |
| Model artifact integrity | SHA-256 pinned in config; refuse mismatches | High | **gap (code)** |
| Untrusted text cannot change policy or call tools | No tool interface exists; keep it that way [OWASP-LLM01] | Critical | verified by design |

### G. Integration and regression tests
| Case | Expected | Sev | Status |
|---|---|---|---|
| Full request → assessment → persistence → GET | Consistent | High | existing |
| Each backend path (llama.cpp, Ollama, ML, heuristic) and every fallback | Correct trace in `model` | High | existing (mocked) |
| History isolation between legitimate and suspicious requests | Flagged rows never trusted | Critical | partial |
| Dataset, training and evaluation reproducibility | Byte-identical dataset; identical metrics | Med | existing |
| One regression test per fixed bug and per probe P01–P17 | Encodes the secure expectation | Critical | proposed |

---

## 6. Acceptance criteria before Phase 2

### 6.1 Software-correctness gates (binary, must all pass)
1. The existing suite passes, plus regression tests for P01–P17 that assert the *secure* behaviour.
2. **No known hard-block bypass.** The adversarial suite (§5 D/E, including the episodes) shows no request beyond operator limits, budget or cumulative caps reaching anything other than `block`.
3. **Model-influence properties hold** under randomized testing (≥10k random analyzer outputs, including adversarial ones):
   * final recommendation ≥ the rules-only recommendation;
   * no `block` unless the rules produced a deterministic block;
   * |Δscore| within the configured bounds.
4. **Replay safety:** with 64 concurrent identical submissions, exactly 1 is `assessed`.
5. **No payment execution or false claims:** there is no PayPal or WhatsApp client in Phase 1 code (static check), and every response has `payment_executed:false`.
6. **Reproducibility:** `data`, then `train`, then `evaluate` on a clean checkout gives identical dataset hashes, model coefficients and metrics.
7. **Artifacts are hash-verified** on load. LLM calls have a hard timeout, and the service fails closed to rules when one is hit.

### 6.2 Model-quality targets (relative, on AgentPal-Bench `locked`)
I'm deliberately not setting absolute accuracy targets, because the labels are synthetic or hand-made and the prevalence is assumed. A candidate model (anything beyond rules-only) is promoted only if **all** of these hold:
* **No harm:** for every high-severity slice, recall is not lower than rules-only (paired test, α = 0.05), and the legitimate-request review rate is ≤ the agreed review budget (Q1).
* **Measurable benefit:** the primary metric improves over rules-only on the pooled OOD slices (`paraphrase`, `unseen_merchants`, `injection_holdout`), with a paired-bootstrap 95% CI lower bound > 0. The primary metric is recall at the review budget, or expected cost under the documented cost matrix.
* **Calibration** (probabilistic models): ECE reported. If probabilities are shown to humans, ECE ≤ 0.05 on `dev`, using sigmoid calibration for small data [SKL-Cal].
* **Latency:** p95 ≤ 1 s on CPU for the default path. Any optional LLM path must have p95 within its timeout and fall back on overrun.
* **Promotion record:** a model card [ModelCards] with the data hash, code commit, hyper-parameters, metrics with CIs, and known failure slices. The promotion goes through a PR with reviewer sign-off; the model version and hash appear in `/model-info` and in the audit rows.

### 6.3 Real-world readiness (not achievable in Phase 1; state it explicitly)
* **Shadow mode** on real AgentPal agent traffic: recommendations are logged, and humans decide everything. Continue until there are enough labelled outcomes to bound FNR on high-risk categories. For example, 100 confirmed risky cases gives a recall CI half-width of about ±6–9 points.
* Temporal (prequential) evaluation with label delay [FDH-Valid], plus drift monitoring.
* An independent security review of Phase 2's approval and payment path.
* **No automatic approval.** Every payment needs human approval via a verified channel, whatever the risk score [OWASP-LLM01: human approval for high-risk actions].

### 6.4 Uncertain or insufficient evidence
* Introduce an explicit `insufficient_evidence` reason, which maps to `require_review`. This is selective classification / abstention [SelectiveClf].
* The UI shows "needs information", never "suspected fraud".
* Agent-claimed evidence must be scored as *claimed*. It can reduce uncertainty only once Phase 2 verifies it, for example by matching a PayPal invoice or merchant record.

---

## 7. Prioritized improvement roadmap

Efforts are for one engineer familiar with the code. They are estimates, not commitments.

### P0: correctness, security, evaluation honesty (≈ 5–7 days in total)
| # | Item | Effort | Depends on | Benefit / acceptance |
|---|---|---|---|---|
| P0-1 | **Atomic replay protection**: reserve `request_id` first (`INSERT … ON CONFLICT DO NOTHING` inside `BEGIN IMMEDIATE`), assess, then commit. Reject losers as `replay_rejected`. | 0.5–1 d | – | Closes P09. Mirrors PayPal's own `PayPal-Request-Id` semantics, where a concurrent second request may fail [PP-Idem]. |
| P0-2 | **Operator-owned budgets and limits, plus a spend ledger**: per-agent budget and limits stored server-side (agent values are informational only), cumulative per-agent/per-merchant spend over rolling windows, velocity counts. | 2–3 d | P0-1 schema | Closes P01 and P02. Prerequisite for temporal features. |
| P0-3 | **Server time governs**: duplicate windows, staleness and TTL use the server receive time; default TTL when `expires_at` is absent. | 0.5 d | – | Closes P03 and P06. |
| P0-4 | **History trust model**: trust only operator-approved merchants and (in Phase 2) human-approved, completed payments. Make "known merchant" per-agent. Exclude untrusted rows from the spend baseline. | 1 d | P0-2 | Closes P07 and P08. |
| P0-5 | **Currency-aware limits**: per-currency limits in the policy, or reject non-base currencies until an FX source exists. | 0.5 d | – | Closes P05. |
| P0-6 | **Text normalization**: NFKC plus default-ignorable stripping before all detectors; UTS #39 skeleton for merchant-name and approved-name matching; mixed-script flag [UTS39]. | 1 d | – | Reduces P10 and P11. Doesn't solve paraphrase (P1-6). |
| P0-7 | **LLM timeout and fail-closed; streaming body limit; audit 422s; artifact SHA-256 pinning.** | 1 d | – | Fixes the false/partial claims in §1.2. |
| P0-8 | **Evaluation honesty**: add Wilson/bootstrap CIs and paired McNemar tests to `evaluation.py`; mark the current test split as retired; correct the README's demo-cause and "hard-rule block" wording. | 0.5–1 d | – | Stops over-claiming. |

### P1: model quality and robustness (≈ 3–4 weeks)
| # | Item | Effort | Depends on | Benefit |
|---|---|---|---|---|
| P1-1 | **Build AgentPal-Bench v1** (§4), including episodes and the hand-labelled gold set with κ. | 6–9 d | P0 done (episodes test the fixed behaviour) | The only credible basis for every later comparison. |
| P1-2 | **Dataset diversity and label quality**: template-disjoint generator v2, paraphrase pools, unseen categories, realistic prevalence weights, label guide and datasheet [Datasheets]. | 3 d | overlaps P1-1 | Reduces template leakage. |
| P1-3 | **Evaluate rules and classifier independently, then redesign the combination.** Replace "OR + blend" with one decision: either stacking, where the LR sees rule outputs and decides the review/allow boundary, or rules-only with the ML used only as a ranked explanation. Choose by benchmark. | 2 d | P1-1 | Removes the hybrid's extra false positives. |
| P1-4 | **Remove spurious and unverifiable features**: drop or neutralize `recurring` as a raw feature; treat evidence presence as *claimed* (no risk reduction until verified). | 1 d | P1-1 | Fixes P13, P15 and P16 on principle, not for the demo. |
| P1-5 | **Calibration and cost-based threshold**: `CalibratedClassifierCV` (sigmoid) on a disjoint split, then `TunedThresholdClassifierCV` with a cost scorer or review-budget constraint; never tune on training data [SKL-Cal], [SKL-Thr], [Elkan01]. | 1 d | Q1 answered, P1-1 | Thresholds that mean something operationally. |
| P1-6 | **Semantic consistency features**: a small CPU embedding model (all-MiniLM-L6-v2 Apache-2.0, 23M params / 91 MB; or multilingual-e5-small MIT, 118M / 471 MB) for purpose↔merchant-category similarity and paraphrase-robust intent features. | 2 d | P1-1 | Targets the 3/10 OOD paraphrase misses without a generative model. |
| P1-7 | **Narrow injection classifier evaluation** (Llama Prompt Guard 2 22M/86M; ProtectAI deberta-v3-base-prompt-injection-v2, Apache-2.0) on the `injection_holdout` slice, *in addition to* the regexes. | 2 d | P1-1, license review | Measured semantic injection detection. Prompt Guard's own card warns about adaptive attacks and application-dependent false positives [PG2]. |
| P1-8 | **Temporal and behavioural features** from the ledger: per-agent velocity, merchant novelty per agent, inter-arrival time, amount z-score against *trusted* history. | 2 d | P0-2, P0-4 | Detects structuring and drift. |
| P1-9 | **GBDT comparison**: `HistGradientBoostingClassifier` with monotonic constraints on amount/budget features; compare against LR on identical benchmark rows. Expect parity at this data size; adopt only if it wins on OOD with a CI [Grinsztajn22], [SKL-HGB]. | 1 d | P1-1 | Evidence-based model choice. |
| P1-10 | **Abstention**: `insufficient_evidence` outcome plus a risk–coverage report [SelectiveClf]. | 1 d | P1-5 | Honest handling of uncertainty. |
| P1-11 | **Retest and regression documentation** after each change; model card per promoted model [ModelCards]. | 0.5 d per change | all | Controlled promotion. |

### P2: optional research
| # | Item | Effort | Gate |
|---|---|---|---|
| P2-1 | **Compact LLM, narrow-task evaluation.** Use an LLM only for specific sub-questions (purpose↔merchant consistency, contradiction between fields and text), not holistic scoring. Ablations: schema with findings *before* score; rule codes removed from the prompt (anchoring); thinking off. Candidates in §8.2. Fix the P17 escalation policy first. | 3–4 d | Must beat P1-6 + P1-7 on the OOD slices with paired CIs, at p95 latency within budget |
| P2-2 | **LoRA fine-tune** of the best compact candidate on benchmark-dev, exported to GGUF. | 5–10 d + GPU | Only if P2-1 shows the base model is close and the gap is attributable to domain shift |
| P2-3 | **Anomaly detection** (IsolationForest / novelty LOF per agent) on *real* spend history [SKL-Outlier]. | 2 d | Needs real, trusted history; unsupervised, so it's a review signal only |
| P2-4 | **Graph features** (agent–merchant–domain bipartite graph: shared new domains across agents, burst linkage). | 3–5 d | Needs multi-agent real traffic; meaningless on the current synthetic data |
| P2-5 | **Prequential temporal validation pipeline** [FDH-Valid]. | 2 d | Real timestamps and labels |

---

## 8. External research summary

### 8.1 Detection approaches: what is practical here
| Approach | Verdict for AgentPal now | Reasoning and sources |
|---|---|---|
| Calibrated logistic regression | **Keep as the ML baseline.** Fix the calibration procedure. | LR is typically well calibrated when well specified and suitably regularized. Isotonic calibration needs about 1,000+ samples, so sigmoid is preferred for small data [SKL-Cal]. |
| Gradient-boosted trees | **Try as a comparison (P1-9), not as a default.** | Tree models are strong on medium-sized tabular data (~10K samples) [Grinsztajn22]. HGB supports NaN natively, monotonic constraints and sample weights, but is mainly faster at tens of thousands of samples [SKL-HGB]. With ~1k synthetic rows and rule-like features, a gain is unlikely, so it must be shown on OOD slices. |
| Cost-sensitive learning / thresholds | **Adopt (P1-5).** | The optimal decision threshold follows from the cost matrix [Elkan01]. scikit-learn's `TunedThresholdClassifierCV` tunes against a custom scorer, and warns never to tune on the training data [SKL-Thr]. |
| Anomaly detection | **Defer (P2-3).** | Unsupervised, and needs clean, trusted per-agent history [SKL-Outlier]. On synthetic history it would only learn the generator. |
| Temporal and behavioural features | **Adopt after the ledger (P1-8).** | Fraud practice relies on time-ordered behavioural aggregates and time-aware validation [FDH-Valid]. |
| Graph-based detection | **Defer (P2-4).** | Needs a real multi-agent graph. It adds complexity without data to justify it. |
| Hybrid rules + ML | **Keep rules authoritative. Change *how* ML is combined (P1-3).** | The current OR-plus-blend raises FPR beyond either component (§3). |
| Selective classification / abstention | **Adopt (P1-10).** | Risk–coverage trade-offs formalize "abstain when unsure" [SelectiveClf]. This maps naturally to `require_review` with an `insufficient_evidence` reason. |

### 8.2 Compact open-weight candidates (metadata verified on Hugging Face, 2026-10-28)
Latencies are **estimates** extrapolated from the measured Qwen2.5-1.5B Q4_K_M run (p50 20.1 s, p95 36.8 s per request on 4 vCPU, with ~200-token JSON outputs). That run is the only measured figure. RAM is roughly the file size plus 0.5–1 GB of KV cache and runtime overhead.

| Model | License (commercial use) | Q4_K_M GGUF | Est. CPU latency | JSON | Notes |
|---|---|---|---|---|---|
| Qwen2.5-0.5B-Instruct | Apache-2.0 ✔ | 0.49 GB | ~7–10 s (est.) | via llama.cpp grammar | Smallest; probably too weak for semantic judgement |
| Qwen2.5-1.5B-Instruct *(tested)* | Apache-2.0 ✔ | 1.12 GB | **20.1 s p50 (measured)** | ✔ grammar | Collapsed to near-constant output |
| Qwen2.5-3B-Instruct | **Qwen Research License: non-commercial** ✘ | 2.10 GB | – | – | **Excluded** (license verified from the LICENSE file) |
| Qwen3-1.7B (2025) | Apache-2.0 ✔ | Q8_0 1.83 GB in the official repo (no official Q4 listed) | ~25–40 s (est., more if thinking is on) | Tool calling documented; JSON via grammar | Thinking mode is on by default and must be disabled (`enable_thinking=False`); greedy decoding is discouraged [Qwen3] |
| Qwen3-4B | Apache-2.0 ✔ | 2.5 GB | ~50–80 s (est.) | as above | Too slow for an inline CPU path |
| IBM Granite 3.3 2B Instruct | Apache-2.0 ✔ | 1.55 GB | ~25–35 s (est.) | via grammar | Enterprise-oriented; worth a narrow-task trial |
| SmolLM2-1.7B-Instruct | Apache-2.0 ✔ | 1.06 GB | ~20 s (est.) | via grammar | English-centric, per the card [SmolLM2] |
| Phi-4-mini-instruct (3.8B, Feb 2025) | MIT ✔ | 2.49 GB | ~50–70 s (est.) | Function calling; the card warns of hallucinated function names [Phi4] | Strongest reasoning in this list, but slow on CPU |
| Llama 3.2 1B Instruct | Llama 3.2 Community License: custom; >700M MAU clause, "Built with Llama" attribution | – | ~10–15 s (est.) | via grammar | Usable, but the license adds obligations [Llama32] |
| Gemma 3 1B IT | Gemma Terms of Use plus Prohibited Use Policy (custom) | 0.81 GB | ~10–15 s (est.) | via grammar | Custom terms; gated [Gemma3] |
| **Non-generative alternatives** | | | | | |
| Llama Prompt Guard 2 22M / 86M | Llama 4 Community License (gated, custom) | 283 MB (22M) | ~tens of ms on CPU (est.) | Classifier | 512-token window; recall 97.5% @1% FPR on Meta's *private* jailbreak set; known gaps for adaptive and indirect attacks [PG2] |
| ProtectAI deberta-v3-base-prompt-injection-v2 | Apache-2.0 ✔ | 738 MB | ~100 ms (est.) | Classifier | Not evaluated here |
| all-MiniLM-L6-v2 | Apache-2.0 ✔ | 91 MB | ~5–20 ms (est.) | Embeddings | English |
| multilingual-e5-small | MIT ✔ | 471 MB | ~20–50 ms (est.) | Embeddings | Multilingual |

**Structured output:**
* llama.cpp converts a *subset* of JSON Schema to GBNF. `additionalProperties` defaults to false; unsupported keywords are **silently skipped**; numeric bounds work only for integers [LCPP-Grammar]. AgentPal already re-validates with Pydantic, which is the right defence.
* Ollama accepts a JSON Schema in `format` (since Dec 2024) and recommends temperature 0 plus validation of the result [Ollama-SO].

**Assessment.** No evidence yet shows that any generative model adds value beyond rules plus LR here. Narrow classifiers (injection) and embeddings (semantic consistency) are cheaper, faster and easier to evaluate. They address exactly the two failure modes the OOD pilot exposed. A generative model should be evaluated (P2-1) only after them, and only for narrow sub-tasks.

### 8.3 Training and evaluation practice
* **Leakage:** dependence between train and test rows is a dominant reproducibility failure; use "model info sheets" [KN23]. → Template-disjoint benchmark, locked test.
* **Temporal validation:** time-ordered hold-out with a delay gap, or prequential validation, which the handbook calls the most robust selection method [FDH-Valid].
* **Imbalance:** prefer PR curves and AP to ROC [SR15], [FDH-Metrics].
* **Calibration and thresholds:** [SKL-Cal], [SKL-Thr], [Elkan01].
* **Behavioural testing:** CheckList's capability × test-type matrix, including invariance tests [CheckList].
* **Provenance and versioning:** datasheets [Datasheets] and model cards [ModelCards]; pin data hashes, code commit and library versions in each report.
* **Adversarial ML taxonomy:** NIST AI 100-2 E2025 (March 2025) covers evasion, poisoning, and direct and indirect prompt injection; history poisoning (P07) is a poisoning-class issue [NIST-AML].

### 8.4 Payment authorization and security (for Phase 2 design)
* **Risk recommendation ≠ authorization.** OWASP LLM01:2025 says to enforce privileges in code, not in the model, to require human approval for high-risk actions, and states that no foolproof prevention for prompt injection is known [OWASP-LLM01]. → The approval service must not consume the model's output as an authorization token.
* **Idempotency:**
  * PayPal's `PayPal-Request-Id` returns the latest status of the original request on retry. Not all APIs support it. A *simultaneous* duplicate may fail. Without the header, a repeat is processed as new [PP-Idem].
  * The IETF `Idempotency-Key` header is still an Internet-Draft (-07, Oct 2025) [IETF-Idem].
  * → Use a server-generated UUID per (request, API-call type) as `PayPal-Request-Id`, and persist it *before* calling PayPal.
* **Authorize, then capture:**
  * An authorization is valid for 29 days, with a 3-day honor period; reauthorization creates a new ID [PP-Auth].
  * This fits "human approves → capture". The order must still be approved by the payer via PayPal's own flow.
  * Note: some AgentPal flows may need a different PayPal product (for example payouts). That needs confirming in Phase 2 (Q5).
* **Webhooks:**
  * PayPal: verify with self-verification (CRC32 of the raw body plus the certificate signature) or with the `verify-webhook-signature` postback. Postback is **not supported for simulator mock events**. Deliveries are retried up to 25 times over 3 days → deduplicate by event ID [PP-Webhooks].
  * Meta/WhatsApp: validate `X-Hub-Signature-256` against the App Secret. Failed deliveries are retried for up to 36 hours, so deduplicate [Meta-Webhooks]. Reply-button taps arrive as `interactive.button_reply` with the button `id` you set [WA-Buttons].
  * → Bind button IDs to single-use server nonces tied to (request_id, approver, expiry).
* **API security:** BOLA on `/assessments/{id}`, broken authentication, unrestricted resource consumption and sensitive business flows are the relevant OWASP API 2023 items [OWASP-API]. Phase 1's API has **no authentication** and must not be exposed beyond localhost.
* **Logging:** never log tokens, passwords or card data; protect log integrity; record when, where, who and what [OWASP-Log].

---

## 9. Weakness deep-dives (the 7 known issues)

**W1. The demo request goes to `require_review` under the default chain.**
* *Root-cause hypotheses:*
  * (a) The LR learned that missing evidence predicts "needs review", because in the generator evidence presence correlates with legitimate categories (+1.72).
  * (b) There is a spurious `recurring` weight (+0.77).
  * (c) The hybrid's blending adds +25 and the ML recommendation escalates.
* *Evidence:* P15 contributions, P16 counterfactuals, §3 coefficients. This is already confirmed.
* *Experiment:* on benchmark-dev, run feature ablations (drop `recurring`; treat evidence as claimed). Measure the legitimate review rate and recall per slice.
* *Improvement:* P1-4 and P1-3. Do **not** special-case the demo. If evidence is unverifiable, the honest outcome for a first, unverified request may well be `require_review`. Phase 2's approval flow handles that.
* *Regression risk:* lower recall on the `missing_or_inconsistent` slice.
* *Acceptance:* the decision is invariant to toggling `recurring` on matched legitimate pairs (≥95% of pairs), and no high-severity slice loses recall.

**W2. The hybrid adds false positives for only a small recall gain.**
* *Root cause:* the combination is effectively an OR, plus blending pushes borderline scores over 25 (18 > 16 FPs on validation).
* *Experiment:* compare rules / LR / stacking / rules-with-ML-explanation on identical benchmark rows, with paired tests.
* *Improvement:* P1-3.
* *Regression risk:* lost recall on combination cases.
* *Acceptance:* the hybrid is not worse than rules on legitimate review rate (paired), and improves OOD recall with a CI lower bound > 0. Otherwise ship rules-only as the default.

**W3. Qwen gives near-constant output and is slow.**
* *Hypotheses:*
  * (a) A small model under a grammar constraint, with `risk_score` emitted before any findings, anchors on a default value.
  * (b) Rule codes in the prompt anchor the output.
  * (c) The orchestrator escalates on any model `require_review` (P17), so even a perfect score signal is overridden by the recommendation field.
  * (d) CPU-bound decoding of ~200 tokens.
* *Evidence needed:* output entropy and score variance across benchmark-dev, under each prompt ablation.
* *Experiment:* run P2-1's ablation grid on ≥200 dev rows; record score variance and AP.
* *Improvement:* narrow tasks, findings-first schema, no rule codes in the prompt, escalation tied to calibrated score.
* *Acceptance:* the P2-1 gate in §7.

**W4. Subtle injections and weak-signal combinations are missed.**
* *Root cause:* lexical regexes, no normalization, additive points with a single threshold.
* *Experiment:* run the `injection_holdout`, `unicode_adversarial` and combination slices before and after P0-6, P1-6 and P1-7.
* *Regression risk:* false positives on legitimate text that mentions "review" or "approval".
* *Acceptance:* injection-holdout recall improves over regex-only with a paired CI, and the legitimate-text FPR is ≤ the review budget.

**W5. The synthetic data shares the rules' assumptions.**
* *Evidence:* §3 (signature-rule firing, template overlap, OOD pilot 5/10).
* *Fix:* P1-1 and P1-2, with independent authorship.
* *Acceptance:* zero verbatim template overlap (an automated test) and a published datasheet.

**W6. History contamination.**
* *Root cause:* trust is derived from our own recommendation, and it's global across agents (P07, P08).
* *Fix:* P0-4, and in Phase 2 trust only human-approved, completed payments.
* *Acceptance:* the poisoning episodes in the benchmark produce no trust transfer.

**W7. No real-world labels.**
* *Fix:* shadow mode in Phase 2 with outcome capture (approved, declined, disputed), plus temporal evaluation.
* *Acceptance:* the §6.3 thresholds. Until then, every metric must carry the "synthetic" label.

---

## 10. Open questions and unresolved risks
* **Q1. Review budget.** What fraction of legitimate requests may the owner be asked to review? This drives every threshold. It is undefined today.
* **Q2. Cost matrix.** Relative cost of a missed risky payment vs a needless review vs a wrongly blocked legitimate payment.
* **Q3. Whether soft indicators may produce `block` (P14).** Currently yes. Policy owners should decide whether `block` is reserved for hard rules, with soft scores ≥ 75 capped at `require_review`.
* **Q4. Which component owns budgets and spend.** This is needed for P0-2. In Phase 2 it should be the approval/ledger service.
* **Q5. PayPal product fit.** An agent paying a merchant may need Orders (checkout-style, payer approval) or Payouts. This needs confirming in PayPal Sandbox documentation before Phase 2 design.
* **Q6. Licensing posture** for gated or custom licenses (Llama 4 / Prompt Guard, Gemma): acceptable for the hackathon? For later commercial use?
* **Risk.** Even after P0, the engine cannot verify merchants or evidence. A careful attacker using an approved merchant, plausible text and in-budget amounts will receive `allow_for_review`. That's why human approval of every payment in Phase 2 is a hard requirement, not an option.

---

## 11. Reproducing existing results
```bash
cd agentpal
python3 -m pip install -r requirements-dev.txt
python3 -m pytest                                  # 108 passed (verified 2026-10-28)
python3 -m agentpal data                           # test split sha256 bfb5c3d7…66f8
python3 -m agentpal train                          # lr C=0.3, threshold 0.30
python3 -m agentpal evaluate --split validation --report reports/validation_report.json
# The held-out test split is RETIRED for model selection; re-running it is only a regression check:
python3 -m agentpal evaluate --split test --report reports/evaluation_report.json
# Optional, slow (~20 s/request): --llm-sample 60 with the Qwen GGUF installed (make install-llm)
# Review notes (read-only, ad-hoc inputs + train/validation only):
PYTHONPATH=. python3 docs/review/probes.py
PYTHONPATH=. python3 docs/review/eval_analysis.py
PYTHONPATH=. python3 docs/review/ood_pilot.py
```

---

## 12. Recommendation on PR readiness
**Not ready for a PR that will be built upon for payment authorization.**

* The architecture is a good foundation and the code quality is reasonable.
* But P09 (replay race), P01 and P02 (agent-controlled budget, no cumulative limit), P03 and P06 (agent-controlled time) and P07 (cross-agent trust poisoning) are correctness and security defects in exactly the controls Phase 2 will rely on.
* There are also documentation inaccuracies: the demo cause, the llama.cpp timeout, and "hard-rule block".

**Suggested path:**
1. P0-1 to P0-8 with regression tests (≈5–7 days).
2. Re-run the suite and the probes; all should now pass their secure expectations.
3. Open a *draft* PR titled as an assessment prototype, linking this review.
4. Build AgentPal-Bench (P1-1) before any further model work.

---

## 13. Sources
All were accessed 2026-10-28. Dates are publication or last-modified dates where shown.

* **[SKL-Cal]** scikit-learn 1.9.1, *Probability calibration* (§1.16). https://scikit-learn.org/stable/modules/calibration.html
* **[SKL-Thr]** scikit-learn, *Tuning the decision threshold for class prediction*. https://scikit-learn.org/stable/modules/classification_threshold.html
* **[SKL-HGB]** scikit-learn, *Ensembles: Histogram-based gradient boosting*. https://scikit-learn.org/stable/modules/ensemble.html
* **[SKL-Outlier]** scikit-learn, *Novelty and outlier detection*. https://scikit-learn.org/stable/modules/outlier_detection.html
* **[FDH-Valid]** Le Borgne, Siblini, Lebichot, Bontempi (ULB Machine Learning Group and Worldline), *Reproducible Machine Learning for Credit Card Fraud Detection: Practical Handbook*, 2022, ch. 5 "Validation strategies". https://fraud-detection-handbook.github.io/fraud-detection-handbook/Chapter_5_ModelValidationAndSelection/ValidationStrategies.html
* **[FDH-Metrics]** Same handbook, ch. 4 "Threshold-free metrics". https://fraud-detection-handbook.github.io/fraud-detection-handbook/Chapter_4_PerformanceMetrics/ThresholdFree.html
* **[KN23]** Kapoor & Narayanan, "Leakage and the reproducibility crisis in machine-learning-based science", *Patterns* 4(9) 100804, Aug 2023. https://pmc.ncbi.nlm.nih.gov/articles/PMC10499856
* **[Grinsztajn22]** Grinsztajn, Oyallon, Varoquaux, "Why do tree-based models still outperform deep learning on typical tabular data?", NeurIPS 2022 Datasets & Benchmarks. https://arxiv.org/abs/2207.08815
* **[SR15]** Saito & Rehmsmeier, "The Precision-Recall Plot Is More Informative than the ROC Plot…", *PLOS ONE* 10(3) e0118432, 4 Mar 2015. https://pmc.ncbi.nlm.nih.gov/articles/PMC4349800
* **[SelectiveClf]** Geifman & El-Yaniv, "Selective Classification for Deep Neural Networks", NeurIPS 2017. https://arxiv.org/abs/1705.08500
* **[Elkan01]** Elkan, "The Foundations of Cost-Sensitive Learning", IJCAI 2001, pp. 973–978. https://mlanthology.org/ijcai/2001/elkan2001ijcai-foundations
* **[CheckList]** Ribeiro, Wu, Guestrin, Singh, "Beyond Accuracy: Behavioral Testing of NLP Models with CheckList", ACL 2020, pp. 4902–4912. https://aclanthology.org/2020.acl-main.442/
* **[Datasheets]** Gebru et al., "Datasheets for Datasets", arXiv:1803.09010 (2018); *CACM* 64(12), 2021. The venue was confirmed only via secondary sources. https://arxiv.org/abs/1803.09010
* **[ModelCards]** Mitchell et al., "Model Cards for Model Reporting", FAT* 2019, DOI 10.1145/3287560.3287596. https://arxiv.org/abs/1810.03993
* **[NIST-AML]** NIST AI 100-2 E2025, *Adversarial Machine Learning: A Taxonomy and Terminology of Attacks and Mitigations*, March 2025. https://csrc.nist.gov/pubs/ai/100/2/e2025/final
* **[OWASP-LLM01]** OWASP Top 10 for LLM Applications 2025, LLM01:2025 Prompt Injection. https://genai.owasp.org/llmrisk/llm01-prompt-injection/
* **[OWASP-API]** OWASP API Security Top 10 2023. https://api-security.owasp.org/editions/2023/en/0x11-t10
* **[OWASP-Log]** OWASP Logging Cheat Sheet. https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html
* **[UTS39]** Unicode Technical Standard #39, *Unicode Security Mechanisms*, 18.0.0 rev. 34, 2026-08-27. https://www.unicode.org/reports/tr39/
* **[PSL]** Public Suffix List, submission guidelines (private section). https://publicsuffix.org/submit/
* **[PP-Webhooks]** PayPal Developer, *Webhooks: verify signatures*. https://developer.paypal.com/api/rest/webhooks/rest/
* **[PP-Idem]** PayPal Developer, *Idempotency*. https://developer.paypal.com/api/rest/reference/idempotency/
* **[PP-Auth]** PayPal Developer, *Authorize a payment and capture funds later*. https://developer.paypal.com/docs/checkout/standard/customize/authorization/
* **[PP-Orders]** PayPal Developer, *Orders API v2*. https://developer.paypal.com/docs/api/orders/v2/
* **[Meta-Webhooks]** Meta for Developers, *Webhooks: Getting started* (X-Hub-Signature-256, retries). https://developers.facebook.com/docs/graph-api/webhooks/getting-started
* **[WA-Buttons]** Meta, *WhatsApp interactive reply buttons messages*. https://developers.facebook.com/documentation/business-messaging/whatsapp/messages/interactive-reply-buttons-messages
* **[IETF-Idem]** Jena & Dalal, *The Idempotency-Key HTTP Header Field*, draft-ietf-httpapi-idempotency-key-header-07, Oct 2025 (Internet-Draft, not an RFC). https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/
* **[LCPP-Grammar]** llama.cpp, *GBNF Guide* (grammars/README.md). https://github.com/ggml-org/llama.cpp/blob/master/grammars/README.md
* **[Ollama-SO]** Ollama, *Structured outputs*, 6 Dec 2024. https://ollama.com/blog/structured-outputs
* **[Qwen3]** Qwen3-1.7B model card (Apache-2.0; technical report arXiv:2505.09388, May 2025). https://huggingface.co/Qwen/Qwen3-1.7B
* **[Qwen2.5-3B-License]** Qwen Research License Agreement (Qwen2.5-3B-Instruct), 19 Sep 2024. https://huggingface.co/Qwen/Qwen2.5-3B-Instruct/blob/main/LICENSE
* **[Phi4]** microsoft/Phi-4-mini-instruct model card (MIT, Feb 2025). https://huggingface.co/microsoft/Phi-4-mini-instruct
* **[Llama32]** meta-llama/Llama-3.2-1B-Instruct model card (Llama 3.2 Community License, 25 Sep 2024). https://huggingface.co/meta-llama/Llama-3.2-1B-Instruct
* **[Gemma3]** google/gemma-3-1b-it model card (Gemma Terms of Use). https://huggingface.co/google/gemma-3-1b-it
* **[SmolLM2]** HuggingFaceTB/SmolLM2-1.7B-Instruct model card (Apache-2.0). https://huggingface.co/HuggingFaceTB/SmolLM2-1.7B-Instruct
* **[PG2]** meta-llama/Llama-Prompt-Guard-2-86M model card (Llama 4 Community License). https://huggingface.co/meta-llama/Llama-Prompt-Guard-2-86M
* File sizes, licenses and last-modified dates for the GGUF and embedding repositories came from the Hugging Face model API (`/api/models/<repo>?blobs=true`): Qwen2.5-{0.5B,1.5B,3B}-Instruct-GGUF, Qwen3-{1.7B,4B}-GGUF, ibm-granite/granite-3.3-2b-instruct-GGUF, HuggingFaceTB/SmolLM2-1.7B-Instruct-GGUF, unsloth/Phi-4-mini-instruct-GGUF, unsloth/gemma-3-1b-it-GGUF, sentence-transformers/all-MiniLM-L6-v2, intfloat/multilingual-e5-small, protectai/deberta-v3-base-prompt-injection-v2, meta-llama/Llama-Prompt-Guard-2-22M.

**Not verified from primary sources:** the exact Cohen's-κ guidance and annotation protocol are proposed, not cited. Meta's documentation does not explicitly say "HMAC" (HMAC-SHA256 over the raw body is the common construction; confirm before implementing). PayPal Orders intent semantics and payer-approval requirements were not fully covered by the fetched index page. All latency figures other than Qwen2.5-1.5B are estimates.
