# Backend deployment requirements (Mission 3)

Status: IMPLEMENTED — TESTED OFFLINE. This is a sandbox backend, not a
production-readiness claim. No live money or production PayPal is supported.

## Supported topology

Run one financial worker on one host, using separate durable identity, wallet,
ledger and rate-limit files on local storage. Start/restart with financial traffic
quiescent. RiskService construction declares a restart: pending attempts become
reconciliation_required even before their deadline. They remain held and cannot
be resubmitted. Multi-worker rolling financial startup is DESIGN ONLY. The rate
limiter itself is tested across independent spawned processes; that does not make
rolling lifecycle startup supported.

Serve TLS directly in Uvicorn for external webhook delivery. A TLS-terminating
proxy is NOT supported in this phase. Forwarded, X-Forwarded-For and
X-Forwarded-Proto are never trusted; the socket peer and ASGI scheme decide access.
A local proxy could turn remote peers into loopback peers and defeat the intended
network boundary. Keep owner/agent API access local.

## Settings

| Environment setting | Meaning/default |
|---|---|
| AGENTPAL_ENV | development or production; production adds the existing startup guards |
| AGENTPAL_AUTH_MODE | required for public webhooks; insecure development mode is loopback-only |
| AGENTPAL_DB_PATH | durable ledger path |
| AGENTPAL_IDENTITY_DB_PATH / AGENTPAL_WALLET_DB_PATH | durable identity/wallet paths; the platform factory defaults to ledger-path siblings |
| AGENTPAL_API_TOKENS_FILE | existing legacy hashed-token configuration; still required in production mode |
| AGENTPAL_RATE_LIMIT_BACKEND | sqlite by default; memory requires explicit development configuration |
| AGENTPAL_RATE_LIMIT_DB_PATH | default: ledger path plus .ratelimit.db; use a dedicated local file |
| AGENTPAL_RATE_LIMIT_PER_PEER / AGENTPAL_RATE_LIMIT_PER_TOKEN | 300 / 60 per 60-second sliding window |
| AGENTPAL_TLS_CERTFILE / AGENTPAL_TLS_KEYFILE | certificate chain and unencrypted private-key PEM paths; both required together |
| AGENTPAL_PUBLIC_WEBHOOK_ENABLED | false by default; true explicitly enables the one remote route |
| AGENTPAL_PUBLIC_WEBHOOK_HOSTS | comma-separated exact public DNS Host names; no wildcard, URL or port |
| AGENTPAL_WEBHOOK_RATE_LIMIT_GLOBAL / AGENTPAL_WEBHOOK_RATE_LIMIT_PER_PEER | 120 / 30 per 60 seconds, in separate shared namespaces |
| AGENTPAL_MAX_REQUEST_BYTES | 32768 default; public webhook cap is min(this value, 512 KiB) |
| AGENTPAL_REQUIRE_MFA_FOR_APPROVAL | false default; lead integration must propagate Settings.require_mfa_for_approval into PlatformDeps.require_mfa_for_approval |
| AGENTPAL_MFA_ENCRYPTION_KEY | URL-safe base64 encoding of 32 random bytes; provided through environment secrets only |

CLI TLS paths override their environment counterparts:

```sh
python -m agentpal serve --platform --host 0.0.0.0 \
  --ssl-certfile /run/agentpal/tls/cert.pem \
  --ssl-keyfile /run/agentpal/tls/key.pem
```

A non-loopback bind requires TLS and the explicit public-webhook switch.
Invalid/mismatched certificate material refuses startup. All existing production
guards remain: required authentication, legacy token configuration, durable ledger,
operator spend policy, model integrity/read-only model storage and loopback names
in the general Host allow-list. Do not change the pinned model to satisfy startup.
TLS files, MFA encryption key, database directories, journal files and backups need
operator-controlled permissions. Private keys/seeds/tokens must never enter git.

## Public webhook contract

Only POST at exactly platform.router.WEBHOOK_PATH
(/v1/providers/paypal/webhooks) can cross the remote-peer gate. Exposure defaults
OFF. It requires the mounted platform, a sandbox provider with verify_webhook and
an event sink with apply_provider_event, HTTPS and an approved Host. General Host
settings do not open other routes. Encoded paths, trailing slashes, extra path
segments, query variants, alternate methods, root-path remounts and websockets
remain denied to remote peers. Remote websocket attempts close with code 1008.

Shared global and socket-peer limits run before body buffering. The streaming
body cap applies with or without Content-Length. Bodies and signature headers
are passed unchanged to A1's route/adapter. Bearer credentials grant nothing on
this path. Verification failures never reach the sink; the existing route also
checks verified=True and matching sandbox provider identity.

A1 owns PayPal signature/certificate-chain validation. Local ingress tests are not
proof of PayPal delivery or of CA-chain validation. Public acceptance requires the
lead to integrate/review A1's adapter changes. Do not configure a broad API proxy.

## Shared limiter and availability

SQLite BEGIN IMMEDIATE serializes admission and audit-throttle updates across
workers on the same host. Each file-backed operation uses a fresh connection;
counters persist across process restart. Namespace configuration must match
across workers. Accepted timestamps and audit windows are stored without bearer
secrets. Active identities are not evicted to make space; saturation refuses new
identities conservatively and bounds audit state.

Store lock timeout, read-only/corrupt/unavailable storage and policy mismatch
return 503 with rate_limit_unavailable instead of admitting traffic. A failure
at limiter initialization installs a permanently refusing limiter; restore the
store/configuration and restart to recover that instance. Later temporary lock
contention can recover on a subsequent request. There is no memory fallback.
This intentionally trades availability for reliable admission limits. Shared
storage remains an availability dependency even for health requests.

This is not a multi-host/NFS/Redis limiter. SQLite serializes writes and synchronous
commits cost I/O; no throughput or high-availability guarantee is claimed. Wall
clock time is clamped to a persisted high-water mark on backward steps. Forward
jumps can expire windows early: synchronize the host clock. Independent files are
independent budgets. Deleting/restoring an old limiter DB resets its history; do
not do that while serving traffic. Development services explicitly injected with
an in-memory ledger can use isolated SQLite memory counters; those are not shared.

## Owner TOTP and step-up

TOTP uses RFC 6238 HMAC-SHA1, six digits, 30-second steps and a bounded +/-1-step
window. Seeds are random per factor and encrypted with AES-GCM and owner/factor
associated data using the existing cryptography dependency. The algorithm itself
uses the standard library. Missing, malformed or wrong encryption keys fail
closed. Keep the key stable across restart; do not replace it without a planned
factor migration. Automated key rotation and lost-factor recovery are DESIGN ONLY.

Use the existing identity database:

```sh
python -m agentpal.identity --db /var/lib/agentpal/identity.db enroll-totp
python -m agentpal.identity --db /var/lib/agentpal/identity.db confirm-totp
python -m agentpal.identity --db /var/lib/agentpal/identity.db step-up
```

These commands prompt for the owner credential and code with hidden input. The
first command prints a provisioning seed once; add it to a trusted authenticator.
Enrollment expires after ten minutes and confirmation must use the same parent
credential. Confirmation consumes its counter; use a newer code for step-up.
Reused code values are refused even if adjacent counters collide; wait for a different
code in that rare case. Five failed attempts cause a durable five-minute lockout. Restarting or restarting
pending enrollment cannot reset it. An enabled factor cannot be overwritten.

Step-up prints a separate apo_ credential once, expiring within five minutes and
no later than its parent. The original credential stays non-MFA. Every use checks
parent revocation/expiry, owner status, current factor and key. Removal requires
an active step-up credential (`remove-totp`) and invalidates all derived sessions.
Do not send credentials/codes as command-line arguments or redirect output into
committed files. Bootstrap/admin issuance remains a trusted local operator action.

The A3 service and existing approval gateway are tested together. Automatic CLI
policy wiring is assigned to the lead. After wiring, run the following additional
integration assertion from the project directory, then the real-factor HTTP test:

```sh
python - <<'PY'
from tempfile import TemporaryDirectory
from pathlib import Path
from agentpal.config import Settings
from agentpal.platform.wiring import build_platform
with TemporaryDirectory() as directory:
    settings = Settings(db_path=str(Path(directory) / 'risk.db'), auth_mode='required',
                        analyzer_chain=['heuristic'], require_mfa_for_approval=True)
    deps = build_platform(settings=settings, environ={})
    try:
        assert deps.require_mfa_for_approval is True
    finally:
        deps.close()
PY
python -m pytest -o addopts='' -q -p no:cacheprovider \
  tests/test_sec_v3_mfa.py::test_real_factor_satisfies_existing_http_approval_policy
```

## Recovery, audit and evidence

See CONSISTENCY.md for commit order and operator handling. Consistency failures
refuse startup; never suppress them to resume financial operations. Take quiescent
snapshots of all databases and retain verified provider facts. Wallet copies cannot
repair approvals, release uncertain attempts or establish payment completion.

Application/Uvicorn logs scrub credential patterns and known environment secrets;
access records retain method, route family and status without raw query strings
or opaque resource IDs. Identity metadata and wallet drafts reject pasted
credentials; existing unsafe identity labels are sanitized on output. This does
not erase secrets already stored by older versions or sanitize arbitrary external
logging handlers. Audit records remain authoritative application records, not a
cryptographic tamper-evident archive against a database administrator. Evidence
hashing detects changed bytes but does not prove that observed content is true.

The standalone store.py credential redactor remains a lead integration handoff:
tests/test_sec_v3_redaction.py is strict xfail until the lead applies that fix and
removes the marker. A passing ingress test does not close this direct-store gap.

## Verification and live blockers

Offline tests cover a real local TLS listener, spawned-process limiter admission,
TOTP vectors/replay/lockout, actual owner HTTP approval, isolation/revocation/rotation,
process-level execution/idempotency races and provider-call process death. They
also cover store failures and cross-DB corruption/commit interruptions. No real
money or production provider is used.

Live PayPal sandbox verification is BLOCKED BY CREDENTIALS: no sandbox client ID,
client secret (either accepted spelling) or webhook ID was supplied. Authentic
public delivery additionally needs a reachable approved hostname, trusted TLS
certificate, registered sandbox webhook and an actual signed event. Successful
capture needs a sandbox payer's approval. Skipped/mocked tests prove none of these.

## Hosted mode on Render (2026-11-05)

`AGENTPAL_HOSTED_PROXY=render` is the one supported way to serve AgentPal behind a
TLS-terminating proxy, and it is refused unless the process runs on Render (`RENDER=true`,
set by Render itself), in production mode, with authentication required, a durable SQLite
limiter and exact `*.onrender.com` Host names. On Render the front proxy is the only network
path into a web service, so `RenderFrontDoor` (outermost middleware) trusts it there only:

* exactly one `X-Forwarded-Proto: https`, else 403; Render's own health probe (no forwarding
  headers, `GET /health` or `/v1/health`) reaches only those two paths;
* the client is the right-most `X-Forwarded-For` entry (the one Render appends), must be a
  valid non-loopback IP, else 400; rate limits and audit use it;
* the scope becomes `https` with that client, so the Host allow-list, authentication, MFA,
  rate limits and the webhook gate all apply unchanged;
* remote clients reach only the authenticated `/v1/...` API and the health paths; the
  legacy `/assess` API, `/docs`, `/openapi.json` stay loopback-only; websockets are refused.

The first owner is bootstrapped from secret env vars `AGENTPAL_BOOTSTRAP_OWNER_ID` and
`AGENTPAL_BOOTSTRAP_OWNER_TOKEN` (an `apo_` token generated off-host; only its salted hash is
stored; idempotent; a revoked token is never revived). Configuration used:
`config/spend_policy.hosted.json` (operator policy, no legacy agents: /v1 agents get limits
only from owner wallets) and `config/api_tokens.hosted.json` (no legacy tokens). Databases
live on a persistent disk (`/var/data`). Tests: `tests/test_hosted_render.py`.
