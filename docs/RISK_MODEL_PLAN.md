# AgentPal risk model: audit, recommendation and training plan

> **Update 2026-10-30:** two real LoRA fine-tunes ran on Modal. Run 2 (aug-2 data) is the pinned, opt-in model (see [`training_runs/2026-10-30-modal-run2/`](training_runs/2026-10-30-modal-run2/README.md)).
> * On bench v2 dev, rules + model miss 8/137 risky units vs 40/137 for rules alone; false positives 20 vs 19 of 73.
> * The default chain is unchanged until the independent benchmark exists.

Milestone branch `agentpal-risk-model` (from `30247da`).

**Status: a reproducible, Kaggle-ready pipeline is prepared. NO model was fine-tuned.** This environment has no GPU. The training loop was executed only as a CPU **pipeline smoke test**: a tiny random Qwen3 test model, a few steps, interrupt and `--resume`, evaluation and GGUF export of the result. The Qwen3-1.7B weights were not downloaded; config, tokenizer, licence and README were. The audit findings (ML-01 … ML-17), the measured numbers and the Kaggle runbook are in [`ML_AUDIT.md`](ML_AUDIT.md).

Deterministic rules remain the security authority. Any model is advisory: it can add a review, but it never blocks, unblocks or relaxes a recommendation (owner decisions 2 and F9; enforced in `orchestrator.py` and tested).

## 1. What the current "AI model" actually is

There are two model backends today. Neither is a dedicated, trained AgentPal language model.

| Backend | What it really is | What it can and cannot do |
|---|---|---|
| `ml` (default chain) | Logistic regression over **34 numbers that the rule detectors already compute** (lookalike flag, injection-pattern hits, budget ratio, ...). Trained on 1,110 synthetic rows. | It **re-weights the rules' own signals**. It never reads the text, so it **cannot catch anything the rules miss** (paraphrased injection, a new kind of scam). For the legitimate demo request `01`, the largest contributions to "needs review" are artifacts: `missing_evidence` (+1.72) and `recurring` (+0.77) (see README). **Correction (ML_AUDIT ML-03):** since owner decision F2 that feature is 1 for every request, so the pinned artifact adds +1.72 to *every* assessment (train/serve skew). On bench v2 dev, a refit from current code scores the same as rules alone. |
| `llamacpp` (opt-in) | **Untrained**, off-the-shelf Qwen2.5-1.5B-Instruct (Q4_K_M GGUF), prompted zero-shot. | It reads the text, but it was never taught AgentPal's task. In Phase 1 it flagged **every** legitimate case in a 60-item sample (FPR 1.0). It also received the rules' findings, so it mostly echoed them. |

**Zero-shot baseline, re-measured in this milestone.** Qwen2.5-1.5B Q4_K_M ran on 40 stratified rows (20 risky, 20 legitimate) of the template-disjoint SFT dev split, on CPU. The script is `zeroshot.py`; its raw output is kept in the session scratchpad, not the repo.

| Prompt variant | Valid JSON | Risky missed | Legitimate flagged | Latency p50 / p95 |
|---|---|---|---|---|
| With rule findings (current serving) | 30/40 | 0 of 11 valid [0, 0.26] | **19 of 19 valid [0.83, 1.0]** | 13.9 / 20.5 s |
| Without rule findings (fine-tuning target) | 35/40 | 0 of 16 valid [0, 0.19] | **19 of 19 valid [0.83, 1.0]** | 12.3 / 16.5 s |

What this shows:
* **It is not discriminating.** "Zero missed" is meaningless, because the model flags every legitimate request it answers.
* **Output is unreliable:** 12–25% of answers are not valid JSON (those run degraded, rules only).
* **These are the "before" numbers** a fine-tuned model must beat.

Latency was measured while other tests were also using the CPU.

**Prompt fix in this milestone.** Until now the prompt showed the agent's *own* `monthly_budget` and `budget_spent_this_period` claims to the model as if they were facts. Now:
* those fields are labelled `agent_claimed_*`;
* the trusted operator budget is given separately (`operator_monthly_budget`, `operator_budget_remaining`);
* `build_messages(include_rule_findings=...)` lets training and serving agree on whether rule findings are shown (tests in `test_orchestrator.py`).

## 2. Training-data audit (`python -m training.audit`)

| Finding | Measured | Why it matters |
|---|---|---|
| **Very few templates** | 1,850 rows contain only 53 distinct purposes, 66 justifications and 26 merchant names. Prompt injections use **8 fixed sentences**. | A model can memorise the wording instead of learning risk. |
| **Repeated variations** | Each scenario is repeated 5× with different numbers: **370 independent scenarios, not 1,850**. | The effective sample size is 5× smaller than it looks, and confidence intervals computed per row are too narrow. |
| **Cross-split leakage** | 311 of 370 validation rows (84%) and 289 of 370 test rows (78%) reuse a training (purpose, justification) template. 18 of 28 validation injection rows reuse a training injection sentence. | Held-out scores mostly measure recall of seen text. |
| **Memorisation gap** | Validation rows with a template seen in training: rules miss **3/187 = 1.6%** of risky requests, rules + ML miss **1/187 = 0.5%**. Rows with an **unseen** template: rules miss **9/53 = 17.0% [9.2, 29.2]**, rules + ML miss **4/53 = 7.5% [3.0, 17.9]**. | The headline miss rates (5.0% and 2.1%) are flattered by memorised templates. Only 6 unseen rows are legitimate, so false positives cannot be measured there. |
| **Spurious evidence signal** | Training rows without evidence are 88% risky; with evidence, 58%. Every `legit_unusual` row (250/250) carries evidence. | The model learns "an invoice is attached → safe", which is exactly the fabricated-evidence weakness the owner ruled out (F2). |
| **Spurious recurring signal** | Recurring rows are 78% risky; non-recurring 60%. | A legitimate subscription renewal looks risky to the ML. |
| **Labels mirror the designer** | Every category has one fixed label, written by the same author as the rules. Labels are not computed by the rules, but they encode the same view. | No synthetic score can show that the system is right, only that it agrees with its author. |

**Conclusion.** This generator is fine for teaching an output **format** and basic behaviour. It **cannot** produce a meaningful test set, and its scores must not be read as accuracy.

## 3. Recommended base model: **Qwen3-1.7B** (one model; it replaces Qwen2.5-1.5B)

**Verified on 2026-10-29** from the Hugging Face API and the files at the pinned revision (see `ML_AUDIT.md` §2.4):
* **Revision:** `Qwen/Qwen3-1.7B` main = `70d244cc86ccca08cf5af4e1e306ecf908b1ad5e`, pinned in `TrainConfig.base_revision`.
* **Licence:** the `LICENSE` file is Apache-2.0.
* **Thinking mode:** the chat template enables thinking by default. Training and serving both disable it (`enable_thinking=False`, which renders an empty `<think></think>` block). The service does this through `analyzers/llm.install_no_think_handler`, because llama-cpp-python 0.3.36 has no `chat_template_kwargs`.

| Candidate | Licence | Size | Fit |
|---|---|---|---|
| **Qwen/Qwen3-1.7B** | **Apache-2.0**, ungated | 2.0B | **Recommended.** Standard dense text model (`Qwen3ForCausalLM`), so LoRA tooling is mature. The llama.cpp already installed here supports it (`qwen3`). Same size class and integration path as the current Qwen2.5-1.5B (the existing GGUF/pinning/staging code is reused). Multilingual, which the owner requires for multilingual attacks. |
| Qwen/Qwen3.5-2B | Apache-2.0 | 2.3B | Newer, but a **vision-language** model with a hybrid linear/full-attention architecture. The vision encoder is unused weight, and fine-tuning tooling for the new architecture is less mature. A reasonable later upgrade once tooling settles. |
| Qwen/Qwen3-4B | Apache-2.0 | 4.0B | Stronger, but roughly 2× slower on this 4-core CPU. Use it only if 1.7B fails the dev bar. |
| Qwen/Qwen2.5-1.5B-Instruct (current) | Apache-2.0 | 1.5B | Older generation. Note that **Qwen2.5-3B is *not* Apache** (`license:other`, the Qwen research licence). |
| microsoft/Phi-4-mini-instruct | MIT | 3.8B | Permissive, but about 2× the size and weaker multilingual coverage. |
| ibm-granite/granite-4.0-micro | Apache-2.0 | 3.4B | Permissive, but about 2× the size. |
| HuggingFaceTB/SmolLM3-3B | Apache-2.0 | 3.1B | Permissive, but about 2× the size. |
| meta-llama/Llama-3.2-3B-Instruct | Llama 3.2 Community Licence (gated) | 3.2B | Commercial use is conditional (acceptable-use policy, attribution, MAU threshold). |
| google/gemma-3-4b-it | Gemma Terms of Use (gated) | 4.3B | A custom licence with a prohibited-use policy. |

**Measured serving cost on this machine** (4 CPU cores, 15 GB RAM, no GPU), using the already-present 1.5B Q4_K_M GGUF as a same-size proxy:
* model load: 10.1 s;
* one assessment (603 prompt tokens + ~150 output tokens): **7.6–15.4 s**;
* RAM: about 1.5 GB.

This fits inside the 30 s model timeout. When the model is slow or failing, assessments run degraded, rules only, and require review.

**Training resources** (estimates; NOT measured on a GPU; sizes in `ML_AUDIT.md` §2.2):
* 2.99M trained tokens for 2 epochs (exact Qwen3 token counts on the sft-2 build);
* 260 optimizer steps;
* one 16 GB GPU (Kaggle T4/P100) in fp16, or bf16 on Ampere+;
* estimated peak ~8–9 GB at batch 2 × 1536 with gradient checkpointing;
* roughly 1–2 h on a T4;
* about 4.1 GB download for the base model.

The hardware check refuses a GPU with less than 15 GB of VRAM. No 4-bit QLoRA path is
implemented.

CPU-only LoRA of the real model is not viable here. On CPU, only the tiny-model pipeline smoke test is run (`--smoke-model`).

## 4. Training pipeline (`agentpal/training/`)

| Step | Command | Guarantees (tested in `tests/test_training_*.py`, `tests/test_ml_*.py`) |
|---|---|---|
| Audit | `python -m training.audit` | Reproducible numbers for section 2. |
| Build SFT data | `python -m training.sft_data --out build/sft` | <ul><li>**New seed** (7319); the retired `data/test.jsonl` is never read.</li><li>Inputs come from the **same prompt builder as serving**, without rule findings.</li><li>Targets are grounded, varied `ModelAnalysis` JSON and **never `block`**.</li><li>Rows whose label the prompt cannot show are dropped (ML-05/06).</li><li>Hand-written augmentation (`training/augment.py`): paraphrased, obfuscated and non-English injections; non-English legitimate requests; near-name impersonation; evidence that is risky only together with another signal; visible duplicates; hard negatives.</li><li>Train and dev share **no template family**.</li><li>Claimed evidence is decorrelated in train **and** dev.</li><li>The build **fails on any overlap with bench v1/v2**.</li><li>The build is deterministic.</li></ul> |
| Check before spending GPU time | `python -m training.hwcheck`, then `python -m training.train_lora --data build/sft --dry-run [--tokenizer DIR]` | Hardware: VRAM ≥ 15 GB, precision, disk, RAM. Data: rejects a `block` target, rule findings in the input, a manifest without the retired-split certificate or with benchmark overlap, empty splits, and over-length examples (exact token counts with a tokenizer). |
| Train (GPU machine) | `pip install -r requirements-train.txt`, then `python -m training.train_lora --data build/sft --output-dir /kaggle/working/agentpal-lora` (add `--resume` to continue) | <ul><li>LoRA r = 16 on all projections.</li><li>Loss on the target JSON + `<|im_end|>` only (prompt/completion; mask verified).</li><li>Thinking disabled.</li><li>Precision chosen at runtime.</li><li>Checkpoint chosen on **dev loss only**.</li><li>Checkpoints every 20 steps and every 25 min.</li><li>NaN guard.</li><li>`final/` published atomically with `COMPLETE.json` (hashes).</li><li>Base revision pinned.</li></ul> |
| Evaluate | `python -m training.evaluate_model --adapter …/final` (or `--zero-shot`, or `--gguf …`) | (a) the SFT dev split; (b) bench v2 through its own `score.py`. Reports invalid-JSON rate, FN/FP, legitimate blocks, review rate, P/R/F1, per category, latency and memory, next to the rules, hybrid and classifier baselines. |
| Export | `python -m training.export --adapter …/final --out …/export --llama-cpp <llama.cpp b11989>` | merge → `convert_hf_to_gguf.py` → Q4_K_M → template and no-think check → `EXPORT_MANIFEST.json` (SHA-256), then `python -m agentpal pin-model llamacpp …`. The served bytes are pinned by digest, staged read-only and verified (owner decision 7). |

**Data separation:**
1. **Training data:** the synthetic train split (format and behaviour only), plus hand-written and externally sourced examples (see section 7).
2. **Development data:**
   * the synthetic template-disjoint dev split (210 rows; it has only 5 injection rows and no "missing or inconsistent" rows, so it is too thin alone);
   * the helper's AI-written v2 dev benchmark (`benchmarks/agentpal-bench-v2/`, `independent: false`).

   Both are used for checkpoint choice and error analysis.
3. **Locked test data:** the externally annotated benchmark only, held by a custodian outside this repository and run once per release candidate (`BENCHMARK_ANNOTATION_PROTOCOL.md`). **It does not exist yet.**

**Serving after training:** the existing `llamacpp` backend, with the same prompt (`include_rule_findings=False`), the timeout, read-only staged loading and the bounded, advisory-only influence. No second language model is added: the fine-tuned Qwen3-1.7B replaces Qwen2.5-1.5B. The fine-tuned model must be served with `include_rule_findings=False` and thinking disabled (`LlamaCppAnalyzer(include_rule_findings=False, disable_thinking=True)`); a `Settings` field for the first is proposed in ML_AUDIT §4.

## 5. Acceptance bar before the model may be enabled (proposal for the owner)

The model is enabled in the chain only if all of these hold:
1. **On the external locked benchmark,** rules + model miss significantly fewer risky payments than rules alone (McNemar p < 0.05). The extra review rate must be within a limit the owner sets.
2. **Never blocks or relaxes anything:** zero legitimate-payment blocks and zero relaxations attributable to the model. This is guaranteed structurally and re-tested.
3. **Fast enough:** p95 latency ≤ 30 s on the target hardware, with a degraded rate reported.
4. **Robust:** no regression on the paraphrased, non-English and obfuscated slices.

Until then, the ML baseline stays advisory, and rules-only vs. rules + ML is decided on the same benchmark (F9).
