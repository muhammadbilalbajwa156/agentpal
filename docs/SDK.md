# Python SDK

The SDK provides asynchronous `OwnerClient` and `AgentClient` wrappers for the `/v1` API. It uses `httpx`, returns response dictionaries and raises typed errors. It does not run an agent, approve on an agent's behalf or bypass financial controls. All payment execution is sandbox-only, and wallet allowance is virtual (`is_real_money: false`).

## Install

From the `agentpal/` project directory, install the SDK package separately from the server:

```bash
python -m pip install -e sdk/python
```

The server must be running locally with `PlatformDeps` injected into `create_app`. Owner credentials come from the configured identity service. Registration returns a one-time agent API key; put it in secure storage and do not log the returned registration or rotation dictionary.

## Owner setup

Amounts accept `Decimal` or decimal strings. A Python `float` raises `TypeError` before any HTTP request. Integers and booleans are not SDK amount types either.

```python
import asyncio
import os
from decimal import Decimal

from agentpal_sdk import OwnerClient

BASE_URL = "http://127.0.0.1:8000"

async def setup():
    async with OwnerClient(
        base_url=BASE_URL,
        credential=os.environ["AGENTPAL_OWNER_TOKEN"],
    ) as owner:
        registration = await owner.register_agent(
            name="procurement-bot",
            capabilities=[
                "wallet.read", "payment.prepare", "owner.request_decision",
                "payment.execute_sandbox",
            ],
        )
        agent_id = registration["agent"]["agent_id"]
        await owner.create_wallet(
            agent_id=agent_id,
            currency="USD",
            monthly_budget=Decimal("100.00"),
            per_transaction_limit="50.00",
        )
        # Deliver registration["api_key"] to secure agent credential storage here.
        # Do not print or log registration.

asyncio.run(setup())
```

The external role's defaults omit sandbox execution. The example explicitly grants it, within the role ceiling. Wallet limits remain capped by the operator, and the owner can later revoke permissions, suspend the agent or freeze its wallet.

## Agent preparation and execution

Run preparation as the agent, then present the exact returned terms to the human owner. The agent cannot approve its own request.

```python
import asyncio
import os
from decimal import Decimal

from agentpal_sdk import AgentClient

async def prepare():
    async with AgentClient(
        base_url="http://127.0.0.1:8000",
        credential=os.environ["AGENTPAL_AGENT_KEY"],
    ) as agent:
        payment = await agent.create_payment_request(
            amount=Decimal("49.99"),
            currency="USD",
            merchant_name="Example Hosting",
            merchant_domain="example.com",
            purpose="Monthly hosting plan",
            idempotency_key="hosting-plan-0001",
        )
        # Present payment["terms"] and payment["terms_sha256"] for human review.
        return payment

payment = asyncio.run(prepare())
```

The human owner's application supplies the reviewed request ID and digest. Only after explicit human approval should it call:

```python
async with OwnerClient(
    base_url=BASE_URL,
    credential=os.environ["AGENTPAL_OWNER_TOKEN"],
) as owner:
    await owner.approve_payment_request(
        reviewed_request_id,
        terms_sha256=reviewed_terms_sha256,
    )
```

Then the agent may request execution:

```python
async with AgentClient(
    base_url=BASE_URL,
    credential=os.environ["AGENTPAL_AGENT_KEY"],
) as agent:
    result = await agent.execute_payment_request(reviewed_request_id)
```

Submission is not confirmation. Check the returned lifecycle status and subsequent request reads. Only a verified provider outcome can report `provider_confirmed`; `reconciliation_required` needs provider reconciliation rather than a new payment attempt.

## Idempotency and errors

`create_payment_request` generates an idempotency key when none is provided. For retries across process restarts or after a lost response, supply a stable key and preserve the same body. Reusing a key with different terms raises a conflict. The SDK does not automatically retry requests; automatic retries of financial operations could conceal an unknown outcome.

All API and transport failures raise an `AgentPalError` subclass. Errors expose `.code`, `.status_code` and `.idempotency_key`. The exported types are `AuthenticationError`, `PermissionDeniedError`, `NotFoundError`, `ConflictError`, `ValidationError`, `RateLimitError`, `NotConfiguredError`, `ProviderError` and `TransportError`. The SDK's error text does not include credentials or reflect an arbitrary server body.

`TransportError` distinguishes a network failure from an API refusal and retains the generated creation key in `.idempotency_key`. Retry creation with that key and the unchanged body to recover a lost response safely.

## Testing with the real app

The SDK accepts a `transport=` argument, including `httpx.ASGITransport(app=app)`, and a configurable `timeout=` (30 seconds by default). Its tests use `httpx.ASGITransport` against the real FastAPI app, with fake owner, approval and lifecycle gateways. No live provider credentials or network money movement are needed. Run from the project directory:

```bash
python -m pytest -o addopts="" -q -p no:cacheprovider tests/test_sdk_clients.py
```

See [DEVELOPER_API.md](DEVELOPER_API.md) for route coverage, gateway wiring and curl examples, and [api/openapi.json](api/openapi.json) for the generated wire contract.

## Real local HTTP and APIMatic checks

The SDK source is preserved. `python -m apimatic sdk-smoke` starts a disposable real
`python -m agentpal serve --platform` process and exercises these clients over TCP,
including authenticated owner decisions, creation replay/conflict and local simulator
submission. `tests/test_sdk_live.py` checks this path and cleanup. It needs no PayPal or
APIMatic key and never claims a verified provider outcome.

APIMatic-generated Python output is kept separately under ignored
`apimatic/generated/python/`; it never overwrites this package. The key-gated generator
and generated-client HTTP probe are described in [APIMATIC_INTEGRATION.md](APIMATIC_INTEGRATION.md).
Generation and that generated-client probe remain BLOCKED BY CREDENTIALS until a real
artifact exists. The generated probe disables v4 retries and performs actual generated
health/agent/owner reads; payment flow coverage currently comes from the preserved SDK.

## End-to-end walkthrough (owner, agent, PayPal sandbox buyer)

```python
import asyncio
from agentpal_sdk import AgentClient, OwnerClient

BASE = "http://127.0.0.1:8000"

async def main(owner_token: str):
    async with OwnerClient(base_url=BASE, credential=owner_token) as owner:
        reg = await owner.register_agent(name="shopping-agent",
                                         capabilities=["wallet.read", "payment.prepare",
                                                       "owner.request_decision",
                                                       "payment.execute_sandbox"])
        agent_key = reg["api_key"]                      # shown ONCE; store it securely
        await owner.create_wallet(agent_id=reg["agent"]["agent_id"], currency="USD",
                                  monthly_budget="100.00", per_transaction_limit="25.00")

    async with AgentClient(base_url=BASE, credential=agent_key) as agent:
        wallets = await agent.get_wallet()              # {"wallets": [...]}
        print(wallets["wallets"][0]["allowance"])       # available / reserved / pending / spent
        p = await agent.create_payment_request(
            amount="1.00", currency="USD", merchant_name="Northwind Office Supply",
            merchant_domain="northwind.example", purpose="Printer paper",
            merchant_paypal_email="agentpal.northwind.16742b@business.example.com",  # payee
            product_query="printer paper 500 sheets")   # optional Channel3 market check

    async with OwnerClient(base_url=BASE, credential=owner_token) as owner:
        view = await owner.get_payment_request(p["request_id"])
        # Show the owner view["terms"] (incl. terms.merchant_paypal_email), view["risk_signals"],
        # view["market_check"] and view["page_check"] BEFORE approving the exact digest:
        await owner.approve_payment_request(p["request_id"], terms_sha256=view["terms_sha256"])

    async with AgentClient(base_url=BASE, credential=agent_key) as agent:
        await agent.execute_payment_request(p["request_id"])   # creates the PayPal order

    async with OwnerClient(base_url=BASE, credential=owner_token) as owner:
        rec = await owner.get_reconciliation(p["request_id"])
        print(rec["provider_references"][0]["payer_action_url"])  # the BUYER approves on PayPal
        # after the buyer approves (409 until PayPal registers it; nothing is captured meanwhile):
        done = await owner.capture_payment_request(p["request_id"])   # provider_confirmed
        refund = await owner.refund_payment(p["request_id"], amount="0.40")
        # refund["status"]: COMPLETED, or FAILED with failure_reason, e.g.
        # "merchant_permission_required" when the capture was paid to ANOTHER merchant.
```

Errors you may see: `409 market_check_unavailable` (free refresh with no matched product),
`409 page_url_unavailable`, `403 mfa_required` (owner MFA enabled), `429 rate_limited`.
Payouts follow the same pattern: `AgentClient.create_payout_request` → owner
`approve_payout_request(terms_sha256=...)` → agent `execute_payout_request`.
See also `PAYPAL_SANDBOX.md`, `PAYPAL_REFUNDS.md`, `PAYPAL_PAYOUTS.md`.

The Python owner and agent clients accept the server origin with or without a trailing
slash or `/v1` (for example `https://example.test`, `https://example.test/`,
`https://example.test/v1` and `https://example.test/v1/`). All four forms generate a
single `/v1/` API prefix.
