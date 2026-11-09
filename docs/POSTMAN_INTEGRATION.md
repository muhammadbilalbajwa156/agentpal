# AgentPal Postman integration

Postman is AgentPal's API test and documentation layer, chosen by the owner.
**IMPLEMENTED — TESTED:** deterministic collection generation, live legacy security
scenarios, disposable Newman runner, and mocked Postman API sync.
**IMPLEMENTED — TESTED:** the `/v1` security run (`python -m postman.run_newman --mode v1`):
17 requests, 34 assertions, 0 failures against a disposable local server (audit AU-C2, 2026-11-08).
The generated collection documents every `/v1` operation; the run exercises the critical scenarios.
**BLOCKED:** authenticated remote sync without `POSTMAN_API_KEY`.

The [official hackathon rules](https://paypalaihackathon.devpost.com/rules)
list Postman among optional tools (§4), but list **no Postman prize** (§8).
The [event's judging panel](https://paypalaihackathon.devpost.com/) includes
Pooja Mistry, Sr. Developer Advocate at Postman. This integration makes no
Postman prize-eligibility claim. These sources were checked on 2026-10-31.

**Setup and dependencies**

Run the commands below from the repository's `agentpal/` directory. Use Python
3.11+ with the existing application/dev dependencies, and Node.js 16+ with npm
and npx on PATH. Verification here used Node 22.22.0 and npm/npx 10.9.4.

```bash
python -m pip install -r requirements-dev.txt
npm ci --prefix postman --ignore-scripts --no-audit --no-fund --cache /tmp/agentpal-npm-cache
python -m postman.generate
python -m postman.generate --check
python -m postman.run_newman
```

`postman/package.json` pins **newman 6.2.3**; `package-lock.json` pins its
dependency graph. The Python runner always performs `npm ci` itself using a
temporary writable npm cache, so it needs registry access. Do not substitute
an unpinned `npx newman`. `postman/node_modules/` and `postman/reports/` are
git-ignored.

**Generated artifacts and authentication**

`postman/generate.py` reads the current legacy app's OpenAPI schema without
entering its lifespan or creating a database. When `docs/api/openapi.json`
exists locally, it also reads that file for v1. No sibling worktree is needed
by generation, tests, or the runner. It writes:

- `postman/legacy.collection.json`: every legacy OpenAPI operation once in
  `OpenAPI legacy`, followed by the hand-written `LEGACY security` folder.
- `postman/v1.collection.json`: local v1 operations, or an empty operation
  folder plus `PENDING v1` when the local specification is absent.
- `postman/environment.json`: loopback base URL only; no credentials.

UUID5 IDs, sorted JSON keys, stable operation order, and no generation timestamps
make output reproducible. `--check` compares bytes and exits 1 for drift without
writing. Generated examples are documentation and can require domain-specific
values; they are not the security suite. The converter supports local references
and JSON request bodies, rather than the entire OpenAPI feature set. Credential
header examples are deliberately excluded.

All bearer auth refers to collection variables: `{{agent_a_key}}`,
`{{agent_b_key}}`, `{{owner_key}}`, or `{{invalid_key}}`. Every committed
credential variable is empty and has `type: secret`. The environment never
overrides credentials. For manual Postman use, import the collection and set
credentials only in your local collection variable values; never export or
sync populated values. Importing the environment is optional. Select the
security folder rather than running all documentation examples.

The legacy runner requires no pre-existing API credentials. It generates
throwaway tokens in memory, writes only their SHA-256 hashes to the server's
temporary token registry, and injects the plaintext into a private temporary
runtime collection. The entire temporary directory is removed afterward.
It sets authentication to `required` explicitly, even under pytest's dev-auth
fixture, selects the heuristic analyzer without changing the pinned ML model,
and uses a fresh SQLite database and the existing demo spending policy.

**Live security run**

`python -m postman.run_newman` allocates a free loopback port, starts a real
`python -m agentpal serve --host 127.0.0.1 --port ...` subprocess, and waits for
an authenticated `/health` configuration before running Newman. It terminates
the server in `finally`, including Newman timeout and failure paths, and
returns Newman's exit code (setup errors return 2).

The nine legacy requests assert safe JSON error bodies and bearer challenges,
invalid-token rejection and token non-reflection, a successful agent-owned
assessment/read, identical cross-agent and unknown-ID 404 responses, a 403 for
an `agent_id` mismatch, a `DUPLICATE_REQUEST_ID` rule signal with `block` on
replay, and `/health.payment_execution_enabled == false`.

Real output excerpts from the successful local run on 2026-10-31:

```text
npm ci: pinned newman@6.2.3 installed.
Local server ready: authentication required; payment execution disabled.
  GET http://127.0.0.1:32833/model-info [401 Unauthorized, 216B, 7ms]
  ✓  refused with safe JSON error
  ✓  bearer challenge is present
  ✓  no assessment existence oracle
  ✓  replay cannot reserve or pass again
│                requests │                9 │                0 │
│              assertions │               21 │                0 │
Newman exit code: 0; sanitized JUnit + JSON reports written.
Local server stopped.
```

Ports and timings change per run. Schema generation may print the app factory's
missing-token/demo-policy warnings; the actual server uses the temporary hashed
token registry and explicitly required authentication. The ready check and
Newman authentication assertions verify this configuration.

JUnit and JSON reports are written to `postman/reports/legacy.xml` and
`postman/reports/legacy.json`, or `--reports-dir PATH`. Newman includes resolved
credentials in request headers and collection variables; the runner redacts
all throwaway tokens before printing output or persisting reports. Raw reports
and server logs remain in the temporary directory and are deleted. Avoid
direct Newman invocations with valuable credentials: those bypass redaction.

**Pending v1**

The draft folder covers owner/agent credential crossover, an agent trying to
approve, tampered approval digests, repeated approval, idempotency replay and
body mismatch, execution before approval, and explicitly virtual allowance.
It also checks that refusals preserve the approval gate. Drafts were informed
by the plan and WS2's read-only draft; tests use small local fixtures and never
depend on that external file.

Current real output:

_Historical note (before the v1 platform was integrated):_

```text
$ python -m postman.run_newman --mode v1
PENDING v1: skipped; docs/api/openapi.json is absent locally.
```

When the local spec arrives, generation renames this folder `V1 security` and
the runner selects it. _(Historical, now done:)_ a passing v1 runtime still requires the
lead's CLI/platform mounting and provisioned owner, v1 agent credentials, active
USD virtual wallet, grants, and WS1 approval/lifecycle gateways. The current
runner provisions legacy tokens only; merely adding the specification cannot
make v1 executable. The draft refusal codes and response shapes must be checked
against the integrated API. This work does not move money or call PayPal.

**Postman API sync**

`python -m postman.sync` previews actions by default. It requires
`POSTMAN_API_KEY` even for preview, but never prints it. Load that variable
through your local secret manager. The default base is `https://api.postman.com`;
`POSTMAN_API_BASE` / `--base-url` can override it (HTTPS required except loopback
HTTP). Redirects are refused. Remote error bodies/headers and transport details
are suppressed because they could reflect credentials.

```bash
python -m postman.sync --workspace YOUR_WORKSPACE_ID
# Only after reviewing the preview, explicitly send the two requests:
python -m postman.sync --workspace YOUR_WORKSPACE_ID --apply
# Replace existing objects using their remote IDs:
python -m postman.sync --collection-id YOUR_COLLECTION_ID --environment-id YOUR_ENVIRONMENT_ID --apply
```

`POSTMAN_WORKSPACE_ID`, `POSTMAN_COLLECTION_ID`, and `POSTMAN_ENVIRONMENT_ID`
provide the same defaults. POST creates objects and includes `workspace` when
provided; PUT replaces objects by ID. Both use the `X-API-Key` header. The
environment payload contains only its name and values. Populated credentials
and environment credential overrides are refused before upload. Collection
and environment updates are two separate requests: a second-request failure
can leave the first object synced. Remote IDs can be obtained in Postman; sync
does not persist them or echo remote response bodies.

Real output with the key absent (exit 2):

```text
BLOCKED: POSTMAN_API_KEY is absent; no Postman API requests sent
```

A real dry-run executed with a generated throwaway key, without transport calls,
produced these action excerpts:

```json
{
  "actions": [
    {"method": "POST", "resource": "collection", "url": "https://api.postman.com/collections?workspace=demo-workspace"},
    {"method": "POST", "resource": "environment", "url": "https://api.postman.com/environments?workspace=demo-workspace"}
  ],
  "status": "DRY RUN"
}
```

**BLOCKED:** successful authenticated remote sync has not been demonstrated;
no valid Postman account key was supplied. Create/update contracts, headers,
workspaces, redirection refusal and sanitization are verified with mocked
transports. API references:
[create collection](https://learning.postman.com/api-docs/api-reference/collections/create-collection),
[update collection](https://learning.postman.com/api-docs/api-reference/collections/put-collection),
[create environment](https://learning.postman.com/api-docs/api-reference/environments/create-environment),
[update environment](https://learning.postman.com/api-docs/api-reference/environments/put-environment).

**Verification and limits**

```bash
python -m pytest -o addopts="" -q -p no:cacheprovider tests/test_postman_*.py
python -m pytest -o addopts="" -q -p no:cacheprovider
ruff check agentpal training tests
ruff check postman
```

Tests cover deterministic generation, checked-in drift, operation coverage,
secret scanning over `postman/` (excluding dependencies/reports/caches), mocked
sync and real Newman against a socket server. The end-to-end test skips only
when node or npx is missing; installation/runtime failures fail the test.
Cleanup is also tested against an actual server when Newman times out.

Final verification on 2026-10-31: **42 Postman tests passed** within the full
suite, **1,577 passed and 37 skipped** in 93.16 seconds. The unchanged baseline
was 1,535 passed and 37 skipped. Both the required ruff command and the separate
`ruff check postman` returned `All checks passed!`.

The legacy service remains loopback-only, with in-memory rate limits and no
TLS. Reports verify this local demo, not production readiness or completed
provider payments. A virtual allowance is never money. No frontend or model
changes are part of this integration.

## Live Postman cloud sync (verified 2026-11-01)

**Status: VERIFIED LIVE.** The owner supplied a Postman API key and the "AgentPal" workspace (personal visibility, ID `f6177053-03cc-4666-8c62-8404cc283539`).

**What was uploaded through the Postman API (`X-API-Key`), all requests answered 200:**

| Object | Postman UID | Contents (read back from Postman) |
|---|---|---|
| AgentPal v1 | `59387148-0f39d5f9-b1e9-4fc5-a76c-97c07c3783d3` | 53 requests, 18 test scripts |
| AgentPal legacy | `59387148-dd9bb867-b991-4fd9-a05b-70894de930cf` | 13 requests, 13 test scripts |
| AgentPal loopback (environment) | `59387148-db284039-4a59-45de-9da3-35a4d90d3158` | `base_url` only |

When the uploads were read back, every credential variable (`agent_a_key`, `agent_b_key`, `owner_key`, `invalid_key`) was empty, and there were no `PMAK-`/`apk_`/`apo_`/`apt_` literals.

**Bug found only by the live API.** Postman rejects `"type": "secret"` on *collection* variables: `400 malformedRequestError`, `variable/N/type: must be equal to one of the allowed values`. Newman had accepted it. Credential variables are now empty `"type": "string"` variables. The sync guard still blocks any credential variable that is non-empty or has any other type, and blocks credentials placed in the environment.

**Proxied networks.** `sync.py` builds its own client with `trust_env=False`, so no proxy or netrc from the environment can intercept the key. On a network where outbound traffic must go through a proxy, inject an explicit client:

```python
client = httpx.Client(proxy=os.environ["HTTPS_PROXY"], verify="/path/to/ca-bundle.crt",
                      trust_env=False, timeout=30)
sync_assets(collection, environment, api_key=os.environ["POSTMAN_API_KEY"],
            workspace=os.environ["POSTMAN_WORKSPACE_ID"], apply=True, client=client)
```

To update the objects later, pass `--collection-id` and `--environment-id` with the UIDs above; the sync then uses PUT instead of creating duplicates. The workspace stays private until the owner chooses to make it public for judges.
