# AgentPal-Bench v1: design and status

**Status (2026-10-28):** the harness, file format, lock mechanism and API episode replay are implemented and tested. **Only a seed `dev` split exists.** It was written by the same person who wrote the rules (`"independent": false`), so it is a harness and regression check, **not** an independent benchmark. The independent `locked` split has **not been created**; see "Next steps".

## Goals

* Measure generalization rather than agreement with the synthetic generator.
* Cover **stateful** attacks: replay, structuring, time manipulation, trust poisoning, baseline inflation.
* Score every approach on **identical** items.

## Files

| File | Contents |
|---|---|
| `cases.jsonl` | Single requests: `id`, `group_id`, `split`, `label` (1 = needs review/block), `rationale`, `author`, `independent`, optional `operator` budget, `request` |
| `episodes.jsonl` | Multi-request sequences: `steps[]`, each with a `request`, `advance_seconds` (server clock), `expect` (`action`, `action_not`, `codes`, `not_codes`), and optional `then` operations. `then` simulates Phase-2 verified events: `owner_decision`, `payment_attempt`, `payment_outcome`. An optional per-episode `spend_policy` can be supplied. |
| `LOCK.json` | Written by `python -m agentpal bench-lock`: SHA-256 of each file plus a timestamp |
| `access_log.jsonl` | Appended on every `locked` evaluation |

## Rules

1. **Scenario groups never cross splits.** All variations of one scenario share a `group_id`, and the loader rejects a group that appears in more than one split.
2. **Lock before selection.** Lock the `locked` split *before* any model, feature or threshold selection that will be judged on it. Evaluation refuses to run if a file no longer matches `LOCK.json`, and every locked run is logged. Use `dev` for development.
3. **Identical examples.** Single cases are scored by rules-only, classifier-only and hybrid on the same items. Episodes are replayed **through the HTTP API** (`POST /assess`, with a fresh database and a controlled server clock) for rules-only and hybrid. A classifier alone has no stateful policy, so it is not run on episodes.
4. **Honest uncertainty.** Every rate is reported with a 95% Wilson interval. Items with `independent: false` are counted and flagged in the report.
5. **No special cases.** Rules must not be written to pass specific benchmark items. Known gaps stay marked `known_gap` and are expected to fail until fixed.

## Seed contents and owner-decision changes (2026-10-29)

* The seed has 16 single cases and 13 episodes (33 steps).
* Four episodes were added for owner decisions 4 and 6: `ep-approval-hold-expiry`, `ep-unapproved-reservation-ttl`, `ep-attempted-payment-keeps-hold` and `ep-unverified-outcome-not-completed`.
* `ep-near-duplicate` was a `known_gap`. It now expects `NEAR_DUPLICATE_PAYMENT_INTENT`, because the gap was fixed in code. The expectation was not edited to make a failing item pass.
* Every seed episode is replayed rules-only in `tests/test_od_multi_request_episodes.py`.

## Next steps (required before using this benchmark to select or promote models)

The full protocol is in [`docs/BENCHMARK_ANNOTATION_PROTOCOL.md`](../../docs/BENCHMARK_ANNOTATION_PROTOCOL.md) (v2: custodian, ≥ 3 independent authors and annotators, κ gates, a locked split held outside the engineering repository). The summary below is kept for reference.

* Recruit at least two annotators who did **not** write the rules or the synthetic generator.
* Write a label guide that defines `allow_for_review`, `require_review` and `block`.
* Author ≥300 single cases and ≥100 episodes. Report Cohen's κ and adjudicate disagreements.
* Mark these items `"split": "locked"` and `"independent": true`, run `bench-lock`, and commit `LOCK.json` before any selection.
* Include paraphrases, unseen merchants and categories, multilingual text, Unicode attacks, and realistic legitimate traffic.
