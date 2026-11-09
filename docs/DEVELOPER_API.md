# Developer API v1


## Bootstrapping the first owner

```sh
export AGENTPAL_DB_PATH=$PWD/agentpal.db
python -m agentpal new-owner alice --name "Alice" --days 30   # prints the apo_ token once
python -m agentpal serve --platform
```

The token is printed once and only its hash is stored. Run the command again to issue another
token for the same owner. In Python (tests, embedding): `identity.create_owner("alice")` then
`token, info = identity.issue_token("alice")` on `build_platform(...).owner_authenticator`.

AgentPal connects agents supplied by developers to scoped financial controls. This API is a local, backend-only sandbox. Wallets represent virtual allowance; AgentPal holds no funds. Wallet, transaction and execution responses identify this with `is_real_money: false`.

## Enable the API

Run `python -m agentpal serve --platform` for the wired local sandbox platform. The factory injects strict identity, approval and lifecycle services; without PayPal sandbox configuration it uses the local simulator, which cannot verify completion. Library integrations can inject `PlatformDeps` into `create_app(..., platform=deps)`. Without that argument, `/v1` is absent. Owner authentication, approval, payment lifecycle and the sandbox provider are separate dependencies. A missing dependency returns `503 {"error":"not_configured"}` on the operations that require it.

```python
from agentpal.api import create_app
from agentpal.platform import PlatformDeps

# These objects are supplied by the integration layer, not by API clients.
deps = PlatformDeps(
    owner_authenticator=owner_authenticator,
    wallet_store=wallet_store,
    approvals=approval_gateway,
    lifecycle=lifecycle_gateway,
    provider=sandbox_provider,
    event_sink=provider_event_sink,
)
app = create_app(service=risk_service, settings=settings, platform=deps)
```

The existing API enforces loopback access, an allowed Host header and rate limits. No production PayPal or live money movement is supported. External webhook delivery to the loopback server remains an integration constraint.

## Authentication and authorization

Use `Authorization: Bearer <credential>`. Owner credentials begin with `apo_` and are validated by the injected `OwnerAuthenticator`. Agent credentials have the form `apk_<key_id>_<secret>` and contain a 256-bit random secret. Registration and rotation return the agent key once; persistent storage contains its SHA-256 digest. Store the returned key securely and avoid printing responses containing it.

Owner credentials and agent keys are distinct credential types. Wrong-type credentials are refused; legacy tokens do not authenticate `/v1`. Revoked, expired or suspended agent credentials cannot authenticate. Owners see only their own resources; unknown and other-owner resources both return `404`.

An external agent defaults to `wallet.read`, `payment.prepare` and `owner.request_decision`. Its owner must explicitly grant `payment.execute_sandbox` before execution. Every agent action is checked against the durable registry; capabilities claimed by agent input grant no authority. No role has both `purchase.propose` and `payment.execute_sandbox`.

## Sandbox walkthrough with curl

Set `AGENTPAL_OWNER_TOKEN` to an owner credential issued by the configured identity service. The example uses `jq` to capture one-time credentials without printing them. Run locally with shell tracing disabled.

```bash
BASE_URL=http://127.0.0.1:8000

registration=$(curl --fail-with-body -sS "$BASE_URL/v1/agents" \
  -H "Authorization: Bearer $AGENTPAL_OWNER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"procurement-bot","capabilities":["wallet.read","payment.prepare","owner.request_decision","payment.execute_sandbox"]}')
AGENT_ID=$(printf '%s' "$registration" | jq -r '.agent.agent_id')
AGENTPAL_AGENT_KEY=$(printf '%s' "$registration" | jq -r '.api_key')
unset registration

curl --fail-with-body -sS "$BASE_URL/v1/wallets" \
  -H "Authorization: Bearer $AGENTPAL_OWNER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"agent_id\":\"$AGENT_ID\",\"currency\":\"USD\",\"monthly_budget\":\"100.00\",\"per_transaction_limit\":\"50.00\"}"

payment=$(curl --fail-with-body -sS "$BASE_URL/v1/payment-requests" \
  -H "Authorization: Bearer $AGENTPAL_AGENT_KEY" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: hosting-plan-0001' \
  -d '{"amount":"49.99","currency":"USD","merchant_name":"Example Hosting","merchant_domain":"example.com","purpose":"Monthly hosting plan"}')
REQUEST_ID=$(printf '%s' "$payment" | jq -r '.request_id')
TERMS_SHA256=$(printf '%s' "$payment" | jq -r '.terms_sha256')
```

The human owner must review the request's exact terms before running the approval command. Neither the agent's request nor an external message is consent. A risk block or exhausted allowance can prevent preparation.

```bash
curl --fail-with-body -sS "$BASE_URL/v1/payment-requests/$REQUEST_ID/approve" \
  -H "Authorization: Bearer $AGENTPAL_OWNER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"terms_sha256\":\"$TERMS_SHA256\"}"

curl --fail-with-body -sS -X POST "$BASE_URL/v1/payment-requests/$REQUEST_ID/execute" \
  -H "Authorization: Bearer $AGENTPAL_AGENT_KEY"

curl --fail-with-body -sS "$BASE_URL/v1/me/wallet" \
  -H "Authorization: Bearer $AGENTPAL_AGENT_KEY"
```

Execution submission does not mean completion. Only the lifecycle service's verified provider outcome may report `provider_confirmed`. An unknown provider outcome requires reconciliation; do not create a replacement request or blindly retry payment execution.

## Routes

All paths below are prefixed with `/v1`. `owner / agent` means either credential may read its own resources.

| Method and path | Credential | Purpose |
|---|---|---|
| `GET /health` | none | Sandbox liveness |
| `POST /agents` | owner | Register an agent and issue its one-time key |
| `GET /agents`, `GET /agents/{agent_id}` | owner | List or read own agents |
| `POST /agents/{agent_id}/credentials` | owner | Rotate key; optional `overlap_seconds` |
| `DELETE /agents/{agent_id}/credentials/{key_id}` | owner | Revoke key |
| `POST /agents/{agent_id}/suspend`, `POST /agents/{agent_id}/reactivate` | owner | Change agent status |
| `GET /agents/{agent_id}/permissions`, `PUT /agents/{agent_id}/permissions` | owner | Read or replace grants within the role ceiling |
| `POST /wallets`, `GET /wallets` | owner | Create or list own virtual wallets |
| `GET /wallets/{wallet_id}`, `PATCH /wallets/{wallet_id}` | owner | Read or update wallet limits |
| `POST /wallets/{wallet_id}/freeze`, `POST /wallets/{wallet_id}/unfreeze` | owner | Control wallet availability |
| `GET /wallets/{wallet_id}/transactions` | owner | Ledger-backed transaction history |
| `GET /me`, `GET /me/wallet`, `GET /me/wallet/transactions` | agent | Own identity, allowance and transaction history |
| `POST /payment-requests` | agent | Prepare and reserve; requires `Idempotency-Key` |
| `GET /payment-requests`, `GET /payment-requests/{request_id}` | owner / agent | Scoped payment requests and lifecycle status |
| `POST /payment-requests/{request_id}/request-decision` | agent | Ask owner to decide; never approves |
| `GET /approvals` | owner | Own pending decisions |
| `POST /payment-requests/{request_id}/approve` | owner | Approve exact `terms_sha256` |
| `POST /payment-requests/{request_id}/reject` | owner | Decline request |
| `POST /payment-requests/{request_id}/execute` | agent | Submit approved request through sandbox lifecycle |
| `GET /payment-requests/{request_id}/reconciliation` | owner | Read lifecycle state and available provider references |
| `POST /providers/paypal/webhooks` | provider signature | Verify event before forwarding to the event sink |

`GET /me/wallet` and transaction reads require `wallet.read`. Preparation needs `payment.prepare`; requesting a decision also needs `owner.request_decision`. Set `request_owner_decision: false` to prepare first and ask later through `/request-decision`.

## Amounts, limits and idempotency

Send amounts as decimal strings, for example `"49.99"`; JSON floats are refused. Wallet limits are capped by operator `PlatformLimits`, and unsupported currencies are refused. Missing or frozen wallets fail closed. Allowance values come from the ledger: reserved, pending, spent and available are internal budget accounting, not a bank balance. Only verified completed outcomes count as spent.

A create key is scoped to the authenticated agent. Reusing the same `Idempotency-Key` with the same body replays the stored response (`200` instead of the initial `201`). A different body with that key returns `409 idempotency_key_reused`. A concurrent claim returns `409 idempotency_in_progress`; claims older than the configured timeout (60 seconds by default) are recovered. Preserve the same key and body when retrying a lost create response. A new key represents a new logical request.

Errors use a constant code, such as `{"error":"terms_mismatch"}`. Typical statuses are `401` invalid credential, `403` denied capability or credential type, `404` unknown/other-owner resource, `409` state or idempotency conflict, `422` invalid input, `429` rate limited, and `503` dependency unavailable. Error bodies do not echo credentials or submitted values.

Validation errors can also contain a sanitized `details` array with `loc`, `msg` and `type`; explicit platform validation refusals may omit it. This differs from FastAPI's default `detail` envelope. Authentication refusals include `WWW-Authenticate`, rate limits include `Retry-After`, and successful create replays include `Idempotent-Replayed: true`.

## Gateway contracts for integration

The exact Protocols live in `agentpal/platform/deps.py`. The integration layer must inject implementations with these signatures:

```python
class ApprovalGateway(Protocol):
    def approve(self, owner: AuthenticatedOwner, request_id: str, *,
                terms_sha256: str) -> Any: ...
    def decline(self, owner: AuthenticatedOwner, request_id: str) -> Any: ...

class LifecycleGateway(Protocol):
    def status(self, request_id: str, *, agent_id: str | None = None
               ) -> PaymentLifecycle: ...
    def begin_execution(self, agent_id: str, request_id: str, terms: ApprovalTerms,
                        provider: PaymentProvider) -> Any: ...
    def submit_to_provider(self, ticket: Any, provider: PaymentProvider
                           ) -> PaymentLifecycle: ...
    def reconcile(self, request_id: str) -> PaymentLifecycle: ...
```

The approval implementation revalidates the authenticated owner session, ownership and terms digest and persists a single-use, expiring approval. The route independently checks the digest before calling the gateway. The lifecycle implementation owns approval consumption, execution idempotency, provider operations, reconciliation and mapping ledger states to `PaymentLifecycle`. The platform has no production lifecycle mapping or provider-order table and does not call the legacy payment-attempt method.

An optional `provider_references(request_id: str) -> list[ProviderOrderRef]` extension supplies references for the reconciliation read. Without it, that response contains an empty list. The reconciliation GET reads state; it does not initiate a provider reconciliation operation. Webhooks use the injected sandbox provider's signature verification and send only verified matching-provider events to `ProviderEventSink`.

## OpenAPI and SDK

`docs/api/openapi.json` is the deterministic checked-in contract, including stable `operationId` values consumed by Postman. From the project directory:

```bash
python -m agentpal.platform.openapi --write
python -m agentpal.platform.openapi --check
```

The generator uses isolated in-memory stores and inert authentication dependencies. It needs no credentials or provider access. See [SDK.md](SDK.md) for the asynchronous owner and agent Python clients.

[APIMATIC_INTEGRATION.md](APIMATIC_INTEGRATION.md) records the contract review done with the APIMatic PayPal context plugin. Amounts are decimal strings on the wire (for example `"22.49"`); integers and floats are refused, matching the published contract.

## Complete payment journey (HTTP)

| Step | Who | Call |
|---|---|---|
| 1 | owner | `POST /v1/agents` (returns `apk_` key once), `POST /v1/wallets` |
| 2 | agent | `POST /v1/payment-requests` (Idempotency-Key; optional `merchant_paypal_email`, `product_query`/`product_url`) |
| 3 | owner | `GET /v1/payment-requests/{id}`: review `terms` (includes `merchant_paypal_email`), `risk_signals`, `market_check`, `page_check` |
| 4 | owner | `POST /v1/payment-requests/{id}/approve` `{"terms_sha256": ...}` (MFA if enabled) |
| 5 | agent | `POST /v1/payment-requests/{id}/execute` (creates the PayPal order) |
| 6 | buyer | open `provider_references[0].payer_action_url` from `GET .../reconciliation` and approve on PayPal |
| 7 | owner | `POST /v1/payment-requests/{id}/capture` → `provider_confirmed` only after a verified PayPal outcome (409 while the buyer has not approved; nothing is sent) |
| 8 | owner | `POST .../refunds` (Idempotency-Key) → `COMPLETED`, or `FAILED` with `failure_reason` |
| 9 | owner | `GET /v1/paypal/reconciliation?start=&end=`: compare with PayPal's own records |

Payouts: agent `POST /v1/payout-requests` → owner `.../approve` → agent `.../execute`.
Details: `PAYPAL_SANDBOX.md`, `PAYPAL_REFUNDS.md`, `PAYPAL_PAYOUTS.md`, `PAYPAL_OPERATIONS.md`.

### Owner request history and developer downloads

`GET /v1/payment-requests/{request_id}/timeline` and
`GET /v1/payout-requests/{request_id}/timeline` require an owner credential. Unknown
requests and another owner's requests return the same 404; agent credentials return
403. The response is `{request_id, events}`, sorted by UTC `at`, with each event carrying
`actor`, `kind`, a plain sentence `label`, nullable `detail`, and nullable `verified_by`.
It projects persisted creation, assessment/hard-rule refusal, advisory checks, owner
approval/rejection, execution, provider observations, capture, refunds and expiry.
It performs no provider calls and returns no raw provider payloads, credentials or
session identifiers. Order-reference events use the latest persisted provider-update
time and explicitly say that the original order-creation time is unavailable; an
execution claim alone never implies that PayPal created an order.

Owner payment reads include nullable `owner_decision: {decision, at}` and
`policy_block: {rule, label}`. The decision survives execution and expiry; policy blocks
are distinguished from human rejection. Agent responses keep both fields null, risk
signals empty and agent context null. Payout owner reads include the same decision
field; payout create/execute/agent reconciliation responses keep it null.

Payment and payout responses include nullable `verification: {verified_by, at}`.
`paypal_read` means an accepted authenticated PayPal GET; `paypal_webhook` means an
accepted signed webhook. A capture POST response alone cannot establish either method
and leaves this field null. Owner payment reconciliation preserves an audit-only GET
receipt after exact order/payee/amount matching and agreement with the settled ledger,
even when the provider event fingerprint duplicates an earlier capture response. These
receipts never authorize settlement or release spending holds. Rejected, unverified,
non-final or mismatched observations never become verification badges.

The dashboard exposes two unauthenticated static downloads at `/app/openapi.json` and
`/app/agentpal.postman_collection.json` (`application/json`). They are the exact committed
`docs/api/openapi.json` and `postman/v1.collection.json`, read once at startup. JSON files
in the frontend directory are never accepted. The existing method, CSP, size, symlink,
Host and HTTPS controls still apply.

Hosted bootstrap optionally imports a second owner from `AGENTPAL_JUDGE_OWNER_ID` and
`AGENTPAL_JUDGE_OWNER_TOKEN`, named **Judge workspace**, independently of the primary
`AGENTPAL_BOOTSTRAP_OWNER_ID`/`AGENTPAL_BOOTSTRAP_OWNER_TOKEN` pair. Set
`AGENTPAL_OWNER_TOKEN_DAYS` to an integer from 1 through 90 for both imports (default 30).
An invalid value refuses startup, even when no owner pair is configured. Imports preserve
existing expiry and revocation; changing the lifetime does not renew a previously
imported credential. Supply tokens through the host's secret environment; bootstrap
never prints them. The judge owner uses the same sandbox owner permissions, scoped to
its own workspace; this is not a new read-only credential role.
