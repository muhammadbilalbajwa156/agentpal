# Financial persistence and startup recovery (SEC-22)

Status: IMPLEMENTED — TESTED (local SQLite and fake-provider tests; no live money).

The assessment/ledger database is the authority for request terms digests, reservations,
approvals, attempts, provider keys, order references and outcomes. The wallet database
holds the owner-facing terms, registry, decision audit and local simulation records.
These databases do not share a transaction. Wallet data can never reconstruct an approval,
release an uncertain attempt or prove that a provider completed a payment.

## Commit ordering and crash boundaries

1. Preparing a request commits its assessment and hold in the ledger before inserting
   the wallet copy. A crash between the commits leaves an orphan `pay_` hold; the check
   reports it and refuses startup. The ledger's normal expiry remains authoritative.
2. Approving commits the structured approval and ledger transition together, then writes
   the wallet decision audit. Execution reads only the structured ledger approval.
3. Beginning execution atomically consumes that approval, transitions approved to pending,
   records an attempt and persists distinct create/capture idempotency keys. A rollback
   consumes nothing. A crash after commit leaves a held, uncertain attempt.
4. Each provider operation also atomically claims a unique invocation marker in
   `provider_operations` before the call. Concurrent workers cannot invoke it twice.
   Provider calls happen only after that transaction commits. A process can die after
   the provider receives a call and before recording its response. Startup therefore moves
   **all pending entries**, including entries whose deadlines have not elapsed, to
   `reconciliation_required`. It never retries a provider operation.
5. Only verified provider evidence resolves uncertainty. A missing order id requires
   operator investigation using the persisted keys; recovery does not invent an order id
   or release the allowance. Local simulations never count as provider confirmation.

`RiskService` runs ledger startup recovery. `WalletService` also checks its registry
store against the ledger when constructed through the read-only `check_consistency()`
function. This second check runs in the current process and does not repeat recovery. The public `startup_recovery()` function can be
used explicitly at application startup. Construct services once; constructing a fresh
risk service means declaring a restart and quarantines pending attempts.

## Fail-closed consistency checks

`check_consistency()` reads both stores and returns structured `Inconsistency` records.
`startup_recovery()` first recovers stale assessment claims, quarantines pending attempts,
and applies ordinary hold deadlines. It then raises `ConsistencyError` with the full
`ConsistencyReport` if any invariant fails. Callers must not suppress this exception to
start financial operations. No mismatch is automatically repaired from wallet data.

Checks cover:

- settled/uncertain ledger entries without attempts;
- consumed approvals paired with unexecuted ledger states;
- strict-mode attempts without provider-order rows;
- provider rows without matching consumed approvals or persisted attempt keys;
- approval owner/digest disagreement with the ledger/request, or consumption outside
  its authenticated approval window;
- ledger amount/currency disagreement with assessed requests (exact Decimal comparison);
- operation invocation keys that disagree with their persisted create/capture keys;
- wallet requests missing their ledger terms or with a different digest/owner;
- invalid canonical wallet terms or canonical terms that disagree with their stored digest;
- wallet agent registry ownership that differs from the request owner;
- local simulation records without attempted ledger state, matching provider reference
  or matching terms digest;
- ledger `pay_` requests absent from the wallet database.

Legacy executions may predate structured approvals/provider tables. The provider-row
presence check applies when `AssessmentStore(strict_execution=True)`; actual provider
rows are always checked. A clean report means structural consistency, not a completed
payment: `report.reconciliation_required` separately lists uncertain attempts.

Entries already in `reconciliation_required` remain held and unchanged across repeated
recovery, including after their original request and attempt deadlines. No startup
recovery code releases or resubmits them. Investigate a mismatch with read-only snapshots
of both stores and retain the ledger/event audit; resolution requires trustworthy ledger
facts or verified provider evidence, followed by a new consistency check.

## Fail-first evidence

`tests/test_lifecycle_recovery.py` was run against the WIP recovery implementation before
its fixes: **7 failed, 1 passed**. The failures showed fresh pending attempts remaining
executable, mismatches merely being reported, unchecked canonical wallet terms/registry
ownership, missing strict provider state, and absent stale-claim recovery. One failure
was the missing safe option for a wallet check in a live process.
Two additional tests then failed before adding provider-attempt-key and simulation-order
reference comparisons (**2 failed, 8 passed**). After the initial fixes, **10 passed**. Additional owner/digest/window, ledger amount/
currency and operation-key checks produced **9 failed, 11 passed** before their fixes;
the final recovery suite has **20 passed** and targeted ruff checks passed. These use in-memory stores and no network or credentials.

Provider event capture identities are retained in the additive
`provider_event_captures` table. Deduplication compares semantic fields, allowing
metadata-only status-response changes while quarantining verified identity conflicts.
A verified delivery supersedes an earlier unverified row; the audit records the upgrade.
Missing or mismatched amount/currency cannot settle a decline, completion or reversal.
A verified full refund can resolve uncertainty after an earlier verified completion.


## Mission 3 additions and deployment limit

Strict stores also compare every approved entry with its structured approval:
matching owner and exact digest, supported auth method and nonempty session,
unconsumed state, valid approval/request time window and matching ledger expiry.
Missing or corrupt approval facts raise ConsistencyError before serving.

Supported financial deployment is one worker with quiescent startup/recovery.
Constructing another RiskService means declaring a restart and quarantines all
pending attempts, including another worker's live attempt. Shared rate-limiter
correctness does not solve lifecycle startup coordination. Rolling multi-worker
financial startup remains DESIGN ONLY; do not disable quarantine to enable it.
The read-only cross-DB comparison is intended for quiescent startup, not a globally
atomic snapshot of actively mutating databases.

Mission 3 tests inject a failed wallet projection commit, then close/reopen both
real SQLite files: the orphan ledger hold remains and startup is refused. A failed
wallet decision-audit insert cannot undo or duplicate the authoritative ledger
approval. Independent spawned processes prove one approval consumption, one
create/capture invocation and one HTTP idempotent reservation. A spawned worker
exits immediately after its fake provider receives the create call; reopening and
recovery quarantine the attempt without releasing its allowance or retrying it.
Repeated recovery does not append more transitions to that quarantined attempt.

The new strict-approval checks were run fail-first. Six corrupt-field cases were
accepted before the fix; the missing-row case also failed at its intended assertion
after its corruption fixture bypassed the existing no-delete trigger. The two
cross-database SQL-failure tests already passed against existing controls. None of
these tests proves live provider completion, cross-host database safety or
cryptographic audit-log integrity against an attacker who can rewrite storage.
