# F1 — final fresh-developer and owner verification

2026-11-05. Base: `ab24447fd1af6013e5403264a1993c3bcac5c9e9`.
IMPLEMENTED — TESTED, with the explicit limits below. No product code, existing
test, generated contract, pinned model or shared memory file was changed.

No new confirmed security finding was reproduced. No suspected financial guard
failure remains from these F1 journeys. This is bounded regression evidence,
not a claim that every possible attack or production deployment is safe.

| Severity | New CONFIRMED findings | New SUSPECTED findings |
|---|---:|---:|
| Critical | 0 | 0 |
| High | 0 | 0 |
| Medium | 0 | 0 |
| Low | 0 | 0 |

There is therefore no new strict-xfail reproduction or CVSS score. The ordinary
regressions assert the guards continue to hold. Documentation gaps are listed
separately because they do not demonstrate a financial authorization bypass.

## Fresh installation and ownership

The documented server dependency install and separate editable SDK install both
succeeded in `/tmp/m8-f1/venv`, a fresh venv with system packages available.
This verifies dependency resolution and package installation on this host; it is
not a clean-machine or hash-locked deployment-install claim.

The harness runs the documented `python -m agentpal new-owner <id> --name ...`,
captures its one-time stdout token privately, and starts its own
`python -m agentpal serve --platform --host 127.0.0.1 --port 8205`.
Both fresh owners, all agents and all databases belong to this test session.
F1 databases live only in unique directories under `/tmp/m8-f1/`.
Repeated bootstrap issues a different usable token for the existing owner.
No identity-library provisioning is needed for the public journeys.

Only normal Channel3 catalogue calls and PayPal Sandbox operations were sent
to providers. Hostile/malformed cases and browser evidence used local TCP or
in-process code with injected offline providers. No live Kernel session was
created. Credentials, browser URLs, raw provider bodies and full resource IDs
are absent from the committed evidence. Access logs are buffered in memory;
configured-secret and plaintext credential scans pass.

## Previous acceptance findings

The fifteen T1/T3/T4 finding IDs below include the shared refresh defect under
both original IDs. T2's bootstrap observation is also covered by T1-002.
All rows are IMPLEMENTED — TESTED. Original regression tests are preserved.

| Previous ID | User-side verification and evidence |
|---|---|
| T1-001 | Installed owner SDK captured four live $1 purchases; `get_reconciliation` and `reconcile_payment_request` worked over TCP. |
| T1-002 / T2-001 | README/API bootstrap commands created two durable owners; reissuing a token, registering agents and creating wallets succeeded. |
| T1-003 | README describes the control layer, authenticated exact-term approvals, sandbox execution and virtual allowance. Old Phase 1-only claims are covered by the preserved T1 regression. |
| T1-004 | Northwind routed-merchant refund returned `FAILED / merchant_permission_required`; no fictitious credit was granted. |
| T1-005 | Live Bose name match remained `medium / no_match` after free refresh; no exact identity or market comparison was invented. |
| T3-1 | Live earbuds and requested ear tips were distinct high-confidence products. Offline ear tips alone cannot support earbud pricing/deals. |
| T3-2 | Prompt injection reasons were present in the owner's single read, request list and approval queue; agent create/read/list returned empty `risk_signals`. |
| T3-3 | EUR MX Anywhere query had `NO_COMPARABLE_OFFERS / no_data`; no currency conversion or fabricated deal. |
| T3-4 | Offline matching variant/range claims did not produce `PAGE_PRICE_MISMATCH`; preserved saved-retailer-page regression also runs. |
| T3-5 | Name-based free detail refresh never became `exact`; new offline transport assertions and live Bose confirm this. |
| T4-1 | All freshly created financial DBs were 0600; no plaintext issued API credential was found in DB files. |
| T4-2 | An exclusive identity DB lock left an authenticated read waiting while `/v1/health` remained responsive; the read completed after unlock. |
| T4-3 | Restart quarantined a simulator attempt; wallet `pending=1.00`, `spent=0`, and the $1 exposure remained committed. |
| T4-4 | Live $0.40 partial refund survived restart and verified reconciliation; the report returned `pending / refund_extension_required`, without a lifecycle mismatch. |
| T4-5 | Preserved forward/backward injected-clock regressions check 409 before provider I/O and leave approval unconsumed outside its valid interval. |

## Guards that held

New F1 tests exercise these boundaries through the real CLI/TCP app or explicitly
injected advisory providers. The full inherited suite adds the original broad
authorization, MFA, SSRF, webhook, concurrency and recovery regressions; F1 does
not claim to have independently reproduced every one of those attacks live.

| Guard | Evidence |
|---|---|
| Owner/agent credential separation | Wrong credential type 403; rotation, revocation and suspension invalidate old agent access. |
| Tenant isolation | Other owner receives 404 for the payment and approval; agent self-approval receives 403. |
| Owner must approve exact digest | Preapproval execute and wrong digest receive 409. Live purchases and payout need their separate authenticated owner decisions. |
| Budget and frozen-wallet enforcement | Frozen prepare refused; a $2 payment exhausts the shared payment/payout budget; payout is refused without an extra hold. |
| Creation replay/body binding | Same key/body replays one request, changed body receives 409. Market replay preserves the historical snapshot and request ID. |
| Input validation | Small float/boolean amounts, forged completion status and a control-bearing payee are 422; health stays available. SDK float is refused before HTTP. |
| Provider evidence | Simulator remains pending, never spent; four real captures are confirmed only after provider verification. |
| Merchant identity | Northwind, Bluepeak and CloudDesk exact payees remain visible on owner reads. Independent authenticated PayPal GETs match each pinned payee, gross amount, currency and both request references. |
| Single execution/capture | Duplicate purchase execute and capture are 409. Payout replay retains the original batch. |
| Refund authority and upper bound | Agent financial authority is not expanded; routed merchant permission refusal is explicit. Own-account $0.40 + $0.60 settles exactly once, over-refund is 409, final payment is reversed. |
| Refund replay and recovery | Same partial-refund key returns the same refund and unchanged spending; partial/full accounting survives restart. Separate authenticated refund GETs verify both COMPLETED receipts. |
| Exact payout recipient and success evidence | Priya payout reaches verified `SUCCESS`, same batch on replay, with $1 counted once. |
| Advisory authority isolation | Market/page checks leave terms, digest, approval state, risk score and recommendation unchanged. Page check is owner-only and rejects off-merchant URLs. |
| Owner-only reasons | Owner sees deterministic risk codes; agents see an empty list, on create and scoped reads. The schema retains the empty field. |
| Secret handling | Issued owner/agent credentials absent from DB bytes; configured credentials absent from in-memory server logs and all delivered files. |

## Live results

`tests/e2e/f1/evidence.json` contains explicit safe aggregate fields, not raw API
responses. The final live run passed **3 tests in 109.12 seconds**.

| Product query | Currency | Initial / refreshed result |
|---|---|---|
| Logitech MX Master 3S | USD | high / high; high confidence, overprice signal |
| Apple AirPods Pro 2 | USD | review / review; high confidence |
| Apple AirPods Pro 2 Ear Tips | USD | no concern / no concern; high confidence for the requested accessory |
| Apple iPhone 16 Pro 128GB | USD | no match; refresh honestly 409 `market_check_unavailable` |
| Apple iPhone 16 Pro Max 256GB | USD | review / review; high confidence |
| Bose QuietComfort Ultra Headphones | USD | no match / no match; medium confidence preserved |
| Sony WH-1000XM5 | USD | review / review; high confidence |
| Samsung T7 Shield 2TB | USD | high / high; low-price caution |
| Logitech MX Anywhere 3S | EUR | no data / no data; no comparable currency |
| Raspberry Pi Pico 2 | GBP | no match / no match; medium confidence preserved |

Every successful catalogue response carries provider evidence. No-match/no-data
is reported honestly, rather than forced into a successful product comparison.

| SDK purchase | PayPal order suffix | Verified outcome |
|---|---|---|
| Northwind Office Supply | 380N | confirmed; routed refund honestly refused |
| Bluepeak Electronics | 215A | confirmed |
| CloudDesk Software | 5052 | confirmed |
| App-linked sandbox shop | 894U | confirmed, $0.40 refund …420F then $0.60 …292X, reversed |

Priya payout batch …2ERQ reached verified `SUCCESS`; replay returned that same
batch. After refund and payout, this agent's final spent allowance was **$4.00**
(three $1 persona purchases plus one $1 payout). Restart preserved that value.
All service-side financial mutations in this journey used the installed public
SDK. The separately labelled browser harness supplies sandbox buyer consent;
its click is never payment evidence. Independent receipt reads afterwards
verified all four order payees/references and both completed refund receipts.

Pinned Newman **6.2.3**, installed with `npm ci` under `/tmp/m8-f1/newman`, ran the
checked-in V1 security folder: **17 requests, 34 assertions, zero failures**.
Credentials were passed through stdin; no normal Newman JSON reporter was used,
because it can persist Authorization headers. Its final sandbox-execute scenario
creates one additional $1 unapproved order, which remains unconfirmed and held.
The report does not claim the generated endpoint catalogue was executed in full.

Budget accounting includes the initial unmodified credentialed baseline: at most
**15 order-create attempts total** (8 inherited baseline, 2 in the initial F1
probe, 5 in the final F1 run including Newman); all orders are USD 1.00. At most
**16 paid Channel3 searches** (2 baseline, 4 initial probe, 10 final). Payouts:
2 inherited baseline plus 1 final F1, all USD 1.00. No subsequent live financial
mutation was run. Initial probe failures were harness assumptions: no-match
refresh may be 409, and payee is a top-level field. They were corrected only in
new F1 files; one captured sandbox purchase and one unapproved Newman order from
that probe were preserved, not blindly repeated under the same logical request.

## Documentation gaps and lead requests

These are usability/review risks, not demonstrated security bypasses.

1. **IMPLEMENTED — TESTED observation:** README/API/SDK alone stop the payment
   walkthrough at execution submission. The core API route table and SDK guide
   omit the newly repaired capture/reconciliation steps, payer-action URL use,
   refund/payout walkthrough and expected routed-merchant refund refusal. A new
   developer needs OpenAPI and the separate PayPal integration docs to finish.
   Request: extend `docs/DEVELOPER_API.md` and `docs/SDK.md` with a complete
   owner/buyer/capture/refund/payout example and links to those supplement docs.
2. **IMPLEMENTED — TESTED observation:** `merchant_paypal_email` is top-level in
   `PaymentOut`, outside `terms`, even though it participates in the digest.
   SDK guidance currently says present `payment['terms']` and digest for review.
   Request: explicitly require the owner UI/client to display the top-level
   payee as well. The payee was visible and bound in our actual API journey;
   this is not evidence of a provider substitution or approval forgery.
3. **IMPLEMENTED — TESTED observation:** `AgentClient.get_wallet()` returns
   `{'wallets': [...]}`; allowance is `['wallets'][0]['allowance']`. The SDK
   examples do not show this shape. Also document `market_check_unavailable`
   when a free refresh has no matched product. Neither is a financial defect.
4. **IMPLEMENTED — TESTED infrastructure observation:** inherited T2/T4 suites
   use fixed shared ports 8102/8104. Simultaneous acceptance worktrees can connect
   to a sibling tester's server or fail with address-in-use, invalid-token and
   crash-gate errors. Request: lead-owned fixture isolation should use unique
   ports and verify process ownership before traffic. F1's own fixture refuses
   an occupied 8205 before sending requests. The provided full-suite runner
   isolates only those two old loopback ports via a temporary `sitecustomize`;
   it changes no app policy, test assertion, provider result or credential.

The latest assignment owns only the F1 test folder and this report, so the shared
memory file was left unchanged. Proposed lead append:

```md
### 2026-11-05 F1 — final fresh-user regression verified
- Done: F1 CLI/TCP/SDK, ten live catalogue products, four captures, partial/full
  refunds, payout, receipt GETs and Newman; tests 076f7de plus evidence commit.
- Decisions/traps: no new confirmed security finding; payee is top-level outside
  terms; no-match refresh may be 409; inherited fixed ports need isolation.
- Next: review /tmp/m8-f1.bundle, complete the SDK/API walkthrough docs; Kernel
  live and public signed webhooks still need hosting.
```

## BLOCKED and evidence limits

- **BLOCKED:** live Kernel browser control, per assignment/network limitation.
  Offline page checks inject a labelled browser provider and use real extraction;
  they are not a live Kernel claim.
- **BLOCKED:** real incoming signed webhook delivery without a public listener.
  Offline inherited signature/replay tests remain in the full suite.
- **IMPLEMENTED — UNVERIFIED:** aged Transaction Search matching for these new
  captures/refunds. Fresh comparisons honestly report indexing lag or the refund
  extension; F1 did not wait three hours or equate a missing Search row to proof.
- **BLOCKED:** clean-machine deployment validation and OS privilege-drop probe.
  This host has preinstalled dependencies and restricts `setgroups/setgid/setuid`.
  File modes and SQLite-enforced read-only behavior are tested separately.

## Reproduce and gates

From `agentpal/`, no existing file needs editing:

```sh
python -m pytest -o addopts='' -q -p no:cacheprovider tests/e2e/f1
# Explicitly authorized normal LIVE sandbox operations; max 5 $1 orders/run:
F1_RUN_LIVE=1 python -m pytest -o addopts='' -q -p no:cacheprovider \
  tests/e2e/f1/test_live_journeys.py
# Own preserved receipts only; GET/OAuth, no financial mutations:
python tests/e2e/f1/verify_receipts.py /tmp/m8-f1/live-<session>
# Required full offline suite, isolating inherited shared ports:
python tests/e2e/f1/run_offline_suite.py --tb=short \
  --junitxml=/tmp/m8-f1/full-final.xml
ruff check agentpal tests
ruff check agentpal training tests
```

The live marker cleanly skips when explicit opt-in or credentials are missing.
The full-suite runner removes PayPal/Channel3/Kernel credentials only from its
child process to preserve the live-call budget, then invokes exactly
`python -m pytest -q -p no:cacheprovider` plus reporting flags. Its temporary
socket mapping affects only bind/connect to loopback 8102/8104 and propagates
to Python children; external endpoints, authorization and policy are untouched.

- Before: required unmodified baseline **2,986 passed, 42 skipped, 8 failed,
  45 errors**, 489.25 s. Failures/errors were inherited shared-port collisions;
  the same T1 module rerun alone was **14 passed, 1 skipped**. Baseline ruff clean.
- New F1 offline: **14 passed, 3 live skips**, 6.56 s. No xfails.
- New F1 live: **3 passed**, 109.12 s. Newman 17/34/0; independent receipt reads pass.
- Final full suite: **3,037 passed, 61 skipped, zero failures/errors/xfails**,
  **500.554 s**, 3,098 collected. XML confirms all selected previous-fix
  regressions passed, including both clock directions and both refunded report
  states. Artifacts: `/tmp/m8-f1/full-final.xml` and `full-final.log`.
  Sixteen inherited live tests skipped in this child to preserve the provider
  budget; the F1 live tests were run separately as reported above.
- Final required ruff commands: clean.

Delivery: isolated review repo `/tmp/m8-f1-review`, branch `m8-f1-review`, based on
the start commit above. Shared Git metadata is outside this worktree's writable
roots, so the original branch/index are not changed. Test commit `076f7de`;
report/evidence commit is the branch tip. `/tmp/m8-f1.bundle` was verified with
`git bundle verify` against the original repository prerequisites.
All delivered files are additions inside the two assigned paths.
