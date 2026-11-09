# AgentPal hosted: live verification report

2026-11-06 · branch agentpal-dev-infra-v1 · 9ed691a4406c · lead: Claude

## 1. Summary

**AgentPal now runs as a hosted service on Render, against the real PayPal sandbox, and the full money journey was verified live through it.** Hosting surfaced one real defect that offline tests could not see: PayPal rotated its sandbox webhook signing certificate on 2026-10-21, so every real signed webhook was rejected. It is fixed, with a regression test on PayPal's real certificate, and verified live (HTTP 200).

| Capability (hosted, https://agentpal-api.onrender.com) | Status |
|---|---|
| Hosted deployment: HTTPS only, persistent disk, required auth, Render front door | IMPLEMENTED — TESTED (live) |
| Owner-approved purchase to a bound merchant (Northwind): buyer approves, verified capture | IMPLEMENTED — TESTED (live: provider_confirmed) |
| Refund on our own capture, partial then rest | IMPLEMENTED — TESTED (live: $0.40 + $0.60 COMPLETED) |
| Refund of a capture paid to another merchant | IMPLEMENTED — TESTED (live: FAILED safely, merchant_permission_required; PayPal needs that merchant's permission) |
| Payout to a freelancer after owner approval | IMPLEMENTED — TESTED (live: SUCCESS) |
| Kernel cloud browser from the hosted service, page price check | IMPLEMENTED — TESTED (live: page loaded, screenshot, review_recommended) |
| Real signed PayPal webhooks: ORDER.APPROVED, CAPTURE.COMPLETED, CAPTURE.REFUNDED | IMPLEMENTED — TESTED (live: verified, HTTP 200) |
| Verified PayPal events AgentPal does not use (payouts) | IMPLEMENTED — TESTED (live: acknowledged as ignored) |
| Forged or unsigned webhooks | IMPLEMENTED — TESTED (live: 400, nothing applied) |
| Reconciliation against PayPal Transaction Search | IMPLEMENTED — TESTED; rows stay pending until PayPal indexes them (about 3 h) |
| Owner web interface | IMPLEMENTED — TESTED (added after this report: dashboard at /app/, sign-in with an owner token) |

## 2. Bugs found live and fixed today

- **Webhook certificate (high impact, found only by hosting):** PayPal's new sandbox signing certificate is issued by DigiCert EV RSA CA G2, which was not in our bundle. All 15 real deliveries were rejected. Fixed: the public intermediate is bundled as an untrusted path hint; trust still comes only from certifi. Fail-first regression test uses PayPal's real current certificate.
- **Real refund events have no order id:** PayPal names only the refunded capture (links rel=up). Fixed: after the signature verifies, the order is read from PayPal with our own token. The URL is built from a strictly validated id, never from the event's link.
- **Payout events made PayPal retry for days:** a verified event of a type we do not use returned 400. Fixed: it is acknowledged as ignored and nothing is applied. Unsigned events of any type are still rejected.
- **Diagnostics:** a rejected webhook now logs which fixed check failed (never headers or body).

## 3. Live security probe of the deployment

| Probe from the internet | Result |
|---|---|
| Forged webhook without PayPal headers / with a non-PayPal certificate URL | 400 / 400 |
| Webhook by GET, or with a query-string variant | 403 / 403 |
| Oversized webhook body (600 KiB) | 413 |
| Owner API with no token, bogus token; approve with no token | 401 / 401 / 401 |
| /docs, /openapi.json, /assess, path traversal, encoded traversal | 403 each |
| Plain http | 301 to https |
| Public /health | 200, reveals nothing |

**Full offline suite: 3,626 passed, 31 skipped.** Ruff clean. OpenAPI and Postman regenerated. GitHub CI green on every pushed commit checked.

## 4. Honest limits

- Sandbox only. is_real_money is always false. No live PayPal.
- PayPal's automatic webhook retries reuse the first send time, so a delivery missed for more than 5 minutes is refused (replay protection). Reconciliation reads PayPal directly and covers it.
- One request from an interrupted test run is flagged reconciliation_required. Its PayPal order was never approved, so no money moved. The flag is the intended safe state.
- Refunds of captures paid to other merchants need that merchant's permission in PayPal; AgentPal reports this instead of pretending.
- Devpost requires a public repository with a license. This repository is private, and that is the owner's decision.
- Rotate every key pasted in chat (PayPal, Render, Kernel, Channel3, Postman) after the hackathon.

## 5. For judges and testers

- Base URL: https://agentpal-api.onrender.com/v1 (Bearer token per owner; python -m agentpal new-owner issues one).
- Branch agentpal-dev-infra-v1, commit 9ed691a4406c. Render redeploys on every push.
- Docs: README.md, docs/DEPLOYMENT.md (hosted section), PAYPAL_SANDBOX.md, DEVELOPER_API.md.
