# Full backend acceptance test: summary (2026-11-03 to 2026-11-05)

Four independent Codex testers (GPT-6.1-Sol) each ran their own real AgentPal server and used it
like real users and attackers, with live PayPal Sandbox and live Channel3 data. They reported
and reproduced defects as strict-xfail tests; the lead (Claude) verified each finding, fixed it
fail-first and flipped the test to a normal regression test. Detailed reports:
`E2E_T1.md` (developer and owner journey), `E2E_T2.md` (security), `E2E_T3.md` (intelligence
quality), `E2E_T4.md` (reliability and operations).

## Findings and fixes

| ID | Severity | Finding | Fix |
|---|---|---|---|
| T3-1 | High | AirPods earbuds matched to $9.99 ear tips | accessory vocabulary extended |
| T3-2 | Medium | owners could not see why a request was risky | owner-only `risk_signals` (agents get none) |
| T3-3 | Medium | no comparable currency labelled `no_concern` | now `no_data` |
| T3-4 | Medium | variant/range pages flagged matching claims high-risk | closest offer + in-range + `MULTIPLE_PRICES_ON_PAGE` warning |
| T3-5 / T1-005 | Medium | free refresh upgraded name match to `exact` | only URL lookups are exact |
| T1-001 | Medium | SDK lacked capture/reconciliation | `capture_payment_request`, `reconcile_payment_request`, `get_reconciliation` |
| T1-002 / T2-001 | Medium / Low | owner bootstrap undocumented | `python -m agentpal new-owner`, docs |
| T1-004 | Medium | routed-merchant refund: bare FAILED | PayPal 403 → FAILED with `merchant_permission_required` |
| T1-003 | Low | README described the old Phase 1 | README rewritten |
| T4-1 | Medium | databases created 0644 | created/tightened to 0600 |
| T4-2 | Medium | identity DB lock froze the server, health included | auth and rate limiting run off the event loop |
| T4-3 | Medium | uncertain holds missing from `pending` | `reconciliation_required` counted as pending |
| T4-4 | Medium | partial refunds reported as lifecycle mismatch | refund check before lifecycle mismatch |
| T4-5 | Medium | backward clock step consumed an approval before it existed | approval usable only in `[approved_at, expires_at)` |

No critical defect was found. The security tester ran 376 authorization cases plus tenant
isolation, MFA, approval binding, concurrent budgets, replays, SSRF, webhook forgery and leak
scans: all held. The reliability tester killed the server at five live stages with no double
money movement, and PayPal's own Transaction Search matched the ledger (+1.00 − 0.40 = 0.60).

## Known limits (not defects)

- Refunding a payment made to ANOTHER merchant needs that merchant's PayPal permission
  (partner onboarding). AgentPal reports it clearly; it does not invent a refund.
- Live Kernel browsing and live signed PayPal webhooks need public hosting (Render).
- Sandbox only; single-node SQLite.

## Final verification (2026-11-05)

- Full suite: 3,057 passed, 24 skipped (local); 3,040 passed, 0 failed in a CI-identical venv.
- Live: 13/13 PayPal + Channel3 live tests; live 6-scenario world demo passed.
