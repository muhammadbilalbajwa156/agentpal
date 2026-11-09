# H2 — authorization and money-integrity verification

2026-11-05; starting commit `ab24447fd1af6013e5403264a1993c3bcac5c9e9`.
IMPLEMENTED — TESTED. Sandbox only; no production payments or real money.

No confirmed or suspected security defect was found in this bounded round. This is
evidence for the exercised guards, not a proof of every possible interleaving or a
production-readiness claim. Product code and all inherited tests remain unchanged.

| Severity | CONFIRMED | SUSPECTED |
|---|---:|---:|
| Critical | 0 | 0 |
| High | 0 | 0 |
| Medium | 0 | 0 |
| Low | 0 | 0 |

There is no confirmed finding requiring a failing regression, CVSS assessment, manual
exploit reproduction or product fix. No strict xfails were added or hidden by broad
exceptions. Existing controls were also calibrated by four runtime-only mutations.

## Scope and method

The new files live only in `tests/e2e/h2/` and this report. We read the project context,
team memory, infrastructure plan, README, API/SDK/OpenAPI contracts, security audit,
PayPal, Kernel and Channel3 integration documentation and T1–T4 acceptance evidence.
There was no `docs/handover/` directory. Current source and fresh observations took
precedence over historical status paragraphs.

Offline tests use the real FastAPI app, durable `apk_` credentials, two authenticated
owners, a same-owner sibling agent, strict wallet approval gateway, risk engine,
lifecycle and SQLite stores. Only provider HTTP is replaced. Fixed-clock tests use
the real TOTP implementation, encrypted random seeds and durable replay counters.
Independent-connection tests instantiate two complete service/store sets over the
same private databases, so their races do not rely solely on one Python lock.

The CLI/TCP harness bootstraps both owners with `python -m agentpal new-owner`,
captures tokens only in memory, and starts `python -m agentpal serve --platform
--host 127.0.0.1 --port 8202`. Databases are unique private subdirectories under
`/tmp/m8-h2`. It strips inherited AgentPal paths and optional provider settings;
offline runs receive no external credentials. A process-level lock prevents two H2
runs sharing the port. No traffic targeted another person's server. No Channel3,
Kernel, GitHub or other third-party attack traffic was sent.

## Guards that held

All items below are IMPLEMENTED — TESTED. Test names are in `tests/e2e/h2/` unless
otherwise identified. Controlled database corruption tests are defense-in-depth
checks, **not** claims of remotely reachable database modification.

| Guard | Fresh evidence |
|---|---|
| Agents cannot approve or reject | `test_no_agent_role_can_approve_or_reject`: all seven roles, payment and payout routes; HTTP 403, no approval rows and no provider I/O. |
| SDK and internal principal forgery do not grant owner authority | `test_sdk_and_forged_principal_do_not_turn_an_agent_key_into_owner`: owner SDK refuses a real agent key; an agent SDK calling the approval route receives a typed permission error; altered authenticated-owner object fails durable session revalidation. |
| Role capability ceilings | `test_role_ceiling_cannot_be_raised_at_registration_or_grant`: registration and later owner grants reject capabilities outside each role ceiling; grants stay empty. Forged context capability/role/owner claims independently fail permission checks. |
| Exact payment approval terms | `test_payment_ledger_independently_rejects_changed_approved_terms`: amount, currency, merchant name/domain, PayPal email, purpose, agent and request identity changed after approval. Even a recomputed wallet digest does not consume approval or reach provider HTTP. |
| Exact payout approval terms | `test_payout_ledger_independently_rejects_changed_approved_terms`: amount, currency, receiver, note and purpose changed after approval, with recomputed metadata digest; execution refuses with unconsumed approval. |
| No HTTP term substitution | Extra approval fields give 422; PATCH gives 405; extra execute-body fields cannot replace persisted approved amounts/payees/receivers. |
| No cross-request, cross-agent or cross-owner approval replay | `test_approval_digest_cannot_replay_across_requests_agents_or_owners`: identical business terms with another request identity cannot reuse the digest or execute. |
| Expiring, single-use approvals | Approval expires after the validity window. Four concurrent approvals produce one approval; six executes produce one provider create and one consumed approval. Reapproval refuses. |
| Tenant IDs are indistinguishable from missing IDs | `test_foreign_owner_ids_are_identical_to_missing_for_all_money_routes` and `test_foreign_agent_and_wallet_ids_are_identical_to_absent`: equal 404 status/JSON for payment, payout, capture, refund, reconciliation, advice, agent/key administration and wallet paths. Sibling and other-owner agents cannot execute another request. No provider I/O on these probes. Timing equivalence is not claimed. |
| Shared payment/payout budget | Ten simultaneous one-cent requests reserve exactly five cents, across USD/EUR/GBP and recurring/nonrecurring requests. Other-currency allowance is untouched. Eight mixed requests on independent service connections reserve exactly three cents. |
| Limits and minor units | Excess per-transaction or cumulative/rolling amounts refuse without extra holds; invalid precision, exponent, numeric JSON, nonfinite, negative and zero inputs cannot reserve. The supported platform currencies exercised all use two minor digits; unsupported currencies were covered by inherited tests. |
| Creation idempotency | Six concurrent same-key creates give one request and one hold; changed amount with the key returns 409. |
| Approval/reject/execute consistency | Contention results in one valid released, approved or pending state, with at most one provider create and exact committed allowance. |
| Capture exactly once | Six captures after verified buyer approval produce one capture POST, one verified outcome and one unit of spent allowance. Capture before buyer approval refuses without a capture POST. |
| Refund ceiling and exactly-once credit | Four distinct concurrent 0.40 refunds of a 1.00 capture admit exactly two. Excess remainder refuses. Four same-key 0.20 refunds result in one provider POST, one refund ID and one final reversal. Reconciliation does not credit twice. A separate 1.01 excess-refund case refuses before provider mutation. |
| Unknown outcomes keep holds | Lost capture, refund and payout responses remain reconciliation_required. Repeated local calls do not repeat the mutation; uncertain refund amounts stay reserved against additional refunds. |
| Provider evidence must match | Changed amount/currency/payee on an approved order prevents capture and retains exposure. Changed payout receiver, note, recipient type, sender item, currency or amount cannot confirm. A successful batch header with a non-SUCCESS item cannot count as completed. |
| Current freeze, suspension, grants and keys | Completed freeze/suspend/revoke/zero-overlap rotation/execute-grant removal overrides prior approval. Capture rechecks freeze and grants after order creation. Freeze/execute races submit at most once and all later executes refuse. Concurrent rotation/revocation cannot resurrect the old key; revoking the new key also invalidates it. Overlapping requests authenticated before revocation may finish; instantaneous cancellation is not claimed. |
| Self-payout refusal | Every documented owner-maintained alias (profile name, email, identity_email, receiver_email), including case normalization, refuses with no hold or provider I/O. |
| Real MFA | Spoofed MFA headers fail payment/payout approval. Enrollment OTP replay fails; a new TOTP mints a valid step-up session that approves both. Parent revocation invalidates that session. Refund creation, its replay and reconciliation enforce MFA before provider I/O. |
| Text remains data | Purpose/product instruction strings, script markup, SQL/template syntax and forged system messages neither approve nor execute. Purpose stays literal in owner JSON; hard limit violations remain blocked. Advisory isolation and market-check risk/state immutability additionally remain covered by inherited `test_channel3_api.py` in the full suite. Text is allowed to add legitimate risk signals; semantic text invariance is not claimed. |
| Credential storage | Plaintext issued credentials do not appear in read-only SQLite dumps or scoped owner resource views after approval/execution. Test reports include no credential values or payer URLs. |
| Restart integrity | A partially refunded capture reopens through the real factory, passes cross-store consistency, stays provider_confirmed and retains exactly 0.60 net spent. |

## Live PayPal Sandbox evidence

IMPLEMENTED — TESTED: three money paths through the owned TCP server: purchase/capture,
refunds and payouts. One custom order of **1.00 USD**, refunds of **0.40 + 0.60 USD**,
and two custom payouts of **1.00 USD each** were created. No order exceeded 1.00 USD.
The inherited baseline also exercised its credential-gated live tests; its source
permits eight order creates and two payout submissions. Thus this round is bounded
to nine Orders creates and four payout submissions in total, below fifteen even
when counting all submissions together. The final full suite disables external
credentials and creates no further live objects. H2 made no Channel3 searches;
the baseline's two Channel3 tests are the only possible paid searches in this round.

| Path | Observed result |
|---|---|
| Purchase/capture | Order `…823E`; agent approval 403, foreign-owner approval/capture 404, unapproved execution 409, capture before buyer approval 409. Execute race `[200,409]`; capture race `[409,200]`; verified provider_confirmed; duplicate capture 409. |
| Refunds | Refunds `…7440` and `…5205`, COMPLETED for 0.40 and 0.60. Same-key concurrent replay returns one refund ID with `[200,201]`; changed body, 1.01 over-refund and 0.61 over-remainder refuse. Payment reversed; spent allowance zero. |
| Payout | Final test batch `…DHQL`, SUCCESS item, provider_confirmed; concurrent executes `[200,200]` safely share one batch; re-execute returns that same batch. |
| Initial payout continuation | Original batch `…A5JU` reached SUCCESS through read-only reconciliation after reopening its own databases. No replacement POST for that request. |

The first payout harness assertion selected the first HTTP-200 response even when
that response was a local in-flight replay with no batch saved yet. The persisted
winner had a pending batch. Selecting the provider-bearing response after all
workers join fixed the assertion; no AgentPal guard was bypassed. A targeted rerun
initially collided with our simultaneously running offline TCP harness and failed
owner authentication, before money submission; the H2 process lock prevents that
harness collision. These were test-harness issues, not confirmed product findings.

The independent labelled sandbox buyer used environment credentials in memory.
Browser clicking never served as payment proof; server-side verified provider
responses settled the ledger. No screenshots, provider raw bodies, bearer tokens,
full resource/provider IDs or payer URLs are included in delivery.

## Mutation evidence

`python tests/e2e/h2/verify_mutations.py` starts four isolated pytest processes.
It changes runtime objects/functions only, never inherited source files. Each
disabled control produced exactly one assertion failure with zero setup/collection
errors; all four were caught. The runner requires this distinction before reporting
success and stores redacted summary evidence under `/tmp/m8-h2`.

| Removed control | Test that failed |
|---|---|
| Self-recipient check | `test_self_payout_is_refused_for_every_owner_maintained_alias[email]` |
| Current execution permission check | `test_completed_policy_changes_override_prior_approval[remove_execute-payment]` |
| Independent ledger terms validation | `test_payment_ledger_independently_rejects_changed_approved_terms[purpose-Changed purpose]` |
| Refund amount <= captured remainder | `test_over_refund_is_refused_before_any_provider_mutation` |

These are calibration failures under deliberately weakened runtime controls, not
findings against the shipped code or strict xfails.

## Reproduction and validation

From `agentpal/`, ordinary H2 verification never contacts third parties:

```sh
python -m pytest -o addopts='' -q -p no:cacheprovider tests/e2e/h2
python tests/e2e/h2/verify_mutations.py
ruff check agentpal training tests
ruff check agentpal tests
```

The live command explicitly creates fresh sandbox objects; run it once within the
stated order budget, with credentials supplied only in environment variables:

```sh
H2_RUN_LIVE=1 python -m pytest -o addopts='' -q -p no:cacheprovider \
  tests/e2e/h2/test_live_sandbox.py
```

It skips cleanly without opt-in or app/buyer credentials. The successful purchase
and refund cases ran together; the corrected payout case passed separately.
Do not run another H2 TCP suite concurrently on port 8202.

Baseline before H2 collection: **3,039 passed / 42 skipped**, 633.45 seconds; required
ruff clean. Final standalone H2 run: **123 passed / 3 skipped**, 53.82 seconds;
the skips are the explicit live opt-in cases. Four mutation calibrations caught.
An earlier expanded offline run passed 122 cases before the separate excess-refund
case was added. The usage reset interrupted the first final full-suite attempt at
87%; that attempt is not counted as a passing gate.

**Final full suite: 3,144 passed / 62 skipped / 0 failures / 0 errors**, 382.860
seconds, exit 0. JUnit recorded 3,206 cases, including H2's 123 passes and three
live opt-in skips. Both `ruff check agentpal training tests` and `ruff check
agentpal tests` passed. Credential scans of all twelve delivered files passed;
the original worktree HEAD and all tracked product/test files are unchanged.

The first completed post-reset rerun produced **3,104 passed / 62 skipped /
40 setup errors / 0 assertion failures**, 346.299 seconds. All 40 errors came
from the inherited T2 session fixture on fixed port 8102: its initial owner
registration received `401 invalid_token`. Port 8102 remained occupied after
our suite ended. H2 itself remained **123 passed / 3 skipped** in that run.
The lead was asked to pause competing T2/full-suite runs; no unidentified server
was stopped, and no existing fixture or assertion was changed to bypass the error.
This attempt is not counted as a passing full-suite gate.

For the final full-suite invocation, a Python subprocess removed `PAYPAL_*`,
`CHANNEL3_*`, `KERNEL_*`, `H2_RUN_LIVE`, `T2_RUN_LIVE` and `AGENTPAL_T1_REFUND_DB`
from its inherited environment, then executed `python -m pytest -q -p no:cacheprovider`
with a JUnit output path under `/tmp`. This preserves offline test coverage while
avoiding repeated provider mutations and paid searches. The baseline retained the
original provider credentials; the difference in live skips is intentional.
The successful final invocation also set `PYTEST_ADDOPTS` to a unique
`--basetemp=/tmp/m8-h2/full-...` directory and started after port 8102 became free.
No inherited fixture, assertion, test selection or product control was changed.
Final output and JUnit are `/tmp/m8-h2-full-final.txt` and
`/tmp/m8-h2-full-final.xml`; the failed collision attempt is retained separately
as `/tmp/m8-h2-full-collision.txt` and `.xml`.

## Limits, BLOCKED items and lead requests

- **BLOCKED — shared memory edit:** the latest hard scope authorizes additions only
  under H2 paths. `docs/memory/PROJECT_MEMORY.md` was not edited. Lead may append the
  short proposed entry below.
- **BLOCKED — shared worktree Git writes:** Git returned read-only filesystem when
  creating `refs/heads/m8-h2-review`. Commits and bundle use isolated metadata under
  `/tmp`; the provided worktree and original branch stay intact.
- **DESIGN ONLY — stronger external self-recipient identity:** current shared agent
  identity has no verified mailbox binding. The implemented self-payout guard was
  verified against every documented owner-maintained alias. Arbitrary undisclosed
  external aliases cannot be proven to belong to the agent by this contract. No
  new bypass of the implemented alias check was found.
- This finite test round does not exhaust scheduling, every provider state history,
  or unsupported multi-worker rolling startup. Corruption tests do not imply an
  HTTP attacker can write SQLite. No product-change request arose from this round.

Proposed team-memory entry (lead-owned; not applied):

```text
### 2026-11-05 H2 — final authorization and money-integrity verification
- Done: no confirmed/suspected security finding; additive real-service, TCP, role,
  exact-term, tenant, race, budget, MFA, provider and restart regressions (acaa71b).
  Live $1 capture, 0.40+0.60 refunds and $1 payouts verified; four mutations caught.
  H2 123 passed/3 skipped; final full suite 3144 passed/62 skipped; ruff clean.
- Decisions/traps: an in-flight payout replay may be 200 before its batch is saved;
  select the provider-bearing response after joining concurrent workers. H2 TCP
  owns port 8202 with a process lock. Final full suite disables external credentials.
- Next: review/import m8-h2-review from /tmp/m8-h2.bundle; no product fix requested.
```

## Delivery

Source commit: `acaa71b` (`test(h2): verify tenant authorization and money integrity`).
Ten new Python files, plus `tests/e2e/h2/evidence.json` and this report. No existing
file changed. Review branch `m8-h2-review` descends from the starting commit in
`/tmp/m8-h2-review.git`. The following report/evidence commit contains the final
gate counts and redacted observations. Both commit bodies end with the requested
co-author trailer. No push, PR, amend, rebase or shared-history rewrite occurred.

Bundle: `/tmp/m8-h2.bundle`, branch `m8-h2-review`, based on the starting commit.
Verification command: `git bundle verify /tmp/m8-h2.bundle`; output is retained at
`/tmp/m8-h2-bundle-verify.txt`. Review the exact commit IDs with
`git --git-dir=/tmp/m8-h2-review.git log --oneline ab24447..m8-h2-review`.
