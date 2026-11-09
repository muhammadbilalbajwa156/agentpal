# P4 live PayPal sandbox evidence

IMPLEMENTED — TESTED against PayPal Sandbox on 2026-11-02. No real money.

Final run: `2026-11-02T19:31:12.471297+00:00`, elapsed **14.297 seconds**.
Command (credentials supplied only by environment):

```sh
PLAYWRIGHT_CHROMIUM_EXECUTABLE=/opt/pw-browsers/chromium \
  python -m agentpal.demo.paypal_world --live --report /tmp/p4-live-world-final
```

The real `/v1` ASGI app, strict owner identity/approval gateway, deterministic
policy, lifecycle and ledger were used. PayPal HTTP and buyer Chromium were live.
Owner approval and the TEST BUYER login were independent actions. TLS verification
was enabled; the configured HTTPS proxy was used. IDs below show only the last four
characters. No passwords, tokens, raw provider bodies or screenshots are committed.

| Scenario | Observed evidence |
|---|---|
| normal_purchase | 1.00 USD; order `…224G`; capture `…360H`; provider_confirmed |
| duplicates | execute 409, capture 409; exactly one capture POST |
| owner_rejects_risky | Shady Deals Outlet: authenticated owner rejection; rejected; zero orders |
| buyer_never_approves | order `…110P`; capture 409; provider_pending |
| forged_webhook | Unsigned forged event naming the actual pending order: HTTP 400; state unchanged |
| over_budget | Monthly budget spent/held: rejected by policy; zero orders |

**Provider POST counts:** 2 creates, 1 capture. **Verified ledger outcomes:** 1.

Reconciliation (`GET /v1/paypal/reconciliation`) returned four caller-owned rows:

| Request suffix | Local state | Result / reason |
|---|---|---|
| `…9d05` | provider_confirmed | pending / paypal_search_pending |
| `…2c57` | rejected | matched / not_submitted |
| `…12a9` | provider_pending | pending / awaiting_paypal_completion |
| `…6a36` | rejected | matched / not_submitted |

The order GET was COMPLETED with an exact completed capture. Search for the
just-created window returned 404; the report correctly kept the row pending as
`paypal_search_pending`. A separate live one-day search returned a documented-style
empty envelope: page 1, total_pages 0, total_items 0, no transaction_details.
That sanitized shape now has a regression fixture. Eventual search indexing after
three hours was not observed during this session.

`python -m agentpal.tools.paypal_webhook_setup --list` returned an empty ID list
against the live sandbox app. No webhook was created. Live signed webhook delivery
remains IMPLEMENTED — UNVERIFIED pending a public HTTPS listener and subscription.

Local evidence (outside Git): `/tmp/p4-live-world-final/paypal-world.json` and
`/tmp/p4-live-world-final/sandbox-buyer-approved.png`. The screenshot is taken only
after clearing input values and replacing credential text. It is browser evidence,
not payment evidence; the verified provider read/capture and ledger are authoritative.

Earlier attempts: the first run stopped before buyer login because Playwright expected
a missing Chromium revision; it created one unapproved order and made no capture.
A subsequent successful live run captured another 1.00 USD (order `…783L`, capture
`…5056`) and exposed the search-lag response shape. The final run above incorporates
that fix. Two sandbox captures total in this workstream, each 1.00 USD; duplicate
requests did not create additional captures. Unapproved sandbox orders are intentionally
left unconfirmed, with no blind retries or automatic cancellation.

Named merchant personas are scenario labels with today's interface. Payments go
to the app-linked Test Store. Exact approval-bound multi-merchant payee routing is a
P1 integration requirement; refunds and payouts await P2/P3. No live claims are made
for those extensions. Demo databases are temporary and do not provide durable
operational recovery across separate scenario runs.

## Regression evidence

- Baseline: 2192 passed / 39 skipped; `ruff check agentpal training tests` clean.
- Initial fail-first: reporting/webhook/buyer tests 27 failed / 1 passed (new modules
  absent); owner reconciliation tests 13 failed (route absent, HTTP 404); scenario
  world tests 2 failed (module absent). Existing assertions were unchanged.
- Live-shape fail-first: `test_reporting_real_empty_search_envelope` and
  `test_reconciliation_recent_search_unavailable_is_labelled_pending` both failed
  on the first implementation, then passed after the lag/empty-envelope fix.
- Mutation tests remove actual guards, run the named regression, and restore source.
  The table below records the final run. Two first-run survivors (order path validation
  and early pagination rejection) were caught after adding tests of dispatcher order
  and the number of requests; downstream guards had masked their removal.

| Removed control | Final mutation | Test |
|---|---|---|
| window_31_days | CAUGHT | `test_paypal_ops_reporting.py::test_reporting_window_fails_before_http` |
| page_size | CAUGHT | `test_paypal_ops_reporting.py::test_reporting_page_size_bounded` |
| page_count | CAUGHT | `test_paypal_ops_reporting.py::test_reporting_page_count_configuration_is_bounded` |
| response_bytes | CAUGHT | `test_paypal_ops_reporting.py::test_reporting_valid_oversized_json_still_refused` |
| redirects | CAUGHT | `test_paypal_ops_reporting.py::test_reporting_no_redirects_or_raw_error_leak` |
| order_identity | CAUGHT | `test_paypal_ops_reporting.py::test_reporting_order_id_is_bound` |
| order_path | CAUGHT | `test_paypal_ops_reporting.py::test_order_id_validation_precedes_internal_request_dispatch` |
| raw_error | CAUGHT | `test_paypal_ops_reporting.py::test_reporting_no_redirects_or_raw_error_leak` |
| search_page_identity | CAUGHT | `test_paypal_ops_reporting.py::test_reporting_untrusted_pagination_metadata_rejected` |
| search_page_cap | CAUGHT | `test_paypal_ops_reporting.py::test_impossible_search_stops_after_first_page` |
| request_count | CAUGHT | `test_paypal_ops.py::test_reconciliation_request_limit_fails_before_provider_io` |
| selected_owner_recheck | CAUGHT | `test_paypal_ops.py::test_reconciliation_rechecks_owner_before_reading_a_selected_reference` |
| owner_query | CAUGHT | `test_paypal_ops.py::test_reconciliation_owner_only_and_empty_owner_never_reads_foreign_orders` |
| reference_provider | CAUGHT | `test_paypal_ops.py::test_reconciliation_foreign_provider_reference_is_not_read` |
| order_unit_count | CAUGHT | `test_paypal_ops.py::test_reconciliation_order_shape_ambiguity_never_matches` |
| capture_count | CAUGHT | `test_paypal_ops.py::test_multiple_captures_are_ambiguous` |
| currency | CAUGHT | `test_paypal_ops.py::test_reconciliation_capture_search_exact_amount_and_currency` |
| amount | CAUGHT | `test_paypal_ops.py::test_reconciliation_amount_mismatch_and_missing_order` |
| completed_lifecycle | CAUGHT | `test_paypal_ops.py::test_reconciliation_lifecycle_mismatch_is_authoritative_despite_search_lag` |
| confirmed_capture | CAUGHT | `test_paypal_ops.py::test_compare_completed_requires_single_real_capture_and_search_success` |
| search_status | CAUGHT | `test_paypal_ops.py::test_compare_completed_requires_single_real_capture_and_search_success` |
| capture_id | CAUGHT | `test_paypal_ops.py::test_compare_completed_requires_single_real_capture_and_search_success` |
| status_allowlist | CAUGHT | `test_paypal_ops.py::test_reconciliation_status_string_is_allowlisted` |
| webhook_https | CAUGHT | `test_paypal_ops_reporting.py::test_webhook_url_refuses_private_and_non_https_before_http` |
| webhook_public_dns | CAUGHT | `test_paypal_ops_reporting.py::test_webhook_url_dns_private_rejected` |
| webhook_subscription_events | CAUGHT | `test_paypal_ops_reporting.py::test_webhook_incomplete_existing_subscription_is_not_accepted` |
| webhook_idempotency | CAUGHT | `test_paypal_ops_reporting.py::test_webhook_setup_scoped_idempotent_and_no_retries` |
| buyer_host | CAUGHT | `test_sandbox_buyer.py::test_buyer_refuses_unsafe_url` |
| buyer_https | CAUGHT | `test_sandbox_buyer.py::test_buyer_refuses_unsafe_url` |
| buyer_userinfo | CAUGHT | `test_sandbox_buyer.py::test_buyer_refuses_unsafe_url` |
| buyer_port | CAUGHT | `test_sandbox_buyer.py::test_buyer_refuses_unsafe_url` |
| buyer_controls | CAUGHT | `test_sandbox_buyer.py::test_buyer_refuses_unsafe_url` |
| buyer_navigation_guard | CAUGHT | `test_sandbox_buyer.py::test_buyer_browser_flow_proxy_scrub_and_failure_redaction` |
| buyer_error_redaction | CAUGHT | `test_sandbox_buyer.py::test_buyer_browser_flow_proxy_scrub_and_failure_redaction` |

## Final validation and delivery

IMPLEMENTED — TESTED:

- Full suite: `python -m pytest -q -p no:cacheprovider`, exit 0, **2255 passed /
  38 skipped** (progress totals; configured double-quiet mode suppresses its count
  summary). Baseline was **2192 passed / 39 skipped**.
- Focused P4 suite: **63 passed**, including live reporting and webhook-list reads.
  Removing sandbox client credentials from a subprocess environment gives **1 clean
  skip** for `test_paypal_ops_live.py`. Buyer tests after adding browser/credential
  isolation: **10 passed**; all seven buyer mutations caught again.
- `ruff check agentpal training tests` and required `ruff check agentpal tests`:
  clean before/after.
- Final full suite and generated-artifact checks ran in `/tmp/m6-p4-review/agentpal`,
  a clone of the starting commit with the identical P4 implementation/test files.
  `python -m agentpal.platform.openapi --write`, `python -m postman.generate`,
  `python -m agentpal.platform.openapi --check`, and
  `python -m postman.generate --check` all succeeded there.
- The original checkout's OpenAPI drift check correctly reports the new route is
  absent from its unchanged generated spec. Generated OpenAPI/Postman artifacts
  are excluded from the commits, as required; integration must regenerate them.
- Actual injected client/buyer credential values were scanned against all owned
  additions without displaying those values: none were found. Existing assertions
  were not edited; API route maps only gain the reconciliation entry.
- Final mutation result: **34 caught / 0 survived / 0 invalid**. The seven buyer
  mutations were repeated with fake credentials and a browser-launch tripwire.

Review branch: `m6-p4-review`, based on
`43b3d3acb9f409bf92f0de324139500c411698ce`. Delivery: `/tmp/m6-p4.bundle`.
Implementation commits: `d24020e` (operations/reporting/SDK), `055d616`
(buyer/world), `5576e05` (offline browser-test isolation); documentation and the
append-only team memory entry are delivered together in the final commit.

Files created: `agentpal/providers/paypal_reporting.py`,
`agentpal/platform/paypal_ops.py`, `agentpal/demo/sandbox_buyer.py`,
`agentpal/demo/paypal_world.py`, `agentpal/tools/paypal_webhook_setup.py`,
`tests/test_paypal_ops.py`, `tests/test_paypal_ops_reporting.py`,
`tests/test_paypal_ops_live.py`, `tests/test_sandbox_buyer.py`,
`tests/test_paypal_world.py`, `docs/PAYPAL_OPERATIONS.md`, and this evidence file.
Files changed: `agentpal/platform/paypal_ops_router.py`,
`sdk/python/agentpal_sdk/client.py` (OwnerClient append only),
`tests/test_api_v1_invariants.py` and `tests/test_api_v1_openapi.py` (route-map
entries only), and `docs/memory/PROJECT_MEMORY.md` (append only).

Lead integration requests:

1. Regenerate OpenAPI/Postman after merging P1–P4 using the commands above.
2. Integrate P1's exact approval-bound payee path; then extend this world's request
   payload only after its API contract is merged. P2/P3 refund/payout reconciliation
   must resolve owned references, not account-wide search rows.
3. Outside P4 ownership, bound the OAuth response in
   `providers/paypal_sandbox.py:PayPalSandboxProvider.access_token`: stream the
   token response, stop after at most 1 MiB, validate the token envelope, preserve
   TLS/redirect refusal, and emit only a constant sanitized ProviderError. P4 reuses
   the existing OAuth implementation; its new record/webhook responses are bounded.
4. Provide a public HTTPS listener before registering a webhook. Registration and
   real signed delivery remain unverified; no subscription was created here.
