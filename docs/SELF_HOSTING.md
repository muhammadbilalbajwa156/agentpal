# Self-hosting AgentPal (PC or VPS)

AgentPal runs as one Python process: the `/v1` API, the owner dashboard at `/app/`, and a
SQLite ledger on local disk. **Everything is PayPal sandbox only: no real money moves.**

## 1. Install and start (about 2 minutes)

```sh
git clone https://github.com/muhammadbilalbajwa156/agentpal.git && cd agentpal
python3 -m venv .venv && . .venv/bin/activate        # Python 3.12 or 3.13
pip install -r requirements.txt
python -m agentpal start
```

The console prints two lines:

```
Dashboard: http://127.0.0.1:8000/app/
One-time setup code: <code>
```

`start` keeps its data in `./data` (database, encryption key and the setup code file, all
created with owner-only permissions). Set `AGENTPAL_DATA_DIR` to put it elsewhere.

## 2. Create your workspace

1. Open the dashboard link on the same machine.
2. Choose **Create your workspace**, enter a workspace id, a display name and the setup code.
3. AgentPal shows your **owner access token** (`apo_…`) once. Save it in your password manager.
   Only its salted hash is stored; it lasts 30 days, and `python -m agentpal new-owner` issues more.

The setup code works once, only while no owner exists, and only from the machine itself
(loopback). Sign-up is then closed.

## 3. Connect your services (or skip)

The **Connect your services** step asks for five services (six values). Each key is checked with
one read-only call to that provider's official API before it is saved. It is then stored encrypted
(Fernet) and never shown again. You can skip any of them and add them later under
**Integrations**.

| Service | Values | Used for | Where to get it |
|---|---|---|---|
| PayPal sandbox | Client ID, Secret (Webhook ID optional) | Orders, capture, refunds, payouts, reconciliation | developer.paypal.com → Apps & Credentials → **Sandbox** app |
| Channel3 | API key | Product search and market price check | trychannel3.com dashboard |
| Kernel | API key | Cloud browser page checks | onkernel.com dashboard |
| Groq | API key | Test Lab AI shopping agent (preferred) | console.groq.com → API keys |
| OpenRouter | API key | Test Lab AI agent fallback | openrouter.ai → Keys |

Without PayPal keys, payments run against the built-in **local sandbox simulator**, clearly
labelled "simulated". Without Channel3 or Kernel keys, those advisory checks say "not configured".
Only the PayPal **sandbox** host is ever called; live PayPal URLs are refused at startup.

## 4. Try it: Test Lab

**Test Lab** in the sidebar creates a scoped test agent (with a $200/month, $50 per payment
virtual allowance) and lets you:

1. ask the AI agent to shop ("USB-C hub for the design team", budget 30), or create a payment
   request by hand;
2. review and **approve the exact terms** under Approvals (the agent can never approve);
3. execute, then open the PayPal sandbox approval page and log in with a sandbox **personal**
   (buyer) account from developer.paypal.com → Sandbox accounts;
4. capture, then refund. Each step appears in the request's timeline.

The Test Lab agent goes through the same policy rules as any external agent. Errands using
server-provided keys are limited to 25 per owner in a sliding 24-hour window, including failed
and policy-blocked errands. Workspace-owned provider keys are exempt from shared-resource caps.

## 5. Connect your own agents

Create agents and wallets in the dashboard, then follow [DEVELOPER_API.md](DEVELOPER_API.md),
the Python SDK ([SDK.md](SDK.md)), or the OpenAPI/Postman downloads in the Developer Portal.

## Running on a VPS

* Keep AgentPal on `127.0.0.1` and put a TLS reverse proxy (Caddy, nginx) in front, or start it
  with `--ssl-certfile/--ssl-keyfile`. Add your public host name to `AGENTPAL_ALLOWED_HOSTS`.
* First-run setup only works from loopback: create the workspace over an SSH tunnel
  (`ssh -L 8000:127.0.0.1:8000 your-vps`) and open `http://127.0.0.1:8000/app/`.
  Behind a reverse proxy on the same host every request looks local, so the one-time setup code
  (printed on the console, 32 random characters, single use, rate limited) is what protects first-run
  setup: keep it private and finish setup before exposing the site.
* Set `AGENTPAL_SECRETS_KEY` (a Fernet key: `python -c "from cryptography.fernet import Fernet;
  print(Fernet.generate_key().decode())"`) in the service environment and back it up; without it
  stored provider keys cannot be decrypted. Back up the data directory.
* Hosted multi-workspace mode (`AGENTPAL_MODE=hosted`) requires `AGENTPAL_SECRETS_KEY`; new
  workspaces need `AGENTPAL_SIGNUP_INVITE_CODE`, otherwise sign-up is disabled.

## Environment variables (optional)

| Variable | Meaning |
|---|---|
| `AGENTPAL_DATA_DIR` | Data directory (default `./data` for `start`) |
| `AGENTPAL_SECRETS_KEY` | Fernet key for stored provider keys (generated into the data dir when absent, self-hosted only) |
| `PAYPAL_SANDBOX_CLIENT_ID`, `PAYPAL_SANDBOX_CLIENT_SECRET`, `PAYPAL_SANDBOX_WEBHOOK_ID` | Server-wide PayPal sandbox keys, used by workspaces without their own |
| `CHANNEL3_API_KEY`, `KERNEL_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY` | Server-wide provider keys (never shown to workspaces) |
| `AGENTPAL_MODE` | `self_hosted` (default) or `hosted` |
| `AGENTPAL_SIGNUP_INVITE_CODE` | Hosted mode only: lets invited users create workspaces |
| `AGENTPAL_HOSTED_MAX_AMOUNT` | Hosted server-PayPal per-transaction ceiling, default `50.00`; applies to wallet limits, payment requests and payout requests, and is rechecked before execution/capture |
| `AGENTPAL_HOSTED_MAX_MONTHLY_BUDGET` | Hosted server-PayPal monthly wallet budget and rolling-window ceiling, default `500.00` |
| `AGENTPAL_SERVER_ERRANDS_DAILY` | Shared-key errands per owner per 24 hours, default `25` |
| `AGENTPAL_SERVER_PAGE_CHECKS_DAILY` | Shared Kernel page checks, agent research browsing and Kernel re-verification per owner per 24 hours, default `30` |
| `AGENTPAL_SERVER_MARKET_CHECKS_DAILY` | Shared Channel3 initial checks, refreshes, Shopper searches and re-verification per owner per 24 hours, default `60` |
| `AGENTPAL_SERVER_MODEL_CALLS_DAILY` | Shared Groq/OpenRouter HTTP calls (including retries/fallbacks) and re-verification per owner per 24 hours, default `150` |
| `AGENTPAL_SERVER_PAYPAL_CHECKS_DAILY` | Shared PayPal credential re-verification per owner per 24 hours, default `30` |

Shared-resource admission is durable across restarts and owner-token changes, and atomic
across workers. Accounting follows the credentials selected for the operation; replacing
workspace keys cannot exempt an in-flight server-key call. An attempt consumes its allowance
before the provider call, even if the provider fails or a later rule blocks the proposal.
Exhaustion returns HTTP 429 with
`Retry-After` and `server_<category>_daily_limit`; unavailable quota storage fails closed.
Payment/payout capture, refund and reconciliation keep their existing financial controls.
Hosted amount refusals return HTTP 422 `hosted_amount_limit_exceeded`. Hosted ceilings apply
only while the owner uses server PayPal keys, even if a different sandbox payee is supplied.
Ordinary operator caps still apply to owners with their own keys. The UI shows hosted
ceilings in wallet creation and limit editing; configuration values are positive decimals.

## Limits

* A workspace's own PayPal keys are verified by authenticated reads. Signed webhooks are wired
  for the server-wide PayPal app only.
* If you replace your PayPal keys while an order is in flight, capture of that order fails safely
  (the new app cannot see it); reconcile it or create a new request.
* SQLite on one host; this is a hackathon sandbox build, not production-ready.

## Judging notes

Owner-token rotation or expiry does not revoke agent API keys previously created in that
workspace. Issuing a replacement owner token also does not revoke earlier owner tokens.
For end-of-event cleanup, the operator must explicitly revoke old owner credentials and
revoke or suspend the workspace's agent credentials. Agents still cannot approve payments.

Judges given the same owner token share mutable workspace data, agent/wallet settings,
request decisions and owner-keyed provider quotas. They can affect each other's walkthroughs;
the audit cannot distinguish people using the same token. Separate owner workspaces provide
data isolation. Separate tokens for one owner retain the same data and shared-provider quota.
These are documented limitations; this change adds no token cascade or judge-data isolation.
