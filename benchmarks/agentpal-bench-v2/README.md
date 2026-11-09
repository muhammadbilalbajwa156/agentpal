# AgentPal-Bench v2: development kit (NOT the independent benchmark)

> **Status.** What exists here is an AI-written **development** set plus the tooling an
> external team needs to build, annotate, lock and score the independent benchmark that
> owner decisions 9/F10 require. Every item in this directory is `split: "dev"` and
> `independent: false`. No result computed on these items is an independent or real-world
> measurement. The protocol is [`docs/BENCHMARK_ANNOTATION_PROTOCOL.md`](../../docs/BENCHMARK_ANNOTATION_PROTOCOL.md).

## 1. What exists

| File | What it is |
|---|---|
| `cases.jsonl` | 144 single cases (dev). Written by an AI helper agent with full access to AgentPal's rules and tests. Each case carries a **proposed** label, action, policy clause and rationale: proposals for annotators, **not** ground truth. |
| `episodes.jsonl` | 30 multi-request episodes, 66 labelled steps (budget, replay, approval hold, structuring, near-duplicates, attempted vs completed spend, unverified outcomes), with expected ledger outcomes. |
| `generate_dev.py` | Deterministic, stdlib-only generator of the two files. `--check` verifies the committed bytes. |
| `leakage_check.py` | Overlap check against the synthetic generator, its template pools, the v1 seed and the rule author's adversarial test strings: ids, non-approved domains, exact text, and word-trigram containment after NFKC/zero-width normalisation. Also refuses `split: "locked"` items anywhere under `benchmarks/`. |
| `score.py` | Scorer. Rules-only, hybrid (rules + pinned ML), classifier-only, and any external approach (`--predictions name=file.jsonl`). It reports FN, FP, legitimate blocks, required blocks missed, review/block/degraded rates and p50/p95 latency, each separately with Wilson 95% CIs, plus paired McNemar comparisons. Slices: risk type, wording, language. A locked run checks the lock, logs the access and writes aggregates only. |
| `custodian_lock.py` | Locks a **locked** split held outside the repository: hashes cases, episodes and `adjudicated_labels.jsonl`; refuses dev or non-independent items; keeps a lock history. |
| `annotation/` | `make_packets.py` (blinded, per-annotator shuffled packets), `ANNOTATOR_GUIDE.md` (draft guideline, ships with every packet), `label_schema.json`, `policy_clauses.json`, `worked_examples.jsonl` (12 guide examples, not benchmark items), `agreement.py` (Fleiss / Cohen κ), `adjudicate.py` (majority + adjudicator), `annolib.py`. |
| `reports/` | `dev_smoke_self_labelled.{md,json}` (a dev smoke run scored with the author's own proposals) and `leakage_report.json`. |
| `.gitignore` | Keeps `locked/`, `custodian/`, packets, raw annotations and per-item predictions out of git. |

**Measured on the dev kit** (2026-10-29, self-labelled, NOT independent; see `reports/` and
`docs/ML_AUDIT.md`):

| Approach | Missed risky (FN) | Legitimate flagged (FP) |
|---|---|---|
| Rules | 40/137 (29%) | 19/73 (26%) |
| Hybrid (rules + ML) | 28/137 (20%) | 33/73 (45%) |
| Hybrid with an ML refit from current code (instead of the pinned artifact) | 40/137 (29%) | 20/73 (27%) |

The last row is ML-03 in `docs/ML_AUDIT.md`. The pinned ML artifact adds a constant logit
to every request, and that constant explains its entire extra recall and extra false
positives here.

**Quality checks** (ML_AUDIT ML-10):
* leakage check: 0 findings for v2 (trigram overlap fraction 0.022);
* proposed labels and proposed actions are consistent;
* no exact duplicate requests;
* 124 groups;
* 45% of legitimate units are hard negatives (protocol minimum: 30%);
* 43 non-English units (11 languages) and 13 obfuscated units.

`score.py` was spot-checked by hand:
* 5 units recomputed independently;
* FN/FP counts recomputed from raw predictions;
* the Wilson interval and the McNemar p-value recomputed.

All of them match.

## 2. What is missing

The independent benchmark itself. Building it needs people this repository cannot supply.

| Missing | Why it matters | Owner |
|---|---|---|
| **Benchmark custodian** (not on the engineering team) | Holds the locked items outside the repository, runs locked evaluations, keeps the access log | Product owner appoints |
| **≥ 3 scenario authors** (payments/fraud practitioners, security testers, finance operators; not AgentPal authors) | The dev items were written by an AI agent that read the rules, so they cannot measure generalisation | Custodian recruits |
| **≥ 3 annotators per item** plus an adjudicator | Labels must come from people who never see AgentPal outputs | Custodian recruits |
| **Frozen guideline** | `annotation/ANNOTATOR_GUIDE.md` is `v0-draft` | Custodian reviews and versions it |
| **Locked split**: ≥ 300 cases and ≥ 100 episodes, ≥ 15 per coverage cell, 70% locked by group | The dev kit has 144 + 30 items, and 14 of its 19 risk types have fewer than 15 units | Authors + custodian |
| **Agreement report** | κ ≥ 0.6 on `risky` (overall and per risk type) is the gate | Custodian |

## 3. How an external team builds, annotates, locks and scores it

All commands run from the `agentpal/` project root. `<V2>` = a directory **outside** this
repository, controlled by the custodian.

1. **Write items** (authors). Use the format of `cases.jsonl` / `episodes.jsonl`, with:
   * `"split": "locked"` (or `"dev"` for the 30% dev share, assigned by group);
   * `"independent": true`;
   * `author`, and a `group_id` shared by all variations of one scenario.

   Authors receive only the public request schema, the owner-decision summary
   (`annotation/policy_clauses.json`) and the spend-policy description. They do not
   receive AgentPal code, prompts, this dev kit or the synthetic generator.
2. **Leakage check** (custodian):
   ```
   python benchmarks/agentpal-bench-v2/leakage_check.py --bench-dir <V2> --report <V2>/leakage.json
   ```
   Every finding must be fixed or the item dropped. The custodian also re-checks 10% of
   items by hand.
3. **Blinded packets** (custodian):
   ```
   python benchmarks/agentpal-bench-v2/annotation/make_packets.py --bench-dir <V2> \
       --annotators ann01 ann02 ann03 --out <V2>/annotator_packets
   ```
   Each packet holds the units in a per-annotator order, a label template, the policy
   context, `ANNOTATOR_GUIDE.md` and the worked examples. It contains no proposed labels
   and no system output.
4. **Pilot**: about 30 items, labelled by everyone. Then:
   ```
   python benchmarks/agentpal-bench-v2/annotation/agreement.py --bench-dir <V2> \
       --annotations <pilot a.jsonl b.jsonl c.jsonl> --out <V2>/pilot_agreement.json
   ```
   If κ on `risky` is below 0.6, revise the guide (new version) and re-pilot. Pilot items
   are discarded.
5. **Main annotation**: three independent labels per unit. Then run `agreement.py` on all
   labels.
6. **Adjudicate**:
   ```
   python benchmarks/agentpal-bench-v2/annotation/adjudicate.py --bench-dir <V2> \
       --annotations a.jsonl b.jsonl c.jsonl [--adjudications adj.jsonl] --out-dir <V2>
   ```
   Ties and any block vs non-block split go to the adjudicator. Ambiguous items are
   excluded. Output: `<V2>/adjudicated_labels.jsonl`.
7. **Lock**, before anyone runs AgentPal on the items:
   ```
   python benchmarks/agentpal-bench-v2/custodian_lock.py --dir <V2> --note "guide v1.0, kappa_risky=0.xx"
   ```
   Only `LOCK.json` may enter the repository.
8. **Score**, once per release candidate:
   ```
   python benchmarks/agentpal-bench-v2/score.py --split locked --bench-dir <V2> \
       --labels <V2>/adjudicated_labels.jsonl --out-dir <V2>/reports --name rc1 \
       [--predictions finetuned=<V2>/finetuned_predictions.jsonl] --require-independent
   ```
   The scorer refuses to run if any of these holds:
   * a file changed after the lock;
   * the labels are not the locked `<V2>/adjudicated_labels.jsonl`;
   * the directory or the output is inside the repository;
   * five locked runs have already happened.

   Engineers receive the aggregate report only (no item ids or text).
9. **Retire** the locked split after five runs or at the first suspected leak, and
   commission a new one.

**Dev runs** (anyone, on this directory; labels are the author's proposals):
```
python benchmarks/agentpal-bench-v2/score.py --labels proposed --out-dir /tmp/v2 --name dev_smoke
python -m training.evaluate_model --gguf <model.gguf> --data build/sft --out <dir>   # a fine-tuned model
```

## 4. Why the leakage thresholds are what they are

`leakage_check.py` flags a pair of texts only when both conditions hold:
* they share **≥ 2 word trigrams**;
* the shared trigrams cover **≥ 50%** of the shorter text's trigrams.

A single shared trigram is ordinary English ("for the team"). Two or more that cover half of
the shorter text mean one text substantially reproduces the other, for example a template
with a suffix. Texts shorter than three tokens, and CJK text without spaces, can only match
exactly. The training-data guard (`training/augment.py: overlap_report`) reuses this check
in the opposite direction. It adds a de-obfuscation pass (homoglyphs, leetspeak, zero-width
characters, spaced letters) and a merchant-name check, so training text cannot copy a
benchmark item.
