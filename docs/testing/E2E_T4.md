# T4 backend acceptance: reliability, recovery, concurrency and operations

Status: IMPLEMENTED — TESTED where evidence is listed below. Sandbox only; no real money.
Live session: 2026-11-03; resumed/final verification: 2026-11-05. Starting commit: `db7d00870563cb55c6702787408e9bbcfbe65fec`.
Owned additions: `tests/e2e/t4/` and this report. No production module, SDK,
inherited test/assertion, generated API artifact or shared document was changed.

## Confirmed defects

| ID | Severity | Area | Evidence / strict regression |
| --- | --- | --- | --- |
| E2E-T4-1 | medium | Persistence confidentiality | All four DBs created `0644` with umask `022`; one strict xfail. OS-user access probe blocked by container privilege restrictions. |
| E2E-T4-2 | medium | Availability under database contention | An identity DB write lock blocks authenticated HTTP processing and the ASGI event loop; concurrent health exceeds a 700 ms timeout. One strict xfail. |
| E2E-T4-3 | medium | Wallet accounting presentation | After SIGKILL, $1 remains committed but reserved/pending/spent all show $0. One strict xfail. |
| E2E-T4-4 | medium | PayPal operational reconciliation | A legitimate partial refund produces `mismatch/lifecycle_mismatch`; reproduced with live PayPal and offline oracle. One strict xfail. |
| E2E-T4-5 | medium | Clock rollback / restart safety | Execution accepts consumption before approval creation; startup then refuses that state. One strict xfail. |

No confirmed critical or high defect, double payment, double credit, lost budget
hold, unverified confirmation, or credential disclosure in application logs was
observed in these probes. This statement is limited to the tested cases.
No additional suspected product defect is asserted; remote acceptance of lost
order/payout replies remains explicitly unverified below.

### E2E-T4-1: databases readable by other local users

IMPLEMENTED — TESTED (reproduction, not a product fix).

Reproduce from `agentpal/`:

```sh
python -m pytest -o addopts='' -q -p no:cacheprovider \
  tests/e2e/t4/test_operations.py::test_financial_databases_are_private_under_standard_umask \
  tests/e2e/t4/test_storage_access.py::test_other_local_user_cannot_read_owner_identity \
  --runxfail
```

Expected: private financial and identity persistence, including credential hashes,
owner identities and transaction records. Actual: `identity.db`, `wallet.db`,
`ledger.db`, and `ledger.db.ratelimit.db` have mode `0644`. The second check attempts
a read-only identity query from uid/gid 65534 in a normal `0755` directory.
**BLOCKED** here: the container reports uid 0 but denies setgroups/setgid/setuid.
The check skips cleanly on that restriction; it is not counted as proof of another
user actually reading records. Standard POSIX mode bits provide the confirmed
file-permission finding. A private parent
directory mitigates access; the application currently relies on operators to
supply it. Our live directories are under a private `/tmp/e2e-t4` parent.

Suggested minimal fix: securely create financial/identity/limiter DBs with mode
`0600`, preserve private permissions for SQLite sidecars, and validate permissions
of existing files. Avoid a process-wide temporary umask around concurrent work.
Owning source: `identity/store.py`, `wallet/store.py`, `store.py`, `ratelimit.py`.

### E2E-T4-2: locked identity DB stalls health and unrelated requests

IMPLEMENTED — TESTED.

```sh
python -m pytest -o addopts='' -q -p no:cacheprovider \
  tests/e2e/t4/test_operations.py::test_identity_database_contention_does_not_stall_health \
  --runxfail
```

Exact sequence: start the real CLI on loopback port 8104; hold `BEGIN IMMEDIATE`
on its identity DB from an independent SQLite connection; launch authenticated
`GET /v1/agents`; after 200 ms send `GET /v1/health` with a 700 ms timeout; release
the lock in `finally`. Expected: liveness remains responsive, and contention is
bounded and represented by a constant 503 if authentication cannot complete.
Actual: health times out while synchronous owner authentication waits for the
SQLite write lock; releasing the lock lets both requests finish. The identity
store has a 30-second busy timeout. No financial mutation or provider call is
needed to reproduce this availability failure.

Suggested minimal fix: move blocking credential verification/persistence off the
ASGI event loop, use bounded admission for those workers and a short authentication
DB busy deadline, and map lock exhaustion to a sanitized 503. Preserve all session
revalidation and audit gates. Owning source: `api.py` SecurityGate authentication
and `identity/service.py` / `identity/store.py`.

### E2E-T4-3: reconciliation holds disappear from the pending breakdown

IMPLEMENTED — TESTED.

```sh
python -m pytest -o addopts='' -q -p no:cacheprovider \
  tests/e2e/t4/test_crash_boundaries.py::test_uncertain_hold_remains_visible_in_pending_allowance \
  --runxfail
```

Exact sequence: owner creates wallet, agent prepares $1, owner approves its exact
digest, agent executes, kill server with SIGKILL, restart against the same files,
then agent reads `/v1/me/wallet`. Expected: $1 remains visibly pending and the
breakdown sums to committed exposure. Actual:

```json
{"available":"99.00","reserved":"0.00","pending":"0.00","spent":"0.00","committed":"1.00"}
```

Ledger state is `reconciliation_required`; enforcement still includes the $1.
The public `pending` description is “Submitted; outcome not yet verified.”
The mapper uses only `breakdown['attempted']` and omits
`breakdown['reconciliation_required']`, although committed includes both.

Suggested minimal fix: include reconciliation-required exposure in `pending`
in `platform/wallets.py:WalletBook.allowance`, or expose an explicit public field
and preserve the accounting identity in every wallet view and SDK response.

### E2E-T4-4: partial refunds falsely signal operational inconsistency

IMPLEMENTED — TESTED (offline and LIVE SANDBOX).

```sh
python -m pytest -o addopts='' -q -p no:cacheprovider \
  tests/e2e/t4/test_reporting.py --runxfail
# Optional fresh LIVE session (up to three $1 orders and one $1 payout):
python tests/e2e/t4/run_live.py --live
```

Exact live sequence: approve and buyer-authorize $1 order …6530; verify capture;
start a $0.40 refund; SIGKILL during its claim; restart and reconcile refunds by
verified reads; refund COMPLETED, net spent $0.60; request owner-only
`GET /v1/paypal/reconciliation` with a window covering the session.
Expected: pending `refund_extension_required`, as documented for captures whose
refund integration is not implemented in the operational report, or a correct
refund-aware matched result. Actual: HTTP 200 row
`provider_confirmed / mismatch / lifecycle_mismatch`. The offline check supplies
exact gross amount/currency, local `provider_confirmed`, order `COMPLETED` and
capture `PARTIALLY_REFUNDED` and gets the same false mismatch. The full-refund
control (`reversed` / `REFUNDED`) correctly returns the extension-required result.

Suggested minimal fix: handle verified refund states before the generic
completed-capture/local-lifecycle mismatch branch in `platform/paypal_ops.py:compare`,
while retaining gross/currency/identity checks. Then implement explicit cumulative
refund comparison if the report should certify refund settlement.

### E2E-T4-5: a backward clock step permits invalid consumption and blocks restart

IMPLEMENTED — TESTED (offline, real HTTP/SQLite; injected trusted time).

```sh
python -m pytest -o addopts='' -q -p no:cacheprovider \
  tests/e2e/t4/test_crash_boundaries.py -k 'clock and backward' --runxfail
```

Exact sequence: create a request and authenticated exact-term approval at time T;
step only the test's injected trusted clock back to T−1 second; execute as the
agent; restore/restart normally. Expected: HTTP 409 before consumption/provider
I/O, since approval validity begins at T. Actual: HTTP 200 provider_pending,
`consumed_at < approved_at`, and one provider create. Startup then raises
ConsistencyError (`provider_approval_differs`) because recovery correctly requires
`approved_at <= consumed_at < expires_at`. The unmodified host clock is never
changed. The forward-expiry control correctly refuses execution.

Suggested minimal fix: enforce `approved_at <= now < expires_at` atomically in
`store.py:AssessmentStore.begin_execution` before consuming approval or creating
attempt/provider rows. Preserve the existing consistency check; do not weaken it
to accommodate invalid timestamps. This is an operational clock-rollback defect,
not evidence that an agent can approve its own payment.

## Passed acceptance evidence

IMPLEMENTED — TESTED.

| Probe | Evidence |
| --- | --- |
| Real developer HTTP / SDK | Own `python -m agentpal serve --platform --host 127.0.0.1 --port 8104`, private DB directory under `/tmp/e2e-t4`; actual SDK owner registration/wallet creation and agent preparation over TCP; remaining operations plain HTTP. |
| Owner approval crash | SIGKILL after approval; reopened exact digest and unconsumed approval; unchanged reservation; live execution subsequently confirmed one $1 capture. |
| Execute crash after provider acceptance | Deterministic external synthetic provider commits one order, stops process before reply, tester SIGKILLs; restart quarantines, duplicate execute 409, one create POST and $1 retained over repeated restarts. |
| Capture crash after acceptance | Same response-loss gate after synthetic capture; verified GET reconciliation settles once; two reads produce one verified outcome, one capture POST and $1 spent. |
| Refund crash after acceptance | Durable synthetic refund accepted before kill; lost refund ID discovered from authenticated order/refund GETs; $0.40 credited once, $0.60 net persists after another restart. |
| Payout crash after acceptance | Synthetic payout accepted before kill; stored batch reference absent; recovery returns 409 `payout_batch_unknown`; local replay emits no second POST, $1 remains held. |
| Buyer approval / capture gap | Offline and live restart quarantines; verified read stays uncertain; capture returns 409, zero capture operations, hold retained. This is the documented conservative recovery limit. |
| HTTP concurrency | 48 identical create calls, one 201, replays/conflicts only, one ledger row/$1; changed-body replay 409. Twenty-four execute calls consume one approval/attempt/provider operation and never falsely confirm simulator output. |
| Rate limits | Eight authenticated reads accepted, next two 429 with Retry-After; SIGKILL/restart with identical settings preserves quota. |
| Upgrade / consistency | Newer wallet schema 999 refused without downgrade; altered ledger amount refuses startup with ConsistencyError; SQLite integrity checks across all four reopened files return `ok`. |
| Read-only persistence | Existing wallet DB reopened with SQLite `mode=ro` (effective even for uid 0); CLI refuses before readiness with `readonly database`. OS-level privilege-drop probe is BLOCKED, not counted as a pass. |
| Invalid configuration | Noninteger limit, invalid MFA boolean, partial sandbox credentials and unsafe production/in-memory configuration all exit nonzero before listening. |
| Clock steps | Forward clock past approval expiry: execution 409, approval unconsumed, zero provider POSTs. Backward step found E2E-T4-5. Host clock is never changed. |
| Secret handling | Live own-session log buffer contains no owner/agent credential, PayPal app ID/secret or buyer email/password; all four database byte scans contain no plaintext owner/agent API credential. Logs retained only in memory, not committed. |

The deterministic crash provider is explicitly **OFFLINE synthetic**. It replaces
only provider HTTP and preserves the real platform, deterministic policy,
authenticated approvals, ledger, SQLite files, TCP server, and SIGKILL/restart.
Its provider state is independent of the server's databases. These gates establish
the exact accepted-but-response-lost boundary, which network timing alone cannot
prove against remote PayPal. Live timing probes below state that limitation.

## Live evidence

IMPLEMENTED — TESTED. All amounts are USD 1.00 orders/payouts and USD 0.40 refunds.

- First live probe: order …3529 confirmed through the SDK/TCP flow; interrupted
  refund recovered COMPLETED, spent $0.60. A second request was correctly blocked
  by `SPLIT_TRANSACTION_EXCEEDS_HARD_LIMIT` because the test wallet hard limit was
  $1. The harness was corrected to use a $5 per-transaction limit; no product
  policy was changed, and the first order/refund was not retried.
- Complete session: directory `/tmp/e2e-t4/live-7jyfeett`; approved order …6530
  survived SIGKILL, buyer action was independently verified, one completed outcome.
  Live Channel3 returned advisory `high_risk` for the $1 mouse request.
- Interrupted live refund: durable claim observed; response unfinished at SIGKILL
  0.6 seconds later. Restart plus verified reads recovered COMPLETED $0.40,
  reserved refund total zero and $0.60 spent. Another restart succeeded.
- Buyer-before-capture order …544T: restart/reconcile
  `reconciliation_required`, capture HTTP 409, zero capture operations;
  combined committed exposure $1.60.
- Live execute interrupted 0.2 seconds after durable claim: response unfinished;
  restart `reconciliation_required`, one create-operation marker, duplicate
  execute HTTP 409; combined committed $2.60. No provider order ID was persisted.
  Whether PayPal accepted that particular order remains unknown.
- Live payout interrupted 0.5 seconds after durable claim: response unfinished;
  restart `reconciliation_required`; local replay retained it; reconciliation
  HTTP 409 (unknown batch), combined committed $3.60. Whether PayPal accepted
  that payout remains unknown; no retry was attempted.
- Busy-session report is read-only: partial-refund row incorrectly mismatch;
  buyer-gap row pending `awaiting_paypal_completion`; lost-order row pending
  `provider_reference_missing`. Payouts are not included in this payment-only
  report; the documented payout extension remains unimplemented.

Independent authenticated provider reads after the busy session confirmed order
…544T `APPROVED` with no captures; order …6530 `COMPLETED`, capture …020Y
`PARTIALLY_REFUNDED` at gross USD 1.00; refund …893T `COMPLETED` at USD 0.40,
matching the local claim. Separate Transaction Search for the preceding hour
returned HTTP 404 (`paypal_unavailable`); the recent index remained unavailable.
Only owned capture IDs were considered; unrelated account transactions were not
reported. This additional check issued GETs and OAuth only, no money mutation.

Nov 5 follow-up after the machine restart: the preserved databases survived.
Free authenticated reads still find captured order …6530 `COMPLETED`; the separate
uncaptured order …544T now returns HTTP 404. Transaction Search for
2026-11-03 16:24:07–16:28:50 UTC now includes capture …020Y, status `S`, +USD 1.00,
and refund …893T, status `S`, −USD 0.40. Both owned IDs are present; net USD 0.60
matches the local completed ledger amount. This completes the previously deferred
aged Search comparison. The disappeared uncaptured order is recorded as provider
read evidence, not treated as proof of completion or grounds for a blind release.

Reproduce with GETs/OAuth only (no money mutation):

```sh
python tests/e2e/t4/read_aged_session.py --directory /tmp/e2e-t4/live-7jyfeett
```

### Required CLI demos and webhook listing

The script `python tests/e2e/t4/run_demos.py --live` runs the three requested CLI
commands and handles `paypal_flow`'s interactive URL privately through the reviewed
buyer harness. URL prompts and credentials never reach the report.

| CLI | Exit / outcome | Live evidence |
| --- | --- | --- |
| `python -m agentpal.demo.paypal_world --live` | 0; six scenarios passed, 24.455 s application elapsed | Captured order …245R/capture …1035, one verified outcome; duplicate execute/capture 409; rejection and over-budget create no order; unapproved order …5026 capture 409; forged webhook 400. |
| `python -m agentpal.demo.paypal_flow --live` | 0; provider_confirmed, 25.66 s wall | States awaiting_approval → approved → provider_pending → provider_confirmed; one verified outcome; labelled sandbox buyer, four-second provider propagation wait. |
| `python -m agentpal.demo.payout_flow --live` | 0; SUCCESS, 21.63 s wall | Batch …EM4U/request …9e3f, one create invocation, one verified outcome, idempotent execution. |
| `python -m agentpal.tools.paypal_webhook_setup --list` | 0 | Empty webhook list; no subscription created. |

The scenario world reports fresh capture `pending/paypal_search_pending`, as
expected during the documented up-to-three-hour Transaction Search indexing lag.
No missing Search row was accepted as proof of settlement. No public webhook
setup or delivery is claimed.

Budget accounting includes existing live tests in both full-suite runs:
8 order-create attempts in each of two credentialed full-suite runs, 3 across
required purchase demos, 4 across our two live sessions = **23 order-create attempts
maximum**. Payouts: two in each credentialed full suite, one payout demo and one
interrupted payout = six $1 payout attempts maximum. Own sessions use two paid
Channel3 searches total; existing live Channel3 tests use two per credentialed
full suite, at most six total. The final full-suite rerun explicitly removes
PayPal/Channel3 credentials from that child process; its live tests skip cleanly.
Offline/load tests make zero paid calls.

## Load and multiple processes

IMPLEMENTED — TESTED for the exact workload. No live provider movement under load.

```sh
python tests/e2e/t4/run_load.py --workers 1
python tests/e2e/t4/run_load.py --workers 2
```

Each run: eight agents, four spawned client processes with four HTTP threads each,
800 wallet reads and 160 same-key creates (key scoped independently to each agent).
Metrics include client process startup; latencies measure individual HTTP calls.

| Server workers | HTTP statuses | Ledger | Rate / latency |
| --- | --- | --- | --- |
| 1 | Reads 800×200; creates 8×201, 150×200, 2×409; no transport/5xx errors | Eight rows, $8 committed, zero provider orders | 19.113 s, 50.2 req/s, p50 248.12 ms, p95 547.20 ms |
| 2 | Reads 800×200; creates 8×201, 151×200, 1×409; no transport/5xx errors | Eight rows, $8 committed, zero provider orders | 10.747 s, 89.3 req/s, p50 148.05 ms, p95 279.08 ms |

The two-worker probe starts **both workers before any financial mutation** and
waits for quiescence. This does not establish safe rolling financial restarts.
`docs/CONSISTENCY.md` explicitly supports only one financial worker and warns that
another RiskService construction quarantines all pending attempts. The app has no
exclusive-process deployment guard. Treat multi-worker financial recovery as an
unsupported deployment limitation, not a newly discovered double-payment defect.

## Limits and BLOCKED work

- **BLOCKED — Kernel live browser capture:** container network limitation explicitly
  supplied in the assignment; no attempts to bypass it and no live session created.
- **BLOCKED — live webhook delivery:** no public listener/subscription; listing only.
- **IMPLEMENTED — UNVERIFIED — remote acceptance of lost order/payout responses:**
  live timing proves a durable local claim and unfinished response, not the exact
  instant remote PayPal committed. Missing provider IDs require operator
  investigation using persisted idempotency keys. Holds remain intact. Exact
  acceptance-window behavior is tested with the independent offline provider.
- **IMPLEMENTED — TESTED — aged Search comparison:** the Nov 5 read-only follow-up
  found the original capture +$1.00 and refund −$0.40, both successful; net $0.60
  matches the preserved ledger. Original Nov 3 indexing-lag evidence remains below.
- **BLOCKED — rolling multi-worker financial recovery:** outside the documented
  supported single-worker deployment. Quiescent two-worker load is reported above.
- **BLOCKED — OS-level privilege drop:** sandbox denies setgroups/setgid/setuid;
  another-user access and chmod-only filesystem read-only behavior cannot be
  established here. SQLite URI read-only mode and actual write-lock contention
  were tested. Actual ENOSPC was not injected.
- **Observed onboarding limitation:** `serve --platform` has no owner-issuance CLI
  and DEVELOPER_API/SDK tell developers to obtain an issued token without a bootstrap
  recipe. Our harness uses the same identity-library `create_owner`/`issue_token`
  route as `apimatic/local_server.py`, with credentials only in memory. Lead should
  document a secure operator bootstrap. This usability observation is not counted
  as a confirmed financial/security defect.

## Verification and delivery

- Baseline command: `python -m pytest -o addopts='' -q -p no:cacheprovider`:
  **2,914 passed, 38 skipped**, 582.70 s; three existing certificate deprecation warnings.
- Baseline `ruff check agentpal training tests`: clean.
- Required focused gate `python -m pytest -q -p no:cacheprovider tests/e2e`:
  **19 passed, 5 strict xfailed, 1 environment skip** (25 cases), **47.120 s**
  after the Nov 5 machine restart. The isolated CLI servers were restarted by
  the tests. Earlier post-fix Nov 3 E2E run: 74.835 s.
- Fail-first command (unchanged product):
  `python -m pytest -o addopts='' -q -p no:cacheprovider tests/e2e/t4 --runxfail -k 'private_under or contention or uncertain_hold or refunded_capture or clock_step'`:
  **5 failed, 2 passed, 18 deselected**, 18.29 s; failures are exactly E2E-T4-1–5.
- Credentialed full-run candidate: **2,932 passed, 39 skipped, 5 strict xfailed,
  1 failed**, 562.97 s. Every pre-existing test passed. It exposed one failure
  in the tester's synthetic
  refund gate: ledger evidence showed the response had applied before SIGKILL.
  Isolated and full-collection isolated retries passed. The gate now publishes
  its marker atomically and explicitly blocks the HTTP response after SIGSTOP,
  even if the process is resumed; all original financial assertions are retained.
  The strengthened final E2E run above passes.
- **Final full suite (Nov 5): 2,917 passed, 55 skipped, 5 strict xfailed**,
  373.44 s, zero failures/errors. Run with PayPal/Channel3 credentials removed
  only from the test child process to stay within the live-call budgets. Sixteen
  provider-live cases previously exercised in the credentialed runs skip here;
  the extra environment skip is the blocked OS-user probe. Total: 2,977 cases.
- The machine restart interrupted the prior offline final run before its report
  was written; that incomplete execution is not counted as a passing gate.
  Nov 5 reran both the required E2E command and the full suite successfully.
- Final `ruff check agentpal tests` and `ruff check agentpal training tests`: clean.
- Five active strict xfail cases represent the five defects; an additional
  OS-user permission reproduction skips on this sandbox; no inherited assertion or
  product control was weakened. `--runxfail` is the fail-first reproduction mode.
- Shared `.git` is not modified. Delivery uses an isolated review clone, branch
  `m7-t4-review`, based on the starting commit, bundled at `/tmp/m7-t4.bundle`.
  Code commits: `6ac165a` (acceptance harness/checks), `fa05f85` (stronger crash
  response gate), `b8f84f9` (aged read-only comparison), plus the final report
  commit on that branch. `git bundle verify /tmp/m7-t4.bundle`: verified.
- Final additions: 12 test/harness Python files and this report (13 files).
  No product code or inherited assertion changed; no shared Git metadata write.
- Evidence artifacts: `/tmp/m7-t4-e2e-final.xml`,
  `/tmp/m7-t4-full-final.xml`, `/tmp/m7-t4-fail-first.xml`.
  Configured-credential scans of source/report and saved test reports pass.

Requests to the lead: fix the five owning areas above; provide operator resolution
for unknown PayPal order/payout references; document bootstrap; guard unsupported
multi-worker deployment. No product changes were attempted outside T4 ownership.

The assignment forbids edits to other docs, so the shared memory file was left
unchanged. Proposed short memory entry for the lead to append after review:

```md
### 2026-11-05 T4 — backend recovery/concurrency acceptance
- Done: strict reproductions and live/offline evidence in docs/testing/E2E_T4.md
  (6ac165a, fa05f85, b8f84f9 plus report commit);
  review bundle /tmp/m7-t4.bundle, branch m7-t4-review.
- Decisions/traps: unknown outcomes stay held, never re-POST; five live crash
  stages; five medium findings (DB modes, lock availability, hidden uncertain
  allowance, partial-refund report, clock-rollback consumption); Kernel/live webhooks remain blocked.
- Next: lead reviews reproductions, applies owning-source fixes, then flips xfails.
```
