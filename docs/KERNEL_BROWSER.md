# Kernel × AgentPal: read-only browser evidence before owner approval

Status: **IMPLEMENTED — TESTED** offline, including the official Python SDK with
MockTransport, execution of the fixed program in Node, and real local Chromium.
Live browser control: **BLOCKED (network)** in this container (2026-11-02).
This document covers K1's provider. Page-check analysis, API/SDK exposure and
financial wiring belong to K2/K3/the lead.

AgentPal lets an external agent or owner visit a merchant page before deciding
whether to approve exact payment terms. Kernel provides an isolated browser;
AgentPal records the observed final URL, redirect chain, status, title, rendered
HTML, visible text and screenshot. SHA-256 hashes identify the stored HTML and
PNG bytes, with a timezone-aware UTC capture time and provider session ID.
Retrieval evidence does not establish that a price, product or merchant is true.

The provider implements the binding `tools/browser_contracts.py` protocol. It
returns a complete `BrowserCapture` or a sanitized `BrowserError`. It has no
financial dependencies and cannot approve, alter terms, lower risk or release a
hold. The page checker must turn browser failures into advisory `no_data`, without
blocking payment creation or execution. Page strings remain untrusted data;
AgentPal never evaluates them as Node/Python code or sends them to a model as
instructions. The isolated browser runs ordinary merchant scripts to render the
page; those scripts do not acquire AgentPal credentials or financial authority.

## Configuration and REST flow

```python
from agentpal.tools.kernel_browser import KernelBrowserProvider

provider = KernelBrowserProvider()  # KERNEL_API_KEY from the environment
if provider.configured():
    capture = provider.capture("https://example.com/", timeout_s=15)
```

A constructor key override, an `httpx.Client` (including MockTransport), or a
Kernel SDK client can be injected. Imports remain usable without the optional
Kernel package; a configured visit without that package raises `not_configured`.
The implementation was checked against installed `kernel==0.139.0`. Declaring
that dependency in lead-owned requirements/package files remains the lead's task.

1. Validate the original URL with `tools.netguard.vet_url(allow_http=True)`:
   public HTTP(S), no userinfo, default port, at most 2048 characters, every DNS
   address public. Reject backslashes/control characters to avoid differing URL
   parser interpretations. A refused target makes **zero Kernel calls**.
2. Acquire bounded admission and create a fresh browser, with a 30-second idle
   timeout. Do not attach a profile, vault, start URL or credentials.
3. Call `browsers.playwright.execute(session_id, code=..., timeout_sec=...)` over
   REST. Its code is one constant program preceded only by a `json.dumps` data
   declaration containing the URL and navigation timeout. No caller supplies code.
4. Create a fresh context, block service workers/downloads and websockets, and
   navigate with `waitUntil: "domcontentloaded"`. The script never clicks, types,
   logs in, submits a form or accepts cookies. It permits only GET requests and
   explicitly strips authorization and cookies (including APIRequestContext's
   cookie jar), and removes Set-Cookie before fulfilling responses to Chromium.
5. Validate remote DNS and public addresses before each navigation/resource fetch.
   **Ordinary Playwright routing does not intercept automatic redirect hops.**
   Fetch responses with `maxRedirects: 0`; replace a redirect with an empty inert
   document, validate its destination and perform a new navigation. This preserves
   the actual final browser URL. Bound navigation to 20 visits. Subresource/frame
   redirects are refused. Revalidate all returned evidence URLs with Python
   netguard before accepting the result.
6. Read bounded DOM/text/title, then call `computer.capture_screenshot`. Always
   call `delete_by_id` for the session allocated by this visit in `finally`.
   Never list sessions or delete another visit's session.

Replay is intentionally omitted (`replay_url=None`). Recording, stopping and
listing/processing replays adds control calls and can return sensitive viewer
URLs. No replay/live-view URL is invented or exposed as a substitute. The
assignment permits this fallback; an opt-in recording lifecycle can follow a
separate review.

## Bounds, deadlines and error handling

| Boundary | Enforced bound |
|---|---|
| Rendered HTML | 2 MiB UTF-8 bytes; explicit truncation flag; hash of stored bytes |
| Visible text / title | 8000 / 300 characters |
| Screenshot | 4 MiB decoded response; PNG signature required; oversized/invalid images fail capture |
| SDK create/cleanup JSON | 64 KiB decoded body |
| Execute JSON envelope | 12.25 MiB decoded body, accommodating JSON escaping of bounded HTML |
| Redirects / URL | 20 navigation visits; at most 2048 characters per URL |
| Navigation | at most 10 seconds, bounded by remaining overall time |
| Overall call | caller's `timeout_s`, capped at 60 seconds, including admission, DNS and cleanup wait |
| Sessions | process-wide maximum 2; `max_concurrent_sessions=1` can lower an instance's ceiling |

An HTTP response hook bounds **decoded** bytes before the SDK parses them,
including compressed responses. It closes bodies immediately on refusal; HTTP
error bodies are never read. The hook caches its bounded read in httpx's internal
`_content` slot because public `read()` has no size parameter. This small SDK/httpx
integration is tested against real SDK calls; review it on dependency upgrades.
SDK retries and HTTP redirects are disabled/refused, avoiding duplicate sessions,
credential forwarding and deadline extension.

A bounded worker owns admission, navigation and cleanup. The caller stops waiting
at the overall deadline even when a resolver, injected transport or slow-dripping
response ignores socket timeouts. The worker retains its slot until its cleanup
attempt ends. A late create response is still parsed (bounded to 64 KiB) so its
own session ID can be deleted; it never proceeds to execute after expiry.
Cleanup uses a two-second HTTP timeout and retains the original body deadline
or a two-second grace period, whichever ends later. This permits cleanup after
the caller has returned a timeout. Consequently, **cleanup may finish after the caller's deadline**. This
is necessary to avoid abandoning a late successful create. A stuck worker retains
its slot, limiting background work rather than allocating more sessions.

| Failure | BrowserError.kind |
|---|---|
| Missing key/SDK | `not_configured` |
| Original or remote navigation target refused | `blocked_target` |
| Kernel HTTP 401/403 | `auth` |
| Kernel HTTP 402/429 | `quota` |
| Overall/socket/execute timeout | `timeout` |
| Connection/5xx/provider outage | `unavailable` |
| Malformed execute output, invalid evidence or excess body/image size | `invalid_response` |

Merchant HTTP status codes are observations, not Kernel authentication errors.
If cleanup also fails, the original failure classification is preserved; a cleanup
failure after an otherwise successful visit still fails the capture. Malformed
timeout arguments also become sanitized errors without allocating a session.
No upstream exception message, error body, stderr/stdout, or credential is included
in public errors. SDK/httpx/httpcore logging is filtered only on capture workers:
SDK DEBUG output can otherwise reveal per-browser JWT URLs. Other threads' logs
are preserved. Provider repr is safe; captures reject literal configured-key
reflections. Hashes identify bounded evidence, not merchant authenticity.

## Network finding and honest deployment limits

The lead established that this container can create/list/delete through
`api.onkernel.com:443`, but its proxy cannot upgrade websockets or reach the
per-browser host on port 8443. K1's live REST probe reproduced **BLOCKED (network)**
and attempted deletion of only its own created session. REST execute avoids a
local websocket, but the installed SDK still routes execute/computer controls to
the per-browser HTTPS host. REST alone does not bypass that network restriction.
A normal server such as Render must allow those HTTPS destinations; successful
live capture on that server remains **IMPLEMENTED — UNVERIFIED**.

Local and remote DNS checks are not DNS/IP pinning. DNS can differ between the
application, browser VM and fetch connection; rebinding between check and connect
remains a deployment trust boundary. Deploy with provider/network egress controls
that independently deny private, link-local and metadata destinations. The remote
IPv6 check deliberately permits only global-unicast space outside special ranges,
which can reject some otherwise public transition/special-use addresses.

A create accepted by Kernel whose response is lost or has no valid nonsecret session ID
cannot be individually deleted safely. Deletion can itself fail during an outage.
The short idle timeout is a fallback, not proof of deletion; no other sessions
are enumerated or swept. The SDK documents an inactivity check interval of five
seconds, so expiration is approximate. The process limit cannot account for a
provider session whose creation/deletion outcome is unknown, or for other
processes using the same account.

GET requests can have merchant-defined side effects; no browser can establish
that a merchant implements GET safely. Read-only here describes AgentPal's allowed
operations. Blocking writes, cookies, subresource redirects and service workers
can prevent some merchant pages from rendering fully. Evidence is advisory and
captures are not atomic: the DOM can change between text extraction and screenshot.

## Verification and fail-first evidence

```sh
cd agentpal
python -m pytest -o addopts="" -q -p no:cacheprovider tests/test_kernel_browser*.py
ruff check agentpal training tests
# With KERNEL_API_KEY provided securely in the process environment:
python -m pytest -o addopts="" -q -p no:cacheprovider tests/test_kernel_browser_live.py -rs
```

Final recorded results (2026-11-02): baseline **2189 passed / 42 skipped**;
final full suite **2315 passed / 43 skipped**; required ruff **0 errors**. The
provider group has **126 offline passes / 1 credential-gated skip**. With the
live key sourced separately, that probe reports **1 BLOCKED (network) skip**.
The three existing certificate deprecation warnings remain outside K1 ownership.

The live probe always uses `try/finally: delete_by_id(own_session_id)` and never
lists sessions. It skips missing credentials separately from unreachable browser
control. Assertion/parse/auth/quota failures are failures, not network skips.
Node execution tests skip if Node is absent; the real Chromium transport test
skips when this container's local Node/Playwright/Chromium installation is absent.
The mocked SDK tests spend no browser credits.

Tests were first collected before the module existed and failed with
`ModuleNotFoundError`. More significantly, 18 controlled boundary-removal mutations
were run against the completed implementation, each restored afterward. All were
caught by assertions (no surviving mutation): initial target checks (14 failures),
URL JSON escaping (1), cleanup (1), stored HTML cap (1), streamed body cap (1),
DEBUG secrecy (6), process admission (1), overall deadline (1), mixed/private DNS
(3), automatic redirect following (11), cookie-jar suppression (1), write-method
blocking (3), remote HTML cap (1), remote DNS checking (7), key-reflection rejection
(2), late-create cleanup (1), unread HTTP error bodies (1), returned-URL vetting (1).
The late-create regression also failed on the initial implementation before its
fix. Eight additional edge cases were added after the first implementation commit:
7 failed and 1 passed before fixes, then all passed. They cover preserving a
primary error when cleanup also fails, refusing a reflected credential as a
session ID before it reaches a request path, execution-deadline error envelopes,
and malformed timeout inputs. Commit messages record this evidence for review.

## Official references

- [Kernel documentation](https://www.kernel.sh/docs): isolated cloud browsers.
- [Playwright execution](https://www.kernel.sh/docs/browsers/playwright-execution):
  server-side JS, returned result, execution timeouts and executors.
- [Create browser session](https://www.kernel.sh/docs/api-reference/browsers/create-a-browser-session):
  session creation and idle timeout; checked against installed SDK 0.139.0.
- [Computer controls](https://www.kernel.sh/docs/browsers/computer-controls): screenshot API.
- [Network access](https://www.kernel.sh/docs/info/network-access): control-plane
  HTTPS and per-browser domains/ports.
- [Egress allowlists](https://www.kernel.sh/docs/browsers/egress-allowlists): provider
  network policy and its limitations, including private-route bypasses.
- [Replays](https://www.kernel.sh/docs/browsers/replays): start/stop/list/download
  lifecycle; viewer URLs are sensitive.
