# Training run 2026-10-30 (Modal): Qwen3-1.7B LoRA

**This is the first real fine-tuning run of the AgentPal risk model.** The evidence is in this directory, and the weights stay on the Modal Volume `agentpal-train-out` (`agentpal-lora/final`). Weights are never committed.

**All results are development evidence** on synthetic and AI-written, self-labelled data. They are **not** real-world accuracy, and no independent benchmark exists yet.

## Run

| Item | Value |
|---|---|
| Code | bundle of commit `261af95` (verified by hash in the container) |
| Data | SFT v2: 2,054 train / 371 dev rows; sha256 `c4cc4069…` / `d8ca8fd8…`, matched in the container |
| Base model | `Qwen/Qwen3-1.7B` @ `70d244cc86ccca08cf5af4e1e306ecf908b1ad5e` (Apache-2.0) |
| Hardware | Modal **NVIDIA L40S** (48 GB), bf16; the earlier `check` step got an A10 |
| Libraries | Python 3.13.18; torch 2.15.0, transformers 5.22.0, peft 0.21.2, trl 1.17.0 |
| Training | LoRA r = 16, 2 epochs, **258 steps, 986 s**. Best checkpoint step 240, dev loss **0.1278** (0.47 at step 20). Training loss 2.84 → 0.115. No NaN, no resume needed. |
| Completion | `COMPLETE.json`: `run_kind: training`, SHA-256 of all 8 adapter files |

## Results: SFT dev (371 rows: 217 risky, 154 legitimate; template-family disjoint from training)

| Approach | Missed risky (FN) | Legitimate flagged (FP) | Legit blocked | Invalid output | p50 / p95 |
|---|---|---|---|---|---|
| Rules only | 59/217 = 0.272 [0.217, 0.335] | 55/154 = 0.357 [0.286, 0.436] | 0 | — | — |
| **Rules + fine-tuned model** | **8/217 = 0.037 [0.019, 0.071]** | **55/154 = 0.357** (unchanged) | 0 | **0/371** | 2.7 / 5.3 s |
| Rules + zero-shot Qwen3-1.7B | 3/217 | **147/154 = 0.955** | 0 | 225/371 (61%) | 4.7 / 6.4 s |

**The fine-tuned model alone:** precision 1.00, recall 0.79, **false-positive rate 0.00** (0/154).

**Per category:** paraphrased, obfuscated and multilingual prompt injections are caught **49/49** by rules + model, against **6/49** by rules alone. See `eval_finetuned.json` (`per_category`, `per_language`, `per_wording`).

**Caveat:** the dev data comes from the same synthetic pipeline as the training data (disjoint templates, same authorship), so it flatters the model.

## Results: bench v2 dev (210 units, written separately; AI-written, self-labelled, not independent)

| Approach | Missed risky | Legitimate flagged | Legit blocked | Review rate |
|---|---|---|---|---|
| Rules only | 40/137 = 0.292 [0.222, 0.373] | 19/73 = 0.260 [0.173, 0.371] | 0/73 | 0.462 |
| **Rules + fine-tuned model** | **10/137 = 0.073 [0.040, 0.129]** | 22/73 = 0.301 [0.208, 0.414] | 0/73 | 0.619 |
| Rules + zero-shot | 3/137 | **66/73 = 0.904** | 0/73 | 0.862 |

**Paired test (McNemar) on identical units:**
* recall: the model caught **30** risky units the rules missed, and the rules caught **0** the model missed. Difference +0.219 [0.146, 0.286], **p < 0.001**;
* false positives: +3 (19 → 22), **not significant** (p = 0.25).

## Safety checks
* **Never less strict than rules:** `never_less_strict_than_rules = true` on dev and bench, for both models.
* **Rules still block:** "Required block missed" is 0/19 for rules + model. Blocks come only from the rules; model-only scoring cannot block by design.
* **No timeouts:** 0 outputs exceeded the 30 s serving timeout.

## Individual failures (bench v2, rules + fine-tuned model)

**Missed risky (10):**
* **Fabricated evidence (5):** `v2-case-072`, `074`, `080` (es: "an invoice I prepared myself"), `081` (ja: "invoice created by the assistant"), `082`. Agent-made invoices are not yet recognised as a red flag.
* **Purpose mismatch, non-English or paraphrased (3):** `v2-case-040` (es: gift cards), `049` (tr: in-game currency), `046` (designer sunglasses "for myself").
* **Unexpected recurrence (1):** `v2-case-137` (es).
* **Missing information (1):** `v2-case-144` ("vendor confirmed the refund by email").

**Legitimate flagged (15 single cases):** 13 are deliberately risky-looking hard negatives (VAT adviser, crypto-accounting course, a "Gift Card Fraud" webinar, full-width characters, Chinese and Japanese routine purchases), most already flagged by the rules.

These are the targets for the next data round. Do **not** add bench v2 items to training: it would leak the evaluation set.

## Export and serving-path evaluation (done after the owner allowed it)

**Export (Modal):**
* merge → GGUF (llama.cpp `78febbad…`, tag b11989) → **Q4_K_M, 1,107,408,736 bytes**, SHA-256 `92522e111321ea348b7981976e6e98a9fbfe27167711ce2d405a2e1da29c9817`;
* the format check passed and the no-think prompt template works;
* provenance is in [`EXPORT_MANIFEST.json`](EXPORT_MANIFEST.json);
* the downloaded copy's hash matches the manifest.

**Serving-path evaluation:** run locally through the real llama.cpp analyzer, on a 4-core CPU like the service host, with no rule findings and thinking disabled. Reports are in [`serving_eval/`](serving_eval/).

| GGUF Q4_K_M + rules | Missed risky | Legitimate flagged | Legit blocked | Invalid | p50 / p95 latency |
|---|---|---|---|---|---|
| SFT dev (371) | 17/217 = 0.078 (rules only: 59) | 55/154 (unchanged) | 0 | 0/371 | 8.6 / 19.1 s |
| Bench v2 dev (210) | 13/137 = 0.095 (rules only: 40) | 20/73 (rules only: 19) | 0 | 0 | — |

**Bench v2 paired comparison:**
* the model caught 27 risky units the rules missed, and the rules caught 0 it missed (significant);
* false positives +1 (not significant).

**Other observations:**
* The model alone falsely flagged 1/154 legitimate dev items.
* The 4-bit quantization costs some recall compared with bf16 (dev misses 17 vs 8).
* **Latency:** p95 is 19.1 s against the 30 s model timeout on 4 CPU cores, so the margin is thin. A slower host runs degraded: rules only, plus review.

## Integration (opt-in, advisory)
* **Model file:** installed at `artifacts/models/agentpal-qwen3-1.7b-q4_k_m.gguf` (mode 0444, directory 0555; git-ignored).
* **Pin:** `config/model_manifest.json → llamacpp` (it replaces the zero-shot Qwen2.5 pin; one-model strategy).
* **Settings:** `Settings.llamacpp_model_path` points at the new file; `llamacpp_include_rule_findings` defaults to **false**, matching training.
* **End-to-end check** (`AGENTPAL_ANALYZER_CHAIN=llamacpp,heuristic`, integrity required, read-only staged load): the demo requests all ran on `backend=llamacpp`, none degraded. Legitimate requests → `allow_for_review`; injection and purpose mismatch → `require_review`; budget exceeded → `block` (rules).
* **Default chain unchanged:** `ml,heuristic`. Enabling the model by default needs the owner's acceptance bar, measured on the independent benchmark.
