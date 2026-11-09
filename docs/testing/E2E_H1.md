# H1 request-edge defensive verification — 2026-11-05

IMPLEMENTED — TESTED (offline regressions and reproducible findings; product fixes are
DESIGN ONLY). Base: `ab24447fd1af6013e5403264a1993c3bcac5c9e9`.

Only `tests/e2e/h1/` and this report are added. Product code, existing tests, generated
contracts, shared memory and the pinned model are unchanged. Every H1 socket targets an
H1-owned child on `127.0.0.1:8201`. Databases and temporary TLS material are isolated below
`/tmp/m8-h1/` and removed by the harness. No PayPal, Channel3, Kernel, GitHub or other
external API is called by H1. Owner credentials are bootstrapped with the documented
`new-owner` CLI for TCP tests; plaintext output is captured in memory. Server children
inherit an allowlist of basic process variables and explicit local settings, without
provider credentials. In-process tests use `build_platform(..., environ={})`.

## Findings

| ID | Severity | Classification | Reproducible result | Status |
|---|---|---|---|---|
| E2E-H1-2 | Medium (conditional on public webhook exposure); lower impact on default loopback deployment | CONFIRMED | An incomplete unauthenticated POST has no body-read deadline; both legacy and v1 sockets remain open after 6.5 seconds of inactivity, ahead of general peer admission | IMPLEMENTED — TESTED |
| E2E-H1-1 | Low | CONFIRMED | About 1.2 KB of nested authenticated JSON raises `RecursionError`, returning 500 instead of rejecting invalid input | IMPLEMENTED — TESTED |

No critical/high finding, authentication bypass, unauthorized approval, cross-tenant
access, false financial completion or credential disclosure was demonstrated in H1.
H1 is request-edge verification, not a repeat of every financial lifecycle test.

### CONFIRMED E2E-H1-1 — recursive credential scan crashes a request

**Evidence:** `platform/models.py:38–44` recursively scans every dictionary/list before
schema validation, through `_In._no_credentials` at lines 82–86. A JSON object containing
600 nested arrays is within the 32,768-byte cap and within the JSON decoder's accepted
depth. The recursive generator-based scan exhausts the Python stack. The actual CLI
server and real-service in-process app return `500 {"error":"internal_error"}` and
record `error_type=RecursionError`. `/health` remains 200, and the process remains alive.
This is a request crash, **not demonstrated process termination**. A malformed body on
`/v1/payment-requests` reaches the same bug with a valid agent key. Without credentials,
both endpoint families still return 401 before JSON validation.

**Severity rationale:** local API access and a valid credential are required; small,
cheap requests cause exception handling and request failure, without demonstrated
confidentiality or financial-integrity loss. CVSS-like baseline:
`AV:L/AC:L/PR:L/UI:N/S:U/C:N/I:N/A:L` (3.3). Service-wide denial of service was not tested.

**Automated proof**, from `agentpal/`:

```sh
python -m pytest -o addopts='' -q -p no:cacheprovider --runxfail \
  tests/e2e/h1/test_body_edges.py -k 'deep_json_returns or deep_json_real_cli'
```

Expected today: **three failures**, each at `assert response.status_code in {400, 422}`,
with actual status 500. Normal runs retain strict `xfail(reason="E2E-H1-1")`.

**Exact manual reproduction** (the helper starts/stops its own fresh CLI server and
bootstraps its own owner; do not run alongside another H1 instance):

```sh
cd agentpal
python - <<'PY'
from pathlib import Path
import sys
sys.path.insert(0, str(Path('tests/e2e/h1').resolve()))
from conftest import local_server
with local_server() as server:
    body = b'{"name":"H1 nested","metadata":' + b'['*600 + b'0' + b']'*600 + b'}'
    result = server.client.post('/v1/agents', content=body, headers={
        'Authorization': 'Bearer ' + server.owner, 'Content-Type': 'application/json'})
    print(result.status_code, result.json()['error'])
    print('health', server.client.get('/health').status_code)
PY
```

Actual: `500 internal_error`, then `health 200`. No credential is printed.

**Minimal suggested fix — DESIGN ONLY:** replace the unbounded recursive scan with an
iterative traversal that enforces a conservative depth/node limit and raises a validation
error on excessive complexity. Retain credential rejection at every traversed key/value;
do not disable the scanner. Reject pathological JSON before business operations. Then
remove the marker and keep these three regressions as ordinary passing tests.

### CONFIRMED E2E-H1-2 — unfinished bodies can pin connections before authentication

**Evidence:** `api.py:149–160` awaits each next ASGI body chunk without an inactivity or
total deadline. `BodySizeLimit` runs outside `SecurityGate` (`create_app`, middleware
installation), so general peer/token limits and auth have not executed while a permitted
size body is incomplete. The CLI does not configure a request-body deadline. Uvicorn's
keepalive timeout does not terminate these unfinished requests. One socket per case,
`Content-Length: 2`, and only the first byte `{` are sufficient. No response or close
occurs within 6.5 seconds, on both `/assess` and `/v1/agents`, with no credentials.
The tests close the socket themselves; health stays responsive. No load/exhaustion test
was run and no server-wide outage is claimed.

The default loopback gate prevents a direct remote attack on the general API. With the
documented opt-in public webhook listener, the same body-buffering middleware is reached
after HTTPS/Host/public admission but before signature verification. Public rate limits
run before the read (a guard that held), but impose no connection lifetime; fresh admission
windows can accumulate unfinished requests. That remote impact follows from the shared
middleware and is an **inference**, not a live public-delivery or load test.

**Severity rationale:** no credentials, interaction or complex payload are required;
only availability is implicated. Conditional public-webhook CVSS-like baseline:
`AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:L` (5.3). Default loopback equivalent is 4.0
(`AV:L`); a high-availability impact was not demonstrated.

**Automated proof**, from `agentpal/`:

```sh
python -m pytest -o addopts='' -q -p no:cacheprovider --runxfail \
  tests/e2e/h1/test_body_edges.py -k incomplete_unauthenticated
```

Expected today: **two failures**, `no close or rejection after 6.5s of body inactivity`.
Normal runs retain strict `xfail(reason="E2E-H1-2")`. The 6.5-second probe is a bounded
test expectation, **not a timeout promised in DEPLOYMENT.md**. Source inspection supplies
the evidence that no application body deadline exists; the probe alone establishes only
that inactivity lasts beyond its observation window. If the lead chooses another bounded
deadline, update the new test's deadline together with that explicit policy decision.

**Exact manual reproduction:**

```sh
cd agentpal
python - <<'PY'
from pathlib import Path
import socket
import sys
sys.path.insert(0, str(Path('tests/e2e/h1').resolve()))
from conftest import local_server
with local_server() as server:
    with socket.create_connection(('127.0.0.1', 8201), timeout=2) as connection:
        connection.sendall(b'POST /assess HTTP/1.1\r\nHost: 127.0.0.1:8201\r\n'
            b'Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{')
        connection.settimeout(6.5)
        try:
            print('server replied or closed', bool(connection.recv(4096)))
        except socket.timeout:
            print('still waiting after 6.5 seconds')
    print('health', server.client.get('/health').status_code)
PY
```

Actual: `still waiting after 6.5 seconds`, then `health 200`. Replace `/assess` with
`/v1/agents` for the second reproduction.

**Minimal suggested fix — DESIGN ONLY:** enforce an inactivity timeout per receive and
a total body-read deadline; send a sanitized 408 and close on expiry. Preserve streaming
size caps and remote-peer rejection. Consider general Host/peer admission before buffering
so unfinished local bodies consume the normal admission budget, without counting a request
twice. Public webhook admission already precedes buffering and must remain there.

## Guards that held

All rows below are IMPLEMENTED — TESTED. Test names refer to `tests/e2e/h1/`.

| Boundary / attack | Evidence |
|---|---|
| Published protected operation coverage | OpenAPI-derived sweep: all 48 protected legacy/v1 method/path operations return 401 without credentials; intentional health/webhook exceptions excluded |
| Malformed auth | 21 malformed values × four route families: missing scheme/token, Basic, tab/space confusion, Unicode/confusables/NUL/high bytes, invalid/prefix-only/uppercased credentials, 8 KiB values → sanitized 401 with `WWW-Authenticate` |
| Duplicate auth | Valid+invalid, invalid+valid, and identical valid duplicate headers all return 401 for owner, agent and legacy; raw TCP also refuses a valid owner mixed with invalid auth |
| Credential separation | Real issued owner/agent credentials on wrong-type routes → 403; legacy/v1 crossover, prefix substitution and credential uppercasing → 401; case-insensitive Bearer scheme remains valid |
| URL and method ambiguity | Encoded slashes, encoded v1, dot segments, double encoding, trailing/double slashes, NUL and rewrite/method-override headers do not bypass auth; valid agent still gets 403 on owner registration, POST health stays 405 |
| Host / proxy trust | Unknown/suffix/userinfo-style/numeric/missing/duplicate Host refused; supplied forwarded Host cannot repair it. Real CLI ignores forwarded remote IP/Host/proto and omits Server header |
| Loopback enforcement | Synthetic remote IPv4/private/mapped/non-IP peers remain 403 despite forwarded loopback claims; default-off exact HTTPS webhook remains 403 |
| Rate admission | Documented 300 peer / 60 credential budgets: next request 429 + bounded `Retry-After`; bad credentials consume peer budget; case/forwarding spoofing and reopening the app do not reset persisted counters |
| Owner lockout | Five malformed TOTP values produce durable five-minute lockout; reopening identity storage, a correct code and restarting enrollment cannot bypass it; a fresh valid code works after controlled clock expiry; ordinary token remains non-MFA |
| Body caps | Actual CLI rejects declared and chunked 32,769-byte bodies with 413 for legacy/v1 without auth; ASGI stream stops after crossing cap without reading another chunk; disconnect returns cleanly |
| HTTP framing | Negative/text/signed/conflicting/NUL Content-Length and contradictory duplicates → 400; identical repeated lengths are safely normalized by h11 and still require auth |
| JSON / numeric abuse | Truncated/scalar/list/null JSON, NaN/Infinity/overflow, 5,000-digit integer, lone surrogates and invalid UTF-8 → 400/422; shallow nesting rejected 422; legacy nonfinite/huge/bool/null amounts rejected |
| Content type | JSON presented as text/plain, octet-stream, form or multipart rejected 422 on legacy and v1 |
| Error/docs/storage leakage | Error values do not echo the synthetic attack marker; development docs/schema have no issued credentials or local DB paths; production docs return no document, including authenticated legacy 404; actual child logs and DBs lack issued plaintext credentials |
| SQL/template/script/log-like input | Accepted description remains unchanged JSON data, including script/template/SQL/newline text; registration/listing and health continue working; no server interpretation demonstrated |
| Public webhook ingress | Exact HTTPS POST preserves raw bytes/headers but a bearer cannot replace signature verification; rejecting synthetic verifier gives 400 and sink is never called |
| Public webhook narrowness | Alternate methods, trailing/encoded/double-encoded/dot/query paths, other API/docs/health paths, root-path remount and websocket attempts fail before input reads; websocket closes 1008 |
| Public Host/TLS/limits | Forwarded scheme/Host do not create trust; default 32 KiB public cap precedes verifier; public peer admission 31 returns 429 before verifier, with no unverified event reaching sink |
| TLS configuration / transport | Missing/unloadable/mismatched pairs, no-TLS public bind, insecure auth public bind, wildcard/URL/port/path public hosts refused; real own CLI TLS listener works with trusted temporary CA, rejects plaintext and untrusted certificate handshake |

### Parsing observation, not a confirmed exploit

Duplicate JSON member names are accepted with last-value semantics by the current JSON
decoder. A bounded manual in-process check of `{"name":"H1 first","name":"H1 last"}`
returned 201 with `H1 last`; changing the final name to `false` returned 422 (also covered
by the malformed-JSON regression). Thus duplicate-member **rejection itself is not a held
guard**. No alternate parser, differing approval interpretation or exact-terms bypass
was demonstrated. This is a suspected parser-ambiguity/policy gap, not an additional
confirmed integrity finding. Lead may choose strict duplicate rejection at the common
decoder; do not describe current parsing as rejecting all unusual JSON.

## Verification and delivery

- Baseline required full command: **2,982 passed, 42 skipped, 9 failed, 48 errors**,
  486.23 seconds. Baseline `ruff check agentpal training tests`: passed.
- Final H1: **253 passed, 5 xfailed**, 31.45 seconds. No H1 skips or ordinary failures.
  The only strict xfails are the five cases attached to the two confirmed findings.
- Fail-first command with all finding cases and `--runxfail`: **5 failed, 48 deselected**,
  15.08 seconds. Every failure is the intended status/deadline assertion, not harness setup.
- Final required `ruff check agentpal tests`, and broader `ruff check agentpal training tests`:
  passed. `git diff --check`: passed.
- Final complete suite: **3,292 passed, 42 skipped, 5 xfailed, zero failures/errors**,
  528.822 seconds, exit 0. JUnit records 3,339 cases, including all 258 H1 cases.
  Its 47 skipped records comprise 42 ordinary skips plus the five strict H1 xfails.
  The initial fixed-port/environment failures did not recur in this fresh run; no
  product code or existing test was changed to obtain that result.

The initial full run exposed an occupied inherited T2 port (8102); its harness can reuse
another listener before checking that its new process bound successfully. After detecting
this, H1 does not contact that listener. Network namespace isolation was attempted and
refused by the environment (`uid_map: Operation not permitted`). The final full run uses
an external, temporary pytest setup hook at `/tmp/m8-h1-runner/h1_fullsuite_guard.py` to
fail the T2 `world` fixture before it can start or send traffic if port 8102 is occupied.
It changes no product code or inherited assertion, and would report a setup error rather
than pretending a pass/skip. Port 8102 was free for the completed final run, so the hook
refused no fixture and all inherited T2/T4 checks ran successfully. Full output/JUnit are `/tmp/m8-h1-final-full.log` and
`/tmp/m8-h1-final-full.xml`. Command:

```sh
cd agentpal
PYTHONPATH=/tmp/m8-h1-runner PYTEST_PLUGINS=h1_fullsuite_guard \
  python -m pytest -q -p no:cacheprovider --junitxml=/tmp/m8-h1-final-full.xml
```

Shared Git metadata is read-only (`index.lock: Read-only file system`). Review commits
therefore live in isolated `/tmp/m8-h1-review`, branch `m8-h1-review`, based on the exact
start commit. Test commit: `477759a`. Bundle: `/tmp/m8-h1.bundle`; final documentation
commit and bundle verification are recorded in the delivery response.

## Requests to the lead

1. Remediate E2E-H1-1 in `agentpal/platform/models.py` and E2E-H1-2 in `agentpal/api.py`
   (plus a documented timeout setting/policy if desired). Those files are outside H1's
   ownership. Fixes above are DESIGN ONLY; no production control was modified.
2. Harden the T2 harness to refuse occupied ports before contacting them, matching
   the existing T4/H1 preflight. The final full gate is green with free ports and the
   temporary safety hook; the initial environment failures were not product fixes.
3. Decide whether duplicate JSON members must be refused and document that behavior.
4. Public signed PayPal delivery and remote real-network ingress remain BLOCKED for H1:
   public hosting and third-party calls are outside these probes. Synthetic-peer ingress
   and local trusted TLS tests do not establish live PayPal delivery.
5. Append the short memory entry below. The latest assignment allows **only new files in
   the two H1-owned paths**, so H1 did not edit the pre-existing shared memory file despite
   the general AGENTS.md append protocol:

```text
### 2026-11-05 M8 H1 — request-edge defensive verification
- Done: 253 guards pass; five strict xfails reproduce nested-JSON 500 and missing body deadlines (477759a; E2E_H1.md).
- Decisions/traps: no product/existing-test edits or external API calls; final full suite 3292 passed/42 skipped/5 strict xfails; initial port collisions cleared, T2 safety hook retained; shared Git is read-only, deliver m8-h1-review bundle.
- Next: lead fixes credential-scan complexity and body timeouts, reviews duplicate-key policy, reruns full gate with free ports, and imports review commits.
```
