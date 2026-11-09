# AgentPal-Bench v2: locked benchmark and independent annotation protocol

**Status: design only; mandated by the product owner (final decision F10), not yet built.** Building it needs people this repository cannot supply: a custodian, plus authors and annotators who did not write the rules. No v2 items exist yet. The v1 seed (`benchmarks/agentpal-bench-v1`, dev split) was written by the rule author and stays a regression check. This protocol defines how the first benchmark able to *measure* AgentPal is built, which owner decision 9 requires.

It reuses the v1 harness unchanged:
* file format
* `group_id` split integrity
* `LOCK.json` SHA-256 lock
* `access_log.jsonl`
* API episode replay
* identical-example scoring

## 1. Goals and non-goals

* **Goal:** estimate missed risky payments, false positives, review and block rates **separately** (owner decision 1), with 95% CIs. The estimates must come from items AgentPal's authors never saw, written in wording the rules were not built around.
* **Goal:** cover multi-request behaviour (budget, replay, approval expiry, structuring, near-duplicates) as episodes, not only single requests.
* **Non-goal:** a real-world fraud-rate estimate. The items are still constructed scenarios, and the report must say so.
* **Non-goal:** tuning. The locked split is never used to choose thresholds, weights, prompts or models.

## 2. Roles (separation of duties)

| Role | Who | May see the locked split? |
|---|---|---|
| **Benchmark custodian** | One person not on the AgentPal engineering team | Yes. Holds the files and runs the locked evaluation. |
| **Scenario authors** (≥ 3) | Fraud/payments practitioners, security testers, finance operators. Not AgentPal rule or model authors. | Their own items only, until the lock |
| **Annotators** (≥ 3 per item) | Independent of authors and engineers; trained on the guideline (§4) | Items assigned to them, without the system's outputs |
| **Adjudicator** | A senior annotator who is not an author | Disagreements only |
| **AgentPal engineers** | Rule, model and prompt authors | **No.** They receive aggregate metrics and failure *categories* only. |

Every item records `author`, `annotators`, `independent: true` and the guideline version.

## 3. Item design

**Size.** At least 300 single cases and 100 episodes. The class balance should be about 50/50 risky/legitimate *by design*, and reported, because prevalence is not realistic.

**Coverage quotas** (each cell ≥ 15 items, so per-cell CIs are interpretable):

| Dimension | Values |
|---|---|
| Risk type | <ul><li>impersonation / lookalike</li><li>**near-name** (approved merchant's name plus extra words)</li><li>purpose mismatch</li><li>injection</li><li>structuring</li><li>duplicate / near-duplicate</li><li>**replay** (re-submitted `request_id`)</li><li>budget pressure</li><li>**fabricated evidence** (internally consistent invoices or justifications)</li><li>unexpected recurrence</li><li>stale / expired</li><li>legitimate routine</li><li>legitimate unusual-but-fine</li></ul> |
| Wording | <ul><li>templated-like</li><li>paraphrased</li><li>non-English (≥ 3 languages)</li><li>obfuscated (leetspeak, homoglyphs, zero-width)</li></ul> |
| Currency | USD, GBP, JPY (zero-decimal), plus at least one currency that is not configured for the agent |
| Episode mechanics (**multi-request attacks** are mandatory) | <ul><li>approval within / after the 15-minute hold</li><li>attempt with terms different from the approved ones</li><li>attempted vs. completed spend</li><li>unverified outcome</li><li>concurrent duplicates</li><li>rolling-window boundary</li><li>owner sharing on / off</li></ul> |

**Hard negatives are mandatory.** At least 30% of the legitimate items must look risky on the surface, for example a real merchant on an unfamiliar ccTLD, or urgent but genuine renewals. Without them, the false-positive rate is meaningless.

**Groups.** Variations of one scenario share a `group_id`. A group lives in exactly one split (`dev` or `locked`); the harness enforces this. The split is 30% `dev` / 70% `locked`, assigned **by group**, at random, by the custodian.

**Prohibited sources:**
* AgentPal source code, rule lists, reason codes and prompts
* the synthetic generator and its templates
* the v1 seed
* any output of AgentPal

Authors receive only:
* the public request schema
* the owner-decision summary (what *should* block vs. review)
* a description of the operator spend policy used by episodes

## 4. Labels and guideline

Each item is labelled against the **owner policy**, not against AgentPal's behaviour.

* **`expected_action`:** `block` (a deterministic policy violation, decision 2), `require_review`, or `allow_for_review`.
* **`risky`:** 0/1. Would a careful owner want to stop and look before money moves?
* **`rationale`:** free text, plus the policy clause it relies on.
* **`difficulty`:** easy, medium or hard. Optional, used for slicing.

The guideline (versioned, frozen before annotation starts) defines each label with ≥ 3 positive and ≥ 3 negative worked examples. None of those examples may come from the benchmark itself. Episodes are labelled per step.

## 5. Annotation procedure

1. **Pilot.** Thirty items are labelled by all annotators. If Cohen's / Fleiss' κ on `risky` is below 0.6, revise the guideline (new version) and re-pilot. Pilot items are discarded.
2. **Main pass.** Each item gets three independent labels. Annotators never see AgentPal outputs, other annotators' labels, or the author's intended label.
3. **Agreement.** Report Fleiss' κ for `risky` and `expected_action`, overall and per risk type. A risk type with κ < 0.6 is reported but excluded from headline metrics.
4. **Adjudication.** Majority label. Ties and any `block` vs. non-`block` disagreement go to the adjudicator, whose rationale is stored. Items still ambiguous after adjudication are moved to an `ambiguous` file and excluded.
5. **Audit sample.** The custodian re-checks 10% of items for leakage, meaning text that copies reason codes or seed items.

## 6. Locking and access

1. The custodian runs `python -m agentpal bench-lock --dir <dir> --note "v2 guideline <ver>, κ=<…>"` **before anyone runs AgentPal on the items.** `LOCK.json` stores the SHA-256 of each file.
2. Locked files are stored outside the engineering repository, in a custodian-controlled location. Only `LOCK.json` and aggregate reports are committed.
3. Every locked evaluation appends to `access_log.jsonl`. Policy:
   * at most one locked run per release candidate;
   * each run is tied to a recorded commit, rules version and model digest.
4. Engineers receive per-category aggregate metrics and failure categories, never item text. Improving the system is done on `dev`. After **five** locked runs, or at the first suspected leak, the locked split is retired and a new one is commissioned.

## 7. Reporting

Each approach is scored on identical items: rules-only, classifier-only and hybrid, plus any LLM backend. **The primary question (final decision F9) is rules-only vs. rules + ML:** does the ML's extra review burden buy enough additional recall to be worthwhile? Until this benchmark answers it, the ML stays advisory.

The report includes, each **separately**:
* false negatives: missed risky (count, rate, Wilson 95% CI)
* false positives: legitimate flagged
* legitimate-payment blocks
* review rate, block rate, degraded rate
* p50/p95 latency
* each of the above per risk type and per wording type, with paraphrased, non-English, obfuscated, fabricated-evidence, near-name, replay and multi-request attacks reported as their own slices

Paired comparisons between approaches use McNemar's test on the same items. A difference is claimed only when it is significant at 0.05 **and** its CI excludes zero.

**Mandatory caveats:**
* constructed scenarios
* the designed prevalence
* the κ values
* that independence is organisational, not proven

## 8. Open items for the product owner

* Approve the custodian and author pool, and the budget for 3× annotation.
* Confirm the "≥ 5 locked runs, then retire" rule.
* Decide whether the hard-negative quota (30%) matches the expected traffic.
