# AgentPal audit ledger (full project audit, 2026-10-29)

Branch `agentpal-risk-model`.

**Initial state:**
* Branch `agentpal-risk-model`, HEAD `ba9f105`.
* Uncommitted: the first benchmark helper's output (`benchmarks/agentpal-bench-v2/`, `tests/test_bench_v2_*.py`) and one doc edit.
* No CI, no lint configuration.
* Initial suite: **692 passed, 0 failed, 0 skipped**.

**Severity scale:**
* **Critical:** an unsafe financial outcome is possible.
* **High:** blocks a release or produces misleading results.
* **Medium:** a real defect with limited impact.
* **Low / Info:** hygiene, or worth noting.

**"Blocks"** says whether a finding blocks Kaggle training (K), a demo (D) or production deployment (P).

## Lead-agent findings

| ID | Sev | Finding and evidence | Reproduction | Fix / status | Regression test (result) | Blocks |
|---|---|---|---|---|---|---|
| LEAD-01 | Info | The repo-root `.env` (storefront, not AgentPal) is **tracked** in git (commit `70a8199`). It holds only `VITE_SUPABASE_*` keys; Supabase publishable keys are meant to be public in client bundles. | `git ls-files .env` | **Open, out of scope**: storefront code. Recommendation: untrack it and rotate the key if it was ever a secret key. | — | — |
| LEAD-02 | Medium | `make evaluate` and `make evaluate-llm` ran on the **retired** test split. | `make -n evaluate` | **Fixed** (`80dc5e8`): validation split; the retired split moves to `make evaluate-retired-test`. | `make -n evaluate` prints `--split validation` (verified) | K |
| LEAD-03 | Low | `orchestrator.py:113`: a lambda in the analyzer loop read `bounded_input` late (ruff B023). A call queued behind a saturated model pool could receive a later iteration's input (deadline). Its result is discarded, so there is no effect on a decision. | Code review; needs pool saturation between two submits | **Fixed** (`80dc5e8`): the input is bound as a default argument. | No deterministic test (the race requires timed pool saturation); timeout suites pass | — |
| LEAD-04 | Low | Two approval-binding tests asserted a blind `Exception` (ruff B017), so they could pass for the wrong reason. | `ruff check --select B017` | **Fixed:** they now assert `InvalidLedgerTransition`. | Both pass | — |
| LEAD-05 | Medium | No CI, no lint or type-check configuration. | `ls .github` | **Fixed:** `.github/workflows/agentpal.yml` (ruff, mypy informational, data generation, pytest, pip-audit) and `[tool.ruff]` / `[tool.mypy]` in `pyproject.toml`. | The CI steps are verified by a local fresh-clone run (see "Verification"); the GitHub-hosted run is not observed yet | P |
| LEAD-06 | Low | mypy reports 29 errors (38 after the helper merges; the new ones were sampled and are the same kind). On review they are all `Optional`/`Literal` narrowing or annotation gaps (e.g. `features.py:411`, which is guarded by `no_policy`); no real defect was found. | `python -m mypy agentpal` | **Open:** mypy runs informationally in CI. | — | — |
| LEAD-07 | Info | Ten financial-control invariants had no single negative-test suite. | — | **Added** `tests/test_invariants.py` (24 cases, through the service and store). | 24 passed | — |
| LEAD-08 | High | **Synthetic data is memorisable.** 370 scenarios × 5 near-identical variations; 53 purposes; 8 injection sentences. 84% of validation rows reuse a training template. On unseen templates, rules miss 17.0% [9.2, 29.2] versus 1.6% on seen ones. | `python -m training.audit` | **Mitigated in the training pipeline** (template-disjoint splits, decorrelated evidence); see the ML-nn findings. Headline synthetic metrics must not be read as accuracy. | `tests/test_training_pipeline.py` | K (data quality) |
| LEAD-09 | High | **The zero-shot local LLM is not usable:** it flags 19 of 19 legitimate rows, and 12–25% of its outputs are invalid JSON. | `docs/RISK_MODEL_PLAN.md`, "Zero-shot baseline" | **Open by design:** the LLM is opt-in and advisory; fine-tuning (Kaggle) is the planned fix. | — | P |
| LEAD-10 | High | **The prompt showed the agent's own budget claims as facts.** | `prompting.py` before `ba9f105` | **Fixed** (`ba9f105`): labelled `agent_claimed_*`, with the trusted operator budget given separately. | `test_prompt_never_presents_agent_budget_claims_as_trusted` passed | K, P |
| LEAD-11 | High | On the AI-written, differently-worded dev benchmark (bench v2, self-labelled), rules miss **40 of 137** risky units (0.292 [0.222, 0.373]); the hybrid misses 28 of 137 but flags 33 of 73 legitimate ones. | `benchmarks/agentpal-bench-v2/reports/dev_smoke_self_labelled.md` | **Open:** a detection weakness. It is the motivation for the fine-tuned model, and needs the independent benchmark to measure properly. | — | P |
| LEAD-12 | Medium | Bench v2 README missing: the first helper stopped (rate limit) before writing it. | `ls benchmarks/agentpal-bench-v2` | Assigned to Helper 2. | — | — |
| LEAD-13 | Info | The storefront UI's WhatsApp items are a plain `wa.me` contact link, unrelated to AgentPal. Neither the UI nor the AgentPal docs claim virtual cards, live PayPal or production readiness. | `grep -rniE "virtual.?card\|paypal" src agentpal/docs` | No action needed. | — | — |
| LEAD-14 | High | **Integration defect: the benchmark library API crashed outside pytest** once auth became required. A 401 on `/assess`, then the `then:` approval step, raised `ApprovalTermsMismatch`. | `env -u AGENTPAL_AUTH_MODE python -c "...evaluate_benchmark(...)"` | **Fixed** (`8ff8f91`): the in-process harness uses the explicit loopback dev auth mode, and an HTTP refusal is a failed step, not a crash. | `test_benchmark_harness_runs_outside_the_test_dev_auth_fixture`, `test_http_refusal_is_a_failed_step_not_a_crash` (pass) | K (baseline eval) |
| LEAD-15 | Medium | **One test depended on the git-ignored ML artifact**, so a fresh clone (CI, Kaggle) failed 1 test. | Fresh clone + `pytest` (1 failed, 796 passed, 33 skipped) | **Fixed** (`e68f5bb`): the test writes its own unpinned file. | `test_ml_artifact_is_verified_from_memory_without_staging` (pass in a fresh clone) | K, P (CI) |
| LEAD-16 | Info | API-level negative tests for the invariants, with auth required: no HTTP route can approve, execute or complete; smuggled status fields get 422 before the claim. | — | **Added** `tests/test_invariants_api.py` (23). | 23 passed | — |
| LEAD-17 | High | **The repository is private:** a Kaggle notebook cannot clone it without a credential, and none may go into the handoff. | `curl api.github.com/repos/...` → `private: true`; unauthenticated → 403 | **Fixed:** `training/kaggle_bundle.py` packs the tracked `agentpal/` files of one commit with per-file hashes; `verify` re-hashes them on Kaggle. | `test_training_kaggle_bundle.py` (3); fresh-bundle simulation (pass) | K |
| LEAD-18 | Medium | **`kaggle_bundle verify --root .` reported every file as changed** (`Path('.').parent` is `.`). The unit test used an absolute path and missed it. | Fresh-bundle Kaggle simulation | **Fixed** (`41bb80c`): the root is resolved first. | `test_verify_works_with_a_relative_root` (pass) | K |
| LEAD-19 | Medium | **The export step (llama.cpp clone + converter) could not be re-run by the lead:** the session's permission policy denied executing external code. | — | **Open:** marked [VERIFIED-SMOKE] by Helper 2 only in `TRAINING_HANDOFF.md` §8. | Helper 2's smoke run (`ML_AUDIT.md` §2.6) | K (first real export is unverified) |
| LEAD-20 | Info | Helper 2 reported 2,065 SFT training rows; the build actually produces **2,054**, byte-identical in the main checkout, Helper 2's worktree and a fresh bundle. | `python -m training.sft_data` in three trees | The handoff uses 2,054 and the SHA-256 prefixes. | Identical hashes (`c4cc4069…`, `d8ca8fd8…`) | — |
| LEAD-21 | Medium | **Two parsing tests required the optional `llama_cpp` package without `importorskip`.** GitHub CI installs only `requirements-dev.txt`, so it would have failed (2 failed). | Clean virtualenv with `requirements-dev.txt` only, run on a fresh clone | **Fixed:** `pytest.importorskip("llama_cpp")` in both tests. | Clean venv: 953 passed, 39 skipped, 0 failed | P (CI) |
| LEAD-22 | Info | **The first real fine-tuning run completed on Modal** (L40S, 258 steps, 986 s; `run_kind: training`). On bench v2 dev, rules + model miss 10/137 risky units vs 40/137 for rules alone (McNemar p < 0.001), with FP 22 vs 19 of 73 (not significant). Zero-shot Qwen3-1.7B is unusable: 61% invalid output, 90–95% of legitimate items flagged. | [`training_runs/2026-10-30-modal/`](training_runs/2026-10-30-modal/README.md) | **Done:** evidence committed; weights on the Modal Volume only. | `never_less_strict_than_rules = true` (dev and bench) | — |
| LEAD-23 | High | **The GGUF export was initially denied** by the session permission policy (it clones and runs llama.cpp). | — | **Resolved:** the owner allowed it (2026-10-30). Exported Q4_K_M, sha `92522e11…`; serving-path evaluation run locally; pinned as the opt-in llama.cpp model (default chain unchanged). | `test_repository_manifest_pins_the_trained_model_with_provenance`; end-to-end assess on `backend=llamacpp` | — |
| LEAD-24 | Medium | **The Modal export stage ran the GGUF evaluation on CPU while billing a GPU.** | 2026-10-30 run | **Fixed:** removed from the stage; the GGUF is evaluated on the serving host. | `tests/test_training_modal.py` | — |
| LEAD-25 | Medium | **llama.cpp generation used half the CPU cores** (the llama-cpp-python default), so the serving p95 was 19.1 s against the 30 s timeout. | Measured on the pinned GGUF: 10.3 s → 6.0–7.4 s per assessment | **Fixed** (`04499e4`): all cores for generation and prompt processing. | `test_llama_uses_all_cpu_cores_by_default` | D |
| LEAD-26 | Info | **Round-2 data and run 2** (aug-2, aimed at run 1's dev errors) replace run 1 as the pinned model. On identical units: aug-2 dev misses 14 vs 28/239, FP 56 vs 65/166; bench v2 misses 8 vs 13/137, FP 20 vs 20/73; 0 invalid; never less strict than rules. | [`training_runs/2026-10-30-modal-run2/`](training_runs/2026-10-30-modal-run2/README.md) | **Done:** pinned `23bd2fde…`; default chain unchanged. | Provenance test; end-to-end assess | — |
| LEAD-27 | Medium | **`modal_train` reused one output directory:** a second run would have auto-resumed run 1's checkpoints on different data. | Code review before run 2 | **Fixed** (`6f38dbc`): `--run NAME` gives each run its own directory. | `tests/test_training_modal.py` | K |

## Helper 1: security, payments and backend

The branch was merged in `17c0628` after lead review of `auth.py`, the `SecurityGate` ordering, fail-closed identity checks and the conftest dev-mode fixture. The full report is in [`SECURITY_AUDIT.md`](SECURITY_AUDIT.md). Helper 1 wrote 84 new tests (`test_sec_auth.py`, `test_sec_lifecycle.py`, `test_sec_migration.py`); each fix has a test that failed before it.

| ID | Sev | Finding | Status | Blocks |
|---|---|---|---|---|
| SEC-01 | High | No authentication; `GET /assessments/{id}` readable by anyone (BOLA) | **Fixed:** hashed per-agent and operator tokens, required by default; cross-agent reads get 404 | D, P |
| SEC-02 | High | An agent could submit another agent's request (spends its budget, burns its `request_id`) | **Fixed:** checked before the claim | D, P |
| SEC-03 | Medium | No rate limiting | **Fixed:** per token and per peer, in memory (single process) | P |
| SEC-04 | Medium | No production configuration guard | **Fixed:** `AGENTPAL_ENV=production` refuses the demo spend policy, disabled auth, integrity off and an in-memory DB | P |
| SEC-05 | High | Approval possible while the assessment is still running; a later `block` left the approval alive | **Fixed** | P (Phase 2) |
| SEC-06 | Medium | An approval could outlive the request's own expiry | **Fixed** | P (Phase 2) |
| SEC-07 | Medium | A non-UTC timestamp could revive an expired approval (ISO strings compared in SQLite) | **Fixed:** all times normalized to UTC | P (Phase 2) |
| SEC-08 | Medium | Duplicate, conflicting, out-of-order or unknown-request outcomes not handled; no amount/currency check | **Fixed:** the amount/currency/reference checks are optional keyword arguments | P (Phase 2) |
| SEC-09 | Medium | A crash between claim and completion left a hold blocking budget for up to 48 h | **Fixed:** recovered at service start-up | D, P |
| SEC-10 | Medium | DNS rebinding could make a web page a loopback peer | **Fixed:** Host allow-list | D, P |
| SEC-11 | Medium | An early v2 database was silently stamped v3, then every claim returned 500 | **Fixed:** columns verified on upgrade | D |
| SEC-12 | Low | Inconsistent error bodies; a 422 reflected attacker-chosen keys | **Fixed** | — |
| SEC-13 | Medium | `request_id` is globally unique: an authenticated agent can squat another agent's guessable ID (denial of service) | **Open:** needs a v4 schema with primary key `(agent_id, request_id)`; agents must use random IDs meanwhile | P |
| SEC-14 | Medium | `transition_ledger` walked reserved → approved → pending → completed without terms or verification | **Fixed by the lead** (`8ff8f91`): only release is allowed; failing-first tests in `test_invariants.py` | P (Phase 2) |
| SEC-15 | Medium | `decided_by` is free text, not bound to an authenticated owner | **Open:** needs the Phase 2 approval service | P |
| SEC-16 | Medium | Pending (attempted) holds never expire; no provider reconciliation | **Open:** Phase 2 | P |
| SEC-17 | Low | Rate limits are per process (N workers allow N times the limit) | **Open, documented** | P |
| SEC-18 | Medium | No TLS | **Open:** the API is loopback-only | P |
| SEC-19 | Low | Dependencies unpinned; the installed `certifi` has advisory PYSEC-2026-4320 | **Fixed by the lead** (2026-10-30): `requirements.lock` (18 packages, SHA-256 hashes). Verified: a clean venv installs it with `--require-hashes`, `pip check` is clean, and `pip-audit` finds no known vulnerabilities. CI installs and audits it. | P |
| SEC-20 | Low | "idempotency key = request_id" is imprecise | **Fixed by the lead:** README and ARCHITECTURE now say per (`request_id`, operation), per the official PayPal idempotency docs | Phase 2 design |
| SEC-21 | Low | A forbidden read is slightly slower than an unknown ID (it writes an audit row) | **Accepted:** status and body are identical | — |

**PayPal documentation verified by Helper 1** (official pages):
* [idempotency](https://developer.paypal.com/api/rest/reference/idempotency/);
* [Orders v2 schema](https://developer.paypal.com/api/orders/v2/schema.json);
* [webhooks](https://developer.paypal.com/api/rest/webhooks/rest/): self-verification or the postback API; postback does not work for simulator mock events; up to 25 retries over 3 days.

No PayPal client exists in AgentPal (Phase 1 is assessment only).

## Helper 2: ML, data, evaluation and training

The branch was merged in `e2ca28e` after lead review. Review covered:
* the strict output parser;
* `dataset.py` (formatting only; regenerated data is byte-identical);
* the training pipeline;
* the pinned libraries, which are present in the environment that ran the smoke test.

The full report is in [`ML_AUDIT.md`](ML_AUDIT.md). The full suite after the merge: **994 passed, 1 skipped** (the skip is the opt-in tiny-model smoke test).

| ID | Sev | Finding | Status | Blocks |
|---|---|---|---|---|
| ML-01 | High | Synthetic data is templated; validation largely memorisable (= LEAD-08) | **Mitigated for SFT** (template-family splits, augmentation); synthetic scores remain inflated | K (quality) |
| ML-02 | High | Evidence presence predicted the label (88% vs 58% risky); evidence type `note` appeared only in injection rows | **Fixed in SFT:** 0.553 vs 0.556 in train | K |
| ML-03 | High | **Stale pinned ML artifact.** Since F2 the `missing_evidence` feature is constant 1.0, which gave a +1.716 logit offset on every request. The hybrid's "extra recall" came entirely from it. | **Fixed by the lead** (`920df70`): retrained with the unchanged procedure and re-pinned (`lr-20261029-C1.0`, sha `fe47e2c0…`). Rules + ML now equals rules-only on validation (12/240, 25/130) and bench v2 (40/137; FP 20 vs 19 of 73). | P (decision F9) |
| ML-04 | Medium | `evaluate` CLI defaulted to the retired test split | **Fixed by the lead** (`920df70`) | K |
| ML-05 | High | 330 SFT rows were labelled from information absent from the prompt, which created a "familiar merchant ⇒ risky" shortcut | **Fixed:** dropped and counted per reason | K |
| ML-06 | Medium | Generator bug: `vary()` drops the injection text, but the row keeps label 1 (13/150 rows) | Excluded from SFT; generator unchanged (changing it would change the data hashes) | — |
| ML-07 | Medium | SFT targets were fixed per category | **Fixed:** 1,951 distinct targets that quote the request | K |
| ML-08 | High | The parser accepted prose, broken think blocks, coercions and duplicate keys (a trailing `"risk_score": 0` won) | **Fixed:** 39 new tests failed on the old parser | K, P |
| ML-09 | High | Qwen3 thinks by default; serving prompts included rule findings while training data has none | **Fixed:** no-think handler and analyzer switch; the lead added `AGENTPAL_LLAMACPP_INCLUDE_RULE_FINDINGS` | K, P |
| ML-10 | High | Bench v2 is AI-written and self-labelled; small per-type counts; bench v1 overlaps the generator | **Documented** (bench v2 README, annotator guide); needs external people | P |
| ML-11 | High | A locked scoring run accepted an unlocked labels file | **Fixed**, with a failing-first test | P |
| ML-12 | Medium | The scorer depended on the test-only dev-auth environment; an HTTP refusal crashed it | **Fixed** | K |
| ML-13 | Medium | `zip()` silently truncated a mis-shaped ML artifact | **Fixed** (13 failing-first tests) | P |
| ML-14 | Medium | The llama.cpp JSON grammar allows raw control characters and unbounded numbers | Parser accepts control characters inside strings; all schema bounds enforced | — |
| ML-15 | High | `train()` had never run: bf16 hard-coded (fails on T4/P100), `warmup_ratio` removed in transformers 5, no resume | **Fixed** and smoke-tested; the lead re-ran train → interrupt → refusal without `--resume` → resume → `final/` from a fresh bundle | K |
| ML-16 | Low | Dangling documentation references | **Fixed** | — |
| ML-17 | Medium | Every label is the generator author's; one fixed label per category | **Limitation** | P |

**Open (Helper 2):**
* the GPU run, fp16 stability on T4, and the memory and time estimates are unverified;
* the non-English augmentation text has not been reviewed by native speakers;
* no independent benchmark exists.


## Final verification (2026-10-30, branch `agentpal-risk-model`)

| Check | Command | Result |
|---|---|---|
| Full suite (development environment, ML artifact present) | `python -m pytest -o addopts="" -q -p no:cacheprovider` | **999 passed, 1 skipped**, run twice. The skip is the opt-in tiny-model smoke test, which **passed** separately (58 s) with `AGENTPAL_TINY_MODEL_DIR` set. |
| Fresh clone (no git-ignored data or artifacts) | same command | **966 passed, 34 skipped, 0 failed**; the skips name the missing ML artifact or the opt-in test |
| CI-equivalent clean venv (`requirements-dev.txt` only) | same command | **953 passed, 39 skipped, 0 failed** (after LEAD-21) |
| Timing and concurrency suites (replay, budget, approvals, lifecycle, timeouts, near-duplicates, invariants: 120 tests) | 10 consecutive runs | **0 failing runs** |
| Lint | `python -m ruff check agentpal training tests benchmarks` | clean |
| Types | `python -m mypy agentpal` | 38 errors, informational (LEAD-06) |
| Dependency vulnerabilities | `python -m pip_audit -r requirements.txt` | no known vulnerabilities |
| Static security scan | `python -m bandit -r agentpal training -ll` | 0 high. 3 medium, all reviewed as false positives: placeholder-only SQL string; revision-pinned or local `from_pretrained` calls. |
| Data audit and evaluation | `make data-audit`, `python -m agentpal evaluate`, `make bench-v2-smoke` | Ran. Validation: rules and hybrid both FN 12/240, FP 25/130. Bench v2 dev: FN 40/137 both, FP 19 vs 20 of 73. |
| Kaggle pipeline (fresh bundle, CPU) | `kaggle_bundle verify` → `sft_data` → `train_lora --dry-run` → `hwcheck` → tiny-model train, interrupt, refused restart, `--resume`, `final/` → `evaluate_model --adapter` | All passed. `hwcheck` refuses without a GPU (exit 1), as designed. **Export was not re-run by the lead** (LEAD-19). |


## Two-agent development session (2026-10-31)

| ID | Decision / finding | Status |
|---|---|---|
| LEAD-28 | Shared contracts (`agentpal/contracts.py`) were defined by the lead before delegation: provenance (observed/claimed/inferred/unavailable), `SourceEvidence`, `ToolResult`, `AgentContext`, and the `Tool`/`Extractor` protocols. | Done (`f0977b0`, `e073da9`) |
| LEAD-29 | Agent 2 raised that `AgentContext.capabilities` is forgeable. The field is documented as advisory; authorization reads only registry grants. | Done (`e073da9`) and tested (forged-context tests) |
| LEAD-30 | Agent 2 raised that `AgentContext` IDs did not match `schemas.ID_PATTERN`. They are now aligned. | Done (`4baf781`) |
| LEAD-31 | **Rejected relaxation.** In Agent 1's adapter, a consistent page observed by our own fetcher cleared MISSING_EVIDENCE/EVIDENCE_UNVERIFIED. This violates rule 8 and F2. The exception was removed and its test inverted. | Fixed (`3535bbb`) |
| LEAD-32 | Agent 1 merged (`47c019b`): 26 files, all in its owned paths, 153 tests. | Done |
| LEAD-33 | Agent 2 merged (`8a260e3`): 29 files, all in its owned paths, no existing file modified, 214 tests. | Done |
| LEAD-34 | The integration seam was only tested with fakes. The lead added `tests/test_integration_agents.py`, which runs the real extractor and assessor inside the real orchestrator and fetch tool (mocked transport), and registered the four new packages in `pyproject.toml` (the wheel was checked). | Done |
| SEC-22 | The wallet DB and the assessment DB are not updated atomically. | Open; fails closed (the wallet refuses to execute) |
| SEC-23 | `OwnerPrincipal` is an in-process object minted from an operator token. There is no per-owner auth or MFA, and no HTTP approval endpoint. | Open (DESIGN ONLY) |
| SEC-24 | Fetch residual risk: a compromised resolver or proxy can still serve false content. Mitigated by rule 8 (content is never trusted). | Accepted, documented |
| ML-18 | The `intel-sft-1` data is synthetic and labelled by its own generator (circular). Not trained, and not real-world evidence. | Documented |

SEC-13, SEC-15 and SEC-16 remain open. Today's work did not change them; the wallet works around SEC-15 by refusing to execute on approvals it did not record itself.

**Final verification (2026-10-31):**
- Full suite: **1379 passed, 1 skipped**. The 2026-10-30 baseline was 1003/1; Agent 1 added 153 tests, Agent 2 added 214, and the lead added 9.
- `ruff check agentpal training tests benchmarks`: clean.
- `mypy agentpal`: 88 errors, informational (LEAD-06). 56 of them are in the new packages: annotation gaps such as `Decimal | None` unions in `intelligence/compare.py` and `**dict` unpacking in `wallet/service.py`. The lead reviewed the security-relevant ones (`wallet/service.py:92`, `workforce/orchestrator.py:322`, `wallet/permissions.py:60`) and each is guarded at runtime: `require()` raises first; `None` prices are filtered out first; the registry clock is always set. Clean-up is a follow-up.
