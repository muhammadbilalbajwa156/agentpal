# APIMatic integration and verification

Verified on 2026-10-31. AgentPal remains a backend sandbox for developers' external agents.
No provider payment calls were made for this work. The existing Python SDK is unchanged.

## Sponsor facts and actual contribution

The [official hackathon rules](https://paypalaihackathon.devpost.com/rules) list three
Best Use of APIMatic awards, each $1,000 plus six months of Business subscription, for
eligible submissions using any APIMatic tools. General submission eligibility includes
PayPal sandbox and an AI integration. Tool use is not a guarantee of an award.

The [sponsor details](https://paypalaihackathon.devpost.com/details/apimatic) specifically
promote the PayPal Context Plugin, which needs no extra APIMatic credentials/sign-in.
The [linked subscription claim form](https://docs.google.com/forms/d/e/1FAIpQLScc2oCgAFACm7d6H3R4mgzv-O34DdyVnLIBNiwmUNggr_-hhA/viewform)
requires evidence of plugin usage, participant/winner verification and manual approval.
The participant offer is one month of Basic; winning plugin-using teams can claim six
months of Business. There is no documented automatically issued hackathon API key.

**IMPLEMENTED / TESTED OFFLINE:** pinned CLI tooling, credential gates, provenance hashes,
OpenAPI drift and response-contract checks, and the preserved SDK exercised over TCP
against a real `python -m agentpal serve --platform` process with the local simulator.
Offline here means no APIMatic generation service or payment provider was contacted;
dependency installation itself requires registry access.

**IMPLEMENTED / verified online:** installed the sponsor-linked PayPal Context Plugin
under `/tmp` and loaded its skills in Claude Code to verify Orders v2 capture details
against the official PayPal SDK source. This is verification of an existing integration,
not a claim that APIMatic built the existing provider adapter or handwritten SDK.

**BLOCKED BY CREDENTIALS:** APIMatic validation/linting and AgentPal SDK generation have
not run against APIMatic. No AgentPal SDK artifact exists, so the generated-client HTTP
probe has not run either. Its complete runner is present; it never substitutes a fake or
the handwritten SDK for generated code. Mock subprocess fixtures test bookkeeping only.

## Verified APIMatic capabilities and authentication

| Capability | Official source and scope |
|---|---|
| CLI | [CLI reference](https://docs.apimatic.io/v4/apimatic-cli/cli-reference/): validation, transformation, SDK, portal and Context Plugin commands. This project pins `@apimatic/cli@2.0.0`. |
| Validation/linting | [`api validate`](https://docs.apimatic.io/v4/define-validate/validate-lint/) performs syntax/semantic validation and linting; blocking/errors fail, warnings/information are reported. Do not invent a separate `lint` command. APIMatic also offers a VS Code extension. |
| SDK generation | [Current v4 languages](https://docs.apimatic.io/v4/generate-sdks/languages-features/) are Python, TypeScript and C#. CLI help lists additional language values but describes them as coming soon; they are not claimed working here. |
| Platform API | [Programmatic generation/transformation](https://docs.apimatic.io/platform-api/); authenticated with `Authorization: X-Auth-Key <key>`. |
| Portal | [API reference, playground, SDK downloads and guides](https://docs.apimatic.io/v4/generate-developer-portals/generate-build/). Portal generation/hosting is not implemented here. |
| Context Plugins | [Coding-agent SDK context](https://docs.apimatic.io/v4/apimatic-cli/cli-reference/); distinguish generating AgentPal context from using the already available PayPal plugin. |
| MCP generation | [Separate generator](https://docs.apimatic.io/generate-mcp-servers/mcp-server-overview/) based on a TypeScript SDK. The docs identify it as an alpha requiring access from APIMatic. No general availability or AgentPal MCP generation is claimed. |

[CLI authentication](https://docs.apimatic.io/v4/apimatic-cli/getting-started/) supports
browser sign-in or `--auth-key`. [Account authentication keys](https://docs.apimatic.io/account-management/obtaining-auth-keys/)
are distinct from API integration identifiers. The wrapper reads `APIMATIC_API_KEY`
from the environment and passes it only as a command argument; it does not perform
`auth login`, save credentials or rely on ambient CLI login. A free account or approved
hackathon subscription does not itself prove the required generation entitlement.

## Reproduce the workflow

Run from the `agentpal/` project directory. [Node 24+ is required](https://docs.apimatic.io/v4/apimatic-cli/install/).
Node 22.22.0/npm 10.9.4 were already present here. npm registry reads and installation
worked with a writable `/tmp` cache. CLI 2.0.0 refused Node 22; its version/help commands
succeeded under an isolated Node 24.11.0 installation. No global Node upgrade was made.

```bash
node --version
npm ci --prefix apimatic --ignore-scripts --cache /tmp/agentpal-npm --no-audit --no-fund
python -m apimatic check
python -m apimatic sdk-smoke
python -m pytest -o addopts="" -q -p no:cacheprovider tests/test_apimatic_workflow.py tests/test_sdk_live.py
```

`sdk-smoke` explicitly tests the existing SDK. It provisions a throwaway authenticated
owner, agent and virtual wallet, prepares a payment, checks stored replay/conflict,
approves exact terms and submits to the local simulator. It asserts pending state,
never verified completion. Owner tokens and issued agent keys stay in memory. The
server uses separate temporary identity, wallet and ledger databases, heuristic analysis,
required authentication and loopback binding. Inherited PayPal/APIMatic credentials and
AgentPal settings are removed from its environment. Logs are suppressed; processes and
temporary databases are cleaned up on failure too.

Once an account key is supplied as an environment secret (never put its value in files):

```bash
python -m apimatic validate
python -m apimatic generate
python -m apimatic smoke
```

Use `--node /absolute/path/to/node24` on `validate`/`generate` if Node 24 is not on PATH.
The wrapper uploads a temporary copy of the canonical spec using these real commands:

```text
apimatic api validate --file <staged-spec> --auth-key <environment-secret>
apimatic sdk generate --language python --input <staged-project> --destination <temporary-sdk> --auth-key <environment-secret>
```

Validation must succeed before generation. A nonzero exit, timeout or absent installable
output stops the workflow. Raw subprocess errors are suppressed; successful diagnostic
output is scrubbed before being saved under ignored `apimatic/reports/`.

Output goes to ignored `apimatic/generated/python/`, with `agentpal-manifest.json`
recording the CLI version, language, input-spec SHA-256 and artifact file SHA-256 values.
Generation failure preserves previous output. `smoke` refuses a missing, modified or
stale artifact before provisioning. Remote generator output is not assumed byte-stable.

The generated probe installs the genuine artifact into a temporary venv, discovers its
exported v4 synchronous client and real operation methods, forces the disposable loopback
base URL, disables retries and makes three real SDK HTTP reads: `v1Health`, agent `getMe`,
and owner `listAgents`. The surrounding existing-SDK flow tests payment/auth/idempotency
over that same live server. This is not a claim that generated payment methods have been
tested. Unknown generated client surfaces fail explicitly, without an alternative client.
Generated dependency installation needs registry access; runtime API reads need no
APIMatic key once a matching artifact exists. No generated SDK is committed or published.

```bash
python -m pytest -o addopts="" -q -p no:cacheprovider tests/test_apimatic_live.py
```

The CLI-start test skips explicitly without Node 24/the pinned installation. The real
generation-and-smoke test skips explicitly without `APIMATIC_API_KEY`; CI has neither
the key nor Node 24. `APIMATIC_NODE` can select a Node 24 executable for those tests.
Local JSON Schema checks use `pytest.importorskip("jsonschema")` until the lead adds
the approved test-only `jsonschema==4.26.0` dependency.

## Contract findings and integration patch

The existing generator/drift check and SDK were preserved. `openapi.py` now documents
the exception/middleware responses FastAPI cannot infer: the actual 422 envelope
(`error`, optional sanitized `details`), Host/loopback/body-limit/rate/internal errors,
`WWW-Authenticate`, `Retry-After`, and `Idempotent-Replayed`. These changes apply only
to `/v1`; route parameters/request models are not patched by the generator.

The following corrections are **DESIGN ONLY in this branch, supplied as a tested patch**:

| Finding | Source / patch |
|---|---|
| Idempotency header optional/nullable despite missing-header 400 | `router.py`: document it as required/string, read and validate explicitly while preserving the existing 400 and malformed-header 422. |
| Integer amounts accepted despite string-only published contract | `models.py`: refuse integers/bools/floats; allow internal Decimal use and wire strings. Integer refusal is a lead-approved behavior change. |
| Empty/all-null wallet update accepted by schema, rejected at runtime | `WalletUpdate`: require at least one non-null change in its model schema. |
| Metadata key/value constraints missing from schema | `AgentCreate`: publish key pattern and 200-character value limit in model fields. |
| PayPal webhook body/transmission headers absent | `router.py`: document raw JSON bytes and all five signature headers through route metadata; never parse/re-serialize before verification. |

Patch: `/tmp/m3-a2-routes.patch`, also preserved as
[`apimatic/integration-routes.patch`](../apimatic/integration-routes.patch). From the git
repository root, the lead applies it with `git apply`, removes the `@ROUTE_FIX` decorators
in `tests/test_apimatic_contract.py`, regenerates OpenAPI and Postman, then reruns tests.
Seven parameterized cases carry the exact requested strict-xfail reason. The patch was
applied only to an isolated `/tmp` package copy: **9 passed with `--runxfail`**. A1's
`router.py` and `models.py` are unchanged in this worktree. A1's later capture/reconciliation
changes require the lead's final regeneration and approved expectation updates.

## PayPal Context Plugin: exact installation and real verification

The sponsor's [Getting Started PDF](https://drive.google.com/file/d/1qa7Xkjt3PGJllHQVCP1IuE-XxlRNz4mN/view)
points to [paypaldev/server-sdk-context-plugin-preview](https://github.com/paypaldev/server-sdk-context-plugin-preview).
At commit `6aa057126f0d4709a42e15002aab832a94935d92` it is a **skills-only plugin** with
native Claude/Codex manifests, not an MCP server. There is no MCP process or endpoint
to start for this bundle. Its README labels it a hackathon preview, not a supported
long-term PayPal product. Do not replace it with PayPal's payment-action MCP server.

The [official installer](https://github.com/apimatic/context-plugins-installer) supports
Claude Code and Codex. For a team's normal installation, the sponsor's exact command is:

```bash
npx context-plugins install https://github.com/paypaldev/server-sdk-context-plugin-preview
```

Select `--targets claude` or `--targets codex` as appropriate. The latter registers a
Codex marketplace/plugin and can change Codex settings; it was **not** run here.

Actual isolated installation performed here (installer pinned to `0.12.0`):

```bash
git clone https://github.com/paypaldev/server-sdk-context-plugin-preview /tmp/m3-a2-paypal-plugin
git -C /tmp/m3-a2-paypal-plugin checkout 6aa057126f0d4709a42e15002aab832a94935d92
CP_STATE_DIR=/tmp/m3-a2-context-state CP_TELEMETRY=off \
CLAUDE_CONFIG_DIR=/tmp/m3-a2-claude \
npm exec --cache /tmp/m3-a2-npm-cache --yes --package=context-plugins@0.12.0 -- \
context-plugins install /tmp/m3-a2-paypal-plugin --targets claude --yes
```

Real installer output:

```text
OK  Added marketplace 'context-plugins-local'
OK  Installed paypal@context-plugins-local (user scope)
OK  Installed into: Claude Code
```

The registered install is `/tmp/m3-a2-claude/plugins/cache/context-plugins-local/paypal/0.1.0`.
Following the installed Python getting-started skill, the official SDK source was fetched
from [paypal/PayPal-Python-Server-SDK](https://github.com/paypal/PayPal-Python-Server-SDK),
branch `main`, commit `778277d9e4f23f80a2c150433af2f49754c76a66`, into `/tmp/m3-a2-paypal-sdk`.
Neither the SDK nor plugin source was vendored into AgentPal.

The [Claude native local-plugin mechanism](https://code.claude.com/docs/en/plugins)
was then exercised with this read-only query:

```bash
CLAUDE_CONFIG_DIR=/tmp/m3-a2-claude claude \
  --plugin-dir /tmp/m3-a2-claude/plugins/cache/context-plugins-local/paypal/0.1.0 \
  --add-dir /tmp/m3-a2-paypal-sdk /tmp/m3-a2-claude/plugins/cache/context-plugins-local/paypal/0.1.0 \
  --print --no-session-persistence --output-format stream-json --verbose \
  --tools Read Grep Glob Skill --allowedTools Read Grep Glob Skill <<'PROMPT'
Invoke paypal:python-getting-started and paypal:python-calling-endpoints. Read
/tmp/m3-a2-paypal-sdk/paypalserversdk/controllers/orders_controller.py. Verify the
Orders v2 capture method/path, PayPal-Request-Id retention, retry response semantics
and concurrent duplicate behavior. Cite source lines. Report absent information
explicitly. No payment API calls or file writes.
PROMPT
```

Actual transcript `/tmp/m3-a2-plugin-trace.jsonl` records both Skill invocations:

```text
Skill {"skill": "paypal:python-getting-started"}
Skill {"skill": "paypal:python-calling-endpoints"}
Result subtype=success is_error=false
```

Real answer excerpt:

> I loaded both skills and read `orders_controller.py`. I made no API calls and wrote no files.

The answer identified `capture_order(options)` at lines 556–557 and POST
`/v2/checkout/orders/{id}/capture` at 611–612. It located the request-id header at
623–625, six-hour retention and account-manager extension to 72 hours at 578–580,
and the idempotent-success response note at 600–601. It explicitly said the source
did not establish same-key retry/concurrent-duplicate behavior, instead of inventing it.
The first verification query also located the buyer-approval-or-valid-payment-source
condition at 560–564. Full raw answers remain in `/tmp`, not in the repository.
Transcript SHA-256: `c926c5417294ecfcc4868618ef3384b50face254bebef9cba3c54a609adddfda`.

Cross-check against our read-only `agentpal/providers/paypal_sandbox.py`:

- `capture_order` (line 420) POSTs to the matching Orders v2 path with `{}`. It does
  not supply a payment source, so payer approval must already exist. This is separate
  from AgentPal's authenticated human approval of exact terms.
- `paypal_request_id` (line 141) derives stable, different headers for create/capture.
  `_call` sends them at line 297. No automatic mutating retries occur; ambiguous
  outcomes/duplicates are held for reconciliation (lines 340–350).
- [PayPal's idempotency reference](https://developer.paypal.com/api/rest/reference/idempotency/)
  establishes that the same ID returns the **latest** status, not necessarily an original
  response snapshot; simultaneous duplicates may fail. This supplements information
  absent from the plugin-directed SDK source. AgentPal's own create-response replay is
  intentionally a stored snapshot and is a different idempotency boundary.
- Documentation caveat reported to the lead: the capture SDK docstring includes a
  mandatory-header statement about single-step **create** calls. Its capture method
  accepts an optional header. Our adapter deliberately requires a key, a stronger control.
- Another source discrepancy: the generic idempotency guide recommends a 38-byte UUID,
  whereas the [official Orders v2 OpenAPI](https://github.com/paypal/paypal-rest-api-specifications/blob/main/openapi/checkout_orders_v2.json)
  allows 108 characters for capture. Our operation-scoped values have 56/57 characters,
  within the endpoint-specific limit. No adapter change was made or is justified by
  the generic recommendation alone.

The separate general ContextMatic endpoint returned HTTP 401 during an exploratory
anonymous MCP initialization; that is not evidence the sponsor-linked skills plugin
needs credentials. It was not used as a substitute for the installed PayPal plugin.

### Codex registration for the lead

The [official Codex local-plugin documentation](https://developers.openai.com/plugins/build/plugins)
supports native marketplaces and `plugin-name@marketplace-name` enablement. To register
the already-created temporary marketplace, the lead may run:

```bash
codex plugin marketplace add /tmp/m3-a2-context-state/marketplace
codex plugin add paypal@context-plugins-local
```

Exact enablement snippet for the lead's chosen Codex config:

```toml
[plugins."paypal@context-plugins-local"]
enabled = true
```

Then invoke the Python getting-started skill and ask for a source-grounded Orders v2
capture verification. This bundle has no MCP server, so an `[mcp_servers...]` stanza
would be fictitious. No `~/.codex/config.toml` edit was made. `/tmp` installations are
temporary; the lead should choose a durable marketplace location for team registration.

## Failure-path evidence

Mutation checks removed one boundary at a time and restored the source afterwards:

| Removed control | Actual regression result |
|---|---|
| Missing-key gate | 2 failed |
| Node minimum version | 2 failed, 1 passed |
| Artifact hash comparison | 1 failed |
| Key redaction | 1 failed |
| Provider credential environment isolation | 1 failed |
| Generated-probe loopback-origin gate | 7 failed |

The unmodified tests pass. Evidence is in `/tmp/m3-a2-fail-first.txt` and the workflow
commit body. No live APIMatic service or provider calls were needed for these checks.

## Credential and access matrix

| Step | Current status / exact dependency |
|---|---|
| Drift/schema checks, existing-SDK TCP flow, CLI installation/help | TESTED OFFLINE; no account key needed. Node 24 is needed to start the CLI. |
| Sponsor-linked PayPal plugin verification | IMPLEMENTED, verified through native Claude skills; no APIMatic key needed. The coding host itself needs its normal model access. |
| APIMatic validation/linting | BLOCKED BY CREDENTIALS: `APIMATIC_API_KEY` absent; no service call made. |
| AgentPal SDK generation | BLOCKED BY CREDENTIALS: same key absent; account language/generation entitlement also unverified. |
| Generated-SDK TCP probe | BLOCKED BY CREDENTIALS indirectly: no artifact yet. A matching existing artifact does not need a key to run. |
| AgentPal portal / generated Context Plugin | DESIGN ONLY, outside implemented workflow; authenticated generation/access would be needed. |
| MCP generation | DESIGN ONLY; APIMatic alpha access is an additional prerequisite, not solved by an ordinary key. |
| Live PayPal sandbox | BLOCKED BY CREDENTIALS: sandbox client credentials absent; webhook delivery also requires a configured sandbox webhook. Not needed by the simulator checks. |

Do not count offline validators, subprocess fixtures, a skipped test or an installed CLI
as successful remote APIMatic validation/generation.

## Recorded verification

- Baseline full suite: **1872 passed, 38 skipped** (121.79 s); required ruff passed.
- Final full suite: **1902 passed, 40 skipped, 7 xfailed** (124.38 s), exit 0.
  The two new skips explicitly cover unavailable Node 24 and missing APIMatic credentials;
  the seven strict xfails are the lead-owned route/model patch cases.
- Required `ruff check agentpal training tests` and the additional `apimatic/` check passed.
- Real pinned CLI under isolated Node 24: **1 passed, 1 skipped** (key absent).
- Isolated route patch with `--runxfail`: **9 passed**; `git apply --check` passed.
- Existing-SDK live CLI simulator flow passed. OpenAPI drift check passed.
- A first full run exposed logging contamination from calling the generated-probe entry
  point inside tests. Logging suppression was moved to standalone execution; the complete
  suite was rerun successfully. No existing assertion was changed.
