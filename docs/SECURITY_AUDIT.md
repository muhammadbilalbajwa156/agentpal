# Security audit: API, payment lifecycle, store (helper 1)

* **Audited:** branch `agentpal-risk-model` at `685d1bb`.
* **Scope:** `agentpal/agentpal/{api,service,store,spending,schemas,config,__main__}.py`, configuration, and the PayPal claims in the docs.
* **Historical phase:** the original audit covered Phase 1 assessment only. WS1 now adds authenticated approvals, a sandbox provider lifecycle and fail-closed recovery; see the WS1 evidence below.
* **Line references** in *Evidence* point at `685d1bb`.

**This is not a production-readiness claim.** The API stays loopback-only (owner decision 10). There is still no TLS; agent/owner isolation is local to one host and SQLite. Live sandbox verification and production deployment remain outside this result.

## Findings

Severity is for the eventual Phase-2 deployment. Every fixed finding has a regression test that failed before its fix; the counts are under "Test evidence".

| ID | Sev. | Finding | Evidence (`685d1bb`) | Status | Tests | Blocks |
|---|---|---|---|---|---|---|
| SEC-01 | High | **No authentication. BOLA:** any caller can read any assessment by id, and can call `/model-info` and `/assess` as anyone. | `api.py:277-288` | Fixed (`5ef4f61`) | `test_sec_auth.py::test_missing_or_invalid_token_is_refused`, `::test_assessment_is_readable_only_by_its_agent_or_an_operator` | demo, production |
| SEC-02 | High | **`agent_id` is not bound to the caller.** Anyone can submit as any agent, and spend that agent's budget and pollute its history. A forged body also burns the victim's `request_id`. | `api.py:277-279`, `schemas.py:132` | Fixed (`5ef4f61`). The check runs before the claim. | `::test_agent_token_assesses_only_its_own_agent`, `::test_operator_token_cannot_submit_assessments` | demo, production |
| SEC-03 | Medium | **No rate limiting.** Assessments can be flooded, along with the audit-log writes behind them. | `api.py` (none) | Fixed (`5ef4f61`). Per-process only. | `::test_per_token_rate_limit_returns_429_and_is_audited_once`, `::test_per_peer_rate_limit_*`, `::test_sliding_window_*`, `::test_limiter_memory_is_bounded` | production |
| SEC-04 | Medium | **No configuration safety check.** Nothing stops a demo spend policy, integrity switched off or an in-memory DB from being used in "production". | `config.py:165-218`, `__main__.py:337-348` | Fixed (`5ef4f61`). `AGENTPAL_ENV`, `Settings.check_runtime`. | `::test_production_refuses_*`, `::test_disabled_auth_is_refused_for_a_non_loopback_bind`, `::test_serve_refuses_production_with_auth_disabled` | production |
| SEC-05 | High | **An approval is possible before the rules have decided.** The reservation is created in the claim transaction, and `record_owner_decision` did not check the assessment status. An approval during assessment survived a later `block`, because `complete_request` releases only `reserved` entries. This breaks invariants 2 and 3. | `store.py:154-159`, `313-334`, `434-436` | Fixed (`77e08b2`) | `test_sec_lifecycle.py::test_approval_refused_while_assessment_is_still_running` | production (Phase 2) |
| SEC-06 | Medium | **An approval outlives the request's expiry.** The hold was reset to approval time + 15 min, even when the request's `effective_expires_at` came earlier. This breaks invariant 4. | `store.py:330-332` | Fixed (`77e08b2`) | `::test_approval_never_outlives_the_request_expiry` | production (Phase 2) |
| SEC-07 | Medium | **Timezone-dependent expiry.** Ledger times are compared as ISO strings in SQLite. A caller-supplied `now` with a negative UTC offset sorted before the stored UTC expiry and **revived an expired approval**. Naive datetimes were accepted, and the service accepted a non-UTC clock. | `store.py:194-196`, `341`, `service.py:345-347` | Fixed (`77e08b2`) | `::test_non_utc_time_cannot_extend_an_expired_approval`, `::test_naive_timestamps_are_refused_by_the_ledger`, `::test_service_normalizes_a_non_utc_clock` | production (Phase 2) |
| SEC-08 | Medium | **Outcome handling.** (a) A redelivered verified webhook raised (not idempotent). (b) A conflicting outcome after settlement was not audited. (c) An *unverified* outcome for an unknown `request_id` was stored. (d) There was no way to check the amount, currency or attempt reference. (e) Out-of-order outcomes were refused but not audited. | `store.py:350-365` | Fixed (`77e08b2`). The (d) checks are **optional** keyword arguments; see SEC-14 / proposals. | `::test_duplicate_verified_outcome_is_idempotent_and_audited`, `::test_conflicting_outcome_*`, `::test_outcome_for_unknown_request_is_refused`, `::test_out_of_order_outcome_*`, `::test_outcome_amount_or_currency_mismatch_is_refused`, `::test_outcome_for_an_unknown_attempt_reference_is_refused`, `::test_concurrent_conflicting_outcomes_settle_exactly_once` | production (Phase 2) |
| SEC-09 | Medium | **No crash recovery.** A crash between claim and `complete_request` left the request `claimed`, with its reservation holding budget until the request expired (up to 48 h). | `store.py:141-162`, `444-453` | Fixed (`77e08b2`). Recovered at service start-up. | `::test_restart_recovers_a_stale_claim_and_releases_its_hold`, `::test_restart_leaves_a_fresh_in_flight_claim_alone`, `::test_restart_preserves_approval_expiry` | demo, production |
| SEC-10 | Medium | **DNS rebinding.** The loopback check trusts the peer address only. A web page on an attacker's domain that rebinds to 127.0.0.1 is a loopback peer and could read assessments (with no auth, see SEC-01). | `api.py:129-154` | Fixed (`5ef4f61`). Host allow-list. | `test_sec_auth.py::test_host_header_allow_list` | demo, production |
| SEC-11 | Medium | **Silent schema mis-upgrade.** Columns were added to `requests` after `user_version` 2 (`0ab29df`) without a version bump, and `CREATE TABLE IF NOT EXISTS` never adds columns. An early-v2 file was stamped v3, then every claim failed with a 500. | `store.py:111-125` | Fixed (`19b9336`) | `test_sec_migration.py` (4) | demo |
| SEC-12 | Low | **Inconsistent errors and input reflection.** The 404/405 bodies used `{"detail": ...}`. A 422 `loc` reflected attacker-chosen JSON keys (for example `<script>`) and was not capped. | `api.py:235-244`, `287` | Fixed (`5ef4f61`) | `::test_errors_are_consistent_json_and_do_not_reflect_input` | — |
| SEC-13 | Medium | **`request_id` is one global namespace.** An *authenticated* agent that can guess another agent's future id can claim it first. The victim then gets `DUPLICATE_REQUEST_ID` (block), a denial of service. No data leaks: a replay response is built from the victim's own request. | `store.py:553` (PK) | IMPLEMENTED — TESTED. Schema v4 uses `(agent_id, request_id)`; replay is scoped to the same agent. v3 migration preserves existing rows and refuses orphan data. | `test_sec_v2_isolation.py`, `test_sec_v2_migration.py` | production |
| SEC-14 | Medium | **`store.transition_ledger` bypasses the gates.** This public method moves `reserved → approved → pending → completed` with no terms, no assessment check and no verified outcome. Not reachable over HTTP. | `store.py:267-274` | IMPLEMENTED — TESTED. Fenced at `8ff8f91`: `transition_ledger` permits only release; approval, attempt and verified-outcome gates cannot be bypassed. | `test_invariants.py` (SEC-14 cases) | production (Phase 2) |
| SEC-15 | Medium | **Owner identity is not authenticated or bound.** `decided_by` is free text and is not checked against the ledger's `owner_id`. | `store.py:313-334` | IMPLEMENTED — TESTED. Durable owner sessions are revalidated at decision time. Structured ledger approvals bind owner/session/org and exact terms, expire, and are consumed once. Legacy adapters remain opt-in; strict flags default False for compatibility. | `test_identity_owner_auth.py`, `test_sec_v2_approvals.py`, `test_lifecycle_execution.py` | production (Phase 2) |
| SEC-16 | Medium | **`attempted` (pending) holds never expire, and there is no provider reconciliation.** | `spending.py:59-67` | IMPLEMENTED — TESTED. Expired/ambiguous attempts retain exposure in reconciliation_required; create/capture claims serialize before provider calls. Verified matching events reconcile, deduplicate, detect conflicts and reverse refunds. | `test_lifecycle_execution.py`, `test_lifecycle_recovery.py` | production (Phase 2) |
| SEC-17 | Low | **Rate limits and audit throttling are in memory, per process.** With N uvicorn workers the limit is up to N times higher, and counters reset on restart. The per-peer key on a loopback service is effectively global. | `ratelimit.py` | IMPLEMENTED — TESTED OFFLINE (Mission 3). SQLite shared admission/audit throttling, fail-closed 503 and spawned-process aggregate tests; single-host/local-storage limits documented in DEPLOYMENT.md. | `test_sec_v3_ratelimit.py` | production |
| SEC-18 | Medium | **No TLS.** | deployment | IMPLEMENTED — TESTED OFFLINE (Mission 3). Direct TLS CLI/env options and non-TLS external-bind refusal. Only the explicitly enabled signed webhook POST can cross the remote gate; other API routes stay loopback-only. | `test_sec_v3_tls.py`, `test_sec_v3_webhook_exposure.py` | production |
| SEC-19 | Low | **Dependencies are unpinned** (`>=`) with no lock file or hashes. The installed environment has `certifi 2026.10.18` (PYSEC-2026-4320, fixed in 2026.10.19), pulled in by httpx. | `requirements*.txt` | **Open.** `pip-audit` details below. | — | production |
| SEC-20 | Low | **Inaccurate PayPal claim.** "`idempotency key = request_id`" / "reuse `request_id` as `PayPal-Request-Id`". The header is per API call, and retention is undocumented for Orders. | `docs/ARCHITECTURE.md:59,67-68`, `README.md:250` | **Open.** Not this helper's files; corrected wording is proposed below. `TRUST_AND_LEDGER.md` is corrected. | — | Phase 2 design |
| SEC-21 | Low | **Small existence oracle.** A cross-agent read of an existing id writes an audit row, so it is marginally slower than an unknown id. Status code and body are identical. | `api.py` (new) | **Open.** Accepted: the audit trail is worth more. | — | — |

| SEC-22 | Medium | **Cross-database crash consistency and startup recovery.** Wallet copies and ledger authority can diverge between commits. | `lifecycle/recovery.py`, `service.py`, `wallet/service.py` | IMPLEMENTED — TESTED. Startup quarantines pending attempts without retries; internal/cross-DB drift and unusable strict approvals raise ConsistencyError; injected failures and process death preserve held exposure. | `test_sec_v3_recovery.py`, `test_sec_v3_approval_concurrency.py`, `test_lifecycle_recovery.py`, `test_sec_v2_approvals.py::test_wallet_constructor_refuses_cross_database_digest_corruption` | lead application wiring |
| SEC-23 | Medium | **Forged owner objects, self-approval and cross-owner/org decisions.** | `identity/service.py`, `wallet/service.py`, `store.py` | IMPLEMENTED — TESTED. Revalidate durable credentials and owner/org membership; refuse agents, mismatched terms, replay and expired/revoked sessions. No independent request org namespace exists: owner IDs are globally unique. | `test_identity_owner_auth.py`, `test_sec_v2_approvals.py` | lead strict-mode wiring |

**Checked, no finding:**
* **SQL injection.** Every query is parameterised. The f-strings build only placeholder lists, constant `PRAGMA`s, or `PRAGMA table_info` over names from the code's own schema.
* **Logging.** It contains no request text, tokens or secrets. The audit table stores error locations and types only.
* **CORS.** No CORS middleware, so browsers cannot read responses cross-origin.
* **Proxy trust.** `proxy_headers=False`. `X-Forwarded-For` is ignored (test_od_decision_10).
* **Terms binding.** It covers request, agent, amount, currency, merchant name and domain, and purpose. Tampering with any of them is refused (`test_sec_lifecycle.py::test_tampered_terms_after_approval_are_refused`).
* **Replay.** An attempt replay is refused (`::test_attempt_replay_is_refused`). Concurrent attempts execute once across connections (`::test_concurrent_attempts_execute_once`).
* **Approve/decline races** serialise. A decline after an approval is a revocation (`::test_concurrent_approve_and_decline_settle_once`).
* **Budget.** `failed`, `cancelled` and `released` free the hold. A retry needs a new `request_id` and a new reservation.

## Authentication and authorization (what was built)

* **Tokens.**
  * Created with `python -m agentpal new-token (--agent ID | --operator) --id LABEL [--append FILE]`.
  * A token is `apt_` plus 256 random bits. It is printed **once**, on stderr.
  * Only `sha256(token)` is stored, in the file named by `AGENTPAL_API_TOKENS_FILE` (format in `agentpal/auth.py`).
  * Lookup hashes the presented token and compares it with **every** entry using `hmac.compare_digest`, with no early exit.
* **Roles.**
  * An `agent` token authenticates exactly one `agent_id`. `/assess` refuses a body with another `agent_id` (403 `agent_mismatch`) **before** the `request_id` is claimed.
  * An `operator` token may read any assessment, but cannot submit one (403 `agent_token_required`).
  * `GET /assessments/{id}` is visible to the owning agent and to operators. Everyone else gets the same 404 `{"error":"not_found"}` as for an unknown id, and the attempt is audited (`rejected:cross_agent_read`).
* **Paths.**
  * `/health` is open, for liveness.
  * In `development`, the OpenAPI docs paths are open too. In `production` they are removed.
  * Everything else requires `Authorization: Bearer <token>`. Missing or malformed → 401 `authentication_required`; unknown → 401 `invalid_token`; either way with `WWW-Authenticate: Bearer`. Two `Authorization` headers → 401.
* **Default: `AGENTPAL_AUTH_MODE=required`.** `disabled_insecure_dev` turns authentication and authorization off. It is:
  * logged as a WARNING at start-up;
  * marked on every response (`X-AgentPal-Auth: disabled-insecure-dev`) and in `/health`;
  * refused in production and for a non-loopback bind.
* **Order of checks.** `LoopbackOnly` (unchanged, outermost) → `BodySizeLimit` → `SecurityGate`:
  * Host allow-list;
  * per-peer limit;
  * authentication;
  * per-token limit.
  The routes then perform the authorization checks.
* **Rate limits.**
  * Sliding windows of 60 s: `AGENTPAL_RATE_LIMIT_PER_TOKEN` (60) and `AGENTPAL_RATE_LIMIT_PER_PEER` (300).
  * Exceeding a limit answers 429 `{"error":"rate_limited"}` with `Retry-After`.
  * A rejection is audited at most once per key and window, without the token.
* **Production (`AGENTPAL_ENV=production`) refuses:**
  * disabled authentication;
  * no token file;
  * a spend policy labelled `demo` (the shipped `config/spend_policy.json`);
  * model integrity or read-only model storage switched off;
  * a `:memory:` database;
  * non-loopback Host names.
  `serve` checks this before binding, and `create_app` checks it too.
* **Settings and tests.** The security settings default to the process environment even when `Settings(...)` is built in code. Explicit arguments still win. The pre-existing tests, the episode replays and the benchmark harnesses all post as many agents without tokens. `tests/conftest.py::_phase1_dev_auth` (session, autouse) runs them in `disabled_insecure_dev` and allows the TestClient host `testserver`. `python -m agentpal bench` passes the dev mode explicitly, because its replay is in-process (TestClient, no socket).

## Test evidence

* **Before:** 658 passed, 1 failed, 33 skipped (692 collected).
  * The failure is `test_od_decision_7.py::test_ml_artifact_is_verified_from_memory_without_staging`. It needs the git-ignored `artifacts/baseline_classifier.json`, which is absent in this checkout, so it is environmental and pre-existing.
  * The 33 skips are the artifact-dependent advisory tests.
* **After:** 742 passed, the same 1 environmental failure, 33 skipped (776 collected).
* **New tests:** 84 in total.
  * `test_sec_lifecycle.py`: 33. 20 failed on `685d1bb`.
  * `test_sec_auth.py`: 47. 34 failed on `77e08b2`; the 13 that passed are unit tests of the new modules plus the positive Host and loopback cases.
  * `test_sec_migration.py`: 4. 2 failed before `19b9336`.

## PayPal claims (checked 2026-10-29 against developer.paypal.com)

| Claim in the docs | Official documentation | Verdict |
|---|---|---|
| `PayPal-Request-Id` returns the latest status of the original request on retry; not all APIs support it; a simultaneous duplicate may fail; without it a repeat is processed as new (`PHASE1_REVIEW_AND_TEST_PLAN.md:406`) | The idempotency page says a reused header value returns "the latest status of the previous request that used that same header". PayPal "processes the first request and might fail the second request". "Not all APIs support this header". Without it, "PayPal duplicates the request". | **Accurate** |
| "idempotency key = `request_id`" / "reuse it as the PayPal `PayPal-Request-Id` idempotency key" (`ARCHITECTURE.md:59,67-68`; `README.md:250`) | In the Orders v2 OpenAPI schema, `PayPal-Request-Id` is an optional header, "A unique ID identifying the request header for idempotency purposes". Separate calls accept it: `POST /v2/checkout/orders`, `/orders/{id}/authorize` and `/orders/{id}/capture`. The idempotency page says IDs are kept for a limited time and points to each API's reference for the period. The Orders schema gives no period. | **Imprecise.** One key per (request, API operation), persisted before the call. Never the bare `request_id` for both create and capture. No reliance on a retention window. |
| Use a server-generated UUID per (request, API-call type), persisted before calling (`PHASE1_REVIEW_AND_TEST_PLAN.md:408`) | Consistent with the above | **Accurate** |
| Webhooks: self-verification (CRC32 of the raw body plus the certificate signature) or the `verify-webhook-signature` postback; postback not supported for simulator mock events; up to 25 retries over 3 days → deduplicate (`PHASE1_REVIEW_AND_TEST_PLAN.md:414`) | Self-verification signs `transmissionId\|timeStamp\|webhookId\|crc32`. CRC32 is decimal, over the raw body, verified with the key from `paypal-cert-url`. Postback must send the event exactly as received. "Postback is not supported for mock events from the simulator" (their webhook id is `WEBHOOK_ID`). A non-2xx answer is retried "up to 25 times over 3 days". | **Accurate.** Deduplication by event id is our design inference, not PayPal text. Store-side, `record_payment_outcome` is now idempotent for a redelivered outcome. |
| "Webhooks must be verified before `record_payment_outcome(verified=True)` is called" (`README.md:251`) | Consistent | **Accurate**, and still not implemented (Phase 2) |

**Sources:**
* https://developer.paypal.com/api/rest/reference/idempotency/
* https://developer.paypal.com/api/rest/webhooks/rest/
* https://developer.paypal.com/docs/api/orders/v2/
* https://developer.paypal.com/api/orders/v2/schema.json (OpenAPI; parameter `paypal_request_id`)

## Dependencies (`pip-audit`, installed with pip)

* **`pip-audit -r requirements.txt -r requirements-dev.txt`:** "No known vulnerabilities found". It resolves the newest versions, because the requirements are unpinned.
* **`pip-audit` on the installed environment:**
  * AgentPal's runtime packages (fastapi, starlette, uvicorn, pydantic, httpx, publicsuffixlist) have no advisories.
  * `certifi 2026.10.18` has PYSEC-2026-4320 (fixed in 2026.10.19); it is a transitive dependency of httpx.
  * Other advisories (pypdf, setuptools, wheel, …) belong to unrelated packages in the container image.
  * `torch` was not auditable (local build).
* **Recommendation:** pin with hashes (`pip-compile --generate-hashes`) and run `pip-audit` in CI.

## WS1 continuation evidence (2026-10-31)

The inherited WIP baseline had 1,567 passed and 37 skipped; full ruff passed.
New regression files cover approval/session boundaries, provider operation claims,
crash rollback/restart at execution steps, concurrent duplicate events and cross-DB drift.
Fail-first runs against the WIP exposed 11 execution failures (16 passed), then 17
additional event/approval-integrity failures (27 passed), two event-ID conflict failures
(55 passed), and one refund-after-conflict failure (63 passed). Approval tests first
exposed two failures (14 passed); recovery runs exposed 7, then 2, then 9 failures as
checks were added. See CONSISTENCY.md for crash ordering and recovery details.

Only verified events with the exact amount and currency can finalize any outcome.
Unverified deliveries cannot poison a later verified event ID. Verified unknown states
and conflicting capture references require reconciliation. Strict stores also refuse
the legacy verified-boolean outcome path. Existing v4 approval triggers are replaced
atomically so all identity/audit fields stay immutable. Additive v4 operation and
capture-evidence tables preserve invocation and event identity without discarding rows.

Sandbox-only tests use a fake provider; no credentials or live provider calls were used.
Lead integration must inject the identity service, enable both strict flags for `/v1`,
and route provider observations through ProviderEventSink. Partial refunds are refused
as amount mismatches; this lifecycle models exact full reversals, not partial accounting.

Final regression snapshot against untouched inherited source: **62 failed, 39 passed**
(101 cases; the later unknown-create regression separately failed before its fix).

Final WS1 source-checkout validation: **1,669 passed, 37 skipped** in 87.16 s;
`ruff check agentpal training tests`: **0 errors**. Baseline: **1,567 passed,
37 skipped** in 97.29 s; ruff also green. New regression cases: **102**.
The skipped artifact/environment cases remain unchanged. Installed-wheel integration
is BLOCKED pending the lead-owned package list adding `agentpal.identity` and
`agentpal.lifecycle`; no packaging file was changed by WS1.


## Mission 3 A3 completion (offline)

- SEC-17: shared local SQLite sliding windows, persistent audit throttling, no active
  identity eviction and no memory fallback on storage failures. A four-process test
  admits exactly one aggregate budget. Initial/runtime outages return 503.
- SEC-18: validated certificate/key pairs and a real local HTTPS listener; forwarded
  headers remain disabled. TLS-terminating proxies are unsupported. Public exposure
  defaults off and permits only exact HTTPS POST WEBHOOK_PATH with an approved Host,
  shared global/peer limits before reads and min(general cap, 512 KiB) streaming cap.
  A1 retains ownership of signature verification and CA-chain validation.
- Owner MFA: real stdlib RFC 6238 TOTP, encrypted seeds, expiring parent-bound
  enrollment, durable replay counters/lockout and separate five-minute step-up
  credentials. Ordinary credentials cannot assert MFA. Parent revocation/expiry,
  disabled owners, factor removal and a missing/wrong key invalidate derived proof.
  AGENTPAL_REQUIRE_MFA_FOR_APPROVAL policy wiring is assigned to the lead.
- SEC-22: strict approved entries also require usable, matching unconsumed approvals.
  Cross-DB failure injections and actual provider-call process death preserve holds;
  startup never retries. Supported deployment is one quiescent financial worker.
  Multi-worker rolling financial startup is DESIGN ONLY.
- SEC-13: legacy authenticated reads now name their agent; shared request IDs remain
  independently readable. Ambiguous operator reads remain refused.
- Leakage review: owned metadata/legacy ingress reject credential pastes; structured
  logs and CLI errors are sanitized. Uvicorn logs exclude queries and opaque IDs.
  The direct store.py redactor remains BLOCKED on the lead's integration fix, tracked
  by the explicitly approved strict xfail in test_sec_v3_redaction.py.
- Audit/evidence: transactional audit and hash-checked evidence tests are retained;
  this does not establish cryptographic tamper evidence against storage administrators
  or establish the truth of observed content. External logging handlers are outside
  the application's formatter guarantees.

Fail-first evidence: six initial shared-limit/GET regressions failed; 24 initial
TLS/exposure regressions failed; 23 initial factor/vector/approval regressions
failed, plus the missing CLI command separately. Seven strict-approval variants
failed at their intended consistency assertion (the missing-row case required
bypassing the existing append-only delete trigger). Seven logging/metadata cases
failed; two initial-store outage cases and a websocket-close case failed before
fixes. An additional encoded-access-target leak failed before route-family logging.
New process-race and interruption tests of existing controls already passed after
correcting test setup; these are regression coverage, not new fail-first claims.
Final suite numbers are recorded in the append-only team memory and delivery report.


Fourteen controlled source mutations were each caught at test failures, with the
original sources restored after every probe: aggregate admission, outage 503,
public TLS, exact method, body cap, TOTP counters/lockout, parent revocation,
encryption-key proof, enrollment parent binding, JSON sanitization, metadata
rejection, strict approval checks and legacy GET scoping. Two further fail-first
probes found adjacent-counter OTP-value collisions and common API-key/Meta-style
secret leakage (four failing cases); those guards were added before the final run.
