# Training run 2, 2026-10-30 (Modal): Qwen3-1.7B LoRA on aug-2 data, **the pinned model**

This run replaces run 1 ([`../2026-10-30-modal/`](../2026-10-30-modal/README.md)) as the pinned, opt-in llama.cpp model. All results are **development evidence** on synthetic and AI-written, self-labelled data, not real-world accuracy.

## What changed from run 1
* **Data (aug-2):** three families aimed at run 1's dev errors, written fresh and gated against every benchmark item (0 overlap findings):
  * agent-authored ("self-made") evidence, risky, 8 languages;
  * non-English purpose mismatch, risky;
  * risky-sounding legitimate topics, legitimate.

  Train 2,194 / dev 405 rows (sha256 `2a94fa75…` / `0488aed4…`).
* **Serving:** llama.cpp now uses all CPU cores (it previously used half).

## Run
* **Hardware and time:** Modal NVIDIA L40S, bf16, **276 steps, 1,080 s**.
* **Loss:** best checkpoint at step 260; dev loss 0.530 → **0.126**, no overfitting visible.
* **Completion:** `COMPLETE.json` has `run_kind: training` and the hashes of all 8 adapter files.
* **Export:** GGUF Q4_K_M, sha256 **`23bd2fdeffc5c7bf586abe25bca5c87a1a76dd413e70138179193ff151cde1e6`** ([`EXPORT_MANIFEST.json`](EXPORT_MANIFEST.json)); the downloaded copy is verified.

## Serving-path evaluation (real llama.cpp analyzer, local 4-core CPU)

**Run 1 vs run 2 on identical units:**

| | Rules only | Run 1 + rules | **Run 2 + rules** |
|---|---|---|---|
| aug-2 dev: risky missed (of 239) | 80 | 28 | **14** [3.5%, 9.6%] |
| aug-2 dev: legitimate flagged (of 166) | 56 | 65 | **56** (the model adds none) |
| bench v2 dev: risky missed (of 137) | 40 | 13 | **8** [3.0%, 11.1%] |
| bench v2 dev: legitimate flagged (of 73) | 19 | 20 | **20** |
| Invalid output | — | 0 | **0** |
| Never less strict than rules | — | yes | **yes** |
| Latency p50 / p95 | — | 7.7 / 16.5 s* | **6.9 / 10.7 s** |

\* Run 1 was timed with all cores on this dev set. Its first evaluation used half the cores: 8.6 / 19.1 s.

**Other results for run 2:**
* **Model alone on aug-2 dev:** precision 1.00, recall 0.73, false-positive rate 0.00.
* **Bench v2, McNemar paired test:** the model caught 32 risky units the rules missed, and the rules caught 0 it missed (significant). False positives +1 (not significant).
* **Target families (dev):**

  | Family | Run 1 | Run 2 |
  |---|---|---|
  | Self-made evidence caught (of 12) | 1 | **8** |
  | Non-English mismatch caught (of 10) | 9 | **10** |
  | Risky-sounding legitimate topics falsely flagged (of 12) | 8 | **0** |

**Caveat:** bench v2 errors guided which families were written (not their text), so the bench v2 improvement is optimistic. Independence needs the locked, human-labelled benchmark.

## Integration
* **Model file:** `artifacts/models/agentpal-qwen3-1.7b-q4_k_m.gguf` (mode 0444, directory 0555; git-ignored).
* **Pin:** `config/model_manifest.json → llamacpp = 23bd2fde…`.
* **Provenance test:** `test_repository_manifest_pins_the_trained_model_with_provenance` checks the pin against this run's export manifest.
* **End-to-end check** (`AGENTPAL_ANALYZER_CHAIN=llamacpp,heuristic`, integrity required, read-only staged load): all demo requests ran on `backend=llamacpp`, none degraded. Legitimate → allow_for_review; injection, purpose mismatch and evidence mismatch → require_review; budget exceeded → block (rules).
* **Default chain unchanged** (`ml,heuristic`): the model stays opt-in and advisory.
