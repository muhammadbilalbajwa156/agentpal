# P0 security and correctness remediation report

| | |
|---|---|
| Branch | `agentpal-p0-remediation`, created from `agentpal-phase1` @ `254198f` (preserved, unchanged) |
| Scope | The eight P0 areas of [`PHASE1_REVIEW_AND_TEST_PLAN.md`](PHASE1_REVIEW_AND_TEST_PLAN.md) |
| Not in scope (not done) | Phase 2, WhatsApp, PayPal, new model training, a PR |
| Test suite | **239 passed**: the original 108 plus 131 new P0 regression tests (2026-10-29) |
| Payments / messages | None executed or sent; no such code paths exist |

Method: for every fix, a regression test was written first and run against the vulnerable code, then the fix was implemented and the full suite re-run. Several "pre-fix" failures are API errors (the new interface did not exist yet). For those, the vulnerable *behaviour* was established by the review's executed probes (`docs/review/probe_results.json`) and, where noted, by direct measurement.

## Summary by issue

| P0 | Issue (review probes) | Fix | Regression tests | Pre-fix → post-fix |
|---|---|---|---|---|
| P0.1 | Concurrent replays all assessed (P09) | `requests.request_id` PRIMARY KEY, claimed in `BEGIN IMMEDIATE` before any work. Losers get a deterministic `block` (`DUPLICATE_REQUEST_ID`), with no model call and no new history. Failed assessments keep the id claimed. Pre-P0 dev databases are refused. | `test_p0_1_replay.py` (5): 8 threads, restart, 8 spawn processes, 2 real uvicorn workers, legacy schema | 2 of 3 original-API tests **failed** (threads; 2 of 8 processes claimed the id) → 5/5, stable ×3 |
| P0.2 | Agent controls budget; no cumulative limits (P01, P02) | Operator `config/spend_policy.json`; ledger with explicit states; claim + snapshot + reservation in **one** transaction; split and rolling-window rules; `NO_SPENDING_POLICY` fail-closed | `test_p0_2_spending.py` (18) | 17/17 failed (API absent; behaviour shown by P01/P02) → 18/18 |
| P0.3 | Client timestamps drive windows; no TTL (P03, P06) | Injectable trusted clock; all windows use server receipt time; `time_policy` (TTL 60 min, max lifetime 48 h); timezone required | `test_p0_3_time.py` (14) | 12/14 failed → 14/14 |
| P0.4 | Own recommendation = trust; global trust; flagged baselines (P07, P08) | Append-only owner decisions, attempts and outcomes; trust only from **verified completed** payments of the **same agent and owner**; per-currency spend baseline | `test_p0_4_trust.py` (9) | 8/9 failed → 9/9 |
| P0.5 | One numeric limit for every currency (P05) | Per-currency limits in operator config, exact decimals, ISO minor units; no FX; unconfigured currency → block | `test_p0_5_currency.py` (12) | 8/12 failed → 12/12 |
| P0.6 | Unicode and domain evasion; subdomain approval (P10, P11, P12) | NFKC + Default_Ignorable stripping + confusable folding for detectors; UTS #39 skeleton for names; Public Suffix List; exact-host approval; delimited-token brand matching; normalized merchant identity; raw domain input kept (redacted) | `test_p0_6_normalization.py` (34) | 9/33 failed, incl. a naive-substring **false positive** → 34/34 |
| P0.7a | llama.cpp had no timeout | Bounded worker pool for every backend; llama.cpp streaming with a deadline check and generator close; `review_on_model_failure` flag | `test_p0_7a_timeout.py` (6) + real-model check | Measured: a 3 s backend blocked for 3.0 s → bounded at the timeout |
| P0.7b | Body limit buffered chunked bodies | Pure-ASGI streaming limit; 400 on bad `Content-Length` | `test_p0_7b_body_limit.py` (9), incl. raw chunked request to real uvicorn | Endless-stream test never terminated → 9/9 |
| P0.7c | 422/413 not audited | Audit rows + security log with error codes only; audit failure cannot change the response | `test_p0_7c_rejection_audit.py` (6) | 5/6 failed, **plus a reflected-input leak found and fixed** → 6/6 |
| P0.7d | Artifacts loaded unverified | Trusted `config/model_manifest.json`; verified-bytes parsing (ML); stream hash before load (GGUF); Ollama digest comparison; `pin-model` CLI; fail closed per backend | `test_p0_7d_integrity.py` (11) | Collection error (no mechanism) → 11/11 |
| P0.8 | Over-claiming evaluation; README error | Wilson CIs; retired test split; benchmark harness with lock, episodes and identical-example scoring; README corrected | `test_p0_8_evaluation.py` (7) | – → 7/7 |

## Notable findings made during remediation

1. **A mocked test hid a real failure (P0.7a).**
   * The first timeout implementation passed `stopping_criteria=` to `create_chat_completion`. The fake accepted it, so the test passed.
   * Against the real `llama-cpp-python` 0.3.36 it raised `TypeError`, and only generic error handling made it fall back.
   * Now the fake mirrors the real signature (no `**kwargs`), and the fix uses streaming.
   * Verified with the real Qwen2.5-1.5B model:
     * 1 s timeout: returned at 1.00 s; generation stopped 0.06 s later.
     * 3 s timeout: returned at 3.00 s; generation stopped 0.10 s later.
     * 120 s budget: full inference completed in 22.4 s.
2. **Reflected input in 422 responses (P0.7c).** The currency validator embedded the submitted value, upper-cased, in its error message. A case-insensitive secret check exposed it, and the message is now generic.
3. **Naive substring lookalike check (P0.6).** `exampleplumbing.org` was flagged as imitating `example.com`. It's replaced by delimited-token matching. `example-billing.com` and `secure-example.com.zip` are still flagged.
4. **Subdomain identity (P0.6).** Merchants under one registrable domain are now treated as one merchant for duplicates and splits. PSL private-section suffixes (e.g. `github.io` tenants) stay distinct.

## Documented changes to pre-existing tests

The original 108 tests all still pass. These justified updates were made; no assertion was weakened or deleted:
* `test_schema.py`: naive timestamps must now be **rejected**. Previously they were assumed to be UTC.
* `conftest.ctx` and `test_rules.codes`: pure rule tests pass an explicit `SpendSnapshot.from_synthetic_fixture` built from the scenario's budget fields, because a context with no trusted spend policy now (correctly) blocks.
* `test_rules.py::test_spike_vs_history`: the baseline history is supplied as verified payments. The test additionally asserts that observed-only history creates **no** baseline.
* `test_orchestrator.py`: the mocked Ollama backend opts out of integrity explicitly (integrity is covered in `test_p0_7d`).

The P0 test files also received fixture corrections between steps (P0.2 helper gained the mandatory per-currency limits; P0.2 concurrency test now uses distinct registrable domains; P0.7a llama fake mirrors the real API).

## Effect on the synthetic evaluation

The validation-split metrics were re-run after every step and stayed **identical** to Phase 1:
* Rules: P 0.958 / R 0.946 / FPR 0.077.
* ML: P 0.975 / R 0.971 / FPR 0.046.
* Hybrid: P 0.929 / R 0.979 / FPR 0.139.

These were regression checks only; nothing was tuned. The old test split was not re-run, and it is marked retired for selection. The out-of-distribution seed cases are unchanged at 5/10 recall: P0 fixed controls, not semantic detection.

## Remaining risks and incomplete items

| Item | Status |
|---|---|
| P04 near-duplicate amounts (+0.01) | **Open** (P1); benchmark episode `ep-near-duplicate` is a `known_gap` |
| P13 fabricated evidence lowers the score; P15 spurious `recurring` and evidence weights in the ML | **Open** (P1); no retraining done in P0 |
| P17 model `require_review` escalates regardless of score | **Open** (P1 policy change) |
| Paraphrased / non-English injection and purpose mismatch | **Open** (P1: semantic detectors per the review) |
| Independent, hand-labelled locked benchmark | **Not created.** Needs annotators independent of the rule author. Only a seed dev split exists. |
| Paired statistical tests in the benchmark | Not implemented (Wilson CIs only) |
| Multi-host deployment | SQLite file locking covers multiple workers on **one host** only; production needs Postgres with the same constraints |
| GGUF hash-then-load window (TOCTOU) | Mitigated by read-only artifact storage (documented) |
| Python threads cannot be killed | A non-cooperative backend thread may keep running after the timeout. Latency is still bounded, and the pool size limits leaked threads. |
| Host clock trust | The service trusts the host UTC clock (NTP required) |
| `approved` ledger holds have no expiry | Phase 2 must define the approval validity |
| No authentication on the API | Must not be exposed beyond localhost (unchanged; Phase 2 prerequisite) |
| Script detection | Approximated from Unicode character names |
| Historical review scripts (`docs/review/*.py`) | Written against the pre-P0 API. Their results are preserved as evidence but they are not maintained; the P0 regression tests supersede them. |

## Decisions that need the product owner

1. **Review budget and costs** (review Q1/Q2), which determine any threshold change.
2. **Soft-indicator blocks** (Q3, P14): may soft indicators summing to ≥ 75 `block`, or only `require_review`?
3. **Time policy:** default TTL of 60 min, maximum lifetime of 48 h, skew of 10 min, staleness of 24 h.
4. **Spend policy values:** demo and default-agent budgets, per-currency hard/review limits (USD 5000/1000, GBP 4000/800, JPY 750000/150000), a 24 h rolling window, and whether to keep a `default_agent` at all.
5. **`review_on_model_failure`:** currently `false` (fall back to the rules without escalating).
6. **Approval validity window** for `approved` holds (Phase 2).
7. **Model integrity default:** required, which means unpinned models are disabled.
8. Whether **agent-level** trust isolation is right, or whether same-owner agents may share verified merchant history.

## Reproduce

```bash
git checkout agentpal-p0-remediation
cd agentpal
python3 -m pip install -r requirements-dev.txt
python3 -m pytest                                   # 239 passed
python3 -m pytest tests/test_p0_*.py                # the 131 P0 regression tests
python3 -m agentpal evaluate --split validation     # regression check (metrics unchanged)
python3 -m agentpal bench                           # seed dev benchmark (NOT independent)
python3 -m agentpal assess examples/requests/*.json
# Pre-fix demonstration of any P0.x test: check out the parent commit of that fix with
# the new test file copied in, then run it (see commit history 0ab29df..12ea185).
```
