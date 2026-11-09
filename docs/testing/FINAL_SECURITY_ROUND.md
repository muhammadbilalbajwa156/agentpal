# Final security and regression round (2026-11-05)

Five independent checks of the whole backend after the earlier acceptance round:
four defensive security verifications (Codex GPT-6.1-Sol, plus one performed by the lead after the
Codex reviewer was stopped by its provider's automated cybersecurity filter) and one fresh-user
functional regression. Each Codex agent ran its own real server; findings were reproduced as
strict-xfail tests, then fixed by the lead and flipped to regression tests.

| Check | Scope | Confirmed findings | Status |
|---|---|---|---|
| H1 | request edge: authentication, headers, URL forms, malformed bodies, rate limits, TLS, webhook listener | 1 medium, 1 low | fixed |
| H2 | inside the tenant model: approvals, terms binding, isolation, budgets, races, idempotency, refunds, payouts, MFA (incl. live PayPal) | **none** | held |
| H3 | upstream data: webhooks, PayPal responses, web pages, Channel3, dependencies, secrets in history | 1 medium, 2 low | fixed |
| H4 | line-by-line code review (lead) | none (one hardening) | held |
| F1 | fresh-user live regression of every journey | none (4 clarity items) | addressed |

## Fixes

| ID | Severity | Finding | Fix |
|---|---|---|---|
| H1-2 | Medium | a stalled request body held a connection open indefinitely | whole body must arrive within 5 s, else 408 |
| H3-1 | Medium | Channel3 buy links reached the owner unchecked | https only, Channel3 redirect host or the retailer's own domain; otherwise the link is removed |
| H1-1 | Low | extremely nested JSON caused a 500 | 422 `invalid_request` |
| H3-2 | Low | a malformed PayPal link relation caused a 500 | non-string relations are skipped |
| H3-3 | Low | webhook certificate / verification responses were read without a streaming limit | streamed with byte limits |
| H4 | Hardening | evidence files readable by all local users | 0400 |
| F1 | Clarity | payee shown outside `terms`; docs stopped at execute; port collisions in tests | payee inside `terms`; full walkthrough; free ports |

## What held (selected)

Constant-time credential checks with no id oracle; parameterised SQL; agents can never approve;
any change to approved terms (amount, currency, merchant, payee, receiver) invalidates approval;
cross-tenant ids are indistinguishable 404s; budgets hold under concurrency; no double capture,
refund or payout under replays and races; forged, replayed or wrongly-certified webhooks never
change state; hostile pages and poisoned upstream data stay advisory; no secrets in the repository,
history or logs.

## Final verification

- Full suite: 3,574 passed, 31 skipped, 0 failed (local); 3,559 passed, 0 failed in a CI-identical pydantic-2.14 venv.
- Live: 13/13 PayPal sandbox + Channel3 tests; 6/6 live scenario world.
- Not covered here (needs public hosting): live Kernel browsing, live signed PayPal webhooks.
