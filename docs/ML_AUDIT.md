# AgentPal ML audit: data, models, training pipeline and bench v2

Branch `agentpal-risk-model`, from `685d1bb`. Helper agent 2 (ML, data, evaluation and
training), 2026-10-29.

**Summary**
* No AgentPal model was trained in this audit.
* The training loop was executed only as a **pipeline smoke test** (CPU, a tiny random
  Qwen3 test model, a few steps).
* All benchmark numbers come from AI-written, self-labelled development data. They are not
  independent and not real-world accuracy.

**Invariants re-checked.** Models stay advisory. Malformed, oversized or timed-out model
output degrades to rules-only and is never less strict than rules
(`tests/test_ml_model_output_parsing.py`; 52 cases on two requests).

## 1. Findings

**Severity** is one of:
* **Critical:** breaks an invariant;
* **High:** invalidates a measurement or a training run;
* **Medium:** makes something wrong or fragile;
* **Low:** hygiene.

The three "Blocks" columns say whether the finding blocks training, the demo, or
production.

| ID | Sev. | Finding | Evidence | Reproduction | Fix / status | Test | Blocks training / demo / prod |
|---|---|---|---|---|---|---|---|
| ML-01 | High | **Template memorisation in the synthetic data.**<ul><li>1,850 rows = 370 scenarios × 5 near-copies.</li><li>53 purposes, 8 injection sentences.</li><li>84% of validation rows reuse a training template.</li><li>Rules miss 1.6% of risky rows with seen templates and 17.0% [9.2, 29.2] with unseen ones.</li></ul> | `agentpal/dataset.py:471` (`VARIATIONS_PER_GROUP = 5`), `:31-110` (pools) | `python -m training.audit --data <dir>` (numbers in §2.1) | **Mitigated for SFT:**<ul><li>template-family splits;</li><li>946 hand-written augmented rows in 190 + 49 families (2296360).</li></ul>Synthetic validation numbers stay memorisation-inflated (documented). | `test_training_pipeline.py::test_audit_*`, `test_training_augment.py::test_families_never_cross_splits` | no / claims only / yes |
| ML-02 | High | **Evidence presence and evidence type predict the label.**<ul><li>Train rows without evidence are 88% risky; with evidence, 58%.</li><li>Every `legit_unusual` row carries evidence.</li><li>The evidence type `note` occurs only in injection rows (40/40 risky).</li></ul> | `dataset.py:203`, `:259-272` (`evidence_prob=1.0`), `:430` | §2.2 | **Fixed in SFT** (2296360):<ul><li>evidence toggled on evidence-neutral rows in train **and** dev;</li><li>benign `note` evidence on legitimate rows;</li><li>augmented invoices risky only with another visible signal.</li></ul>Risky rate with/without evidence: train 0.553/0.556, dev 0.598/0.558. | `test_training_augment.py::test_evidence_presence_*`, `::test_note_evidence_*`, `::test_fabricated_evidence_*` | was yes, now no / – / – |
| ML-03 | High | **The pinned ML artifact is stale (train/serve skew).**<ul><li>Since owner decision F2, the ML feature `missing_evidence` is `missing or unverified`, so it is 1 for **every** request.</li><li>The pinned artifact was trained while it varied (mean 0.23), so it adds a constant **+1.716 logit** to every assessment. This is the "+1.72" in RISK_MODEL_PLAN §1.</li><li>A refit from current code differs (C 1.0 vs 0.3).</li><li>On bench v2 dev, the hybrid's extra recall (12 risky) **and** its extra false positives (+14 legitimate) come entirely from this offset. A refit hybrid scores FN 40/137, FP 20/73, i.e. the same as rules (19/73).</li></ul> | `features.py:585`; `artifacts/baseline_classifier.json` (sha `cd20c6d0…`) | §2.3 | **Not fixed (needs owner/lead):**<ul><li>the artifact is git-ignored;</li><li>the pin lives in `config/model_manifest.json`;</li><li>the feature lives in `features.py` (not mine).</li></ul>Proposal in §4. The F9 decision (rules-only vs rules + ML) must not use the pinned artifact's numbers. | refit and comparison scripts in §2.3 | no / **yes** (ML claims) / **yes** |
| ML-04 | Medium | **The retired test split is still the CLI default:** `python -m agentpal evaluate` defaults to `--split test`. | `agentpal/__main__.py` (`evaluate` parser, `default="test"`) | `python -m agentpal evaluate --help` | Lead fixed the Makefile. Proposal: default `validation` (not my file). | – | no / no / no |
| ML-05 | High | **SFT labels the model input cannot show.** The prompt has no history, no timestamps and no per-transaction limits, so these rows are labelled from information the model never sees:<ul><li>duplicates and replays (150 rows);</li><li>unknown references (60);</li><li>odd hours (30);</li><li>per-transaction review/hard limits (90).</li></ul>The duplicate rows also create a shortcut: `merchant_seen_in_previous_requests` is true for 135/135 duplicates but only 65/365 legitimate rows. | `analyzers/prompting.py:51-80` (`request_view`, `system_facts`) | §2.2 | **Fixed** (2296360):<ul><li>such rows are dropped and counted in `manifest.dropped_label_not_visible_in_model_input`;</li><li>visible duplicates (re-send wording, repeated invoice) and legitimate renewals were added.</li></ul>The rules remain responsible for these risks. | `test_training_augment.py::test_rows_whose_label_*` | was yes / – / – |
| ML-06 | Medium | **Generator label noise:** `_Builder.vary()` re-draws the justification with p = 0.3. Injection variants whose injection sat in the justification keep label 1 without any injection text: **13/150** injection rows in the shipped seed 20260 (9 train, 2 validation, 2 test). This also inflates the "rules miss injections" counts. | `dataset.py:235` | `audit.injection_text(row) is None` on `category == "prompt_injection"` | **Excluded from SFT** (`injection_text_lost`). The generator is unchanged, because the shipped data and the pinned baseline depend on it. Proposal: `DATASET_VERSION 1.1` keeps injected text across variations. | `sft_data.invisible_reason` | no / no / no |
| ML-07 | Medium | **Targets were fixed per category:**<ul><li>only 35 distinct targets in train;</li><li>`summary` = the generator's label reason;</li><li>scores always 70/10.</li></ul>A model would memorise strings. | old `training/sft_data.py:target_for` | §2.2 | **Fixed:** grounded findings quote the request; scores and wording vary deterministically. 1,951 of 2,065 train targets are distinct. | `test_training_augment.py::test_targets_are_varied_*` | was yes / – / – |
| ML-08 | High | **The model-output parser was lenient.** It took the first `{` to the last `}`, so it:<ul><li>accepted prose around JSON;</li><li>accepted unclosed or repeated `<think>` blocks;</li><li>let a **trailing duplicate `"risk_score": 0` win**;</li><li>coerced `"90"` and `true`.</li></ul>It also had no size bound. | `analyzers/base.py:92` (old version) | 39 of 90 new tests fail on the old parser | **Fixed** (01ecc8a; 37777e7):<ul><li>exactly one JSON object;</li><li>an optional single bounded think block;</li><li>duplicate keys, NaN and oversize (> 32k chars output, > 8k think) rejected;</li><li>strict schema;</li><li>rejection → degraded and rules-only.</li></ul> | `tests/test_ml_model_output_parsing.py` (96) | no / no / was yes |
| ML-09 | High | **Qwen3 thinking mode and the serving contract.**<ul><li>Qwen3's chat template enables thinking by default.</li><li>llama-cpp-python 0.3.36 `create_chat_completion` has no `chat_template_kwargs`.</li><li>The llama.cpp analyzer built prompts **with** rule findings, while SFT trains **without** them.</li></ul> | `analyzers/llm.py` (old `build_messages(data)`) | §2.4 | **Fixed** (01ecc8a):<ul><li>`install_no_think_handler` renders the GGUF's own template with `enable_thinking=False`; verified identical to `transformers.apply_chat_template(enable_thinking=False)` on the real Qwen3-1.7B template;</li><li>new `LlamaCppAnalyzer(include_rule_findings=…, disable_thinking=True)`;</li><li>training asserts that the prompt ends with the empty think block.</li></ul>**Remaining:** a `Settings` field must switch rule findings off for the fine-tuned model (§4). | `test_ml_model_output_parsing.py::test_no_think_*`, `::test_llamacpp_serves_*`, `test_training_kaggle.py::test_prompt_is_rendered_*` | **yes until the Settings field exists** / – / yes |
| ML-10 | High (as a measurement) | **Bench v2 is development data only.**<ul><li>It was written by an AI agent with full access to rules and tests; the labels are its own proposals; `independent: false`.</li><li>14 of 19 risk types have fewer than 15 units.</li><li>167 of 210 units are English.</li><li>Bench **v1 overlaps the generator** (79 findings), so v1 cannot evaluate a model trained on generator data.</li></ul>Quality is otherwise sound:<ul><li>leakage check 0 findings (trigram overlap 0.022);</li><li>labels consistent with actions;</li><li>no duplicate requests;</li><li>45% of legitimate units are hard negatives.</li></ul> | `benchmarks/agentpal-bench-v2/*`; `leakage_check.py` output | `python benchmarks/agentpal-bench-v2/leakage_check.py` | Documented in the new `benchmarks/agentpal-bench-v2/README.md`. **Needs people:** custodian, authors, annotators. | `test_bench_v2_*` | no / claims only / **yes** |
| ML-11 | High | **A locked run could score unlocked labels.** `score.py` checked only `Path(labels).name` at the bench root, so `<bench>/sub/labels.jsonl` (never locked or hashed) was accepted. | `score.py:620` (old version) | `test_bench_v2_locked_labels.py` (2 of 4 failed before) | **Fixed** (5b867d7): labels must be `<bench>/adjudicated_labels.jsonl` and listed in `LOCK.json`. | same | no / no / yes (benchmark integrity) |
| ML-12 | Medium | **Scorer and auth (lead).** The in-process replay depended on the process `AGENTPAL_AUTH_MODE`, and a non-200 `/assess` crashed the run. | `score.py:159` (`harness_settings`) | `test_bench_v2_scoring_auth.py` | **Fixed** (5cc8de1):<ul><li>explicit dev auth whenever `Settings` has the field;</li><li>an HTTP refusal is a failed unit (blocked, counted in `http_rejected`), and its ledger follow-ups are `not_run`.</li></ul> | same | – |
| ML-13 | Medium | **ML artifact arrays were not validated (lead):** `zip()` truncated a mis-shaped artifact silently. The same applied to the metric helpers. | `analyzers/ml.py:64` (`_validated`), `evaluation.py:112-153` | `test_ml_artifact_shape.py` (13 failed before) | **Fixed** (fa1f820): length/type/threshold validation; `zip(strict=True)`. | same | – |
| ML-14 | Medium | **The llama.cpp JSON-schema grammar does not enforce the schema.**<ul><li>It allows raw newlines in strings (strict `json` rejects them).</li><li>It does not bound numbers: the tiny model emitted `risk_score: -137`, and zero-shot Qwen2.5 emitted `uncertainty: 2`.</li><li>It can stop mid-object at `max_tokens`.</li></ul>This is part of the 12–25% invalid-JSON rate. | `analyzers/base.py:111-115` | §2.6 | Raw control characters inside strings are now accepted (37777e7); numeric and length bounds remain hard rejections (degraded). The fine-tuned model is trained to stay in bounds; measure on the GGUF. | `test_ml_model_output_parsing.py::test_raw_newline_*`, `::test_out_of_bounds_*` | no / no / measure |
| ML-15 | High | **`train()` had never run and would have failed on Kaggle.**<ul><li>`bf16=True` is hard-coded, which is wrong for T4/P100.</li><li>`warmup_ratio` was removed in transformers 5.x, so the job would crash at start.</li><li>`assistant_only_loss` needs `{% generation %}` markers that the Qwen3 template lacks.</li><li>There was no resume, no hardware check, and no atomic output.</li></ul> | old `training/train_lora.py` | `--smoke-model` run, §2.5 | **Fixed** (37777e7):<ul><li>runtime precision;</li><li>prompt/completion loss (mask verified);</li><li>`--resume`;</li><li>NaN guard;</li><li>time-based checkpoints;</li><li>atomic `final/` with `COMPLETE.json`;</li><li>`hwcheck`, `evaluate_model`, `export`;</li><li>pinned versions.</li></ul>The real GPU run is **unverified**. | `test_training_kaggle.py` (23), `test_training_smoke.py` (opt-in; passed locally) | was yes / – / – |
| ML-16 | Low | **Dangling references** to `docs/BENCHMARK_V2_README.md` and `docs/BENCHMARK_V2_ANNOTATOR_GUIDE.md`, the latter shipped to annotators. | `leakage_check.py:29`, `annotation/make_packets.py` | `test_bench_v2_packets_guide.py` | **Fixed:**<ul><li>bench README written;</li><li>`annotation/ANNOTATOR_GUIDE.md` (`v0-draft`) now ships inside every packet.</li></ul> | same | – |
| ML-17 | Medium | **Synthetic labels mirror the designer.** Each category has one fixed label, and the prevalence is 64.9% risky. The new augmentation is also author-labelled. | `dataset.py` `CATEGORIES` | `audit.labels_by_category` | Limitation. Only the externally annotated benchmark can measure the system. | – | no / claims / **yes** |

## 2. Measured numbers

### 2.1 Synthetic data (seed 20260)
* **Reproducibility:**
  * Regenerated twice; byte-identical. SHA-256: train `fc7c2309…`, validation `c41bb253…`, test `bfb5c3d7…`.
  * The baseline refit is deterministic (identical numeric fields twice).
  * The refit does **not** equal the pinned artifact (ML-03).
* **Diversity:**
  * 1,850 rows, 370 groups;
  * 53 purposes, 66 justifications, 26 merchant names, 219 text templates, 8 injection sentences.
* **Overlap with train:**
  * Validation: 311/370 rows reuse a train template; 18/28 injection rows reuse a train injection sentence.
  * Test (counted only): 289/370 and 23/28.
* **Validation rows, by whether their template was seen in training:**

  | Subset | Rules miss | Rules + ML miss | Rules + ML flag legitimate |
  |---|---|---|---|
  | Template seen (311 rows) | 3/187 | 1/187 | 45/124 |
  | Template unseen (59 rows) | 9/53 = 0.170 [0.092, 0.292] | 4/53 | – (6 legitimate rows only) |
* **Class balance and coverage:**
  * 64.9% risky (train);
  * 10 categories, each with a single fixed label;
  * English only;
  * USD only.

### 2.2 SFT dataset v2 (`python -m training.sft_data --out build/sft`, seed 7319)

| | Train | Dev |
|---|---|---|
| Rows | 2,065 (1,145 risky = 55.4%) | 371 (217 risky = 58.5%) |
| Template families | 190 | 49 |
| Families shared with the other split | 0 | 0 |
| Distinct targets | 1,951 | 367 |
| Languages | 17 (150 non-English rows) | 9 (37 non-English) |
| Wording | 1,305 templated, 470 plain, 150 non-English, 70 paraphrased, 70 obfuscated | 185 / 115 / 37 / 17 / 17 |

**Per-category counts (train / dev):**

| Category | Train | Dev |
|---|---|---|
| legit_low_risk | 365 | 35 |
| legit_unusual | 215 | 35 |
| purpose_mismatch | 140 | 10 |
| unexpected_recurring | 120 | 30 |
| prompt_injection | 115 | 5 |
| suspicious_domain | 115 | 35 |
| suspicious_combination | 105 | 15 |
| missing_or_inconsistent | 90 | 0 |
| excessive_amount | 40 | 20 |
| aug_legit_multilingual | 90 | 22 |
| aug_injection_paraphrased | 70 | 17 |
| aug_injection_obfuscated | 70 | 17 |
| aug_injection_multilingual | 60 | 15 |
| aug_evidence_routine_legit | 60 | 15 |
| aug_near_name | 50 | 12 |
| aug_fabricated_evidence_with_signal | 50 | 12 |
| aug_missing_or_inconsistent | 50 | 12 |
| aug_near_name_legit | 40 | 10 |
| aug_purpose_mismatch | 40 | 10 |
| aug_renewal_legit | 40 | 10 |
| aug_duplicate_visible | 30 | 7 |
| aug_hard_negative (four kinds) | 110 | 27 |

**Dropped rows:**
* label not visible in the model input: duplicate 150, unknown_reference 60, over_review_limit 50, over_hard_limit 40, odd_hours 30;
* injection text lost: 11 (seed 7319);
* mixed-template rows: 30.

**Correlations:**

| Signal | Before (generator train) | After: train | After: dev |
|---|---|---|---|
| Risky rate, evidence present / absent | 0.523 / 0.653 | 0.553 / 0.556 | 0.598 / 0.558 |
| Risky rate, merchant seen / not seen | – | 0.377 / 0.600 | 0.441 / 0.633 |

The "merchant seen" difference is plausible (familiar merchants are less risky) and is no
longer label-defining.

**Overlap gate:**
* SFT vs bench v2: 0 findings; trigram overlap fraction 0.040.
* Augmented rows vs v1 + v2 + adversarial test strings: 0 findings. The de-obfuscated pass
  is included.
* The guard demonstrably fails on planted bench text (verbatim, suffix, homoglyph,
  zero-width, leet).

**Token counts** (exact, Qwen3-1.7B tokenizer at the pinned revision, prompt + completion):
* train: max 950, p99 860, mean 725;
* dev: max 896.

`max_seq_len` = 1536. 2.99M trained tokens; 260 optimizer steps at an effective batch of 16
for 2 epochs.

The pre-existing `merchant_seen` shortcut and the `note` type leak are gone. The old SFT
build had 1,610 / 210 rows and 35 distinct targets.

### 2.3 ML baseline: pinned vs refit (bench v2 dev, self-labelled)

| Approach | Missed risky | Legitimate flagged | Review rate |
|---|---|---|---|
| Rules | 40/137 [0.222, 0.373] | 19/73 [0.173, 0.371] | 0.462 |
| Hybrid, pinned artifact | 28/137 [0.145, 0.280] | 33/73 [0.343, 0.566] | 0.586 |
| Hybrid, refit from current code | 40/137 [0.222, 0.373] | 20/73 [0.185, 0.386] | 0.467 |

**McNemar, pinned vs refit:**
* recall: 12 vs 0, p = 0.0005;
* legitimate units not flagged: 0 vs 13, p = 0.0002.

**Refit vs pinned artifact:**

| | C | Validation F1 | `missing_evidence` coefficient |
|---|---|---|---|
| Refit | 1.0 | 0.948 | 0 (the feature is now constant) |
| Pinned | 0.3 | 0.973 | +0.937 (scaled: +1.716 logit on every request) |

### 2.4 Base model (verified 2026-10-29 from the Hugging Face API; no weights downloaded)

| | |
|---|---|
| Repository | `Qwen/Qwen3-1.7B` |
| Commit | `main` = **`70d244cc86ccca08cf5af4e1e306ecf908b1ad5e`** (lastModified 2025-07-26), not gated |
| Licence | `LICENSE` = Apache License 2.0 (SHA-256 `832dd9e0…e92e`); card tag `license:apache-2.0` |
| Architecture | `Qwen3ForCausalLM`; 28 layers; hidden 2048; FFN 6144; 16 query / 8 KV heads (head_dim 128); vocab 151,936; tied embeddings; bf16 weights (2 safetensors shards, 3.44 GB + 0.62 GB) |
| Context | 32,768 (README); `max_position_embeddings` 40,960; rope θ = 1e6 |
| Tokenizer | `tokenizer.json` SHA-256 `aeb13307…` (equals the LFS oid); eos `<|im_end|>`, pad `<|endoftext|>` |
| Chat template | Thinking is **on** by default. `apply_chat_template(..., enable_thinking=False)` appends `<think>\n\n</think>\n\n` to the generation prompt. The `/no_think` soft switch only works while thinking is enabled, and still emits an (empty) think block. |
| Card sampling advice | Non-thinking: T = 0.7, top-p 0.8. We serve greedily (T = 0) with a JSON grammar. The card warns that greedy decoding risks repetition **in thinking mode**; thinking is disabled here. |

**Serving contract (verified):**
* the GGUF template renders with `enable_thinking=False`;
* the result is identical to transformers' rendering;
* the real Qwen2.5 GGUF still produces JSON through the new handler (template unchanged).

### 2.5 Pipeline smoke test (CPU; tiny random model; NOT training)

**Setup:**
* model: `trl-internal-testing/tiny-Qwen3ForCausalLM` @ `25bb9b89…`, a 2-layer random model;
* 12 train / 6 dev rows of the SFT v2 build;
* `--max-steps 6`, eval and save every 2 steps, fp32, CPU.

**Run 1** (`--interrupt-after-step 3`):
* checkpoints 2 and 3 written;
* eval at step 2: `eval_loss 11.93`;
* log line: "interrupted on purpose after step 3 (resume test); final/ NOT written".

**Run 2:**
* without `--resume`, the run was refused ("already has checkpoints: pass --resume").
* A half-written `checkpoint-99` (no `trainer_state.json`) was planted. With `--resume`, the
  log reads "resuming from …/checkpoint-3". The half-written checkpoint was skipped.
* The run reached step 6, with evals at steps 4 and 6, and published
  `final/ (COMPLETE.json written)`.
* `verify_final`: 8 files hashed; `run_kind = pipeline-smoke-test`.

**Loss mask, verified on the tokenized dataset:**
* the trained tokens are exactly `target JSON + <|im_end|>` (78/82/54 tokens);
* the prompt tokens (689/599/728) end with the empty think block.

The loss of 11.93 ≈ ln(151,936) is expected for a random model. It says nothing about
quality.

**Libraries:** torch 2.15.0+cpu, transformers 5.22.0, peft 0.21.2, trl 1.17.0,
datasets 5.1.0, accelerate 1.15.0 (Python 3.13.17).

### 2.6 Evaluation and export path (smoke; meaningless metrics)

**`training.evaluate_model`, HF backend, on the smoke adapter:**
* 8 dev rows, plus bench v2 (6 cases + 2 episodes, 10 units scored by `score.py` next to
  rules_only, hybrid and classifier_only).
* Invalid JSON 8/8. The tiny model emits text. All units ran degraded, and none was less
  strict than rules.
* Latency p50 1.0 s; peak RSS 0.58 GB.

**`training.export`:**
* Refuses the smoke adapter without `--allow-smoke`.
* With the flag:
  * merge (fp32);
  * `convert_hf_to_gguf.py` from llama.cpp tag **b11989** (commit `78febbad…`), f16;
  * `llama_model_quantize` Q4_K_M via llama-cpp-python 0.3.36. Tiny tensors fall back to
    f16/q5_0/q8_0 because their width is not divisible by 256; Qwen3-1.7B's widths (2048,
    6144) are divisible.
* Verification: architecture `qwen3`; rope freq_base 1e6; the template carries the
  `enable_thinking` switch and renders the no-think prompt.
* `EXPORT_MANIFEST.json` written; q4 SHA-256 `2294dfec…`.

**`training.evaluate_model --gguf` (the serving analyzer) on that file:**
* invalid 6/6, latency p50 13.7 s on CPU;
* the raw output had a newline inside a string and out-of-range numbers (ML-14).

### 2.7 Bench v2 scorer spot-check

These checks all match the committed `reports/dev_smoke_self_labelled.md` exactly:
* **Five units, recomputed independently through `orchestrator.assess` with the same
  fixture:**
  * v2-case-001: legitimate, allow;
  * v2-case-030: hard negative, review (a false positive);
  * v2-case-061: lookalike, review;
  * v2-case-100: injection, allow (a miss);
  * v2-case-140: missing info, review.
* **Counts recomputed from raw predictions:**
  * rules FN 40/137, FP 19/73;
  * hybrid FN 28/137, FP 33/73.
* **Statistics recomputed by hand:**
  * Wilson interval: [0.2223, 0.3729];
  * McNemar on recall: 0 vs 12, exact p = 0.000488.

## 3. Limitations
* **Nothing measures real-world accuracy.**
  * The synthetic data and the augmentation are author-labelled.
  * Bench v2 is self-labelled by an AI author who read the rules.
  * The independent locked benchmark does not exist.
* **The real training run, the GPU memory estimate (~8–9 GB peak at batch 2 × 1536 with
  gradient checkpointing) and the fp16 numerical stability of Qwen3 on T4 are
  unverified.**
  * fp16 overflow is possible.
  * The NaN guard stops the run and keeps the last good checkpoint.
  * `--precision fp32` is the fallback. It needs more memory; unverified.
* **The 1.7B export is unverified:** sizes, time and the converter's
  `Unknown RoPE type: default` warning (harmless on the tiny model; rope θ was written).
* **The HF-backend evaluation has no output grammar,** so its invalid-JSON rate
  overstates serving. Use `--gguf` for serving numbers.
* **Augmentation translations** were written by an AI agent. They are plausible but not
  reviewed by native speakers.
* **The lock file** was produced on Python 3.13.17 and needs Python ≥ 3.12 (numpy 2.5.x).
  The top-level pins support ≥ 3.11.

## 4. Proposed changes to files I do not own
1. **`config.py` / `analyzers/__init__.py`:** add `Settings.llamacpp_include_rule_findings`
   (env `AGENTPAL_LLAMACPP_INCLUDE_RULE_FINDINGS`, default `true` for the current zero-shot
   model). It **must be `false`** when the fine-tuned GGUF is pinned. `build_analyzer_chain`
   already reads it via `getattr`.
2. **ML-03:** either retrain and re-pin the LR baseline from current code
   (`python -m agentpal data && python -m agentpal train && make pin-ml`), or drop the
   constant `missing_evidence` component from `features.ml_vector` and re-pin. Then
   re-measure F9 (rules vs rules + ML) on the independent benchmark. Until then, do not
   cite the hybrid's extra recall.
3. **`__main__.py`:** make `evaluate --split` default to `validation` (ML-04).
4. **`docs/TRAINING_HANDOFF.md`** (lead): use the Kaggle commands in §5 and the artifact
   names in `training/export.py`. Pin the fine-tuned GGUF with the `pin_command` from
   `EXPORT_MANIFEST.json`.
5. **`Makefile`:** add `sft`, `train-dry-run` and `bench-v2-leakage` targets
   (`python -m training.sft_data --out build/sft`,
   `python -m training.train_lora --data build/sft --dry-run`,
   `python benchmarks/agentpal-bench-v2/leakage_check.py`).
6. **`dataset.py`** (mine, deferred on purpose): version 1.1 keeps injection text through
   `vary()` (ML-06). This changes the data hashes and the pinned baseline, so it is the
   owner's call.

## 5. Kaggle runbook (GPU steps UNVERIFIED here)

```bash
# 0. session: GPU T4 x2 or P100, Internet on. Outputs persist only under /kaggle/working.
git clone <repo> && cd <repo>/agentpal
pip install torch==2.15.0 --index-url https://download.pytorch.org/whl/cu126   # match the driver
pip install -r requirements.txt -r requirements-train.txt
export HF_HOME=/tmp/hf          # base-model cache outside /kaggle/working (not persisted)
OUT=/kaggle/working/agentpal-lora

# 1. hardware check (refuses < 15 GB VRAM; uses GPU 0 only)
python -m training.hwcheck --output-dir $OUT
# 2. data (deterministic; fails on benchmark overlap), then dry run with exact token counts
python -m training.sft_data --out build/sft
python -m training.train_lora --data build/sft --dry-run
# 3. train (checkpoints every 20 steps and every 25 min under $OUT/checkpoints)
python -m training.train_lora --data build/sft --output-dir $OUT
# 3b. after a session ends: same command plus --resume
python -m training.train_lora --data build/sft --output-dir $OUT --resume
# 4. evaluate: (a) SFT dev and (b) bench v2, for the adapter and the zero-shot base
python -m training.evaluate_model --adapter $OUT/final --data build/sft --out $OUT/reports
python -m training.evaluate_model --zero-shot --data build/sft --out $OUT/reports
# 5. export: merge -> GGUF f16 -> Q4_K_M -> verify -> manifest
git clone --depth 1 --branch b11989 https://github.com/ggml-org/llama.cpp /kaggle/working/llama.cpp
python -m training.export --adapter $OUT/final --out $OUT/export --llama-cpp /kaggle/working/llama.cpp
# 6. evaluate the exported GGUF through the serving analyzer, then hash
python -m training.evaluate_model --gguf $OUT/export/agentpal-qwen3-1.7b-q4_k_m.gguf \
    --data build/sft --out $OUT/reports
sha256sum $OUT/export/agentpal-qwen3-1.7b-q4_k_m.gguf   # must equal EXPORT_MANIFEST.json
```

**Expected artifacts:**
* `$OUT/checkpoints/checkpoint-<step>/`;
* `$OUT/final/`: `adapter_model.safetensors`, `adapter_config.json`, tokenizer files,
  `train_config.json`, `COMPLETE.json`;
* `$OUT/logs/train_log.jsonl`;
* `$OUT/reports/`: `eval_finetuned.json`, `eval_zero_shot.json`, `eval_gguf.json`,
  `bench_v2_<name>.{json,md}`, predictions JSONL;
* `$OUT/export/`: `agentpal-qwen3-1.7b-q4_k_m.gguf` and `EXPORT_MANIFEST.json`.
