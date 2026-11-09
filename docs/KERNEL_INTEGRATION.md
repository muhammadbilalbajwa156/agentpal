# KERNEL × AgentPal: visit the merchant before approving an AI-agent payment

**Status:** IMPLEMENTED — TESTED (offline platform, SDK and synthetic demo).
**Live browser control:** BLOCKED (network) in this container, as verified by the lead
on 2026-11-02. K3 implements only against the binding `BrowserProvider` and `PageChecker`
protocols; the lead integrates K1's provider and K2's production page checker.

## Why it matters

AgentPal controls financial permissions for external AI agents. Before an owner approves
an agent's purchase, a browser can show what the merchant actually served: product name,
price, stock status and final domain after redirects. A screenshot digest binds the visual
evidence to captured bytes. An optional replay helps the owner inspect the visit.

A page saying “99.99 USD” while the agent requests “250.00 USD”, or a merchant redirecting
to another domain, deserves attention. Browser observations are still untrusted third-party
data: a convincing page can lie. Evidence never approves a payment, lowers risk, changes
terms, bypasses the owner, or blocks a payment because retrieval failed.

## How KERNEL is used

KERNEL provides isolated cloud browsers. The integration uses the official Python SDK
(`kernel==0.139.0`, installed and verified by the lead), with lifecycle creation and
`delete_by_id(own_session_id)` cleanup. K1 owns these calls and their tests.

The selected control surface is [Playwright Execution](https://www.kernel.sh/docs/browsers/playwright-execution):
`browsers.playwright.execute(session_id, code=..., timeout_sec=...)` runs JavaScript beside
the browser and returns JSON. The code has `page`, `context` and `browser` available.
AgentPal supplies a fixed read-only program; the URL is encoded using `json.dumps` as data.
Neither API route accepts code, clicks, typing, login, cookie acceptance or form submission.

[KERNEL replays](https://www.kernel.sh/docs/browsers/replays) can record the visit and
supply a replay URL. Screenshots come from the computer screenshot API. Optional replay
URLs are returned only when supplied by the provider; the demo never invents a live replay.
Treat replay links as sensitive evidence and share them only with authorized viewers.

The [network allowlist](https://www.kernel.sh/docs/info/network-access) distinguishes the
public API on `api.onkernel.com:443` from browser control hosts under `*.kernel.sh` and
`*.onkernel.com` on port 8443. Creating and deleting sessions works here, but this container
blocks the per-browser control host and websocket upgrades. We use REST execute and do
not use CDP websockets. Standard hosting such as Render can support this path with the
required egress; a successful live run there remains to be verified after integration.

## The real platform flow

1. An agent creates `POST /v1/payment-requests` with its claimed amount, merchant domain
   and `product_url`. The URL is persisted separately from approval terms, even when
   Channel3 is disabled. Replaying payment creation never triggers browser work.
2. The authenticated owner calls `POST /v1/payment-requests/{request_id}/page-check`
   (`checkMerchantPage`), optionally with `{"url":"https://merchant.example/product"}`.
   The URL must match the stored merchant host or one of its subdomains, using a label
   boundary. Another owner's request and an unknown ID both return the same 404;
   agent keys receive 403. No stored URL and no supplied URL gives 409.
3. The advisory snapshot is upserted by request ID in `platform_page_checks`. Refreshing
   updates that one row. `GET /v1/payment-requests/{request_id}` and `/v1/approvals`
   include the stored `page_check`. The snapshot carries `advisory_only: true`,
   `authoritative: false`, signals, observed facts, hashes and the untrusted-data notice.
4. Independently, an agent with an explicit registry grant for `research.fetch` can call
   `POST /v1/research/browse` (`browseUrl`) with `{"url":"https://merchant.example/product"}`.
   External agents do not receive this grant by default. Every permission decision is
   audited by the existing registry-backed checker, including denials and revoked grants.
5. Research returns final URL, redirects, HTTP status, title, deterministic product facts
   with source provenance, HTML and screenshot SHA-256 digests, timestamp and optional
   replay URL. It returns no raw HTML, visible-text dump, screenshot bytes or browser handle.
   An injected `PlatformDeps.evidence_store` stores exact captured bytes using the existing
   content-addressed `EvidenceStore`; otherwise only hashes are retained by this route.
6. The owner still approves the exact original terms digest through the existing approval
   route, or rejects. A different merchant or changed amount requires new terms and approval.

SDK methods are `await owner.check_merchant_page(request_id, url=None)` and
`await agent.browse(url)`. A caller should allow at least 45 seconds plus transport overhead
for these requests (for example, construct the SDK client with `timeout=50`).

## Security and failure model

- Both entry points vet public HTTP(S) destinations through `tools.netguard`, including
  DNS checks of **every** resolved address. Credentials and custom ports are refused.
  K1 remains responsible for redirects, subresources, fixed-code execution and cleanup.
- Capture bounds are 2 MiB HTML, 8,000 visible-text characters and 4 MiB screenshot.
  K3 independently refuses oversized research captures and recomputes hashes from bytes.
- Research extraction uses the existing deterministic product extractors. No browser
  content is sent to a model or treated as instructions.
- Optional provider/checker construction failures disable browser tooling without stopping
  platform startup. The auxiliary browse limiter opens lazily, so its storage failure
  cannot disable payment creation; the research route alone fails closed with 503.
- Provider exception text is never returned or logged by these routes. Invalid checker
  output, unavailable components and timeouts become `no_data`. Payment state, risk,
  approval, allowance and terms remain unchanged.
- The overall visit deadline is 45 seconds, including DNS. A shared process cap allows
  four active workers with no queue. A timeout keeps its slot until provider cleanup
  finishes; late page-check results cannot overwrite the stored timeout snapshot.
- Research allows six requests per agent per minute, using the existing SQLite limiter.
  File-backed deployments share admission across workers/restarts; key rotation does not
  reset the agent's quota. Rate rejection returns 429 with `Retry-After`. Storage failure
  for admission fails closed with 503. The concurrency cap is per process; the existing
  single-financial-worker deployment restriction still applies.
- Browser session release is K1's responsibility on every success/error path. K3 never
  lists or deletes sessions and cannot clean up another caller's session.

## Reproduce

```sh
cd agentpal
python -m agentpal.demo.browser_check --end-to-end --offline
python -m pytest -o addopts="" -q -p no:cacheprovider tests/test_kernel_api*.py
ruff check agentpal training tests

# After the lead integrates K1 and K2, on a host with the documented network access:
export KERNEL_API_KEY=...  # environment/secret only; never commit the value
python -m agentpal.demo.browser_check --end-to-end --url https://merchant.example/product
```

The default live URL is `https://example.com/`, useful as a reachability probe; it is not
a product listing. Supply a real public product page for useful price/stock evidence.
Without credentials, integrated components or reachable control hosts, the demo prints
`BLOCKED (network/credentials)` and exits 2. It does not claim a successful live visit.

The offline mode uses a synthetic embedded HTML product fixture and a one-pixel PNG,
with two public IP literals to avoid even DNS traffic. It prints **OFFLINE synthetic
fixture (not live evidence)**. Its price mismatch and redirect signals are precomputed
fixture results, not a substitute for K2's production analysis. It creates no Kernel
session, approves no request and executes no payment.

Example offline output:

```text
Owner sees page price 99.99 USD; advisory level high_risk
Final page URL: https://1.1.1.1/another-shop/office-mouse
Agent research.browse: ok; 7 provenance-tagged facts
Payment state unchanged: awaiting_approval -> awaiting_approval; 1 request(s) await an owner decision.
```

## Validation and integration handoff

`tests/test_kernel_api*.py` covers credential separation, cross-owner 404 isolation,
merchant binding for both stored and supplied URLs, explicit grants/revocation/audit,
private or mixed DNS answers, code rejection, bounded captures, hash integrity, evidence
storage, per-agent limits across rotation, deadline/capacity retention, no late payment
writes, advisory failures, SDK requests, lazy wiring and the real-service offline demo.
Security boundaries are tested fail-first and with deliberately removed controls.

The OpenAPI route and operation-ID maps have only the two authorized additions. The
checked-in OpenAPI JSON and Postman artifacts are intentionally unchanged: the lead must
regenerate them after integrating all workstreams. Until then, drift checks are expected
to report stale generated artifacts. No production-readiness claim is made.
