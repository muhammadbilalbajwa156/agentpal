# AgentPal Shopper (UI-A1)

Shopper is a third-party-style **PayPal Sandbox demonstration**, using a real free
OpenRouter model and Channel3 product search. AgentPal remains the financial
authority. Shopper requests payments; the human owner approves exact terms and
captures in the AgentPal dashboard. Shopper has no approval, capture, refund or
owner credential interface.

## Layout and installation

The importable source package is `agentpal/agents/shopper/` **relative to the
Python project directory** (`repo-root/agentpal`). Thus its full repository path
is `agentpal/agentpal/agents/shopper/`. The assignment's literal shorter path
would sit outside the existing Python package and break the required module CLI.
No existing AgentPal source or dependency manifest was changed.

Run from the Python project directory:

```sh
cd /tmp/ui-a1/agentpal
pip install -r requirements.txt
python -m agentpal.agents.shopper --help
```

The source checkout works without adding SDK paths. Shopper uses `httpx` directly:
the repo SDK has owner and agent clients, but the demonstration needs only three
agent routes and an explicit outbound route allow-list. `httpx`, FastAPI and
uvicorn are already project dependencies. The lead must add `agentpal.agents`,
`agentpal.agents.shopper`, and `agentpal.agents.shopper.astropods` to the explicit
setuptools package list, plus static HTML/JS/CSS and deployment assets to package
data, before distributing a wheel.

## Credentials and owner preparation

Supply these through environment variables or a deployment vault, never source:

| Variable | Meaning |
| --- | --- |
| `OPENROUTER_API_KEY` | OpenRouter credential; use `proxy-injected` only in this container |
| `CHANNEL3_API_KEY` | Channel3 API credential, sent only as `x-api-key` |
| `AGENTPAL_AGENT_KEY` | An `apk_…` agent credential; owner `apo_…` credentials are refused |
| `AGENTPAL_BASE_URL` | AgentPal origin, e.g. `https://agentpal-api.onrender.com`; defaults to `http://127.0.0.1:8810` |
| `OPENROUTER_MODELS` | Optional comma-separated list or JSON array; every model must end in `:free` |
| `SHOPPER_JOURNAL` | Optional private SQLite journal path for CLI/web |

Owner credentials and PayPal credentials belong only in the separate AgentPal
server/dashboard or the operator's provisioning process. Do not supply them to
Shopper. Give the external agent exactly `wallet.read`, `payment.prepare`,
`owner.request_decision`, and `payment.execute_sandbox`; provision a USD virtual
wallet with appropriate monthly and per-transaction limits. AgentPal's wallet
limits still apply even when the task budget is higher.

Default OpenRouter fallback routing sends this exact `models` array:

```json
["nvidia/nemotron-3-ultra-550b-a55b:free", "nvidia/nemotron-3.5-super-vl:free", "google/gemma-4-31b-it:free"]
```

## CLI

```sh
python -m agentpal.agents.shopper "Buy a wireless mouse for the office" --budget 40
```

Output is readable JSON step events, flushed immediately. Save the `errand_id`.
The runner polls with delays increasing from 2 to 30 seconds, waiting up to an
hour at each approval/provider phase by default. `--wait-seconds 0` returns the
current state immediately. Interrupting or timing out does not imply a rejection
or a completed payment.

Resume with the **same task, budget, errand ID, API origin, agent credential and journal**:

```sh
python -m agentpal.agents.shopper "Buy a wireless mouse for the office" \
  --budget 40 --errand-id SAVED_ERRAND_ID
```

To route to a named sandbox merchant, include the mailbox in the task and pass
`--merchant-paypal-email merchant@example.com`. This option binds only that
user-provided mailbox; catalogue/model output cannot supply a payee. Use an
actual sandbox merchant mailbox, never a production merchant.

The journal freezes the validated payment body before creation. Creation retries
reuse its body and deterministic idempotency key, including after a lost HTTP
response. Reusing an errand ID with different input or a different API/credential
scope fails, preventing a lost create from being replayed under another agent.
A planning failure or
interruption seals that errand rather than spending another six LLM calls. An
ambiguous execution attempt is recorded before the POST and never automatically
retried by CLI/web; inspect it in the owner dashboard. Keep the journal private
and durable when restarting; deleting it loses local recovery information.

## What the current agent API exposes

`GET /v1/payment-requests/{id}` intentionally returns **empty `risk_signals` for
agents**. Shopper says that detailed signals are owner-only. It reports the
advisory market-check status and messages when those are available; unavailable
data never becomes an invented assurance.

Current `ExecutionOut` contains the request ID, status and sandbox label, **no
`payer_action_url`**. `PaymentOut` also omits that link. Provider references and
the buyer link are exposed only by **owner-only** reconciliation. Shopper never
calls that route or borrows an owner token. It directs the owner to the dashboard
for the PayPal sandbox buyer step and capture, then polls agent-visible status.
If the lead exposes a sandbox-validated `payer_action_url` on the agent payment or
execution response, Shopper already displays it without fetching it. Only
`https://www.sandbox.paypal.com` links are accepted for that display.

Only `provider_confirmed` means verified completion. Rejected, expired, failed,
reversed and reconciliation-required outcomes stop the runner. A buyer click,
approval, or `provider_pending` is never reported as a completed payment.

## Render Workflows (Python SDK v1)

Based on the official [overview](https://render.com/docs/workflows.md),
[tutorial](https://render.com/docs/workflows-tutorial.md),
[flows](https://render.com/docs/workflows-defining-flows.md) and
[Python v1 SDK](https://render.com/docs/workflows-sdk-python.md), read 2026-11-06.
The optional dependency is **`render>=1.7.0,<2`**. It is imported only by
`create_workflows()`; CLI/web and normal imports work without it. No lockfile was
edited.

In the Render dashboard:

1. Choose **New > Workflow**, connect the reviewed repository/branch, choose
   **Python 3**, and set Root Directory to `agentpal`.
2. Build Command: `pip install -r requirements.txt && pip install 'render>=1.7.0,<2'`.
3. Start Command: `python -m agentpal.agents.shopper.render_workflow`.
4. Set the four required variables above (use the hosted HTTPS base URL). Keep
   the credentials secret. Add `OPENROUTER_MODELS` only to choose other free
   models. No owner or PayPal credentials are needed.
5. Deploy Workflow. Render supplies its SDK execution/registration environment.
   A bare local `app.start()` without `RENDER_SDK_MODE` is not a local worker.
6. Select flow **`shop_errand`** and trigger it with positional input
   `["Buy a wireless mouse for the office", "40"]`.

The synchronous v1 `ctx.step` checkpoints are `planning_execution`,
`plan_and_search`, `request_payment`, `wait_for_owner_decision`, `execute`, and
`wait_for_provider`. Flow-run ID is the stable errand/idempotency identity. The
planning process claim prevents an interrupted, unrecorded model loop from
starting another six-call loop after replay; completed plans replay from their
checkpoint. Create replays use the checkpointed exact body. Execute re-reads
current approval before calling AgentPal, whose lifecycle fences provider
invocation. A lost execution result must never trigger direct PayPal calls.

Owner and provider waits each allow six hours; the flow timeout is 24 hours.
These are **ordinary bounded polling waits within steps**, consuming the flow
instance while waiting; this SDK integration does not claim suspended/bill-free
waiting. A wait deadline returns the observed pending status. Check the original
request in AgentPal before starting a new errand. No automatic financial mutation
retry policy is configured.

## Astropods FRONTEND

The [package spec](https://docs.astropods.com/astropods-package-spec.md),
[frontend guide](https://docs.astropods.com/frontend-agents.md),
[vault guide](https://docs.astropods.com/secrets.md), and
[deployment guide](https://docs.astropods.com/deploy-agent.md) were read through
the [documentation index](https://docs.astropods.com/llms.txt) on 2026-11-06.

`astropods/astropods.yml` declares `blueprint/v1`, an agent Docker build, and
`agent.interfaces.frontend: true` / `messaging: false`. Build context is the
Python project directory, four levels above the spec. The Dockerfile path is
relative to that context. The image copies only this new agents package and
serves uvicorn on **port 80**, as the platform requires. No runtime credentials
are baked into the image.

Local web runner, from the Python project directory with the same env vars:

```sh
uvicorn agentpal.agents.shopper.astropods.web:app --host 127.0.0.1 --port 8800 --no-access-log
```

Open `http://127.0.0.1:8800`. The page accepts an errand and decimal USD budget,
polls the shared core's steps, and shows the request ID/status and owner approval
instructions. HTML is static; JavaScript and CSS are external. Output uses DOM
text nodes and `textContent`, with a restrictive CSP. Browser sessions isolate
job reads, cross-origin writes are refused, and duplicate submission IDs reuse
the same job. Two concurrent errands and bounded job/event retention limit local
resource use. Refreshing the same tab reconnects using `sessionStorage`.

With an installed, authenticated Astropods CLI, an operator can use:

```sh
cd agentpal/agentpal/agents/shopper/astropods  # from repository root
ast spec validate
ast secrets create OPENROUTER_API_KEY
ast secrets create AGENTPAL_AGENT_KEY
ast secrets create CHANNEL3_API_KEY
ast secrets create AGENTPAL_BASE_URL --plain --value https://agentpal-api.onrender.com
ast blueprint push
ast blueprint deploy agentpal-shopper \
  --var OPENROUTER_API_KEY=@OPENROUTER_API_KEY \
  --var AGENTPAL_AGENT_KEY=@AGENTPAL_AGENT_KEY \
  --var CHANNEL3_API_KEY=@CHANNEL3_API_KEY \
  --var AGENTPAL_BASE_URL=@AGENTPAL_BASE_URL
```

Interactive secret prompts avoid putting credentials in command history. These
are operator instructions: UI-A1 did not log in, push, or deploy. Astropods
provides OIDC at its front door; this is a single configured-agent demonstration,
not a multi-tenant wallet application. Keep access limited to the demo's intended
users. Job display state is process-local: use one uvicorn worker/replica. Keep
the SQLite journal on durable storage if restarting; after a container restart,
inspect the original request in AgentPal rather than submitting the same errand
again with a new ID.

## Guards and verification

The model may search or propose one returned offer; it cannot supply payment
URLs, merchant domains, payees or an owner decision. Payment amounts must be
positive decimal strings, match the catalogue price, and fit the task budget.
Search itself cannot raise the budget. USD, in-stock offers only; no claim that
shipping/tax is included. Strings are stripped/bounded and instruction-shaped
titles are withheld. Catalogue data stays in an explicitly untrusted tool-data
envelope. Deterministic tool/route/amount fences remain the authority even if
that text influences the model.

At most six OpenRouter HTTP calls per errand (429 attempts included). A 429 waits
with bounded backoff and rotates the free-model fallback order. Other model
errors stop before payment creation. Response sizes are bounded, redirects are
refused, remote error bodies are never surfaced, and configured secrets are
redacted before prompts, downstream tool data, events or journal checkpoints.
Only Channel3 `/v1/search`, OpenRouter chat completions, and the configured
AgentPal create/read/execute routes can be called. Merchant and buyer URLs are
display data; Shopper never visits them.

```sh
python -m pytest -q -o addopts="" tests/test_shopper_agent.py
python -m ruff check agentpal tests training
python -m pytest -o addopts="" -q -p no:cacheprovider
```

Offline tests use scripted tool calls and `httpx.MockTransport` for every remote
service. They cover approval/rejection, budget/decimal/offer validation,
instruction-shaped catalogue text, forbidden tools/routes, model failure and
429 limits, frozen idempotent retries, uncertain execution, secret redaction,
Render checkpoints and browser session/rendering boundaries. Security mutation
evidence and final counts are recorded in the review commit bodies and team log.

Full-suite baseline: **3,616 passed / 63 skipped**. Final full suite:
**3,699 passed / 63 skipped**, 4 warnings (same warning count as baseline),
535.43 seconds. Baseline and final repository ruff checks passed.

Focused result: **83 passed**, ruff clean. The final mutation run removed each
of these 28 controls separately in an isolated copy (never the worktree). Every
selected pytest invocation failed with exit 1; each mutation was then restored.
The test names below are selectors within `tests/test_shopper_agent.py`.

| Removed controls (count) | Regression selector(s) | Result |
| --- | --- | --- |
| Budget ceiling; exact offer price (2) | `search_cannot_relax_budget`, `tool_arguments_cannot_change_catalogue_terms` | Caught |
| USD, stock, merchant-domain checks (3) | `catalogue_filters_currency_stock_domain_and_price` | Caught |
| Text stripping and length bounds (2) | `catalogue_text_is_stripped_and_bounded` | Caught |
| Response and web-request byte caps (2) | `response_size_is_bounded`, `web_body_limit_refuses_large_input_before_model_or_payment` | Caught |
| Response request identity; current approval (2) | `remote_identity_or_unknown_completion_status_is_refused`, `execute_requires_current_server_approval` | Caught |
| Single tool call (1) | `multiple_requests_in_a_single_model_response_refused` | Caught |
| Owner-route and owner-credential refusal (2) | `network_boundary_refuses_owner_actions_and_paths`, `owner_credential_cannot_be_used_as_agent_key` | Caught |
| Decoded-secret and errand-ID redaction (2) | `json_escaped_secrets_are_redacted_before_downstream_tools`, `secrets_never_reach_prompts_outputs_or_journal` | Caught |
| Catalogue instruction filtering (1) | `catalogue_injection_is_data_and_instructions_are_not_followed` | Caught |
| Free-model, sandbox-link and redirect checks (3) | `paid_model_overrides_are_refused`, `buyer_links_only_display_sandbox_paypal`, `redirect_is_never_followed_and_error_body_is_not_reflected` | Caught |
| Stable idempotency; no ambiguous execute retry (2) | `idempotent_retry_reuses_frozen_body_and_key_after_response_loss`, `uncertain_execution_is_never_retried` | Caught |
| Local and Render planning attempt fences (2) | `model_failure_stops_and_does_not_restart_planning_for_same_errand`, `render_incomplete_planning_cannot_repeat_model_budget_on_replay` | Caught |
| Journal and Render agent/origin scope (2) | `resume_cannot_switch_agent_or_origin_after_ambiguous_creation`, `render_checkpoint_cannot_create_under_a_different_agent` | Caught |
| Literal DOM rendering and session isolation (2) | `web_job_session_isolation_and_escaping` | Caught |

Additionally, an actual Node DOM probe rendered an HTML/script-shaped message
through the shipped JavaScript as a literal text node, with an `innerHTML` setter
that throws if used. JavaScript syntax and YAML/Docker context paths checked
successfully. YAML/path checks are not a substitute for `ast spec validate`.
A real uvicorn HTTP smoke also passed page/assets/health/job polling, with a
missing agent credential stopping the job before any model or payment call.

**Live CLI: IMPLEMENTED — TESTED to awaiting approval**, 2026-11-06: a real
Channel3 Dell wireless mouse at **14.99 USD**, budget 40 USD, produced local
request `pay_68235258dd0c45acab6841eded8321b7`. CLI exited 0 at
`awaiting_approval`, reported owner-only risk signals and an unavailable advisory
market check. Two model calls in the successful run; an earlier safely stopped
probe used two more. **No approval, capture, or PayPal order** was made. Live
provider completion remains IMPLEMENTED — UNVERIFIED; its full state flow is
covered offline.

**Render: IMPLEMENTED — TESTED offline**, including actual SDK 1.7.0 registration;
hosted execution is IMPLEMENTED — UNVERIFIED. **Astropods: IMPLEMENTED — TESTED
offline** as a FastAPI app; spec CLI validation is BLOCKED (`ast` absent), and
image build/hosted deployment is IMPLEMENTED — UNVERIFIED (Docker daemon absent).

Lead requests: expose a narrowly scoped sandbox buyer link to the owning agent
if direct agent link display is required; preserve the existing owner-only risk
policy; add the new packages/assets to lead-owned packaging metadata; decide
whether to pin the optional Render SDK in the shared dependency manifests.

Delivery: shared worktree Git metadata is read-only. The requested
`/tmp/ui-a1.patch` is generated with `git add -N` and `git diff` using a separate
writable index/object directory. Review commits are in `/tmp/ui-a1-review` and
exported to `/tmp/ui-a1.bundle`; the original branch/index/history remain intact.

## Shared helpers and the company flow (CO-A2, 2026-11-06)

The guarded helpers (`money`, `text`, `identifier`, `display_url`, `Redactor`, free-model
parsing, `FREE_MODELS`, statuses, and the bounded no-redirect JSON transport) moved
unchanged into `agentpal/agents/common.py`. `shopper.core` re-exports them, and
`ShopperError` is now an alias of `common.AgentError`. `Journal` gained an optional `table`
argument (default `shopper_errands`). `create_workflows()` also registers the
`company_day` flow; see `docs/AUTONOMOUS_COMPANIES.md`. Note that Render SDK 1.7.0 ends
the whole run when a step raises, even if the flow catches it: `shop_errand` steps
therefore still fail the run closed.
