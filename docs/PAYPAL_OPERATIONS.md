# PayPal sandbox operations (P4)

IMPLEMENTED — TESTED. Sandbox only; `is_real_money` is always false.

`GET /v1/paypal/reconciliation?start=<RFC3339>&end=<RFC3339>` is authenticated
owner-only (`reconcileWithPaypal`). The SDK exposes
`OwnerClient.reconcile_with_paypal(start=..., end=...)`. Agent keys receive 403;
other owners' requests and account-wide PayPal transactions are never returned.
The report selects the caller's requests by creation time, rechecks ownership
before reading references, and performs authenticated PayPal GETs. It never
reconciles the lifecycle, settles an outcome, releases a hold, or changes a ledger.

Windows must include time zones, be positive, and span at most 31 days. At most
100 requests are returned; larger selections receive `reconciliation_request_limit`
(422), requiring a narrower window. Provider reads stream at most 1 MiB per
response, use the existing bounded connection/read timeouts, refuse redirects,
and never retry mutations. Search uses `fields=transaction_info`, at most 100
rows per page and 10 pages; incomplete/oversized searches stay unavailable.

Rows contain only request ID, local lifecycle, the caller's stored order ID,
an allowlisted PayPal status, result, and a constant reason:

| Result | Meaning |
|---|---|
| matched / not_submitted | Locally unsubmitted request, with no provider reference |
| matched / verified_order_and_search | One completed capture, exact order and capture amount/currency, matching successful search record |
| mismatch | Exact amount/currency or lifecycle disagrees, or search contradicts the capture |
| missing_at_paypal | Stored order's authenticated GET returned 404 |
| pending / paypal_search_pending | Verified capture is recent (up to three hours), or its timestamp is unavailable; search is absent/unavailable |
| pending / paypal_search_unavailable | An older capture cannot be checked against search |
| pending / awaiting_paypal_completion | Order exists but has no verified completed capture |

Orders v2 is authoritative for lifecycle comparisons. Search lag never hides a
contradiction in the order itself. An empty search is never proof of completion.
Ambiguous units/captures, malformed Decimal values, identity mismatches and provider
errors fail closed. Capture/search records are linked by exact stored capture ID;
unrelated account transactions and payer details never leave the service.
A real empty Search envelope can omit `transaction_details` when both
`total_pages` and `total_items` are zero. Recent empty windows may return 404.

P2/P3 extension points are documented in `platform/paypal_ops.py`: refunds and
payouts require their own owned references and comparisons. Refunded captures
currently report `refund_extension_required`; no independent settlement mapper
is introduced here. A payment created inside the requested window but captured
outside it may lack a Search row: choose a window covering both timestamps.

## Sandbox buyer TEST HARNESS

`demo/sandbox_buyer.py` uses synchronous Python Playwright/Chromium. It is **not
AgentPal owner approval**, and its `approved` result only means the buyer submit
button was clicked. The actual app must read PayPal before capturing and verify
the resulting provider outcome. Production platform code never imports the harness.

Credentials come only from `PAYPAL_SANDBOX_BUYER_EMAIL` and
`PAYPAL_SANDBOX_BUYER_PASSWORD`. HTTPS is mandatory, with the exact hosts
`www.sandbox.paypal.com` or `sandbox.paypal.com`, no userinfo or nonstandard port.
Main-frame navigation is guarded as well as the initial URL. The configured
`HTTPS_PROXY` is passed to Chromium. TLS verification remains enabled. No traces,
videos, failure screenshots, raw browser exceptions or credential logs are written.
Optional successful screenshots remove credential values/text before capture.
Screenshots belong in a caller-provided directory outside Git.

Install Playwright and Chromium separately (optional test tooling; the runtime
package dependencies are unchanged). `PLAYWRIGHT_CHROMIUM_EXECUTABLE` can select
an already installed Chromium binary. In this container, use
`PLAYWRIGHT_CHROMIUM_EXECUTABLE=/opt/pw-browsers/chromium` because the Python
Playwright package expects a different cached browser revision.

## Scenario world

From `agentpal/`:

```sh
python -m agentpal.demo.paypal_world --report /tmp/paypal-world-offline
PLAYWRIGHT_CHROMIUM_EXECUTABLE=/opt/pw-browsers/chromium \
  python -m agentpal.demo.paypal_world --live --report /tmp/paypal-world-live
python -m pytest -o addopts='' -q -p no:cacheprovider tests/test_paypal_ops_live.py
```

Both modes use the real ASGI `/v1` app, durable identity/approval services,
deterministic policy and wallet ledger. Only PayPal HTTP is replaced by
`httpx.MockTransport` offline. A real sandbox run captures **1.00 USD once**;
creates one additional unapproved order; rejects Shady Deals without provider
I/O; rejects duplicate execute/capture, a forged webhook, and an over-budget
request; then runs the owner reconciliation report. IDs in the saved JSON are
redacted to their last four characters. Screenshots remain outside Git. The
JSON records states, response codes, elapsed time, provider POST counts and
verified outcome count. No approved terms or security checks are patched.

Personas supply merchant names. Today's provider pays its app-linked **Test Store**;
P1 must integrate exact approval-bound payee routing. No claim is made that the
named persona received money. Refunds/payouts await P2/P3 integration. Demo stores
are temporary and are removed on exit; never use this harness as an operational
payment runner. A failed run can leave an unapproved sandbox order. Unknown
payment outcomes require a verified provider read, never a blind re-execution.

## Webhook setup

```sh
python -m agentpal.tools.paypal_webhook_setup --list
python -m agentpal.tools.paypal_webhook_setup \
  --url https://PUBLIC_HOST/v1/providers/paypal/webhooks
```

The setup CLI rejects non-HTTPS, userinfo, nonstandard ports, localhost/private
addresses, and names resolving to any nonpublic address. It lists existing
subscriptions before creation, checks required events, and derives a distinct
`agentpal-webhook-...` request ID from URL and event set. An unknown POST outcome
is never retried automatically: list existing subscriptions first. It prints
only webhook IDs and constant errors, never tokens or provider bodies. It does
not connect to the callback URL. Dispute events are enumerated as CREATED,
UPDATED and RESOLVED; a literal `CUSTOMER.DISPUTE.*` is not sent as an event name.
No webhook was created during P4 because no public listener URL was supplied.
Registration alone does not prove delivery or verification.

Official reference contracts consulted:
[Transaction Search](https://developer.paypal.com/api/transaction-search/v1),
[Webhook management](https://developer.paypal.com/api/webhooks/v1), and
[Orders v2](https://developer.paypal.com/api/orders/v2).

Existing provider OAuth is reused; bounding its OAuth response body is outside
P4 ownership and remains a lead request. New reporting/webhook response reads are
bounded. Generated OpenAPI/Postman artifacts are intentionally excluded from P4
commits: the lead must regenerate them after integrating all workstreams.
