# Autonomous companies (CO-A2): AI agents that spend only through AgentPal

A **PayPal Sandbox demonstration only. No real money.** Two fictional companies run a
"business day". Each AI agent has its own AgentPal agent key and USD wallet (a virtual
allowance, not money). Agents only **request** payments and payouts through `/v1`. The human
owner approves or rejects the exact terms (`terms_sha256`) in the AgentPal dashboard.
Deterministic wallet limits always apply, and only PayPal-verified outcomes count as done.
Agents never approve, capture or refund, and they never hold owner credentials.

Package: `agentpal/agentpal/agents/company/` (Python project path `agentpal/agents/company/`).

| File | Purpose |
| --- | --- |
| `scenarios.py` | Companies, agents, missions, budgets, capabilities, errands, freelancer payees (reviewed config) |
| `catalogue.json` / `catalogue.py` | Reviewed sandbox vendor catalogue (vendors, payee mailboxes, fixed prices 1–25 USD) and its validating loader |
| `operator.py` | The Operator: LLM tool loop plus deterministic guards; scripted no-model planner |
| `runner.py` | Business-day runner, journal-backed idempotent reruns, optional `--wait` |
| `provision.py` | Owner-side provisioning of agents and wallets; keys go to a 0600 file |
| `render_flow.py` | Render Workflows flow `company_day` (registered from the shopper start module) |
| `hq/` | "Company HQ" Astropods web page (`web.py`, `static/`, `astropods.yml`, `Dockerfile`) |
| `../common.py` | Guarded helpers shared with Shopper (money/text/identifier/URL checks, `Redactor`, free-model list, bounded JSON transport) |

## The cast

Run `python -m agentpal.agents.company cast` for the live listing.

### Northstar Digital: an autonomous marketing and web-building agency

| Agent (AgentPal name `Northstar Digital · …`) | Wallet / month | Per tx | Capabilities | Errands (budget) |
| --- | --- | --- | --- | --- |
| Atlas · Ads & Media Buyer | 40.00 | 25.00 | wallet.read, payment.prepare, owner.request_decision, payment.execute_sandbox | LED ring light (25, Channel3); lavalier mic (25, Channel3) |
| Forge · Web Builder | 40.00 | 20.00 | same as Atlas | CloudDesk starter hosting (12); CloudDesk theme license (15) |
| Muse · Content Studio | 35.00 | 20.00 | same as Atlas | 128GB SD card (20, Channel3); photo reflector (20, Channel3) |
| Ledger · Freelancer Payroll | 60.00 | 25.00 | wallet.read, payment.execute_sandbox, payout.request | Payout Priya Shah 20.00 (logo); payout Tom Walsh 15.00 (translation) |

### Lumen Cloud: an autonomous SaaS company

| Agent (`Lumen Cloud · …`) | Wallet / month | Per tx | Capabilities | Errands (budget) |
| --- | --- | --- | --- | --- |
| Orbit · DevOps | 60.00 | 25.00 | purchase set (as Atlas) | CloudDesk CI seat (12); Bluepeak USB-C hub (20) |
| Pulse · Growth | 50.00 | 25.00 | purchase set | Conference lanyards (15, Channel3); **Shady Deals speaker (10)** |
| Keel · Finance Ops | 50.00 | 20.00 | purchase set + payout.request | Northwind notebooks (10); payout Priya Shah 18.00 (illustrations) |

Capabilities are derived from each agent's errands (`AgentSpec.capabilities`). Every agent is
role `external`. `payout.request` is granted only to Ledger and Keel.
`payment.execute_sandbox` lets an agent ask AgentPal to execute after the owner approves. It
is also the capability AgentPal requires for executing payouts.

### Vendors and payees (sandbox only)

| Vendor (catalogue id) | Payee mailbox (sandbox business persona) | Display domain |
| --- | --- | --- |
| CloudDesk Software (`clouddesk`) | `agentpal.clouddesk.c3844c@business.example.com` | `clouddesksoftware.com` |
| Bluepeak Electronics (`bluepeak`) | `agentpal.bluepeak.df8162@business.example.com` | `bluepeakelectronics.com` |
| Northwind Office Supply (`northwind`) | `agentpal.northwind.16742b@business.example.com` | `northwindofficesupply.com` |
| Shady Deals Outlet (`shadydeals`) | `agentpal.shadydeals.905fd8@business.example.com` | `shadydeals-outlet.top` |

Freelancers (`scenarios.FREELANCERS`): Priya Shah `agentpal.priya.cbb860@personal.example.com`
and Tom Walsh `agentpal.tom.53025e@personal.example.com`. A test keeps every mailbox in sync
with `agentpal/demo/paypal_sandbox_personas.json`.

Domains are fictional display strings that the agents never fetch. Vendor domains
deliberately avoid `*.example`: the risk engine reads `x.example` as a lookalike of the
approved `example.com`, which scored a legitimate vendor at 100 during the live smoke.

## Safety design (what the code enforces, not the model)

* **Payees** come only from the reviewed catalogue vendor entry (for payments) or
  `FREELANCERS` plus the errand's approved invoice (for payouts). The tool schemas contain no
  payee field. A model-supplied email, an unknown `payee_id`, or a payee without an invoice
  for this errand is refused.
* **Offers**: the model may pick only an `offer_id` it was shown in *this* errand. That is
  either a Channel3 offer from the latest search, normalised by Shopper's reviewed code, or
  a catalogue item listed through `list_vendor_catalogue` for a vendor the errand allows.
  The amount must be a decimal string equal to the listed price.
* **Spend ceiling** = min(errand budget, wallet `available`, wallet `per_transaction_limit`).
  It comes from `GET /v1/me/wallet` before planning and can only shrink on re-read. Items
  above the ceiling are not even offered. AgentPal re-checks every limit atomically
  server-side.
* **One request per errand.** Two request calls in one model reply are refused. Read-only
  calls in the same reply run first, whatever order the model used.
* **At most 5 model calls per errand** (429 retries included), with free models only
  (`:free`, the shared `FREE_MODELS` fallback list, `OPENROUTER_MODELS` validated).
  `parallel_tool_calls: false` is sent.
* **Model failure** stops the errand with nothing requested. Its planning claim is sealed,
  so a rerun will not spend model calls again unless you pass `--replan`.
* **Untrusted data**: tool results are wrapped as `untrusted_data` with
  `instructions_in_data: IGNORE`. Instruction-shaped or email-bearing product/catalogue text
  is withheld from the model.
* **Secrets** (OpenRouter, Channel3, every agent key, owner token, any `apk_`/`apo_`/`sk-`
  string) are redacted from prompts, downstream tool arguments, events, journal and output.
* **Network allow-list**: OpenRouter chat completions, Channel3 `/v1/search`, and these
  agent routes only: `GET /v1/me[/wallet]`, `POST/GET /v1/payment-requests[/{id}]`,
  `POST /v1/payment-requests/{id}/execute`, `POST /v1/payout-requests` and its `/execute`.
  Provisioning has its own owner allow-list (list/create agents, list/create wallets).
* **Idempotency**: the journal key is `company-agent-errand-YYYY-MM-DD`. The AgentPal
  `Idempotency-Key` is derived from it (`shopper-<sha256>` for payments, `payout-<sha256>`
  for payouts). Reruns of the same day report statuses without model calls. A lost create
  is replayed with the same frozen body and key. The journal also binds origin, agent key
  scope and catalogue version: a changed catalogue makes the errand stop with
  `errand_conflict…` (start a new day or journal).
* **Dry run** (`--dry-run`) plans and prints the would-be request. It never writes to
  AgentPal (enforced again in the operator) and never touches the durable journal.

A **scripted planner** (`--planner scripted` or `COMPANY_PLANNER=scripted`) replaces the
model with a deterministic script that emits ordinary tool calls, so every guard still
applies. Use it when the shared free-model quota (~50 requests/day) is spent. A full company
day with the LLM costs roughly 2–3 model calls per errand (16–24 for Northstar).

## Credentials

| Variable | Used by | Meaning |
| --- | --- | --- |
| `AGENTPAL_OWNER_TOKEN` | `provision` only (owner) | `apo_…` owner token; never given to agents |
| `COMPANY_AGENT_KEYS_FILE` | runner / Render / HQ | Path to the private 0600 JSON file `{slug: apk_…}` written by `provision` (refused if group/world-readable) |
| `COMPANY_AGENT_KEYS` | runner / Render / HQ | Alternative: the same JSON object as a secret env var (use this on Render/Astropods) |
| `AGENTPAL_BASE_URL` | runner | AgentPal origin (HTTPS, or HTTP on loopback only) |
| `OPENROUTER_API_KEY` | runner (`llm` planner) | OpenRouter key; `proxy-injected` in the dev container |
| `CHANNEL3_API_KEY` | Channel3 errands | Channel3 key (`x-api-key`) |
| `OPENROUTER_MODELS`, `COMPANY_PLANNER`, `COMPANY_JOURNAL` | optional | free-model override; `llm`/`scripted`; journal path |

Slugs: `northstar-atlas`, `northstar-forge`, `northstar-muse`, `northstar-ledger`,
`lumen-orbit`, `lumen-pulse`, `lumen-keel`.

## Provision (owner, run locally by the lead)

```sh
cd agentpal
export AGENTPAL_OWNER_TOKEN=…   # from your shell/vault, never typed into a file in the repo
python -m agentpal.agents.company provision --base-url https://agentpal-api.onrender.com \
  --keys-out ~/.agentpal/company-agent-keys.json            # add --company northstar|lumen
```

The command creates each missing agent (role `external`, the capabilities above) and a USD
wallet (monthly budget plus per-transaction limit). It writes new keys atomically to the
`--keys-out` file with mode 0600, saving after each agent, and never prints them. It is
idempotent: agents that already exist by name are skipped, though a missing wallet is still
created. For an existing agent whose key is not in the file, rotate its credential in the
dashboard.

## Run a business day

```sh
export AGENTPAL_BASE_URL=… COMPANY_AGENT_KEYS_FILE=~/.agentpal/company-agent-keys.json
export OPENROUTER_API_KEY=… CHANNEL3_API_KEY=…
python -m agentpal.agents.company run --company northstar            # whole day, LLM planner
python -m agentpal.agents.company run --company lumen --agents pulse  # one agent
python -m agentpal.agents.company run --company lumen --dry-run --planner scripted
python -m agentpal.agents.company run --company northstar --wait      # wait, then execute approved
```

Output is readable `[Company · Agent] step (details)` lines; add `--json` for JSON lines.
Without `--wait`, each errand stops at `awaiting_approval`. With `--wait`, payment errands
poll for the owner's decision and execute approved requests once (an ambiguous execution is
never retried). Payout status is owner-only, so for payouts `--wait` makes one execute
attempt: AgentPal refuses it (409) until the owner approves, and the claim is released. A
later rerun with `--wait` executes once approved. Other options: `--day YYYY-MM-DD`,
`--replan`, `--journal PATH`.

## Render Workflows

Per https://render.com/docs/workflows-defining-flows.md (read 2026-11-06) and the installed
SDK `render==1.7.0`, `agentpal.agents.shopper.render_workflow.create_workflows()` now
registers both `shop_errand` and **`company_day`** (timeout 2 h). Registration was verified
with the real SDK: `get_task_names() == ['shop_errand', 'company_day']`. The deployed workflow
(`agentpal-shopper`, start command `python -m agentpal.agents.shopper.render_workflow`) needs
no new service: add `COMPANY_AGENT_KEYS` (secret) and `CHANNEL3_API_KEY` /
`OPENROUTER_API_KEY` if absent, redeploy, then trigger `company_day` with `["lumen"]` or
`["northstar", "scripted"]`.

Steps (fixed order, deterministic on replay): `business_day`, then for each agent and errand
`<agent>.<errand>.claim_planning` → `.plan` → `.request`. The time read happens inside a
step. The claim records the process nonce before the bounded model loop. A replay on a new
instance after an interrupted `.plan` returns `stopped_safely` for that errand: no second
model loop and no request. `.request` re-sends the checkpointed frozen body with the
day-derived Idempotency-Key, so a replayed create gets AgentPal's stored response.

**SDK finding:** in `render` 1.7.0 a step that *raises* ends the whole run *even if the flow
catches the error* (`durable_context._run_attempts`). Company steps therefore **return** a
`{"status": "stopped_safely", "error": code}` value instead of raising, so one failed errand
does not abort the company day. The flow does not wait for owner decisions; use the CLI
`--wait` or rerun later.

## Company HQ (Astropods page)

`hq/web.py` follows the Shopper app pattern. It sends the same security headers (strict CSP
with no inline script, nosniff, no-referrer, no-store) and accepts only same-origin,
JSON-only POSTs with an 8 KiB cap. Per-browser session cookies isolate job reads, and job and
event retention is bounded. It runs one business day at a time to protect the free model
quota. A visitor picks a company, sees the cast (no mailboxes or keys), presses **Start the
business day**, and watches steps plus a results table. The durable journal makes repeated
starts of the same company day replay statuses instead of creating requests or spending
model calls.

Local:

```sh
cd agentpal
COMPANY_PLANNER=scripted uvicorn agentpal.agents.company.hq.web:app --port 8801 --no-access-log
```

Deploy as a **second blueprint** (operator steps, from the repository root; not run by CO-A2):

```sh
cd agentpal/agentpal/agents/company/hq
ast spec validate
ast secrets create OPENROUTER_API_KEY
ast secrets create CHANNEL3_API_KEY
ast secrets create COMPANY_AGENT_KEYS          # paste the JSON object from the keys file
ast secrets create AGENTPAL_BASE_URL --plain --value https://agentpal-api.onrender.com
ast blueprint push
ast blueprint deploy agentpal-company-hq \
  --var OPENROUTER_API_KEY=@OPENROUTER_API_KEY --var CHANNEL3_API_KEY=@CHANNEL3_API_KEY \
  --var COMPANY_AGENT_KEYS=@COMPANY_AGENT_KEYS --var AGENTPAL_BASE_URL=@AGENTPAL_BASE_URL
```

`astropods.yml` uses build context `../../../..` (the Python project directory) and
`agentpal/agents/company/hq/Dockerfile`. The image copies only `agentpal/agents` (including
the catalogue), installs httpx/fastapi/uvicorn and serves port 80. Use one replica: job state
is process-local. Keep `COMPANY_JOURNAL` on durable storage if you restart.

## Demo script (video)

1. **Cast** (`… cast`, or the HQ page): two companies, seven agents, each with its own key,
   wallet, and minimal capabilities.
2. **Lumen · Orbit** buys a CI seat from CloudDesk Software and a USB-C hub from Bluepeak.
   *Shows:* payee binding (`merchant_paypal_email` is in `terms_sha256`). Low risk: only
   UNAPPROVED_MERCHANT and MISSING_EVIDENCE. Owner **approves**.
3. **Lumen · Pulse, "speaker-deal"**: a JBL Charge 5 for 6.99 from Shady Deals Outlet.
   *Shows:* the agent itself relays the advisory market check ("only 0.04x the market median:
   too good to be true…", "shadydeals-outlet.top is not among the retailers…"). The owner
   dashboard shows SUSPICIOUS_TLD and UNAPPROVED_MERCHANT. Owner **rejects** on camera, and
   the rejection is final for that errand.
4. **Northstar · Forge, "theme"**: the 24.00 bundle exceeds the 15.00 errand budget and is
   never offered. *Shows:* deterministic budget guards ahead of the model.
5. **Northstar · Ledger / Lumen · Keel payouts** to Priya Shah and Tom Walsh. *Shows:*
   payees only from config. The owner approves exact payout terms, and `--wait` then
   executes in the sandbox.
6. **Rerun the same day.** *Shows:* idempotency. Every errand reports "already requested
   today", with no model calls and no duplicate requests.
7. Optional: `--dry-run` shows planning without any AgentPal write.

Offline evidence for the remaining safety behaviours (invented payee, price mismatch,
injection text, non-free model, model failure, 5-call cap) is in
`tests/test_company_agents.py`.

## Verification

```sh
cd agentpal
python -m pytest -q -o addopts="" tests/test_company_agents.py tests/test_shopper_agent.py
python -m ruff check agentpal tests training
```

Results and the live smoke are recorded in the CO-A2 report (`/tmp/co-a2-report.md`).
