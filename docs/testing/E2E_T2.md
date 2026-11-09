# T2 adversarial backend acceptance — 2026-11-03

IMPLEMENTED — TESTED. Starting commit: `db7d00870563cb55c6702787408e9bbcfbe65fec`.
Owned additions: `tests/e2e/t2/` and this report. Product code, SDK, existing tests,
shared documentation, model and shared Git metadata are unchanged.

## Findings

| Confirmed severity | Count | Finding |
|---|---:|---|
| Critical | 0 | None demonstrated |
| High | 0 | None demonstrated |
| Medium | 0 | None demonstrated |
| Low | 1 | E2E-T2-001: no actionable owner bootstrap in developer entry-point docs |

No authorization bypass, unapproved payment, allowance overspend, duplicate financial
settlement, credential disclosure or successful SSRF was demonstrated. This is evidence
about the tested build and configuration, not a production-readiness claim.

### CONFIRMED E2E-T2-001 — low — developer onboarding / identity provisioning

IMPLEMENTED — TESTED (reproducer and strict xfail; product fix not implemented).

**Reproduce**, from `agentpal/`:

```sh
python -m agentpal --help
python -m agentpal new-token --help
python -m pytest -o addopts='' -q -p no:cacheprovider --runxfail \
  tests/e2e/t2/test_hostile_inputs.py::test_documented_owner_bootstrap_is_actionable
```

Read README, DEVELOPER_API and SDK in the prescribed order. The API walkthrough begins
with an already-issued `AGENTPAL_OWNER_TOKEN`. There is no owner-provisioning CLI
subcommand. `new-token` issues legacy credentials, which do not authenticate `/v1`.
None of these three entry-point documents supplies `create_owner` / `issue_token`
or another runnable owner issuer. The unmarked regression fails at that missing
prerequisite; the normal run records strict `xfail(reason="E2E-T2-001-1")`.

**Expected:** a developer can bootstrap a durable owner using a documented operator
procedure before registering an agent. **Actual:** testing requires finding and importing
internal identity APIs and discovering the identity database path in `platform/wiring.py`.
This is an onboarding defect; no unauthenticated owner-creation endpoint is requested.

**Suggested minimal fix:** document the trusted operator procedure using
`OwnerIdentityService(IdentityStore(identity_db_path))`, `create_owner`, and `issue_token`,
the identity DB configuration/default, and secure one-time credential delivery. Alternatively
provide a trusted local CLI. Explain that legacy `new-token` credentials cannot substitute.
Then remove this xfail and update the check to the approved documented procedure.

### SUSPECTED S-T2-01 — possible medium integration gap — routed-merchant refunds

BLOCKED (successful Northwind refund); root-cause backend defect **not confirmed**.

A buyer-approved USD 1.00 Northwind payment completed. Authenticated PayPal reads returned
200 and confirmed its exact requested payee and amount. An owner-authorized USD 0.75
refund returned HTTP 201 with refund status `FAILED`. A separate owner-authorized USD 0.01
diagnostic, after the first definite failure, reproduced PayPal HTTP **403**,
`NOT_AUTHORIZED`, detail `PERMISSION_DENIED`. Both local refund claims are FAILED;
no allowance was credited and no refund was invented. The diagnostic did not retry the
same mutation/key. Only finite status/error codes were retained, not provider bodies.

Request suffix **a9a0**, order suffix **660G**. Read-only follow-up verified one unit,
USD 1.00, exact Northwind payee. Capture remains completed and this USD 1.00 sandbox
payment remains unrefunded. The app-linked merchant flow below completed both refunds.

**Reproduce with credentials**, separately from any other T2 server:

```sh
T2_RUN_LIVE=1 T2_LIVE_MERCHANT=northwind python -m pytest -o addopts='' -q -s \
  -p no:cacheprovider tests/e2e/t2/test_live_acceptance.py::test_live_sandbox_authorization_capture_and_refund_races
```

This explicit diagnostic currently fails at the expected completed-refund assertion.
It is not marked as a confirmed backend defect or included in default live acceptance.
The default suite skips these opt-in mutations. **Lead action:** verify sandbox merchant
delegation/permissions and decide whether routed merchants are intended to support refunds
with this app. If unsupported, document that limitation and surface it explicitly; if
supported, configure the required merchant authorization and repeat this exact flow.
The backend's fail-closed handling of the provider denial worked.

## Execution and pass evidence

The TCP harness launches the documented command as its own child:

```sh
python -m agentpal serve --platform --host 127.0.0.1 --port 8102
```

Each run has distinct risk, identity, wallet and limiter files below `/tmp/e2e-t2/`.
Owner bootstrap uses the trusted identity API; all agent registration, wallet creation,
adversarial financial operations and SDK calls then use the real TCP server. Tokens remain
in process memory. No plaintext credential fixture is written. The harness keeps required
authentication and shipped rate limits; it paces bulk traffic below the default quotas.
Offline runs strip provider credentials only in their child environment, using the existing
local simulator. Clock and observable-egress cases separately use the real ASGI app.

| Attack / workflow | Observed evidence | Status |
|---|---|---|
| All 47 published `/v1` method/path operations × eight credential cases | 376 matrix rows: anonymous, wrong prefix, revoked key, A/B agents, A/B owners, ordinary owner without MFA; type refusals 403, invalid credentials 401 | IMPLEMENTED — TESTED |
| Public health / webhook exception | Health 200; unsigned webhook 400 regardless of bearer; these are intentional public routes | IMPLEMENTED — TESTED |
| Other-owner versus absent resources | Byte-identical 404 envelopes for owned payment/agent/wallet mutations and reads; wrong credential types consistently 403 | IMPLEMENTED — TESTED |
| Same-owner sibling agent | Another agent's payment GET/execute/request-decision 404; absent from its list | IMPLEMENTED — TESTED |
| Payout isolation / self payout | Other-owner read/approve/reject/reconcile 404; owner-maintained self email refused; changed receiver with reused create key 409 | IMPLEMENTED — TESTED |
| Registry changes | Suspended/rotated-away key 401; removed prepare or execute grant 403; frozen wallet overrides prior approval, 409 | IMPLEMENTED — TESTED |
| Exact approval | Wrong/old digest 409; injected amount/currency/merchant/payee/purpose/receiver fields at approval 422; PATCH terms 405; execute body cannot replace persisted terms | IMPLEMENTED — TESTED |
| Expiry / MFA | Controlled trusted clock: expired approval execute and late approve 409; unsatisfied MFA approvals/refunds/payout approvals 403, including spoofed MFA header | IMPLEMENTED — TESTED |
| Single-use / concurrency | Parallel approvals then executions yield one pending ledger transition; four genuinely overlapping approve/execute races preserve approval gate and at most one order | IMPLEMENTED — TESTED |
| Money syntax | 15 negative/zero/exponent/extra precision/huge/Unicode/numeric/bool/NaN/whitespace cases 422; six currency tricks refused | IMPLEMENTED — TESTED |
| Concurrent budget exhaustion | 12 parallel USD 1.00 preparations against USD 5.00: five awaiting approval, seven blocked assessments, committed 5.00 and available 0 | IMPLEMENTED — TESTED |
| Creation idempotency | Same body/key 201→200 with replay header; changed financial terms 409; independent agent can use same key without crossing scope | IMPLEMENTED — TESTED |
| Python SDK over TCP | Decimal request and authenticated owner approval work; Python float raises TypeError before HTTP | IMPLEMENTED — TESTED |
| Injection / length / controls | Seventeen additional field probes: accepted payloads remain awaiting approval or blocked, invalid fields 422; no injected consent; live Channel3 result remains advisory | IMPLEMENTED — TESTED |
| SSRF offline guards | Private, metadata, IPv6/mapped/NAT64/6to4, numeric/hex/octal forms, private/mixed DNS, file, userinfo and custom port refused by deterministic guard; private URL never reaches observable browser seam | IMPLEMENTED — TESTED |
| Page-check binding | Off-merchant/suffix-trick/userinfo/file/port inputs 422; no-data result remains advisory, original digest/status unchanged | IMPLEMENTED — TESTED |
| Webhook attacks | Unsigned/forged event replay 400; duplicate transmission header 400; 33,000-byte body 413; no event accepted | IMPLEMENTED — TESTED |
| Leakage | Errors do not echo submitted sentinel or keys; stored DB/log bytes lack issued keys, rotated keys and configured secrets; query/path credential probes do not leak into response or logs; no stack trace in captured server logs | IMPLEMENTED — TESTED |
| Rate limits / lockout | Default per-token request 61 returns 429 and Retry-After; research request 7 returns 429 and rotation does not reset it; five invalid TOTP codes lock enrollment for five minutes, correct code/re-enrollment cannot bypass | IMPLEMENTED — TESTED |

The eighth matrix case uses an ordinary token with `mfa_satisfied=false` under normal
configuration. Separate controlled-app checks enable the MFA requirement and verify the
four sensitive operation families. For correctly typed callers, absent-resource requests
exercise route authorization without destructive resource changes. Offline provider-dependent
disputes/report reads correctly return 503 `not_configured`.

HTTP 201 is not approval or completion: blocked preparations are persisted as assessments.
HTTP 200 execution likewise counts as completed only with a verified terminal lifecycle.

### LIVE PayPal sandbox

IMPLEMENTED — TESTED, two T2-created orders, each USD 1.00, loopback HTTP server.

| Evidence | Northwind routed merchant | App-linked Test Store |
|---|---|---|
| Request / order suffix | a9a0 / 660G | 5d20 / 101Y |
| Agent approve / unapproved execute | 403 / 409 | 403 / 409 |
| Competing execute calls | 200 / 409, one order | 200 / 409, one order |
| Agent / other-owner capture | 403 / 404 | 403 / 404 |
| Capture before buyer approval | 409 | 409 |
| Buyer harness | Submit clicked; independently verified by server's provider reads | Submit clicked; independently verified by server's provider reads |
| Competing captures after approval | 409 / 200, provider_confirmed | 200 / 409, provider_confirmed |
| Refund above gross capture | 409 | 409 |
| Competing USD 0.75 refunds | 201 / 409; provider denial, no credit | 201 / 409; one COMPLETED |
| Remaining USD 0.25 | Not attempted; merchant permission issue | COMPLETED; same-key replay 200; changed body 409 |
| Verified final state | provider_confirmed, USD 1.00 spent | reversed, USD 0.00 spent; USD 1.00 refunded |

App-linked refund suffixes **260R**, **381U**. Final owner refund reconciliation verified
the total. Buyer-click output alone was never counted as financial evidence. No production
PayPal or real funds were used. Existing live tests also ran in baseline/final full suites;
combined Orders creation stays below the 25-order cap (see final gate record).

### LIVE Channel3

IMPLEMENTED — TESTED, one paid search in the T2 custom run. Request suffix **5204**.
Real Logitech MX Master 3S data yielded `high_risk`, `PRICE_FAR_BELOW_MARKET` and
`MERCHANT_NOT_IN_MARKET` for USD 1.00 at Shady Deals. Purpose included prompt-injection
text and HTML. Request stayed `awaiting_approval`; unapproved execute returned 409.
No market evidence authorized a payment. Baseline/final built-in live tests consume
additional searches, still well below 60.

## BLOCKED / limits

- **BLOCKED:** live Kernel capture, redirects, hostile page subresources, browser replay and
  DNS rebinding at the actual cloud browser. The assigned environment explicitly blocks
  browser control; `KERNEL_API_KEY` is absent. No session was created and no network
  workaround was attempted. Deterministic guard tests are offline evidence only.
- **BLOCKED:** genuine signed PayPal webhook delivery and valid-signature replay. No
  `PAYPAL_SANDBOX_WEBHOOK_ID` or public HTTPS listener is configured. Forgery/duplicate/
  unsigned replay tests do not substitute for these.
- **BLOCKED:** successful Northwind refund with the configured app; provider permission
  diagnosis above. This is not a claim of unauthorized refund or false completion.
- Payout authorization, self-payment, exact receiver digest and idempotency were tested
  offline. T2 did not execute an additional custom live payout. The existing live payout
  tests are covered by the full-suite gates, not relabeled as custom TCP evidence.
- These tests do not exhaust every possible race interleaving, DNS transition or hostile
  Unicode sequence. Mutable database corruption was not represented as an HTTP exploit.

## Reproduction / final gates

Run T2 servers sequentially because each deliberately owns port 8102:

```sh
cd agentpal
python -m pytest -q -p no:cacheprovider tests/e2e
PYTHONPATH=. python tests/e2e/t2/adversarial_sweep.py
T2_RUN_LIVE=1 python -m pytest -o addopts='' -q -s -p no:cacheprovider \
  tests/e2e/t2/test_live_acceptance.py
python -m pytest -o addopts='' -q -p no:cacheprovider
ruff check agentpal training tests
ruff check agentpal tests
```

Live checks skip without their explicit opt-in and credentials. The published passing
live refund flow defaults to the app-linked merchant. Northwind is an explicit diagnostic.
Each invocation of the PayPal test can create one sandbox order; do not loop it blindly.

Baseline full suite: **2,914 passed, 38 skipped, 3 warnings, 593.78 s**.
Full suite (2026-11-03): **2,978 passed, 40 skipped, 1 strict xfailed, 3 warnings, 663.67 s**,
exit 0. The sole xfail is the confirmed owner-bootstrap documentation defect; the two
additional skips are explicit opt-in T2 live cases. Baseline and final ruff, both
`agentpal training tests` and `agentpal tests`: **all checks passed**.
The bootstrap check with `--runxfail`: **1 failed**, proving the confirmed defect is live.
Controlled boundary checks: **8 passed**. Additional sweep: **17 input probes + 4 races
passed**. Custom live checks: **1 app-linked PayPal pass + 1 Channel3 pass**; the earlier
Northwind run failed at the refund-completion assertion and is reported above.

Budget accounting: **18 Orders creates** maximum across baseline/final built-in live tests
(8 each) and two T2 custom orders; **at most 5 paid Channel3 search calls** across the two full suites
and the custom run. The inherited live payout tests additionally ran two sandbox payout
submissions per full suite. All live principal amounts were USD 1.00 or less per operation.

Required standalone `python -m pytest -q -p no:cacheprovider tests/e2e`:
**64 passed, 2 skipped, 1 strict xfailed**, exit 0. Its double-quiet output contains
64 pass markers, two skip markers and the documented xfail, with no failure markers.
The two clean skips explicitly protect custom live tests from repeated money mutations.

Captured logs and sanitized evidence are under `/tmp/e2e-t2/`, with pytest outputs under
`/tmp/t2-*.txt`. A durable sanitized snapshot is delivered as
`tests/e2e/t2/evidence.json` (all 376 matrix statuses, live outcomes and sweep cases).
Only this report, test source and that snapshot are delivered; no DB, server log,
credential, provider response body, screenshot or payer URL is committed.

## Requests to the lead / memory handoff

- Fix E2E-T2-001 in README/DEVELOPER_API/SDK or CLI (outside T2 ownership).
- Resolve Northwind merchant authorization and repeat the routed refund flow.
- Supply a suitable environment for Kernel and a subscribed public HTTPS webhook listener
  for the blocked acceptance items.
- This task's latest restriction permits only T2 paths, so the shared append-only
  `docs/memory/PROJECT_MEMORY.md` was deliberately not edited. Proposed team-memory entry:

```text
### 2026-11-05 T2 — adversarial acceptance and live sandbox races
- Done: owned TCP/SDK/controlled-app checks; one low onboarding defect with strict xfail;
  verified capture/refund races and live Channel3; review commits in delivery record.
- Decisions/traps: 201 blocked assessment is not spending; Northwind refund POST denied
  by PayPal 403/PERMISSION_DENIED, with no credit; app-linked refunds verified.
- Next: document owner bootstrap; resolve merchant refund permission; verify Kernel and
  subscribed webhook delivery in a capable environment.
```

## File manifest and review delivery

All files are new. Under `tests/e2e/t2/`: `__init__.py`, `conftest.py`,
`test_authorization.py`, `test_controlled_boundaries.py`, `test_hostile_inputs.py`,
`test_money_and_binding.py`, `test_live_acceptance.py`, `adversarial_sweep.py`, and
`evidence.json`. The tenth file is this `docs/testing/E2E_T2.md` report.

Review source commit: `55e1343`. Report/evidence commit follows it. Both commits use
the required co-author trailer and descend directly from the starting commit. Review
Git is isolated at `/tmp/m7-t2-review.git`, branch `m7-t2-review`; shared HEAD remains
at the starting commit. No push, PR, history rewrite or product fix was performed.

Delivery bundle: `/tmp/m7-t2.bundle`, branch `m7-t2-review`. Verify from a checkout
containing the starting commit with `git bundle verify /tmp/m7-t2.bundle`. The verification
output is saved separately at `/tmp/m7-t2-bundle-verify.txt` after the report commit.

## Restart verification — 2026-11-05

IMPLEMENTED — TESTED. After the machine restart, the unchanged T2 harness started
a fresh authenticated CLI server on port 8102 with new databases under `/tmp/e2e-t2/`.
The required standalone E2E command again exited 0: **64 passed, 2 skipped, 1 strict
xfailed**, including all 376 authorization cases. Both required ruff commands passed.
Python test sources match review commit `55e1343`; backend HEAD remains the starting
commit. The full-suite and live-provider results above are preserved 2026-11-03 evidence.
No additional live provider calls were made during restart verification, preserving the
original sandbox-operation budget. The harness stopped its server after verification.
