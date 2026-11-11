# AgentPal MCP: agent tools with a human approval gate

AgentPal's local MCP server lets an agent application discover and call the existing
agent API. Run `python -m agentpal.mcp_server`. It uses newline-delimited JSON-RPC 2.0
over stdio, negotiates protocol version `2025-06-18`, and adds no dependencies.
Wallets are virtual allowances; all provider execution remains PayPal sandbox only.

There is deliberately **no approve, capture, refund or payee-change tool**. A model's
tool confirmation is not the authenticated owner's approval in AgentPal. The human
reviews exact terms in the dashboard; the API checks that approval again on execution.
Neither the MCP server nor its agent key can give human consent.

| Tool | Agent API / result |
|---|---|
| `get_wallet` | `GET /v1/me/wallet`: this agent's allowance |
| `request_payment` | `POST /v1/payment-requests`: status, request ID, exact terms and approval notice |
| `get_payment_request` | `GET /v1/payment-requests/{id}`: status, terms, verification; payer link only when supplied by the API |
| `list_my_payment_requests` | `GET /v1/payment-requests`: only this agent's requests |
| `execute_approved_payment` | Only `POST /v1/payment-requests/{id}/execute`: server-enforced approval gate |

`request_payment` requires decimal-string `amount`, `currency`, `merchant_name`, and
`purpose`. Optional strings: `merchant_domain`, `product_query`,
`business_justification`, `idempotency_key`. Unknown properties and numeric amounts
are refused. Merchant PayPal destination changes are not exposed through MCP.

Without an explicit key, creation uses `mcp-` plus SHA-256 of the canonical, sorted
argument body. Identical arguments retain the same key across process restarts and
do not create another request. For an intentional second purchase with identical
terms, supply a **new explicit idempotency key**. Retrying an uncertain creation must
preserve both key and arguments. The API rejects changed arguments under an existing
key. Execution has no automatic retry: read status and ask the owner to reconcile an
uncertain provider outcome.

## Install and configure

From the project directory, use the same Python environment as the server:

```sh
python -m pip install -e .
python -m agentpal serve --platform
```

Follow [DEVELOPER_API.md](DEVELOPER_API.md) to register a scoped external agent and
create its wallet. Normal preparation needs `wallet.read`, `payment.prepare`, and
`owner.request_decision`; execution additionally needs the owner's explicit
`payment.execute_sandbox` grant. Give the MCP process only that agent's key.

Configuration comes from the environment only:

| Variable | Value |
|---|---|
| `AGENTPAL_BASE_URL` | API origin, default `http://127.0.0.1:8000`; optional `/v1` suffix |
| `AGENTPAL_AGENT_KEY` | Required agent credential (`apk_…`); owner credentials are refused |

No CLI credential arguments or dotenv loading are supported. HTTP uses a 30-second
timeout (5-second connect timeout), refuses redirects and ignores proxy environment
settings. Errors never reflect API error bodies or exception text, and `apk_` values
are redacted from outgoing results. API refusals are MCP tool results with
`isError: true`; malformed protocol/tool parameters use JSON-RPC errors.

### Claude Desktop

Open Settings → Developer → Edit Config and add the following `mcpServers` entry to
your **private local** `claude_desktop_config.json`. Replace the placeholder locally;
never commit a credential. Use an absolute Python executable path if the application's
PATH differs from your shell, and install AgentPal into that interpreter first.

```json
{
  "mcpServers": {
    "agentpal": {
      "command": "python",
      "args": ["-m", "agentpal.mcp_server"],
      "env": {
        "AGENTPAL_BASE_URL": "http://127.0.0.1:8000",
        "AGENTPAL_AGENT_KEY": "<your scoped agent key, supplied privately>"
      }
    }
  }
}
```

Restart Desktop and check that exactly five AgentPal tools appear. See the
[official local-server setup guide](https://modelcontextprotocol.io/docs/develop/connect-local-servers).

### Claude Code

Use the same JSON structure in `.mcp.json`, with the key value changed to
`${AGENTPAL_AGENT_KEY}`. Export the key securely in the environment that launches
Claude Code. Claude Code expands that variable; do not put an owner token in it.
Open `/mcp` to inspect the server connection. Project MCP trust/confirmation is
separate from payment approval. See
[Claude Code MCP configuration](https://code.claude.com/docs/en/mcp).

### Cursor

Use the same `mcpServers` JSON structure in `.cursor/mcp.json` (project) or
`~/.cursor/mcp.json` (user), adding `"type": "stdio"` inside the `agentpal` entry.
Set the key value to `${env:AGENTPAL_AGENT_KEY}` and make
that environment variable available to Cursor's process. Enable AgentPal in Cursor's
MCP settings and check tool discovery. See [Cursor's MCP guide](https://cursor.com/docs/mcp).

These are documented configuration recipes, not claims of interactive validation in
each desktop client. The automated suite tests the handler against the real FastAPI
app and launches the module over stdio against a local HTTP bridge to that app.

## Example conversation

1. Human: “Check your allowance, then request $1 for printer paper from Example Hosting.”
2. Agent calls `get_wallet`, then `request_payment` with `amount: "1.00"`,
   `currency: "USD"`, `merchant_name: "Example Hosting"`, `purpose: "Printer paper"`.
3. Result: `awaiting_approval`, a request ID and:
   “A human must approve these exact terms in AgentPal before any money moves.”
4. The human opens AgentPal, reviews the exact terms and fingerprint, and approves.
   Asking the agent to approve cannot work: no such tool exists.
5. Agent reads `get_payment_request`, then calls `execute_approved_payment` after
   approval. An early execution is refused. Provider submission is not completion.
6. For PayPal sandbox, the human opens the buyer approval link from the dashboard,
   approves as a sandbox buyer and captures through the owner UI. The agent may read
   status again; only a verified provider outcome counts as paid. The current agent
   API does not expose the owner-only reconciliation link, and MCP does not fetch it.

See [DEMO_SCENARIOS.md](DEMO_SCENARIOS.md) for the owner walkthrough and
[STANDARDS.md](STANDARDS.md) for the comparison with payment standards. MCP is a tool
interface, not AP2/ACP/x402 interoperability or a payment authorization standard.
