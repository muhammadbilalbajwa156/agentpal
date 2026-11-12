# Judge walkthrough: rules, exact approval, verified sandbox payment

All amounts below are USD test amounts. Wallets are virtual allowance, not money.
Use a fresh, isolated workspace and the default Test Lab wallet ($200 monthly,
$25 per payment). Operator/hosted limits can be lower; check the wallet first.
For policy demos no external service is needed. For the happy path, configure
PayPal **sandbox** in Integrations and have a separate sandbox personal buyer.
The local simulator can show approval and submission, but cannot show verified
PayPal capture or refunds. Do not present a simulated result as settlement.

The API examples use the **human owner's** private token because Test Lab is an
owner UI for an ordinary scoped lab agent. It does not give agents approval rights.
Set `AGENTPAL_OWNER_TOKEN` securely with shell tracing disabled; never paste it into
the agent or MCP configuration. Run each sequence once, retaining IDs and keys on
lost responses. The API returns policy blocks as stored requests (`201`), not HTTP
errors; inspect `recommendation`, `policy_block` and `risk_signals`.

```sh
BASE_URL=http://127.0.0.1:8000
curl --fail-with-body -sS -X POST "$BASE_URL/v1/lab/agent" \
  -H "Authorization: Bearer $AGENTPAL_OWNER_TOKEN"
```

## Try an attack: one-click controls in Test Lab

Open **Test Lab → Try an attack**. Four buttons call the owner-only
`POST /v1/lab/scenarios/{name}` endpoint, using the ordinary Lab agent, create
path, rules and owner approval path. There are no matching-hash approvals,
execution, PayPal orders or captures in these scenarios.

| Button | Scenario | Judge sees | Virtual reservation in a fresh default wallet |
| --- | --- | --- | --- |
| Over the $25 limit | `over_limit` | $30 blocked by `AMOUNT_ABOVE_HARD_LIMIT` before owner review | $0 |
| Split a purchase | `split_purchase` | Second $15 request to the same merchant blocked by `SPLIT_TRANSACTION_EXCEEDS_HARD_LIMIT` in the existing rolling window | $0 (the first part is declined afterwards) |
| Change the terms | `changed_terms` | $1 original and $2 replacement; the original fingerprint is refused for the replacement with `terms_mismatch` | $0 (both declined afterwards) |
| Swap the payee | `payee_swap` | Two $1 requests with different merchant PayPal emails; exact-term binding refuses the original fingerprint for the replacement | $0 (both declined afterwards) |

The shield card replaces the disabled walkthrough steps, shows the actual rule or
refusal code, reason, amount and merchant, and offers **View timeline**. The terms
card says **Approval refused: the terms changed after the fingerprint was taken**.
Payee swap demonstrates exact-term binding; it does not claim a page-content
classifier or automatic domain-mismatch blocking. Requests are immutable: both
fingerprint examples create a replacement, and neither request is approved.
Afterwards, any demonstration request still awaiting a decision is declined through
the ordinary owner reject path, so attacks leave nothing in Approvals and no allowance
reserved.

Each click uses a fresh synthetic merchant to isolate demonstrations; split parts
within the same click share that merchant. Reservations accumulate under the
ordinary budget rules. Pending example requests can be rejected in Approvals to
release their holds. Existing lower hosted/operator limits, frozen wallets,
revoked Lab credentials, rate limits and shared daily Lab quotas still apply.
The endpoint returns `scenario`, `outcome`, `rule_codes`, `plain_reason`,
`request_ids`, and an owner-authenticated `timeline_url_hint`.

```sh
curl --fail-with-body -sS -X POST "$BASE_URL/v1/lab/scenarios/over_limit" \
  -H "Authorization: Bearer $AGENTPAL_OWNER_TOKEN"
```

The AI shopping errand now preserves **Original task** separately and shows its
model-selected search plan, up to three accepted Channel3 candidate offers and
the model's stated selection justification. Only returned facts are shown; one
offer yields one candidate, and an unavailable title is omitted. Candidates and
selection reasons remain advisory: **AI proposes · Rules decide · You approve**.

Offline proofs: `tests/test_platform_lab_scenarios.py`,
`tests/test_platform_lab_reasoning.py`, and the frontend workspace browser suite.

## A. Over-limit request: $30 blocked before a human decision

1. Open **Test Lab → Create a test payment request**.
2. Enter `30.00`, merchant `Limit Demo`, purpose `Printer paper`; create the request.
3. Judge sees **Blocked before any human was asked**, `AMOUNT_ABOVE_HARD_LIMIT`,
   and a shield card with the reason, amount, merchant and **View timeline**.
   The request remains visible for audit but never enters the
   pending approvals queue. No provider order is created.

API equivalent:

```sh
curl --fail-with-body -sS "$BASE_URL/v1/lab/payment-requests" \
  -H "Authorization: Bearer $AGENTPAL_OWNER_TOKEN" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: demo-over-limit-1' \
  -d '{"amount":"30.00","currency":"USD","merchant_name":"Limit Demo","purpose":"Printer paper"}'
```

## B. Split purchase: $15 + $15 exceeds the same merchant's $25 limit

1. In Test Lab create a `15.00` request to `Paper Supply`, purpose `Paper part one`.
   Under Optional merchant details set merchant domain to `paper.example`.
2. Leave it awaiting approval. Create another `15.00` request to the **same merchant
   and domain**, purpose `Paper part two`, immediately after the first.
3. Judge sees the first awaiting human approval and the second **Blocked by rule**:
   `SPLIT_TRANSACTION_EXCEEDS_HARD_LIMIT`. The rolling merchant commitments include
   the first reservation; neither dividing the amount nor changing the purpose
   bypasses the $25 hard limit. No approval or execution is offered for the second.

Proven through Test Lab API by
`tests/test_platform_demo_scenarios.py::test_lab_over_limit_and_split_purchase_are_blocked_before_approval`.
The calls below reproduce it in a fresh workspace (use new logical keys for a fresh
run; replaying these keys intentionally returns the original requests):

```sh
curl --fail-with-body -sS "$BASE_URL/v1/lab/payment-requests" \
  -H "Authorization: Bearer $AGENTPAL_OWNER_TOKEN" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: demo-split-1' \
  -d '{"amount":"15.00","currency":"USD","merchant_name":"Paper Supply","merchant_domain":"paper.example","purpose":"Paper part one"}'
curl --fail-with-body -sS "$BASE_URL/v1/lab/payment-requests" \
  -H "Authorization: Bearer $AGENTPAL_OWNER_TOKEN" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: demo-split-2' \
  -d '{"amount":"15.00","currency":"USD","merchant_name":"Paper Supply","merchant_domain":"paper.example","purpose":"Paper part two"}'
```

## C. Changed terms cannot inherit approval

Payment terms have no public edit endpoint. Demonstrate a replacement purchase,
rather than pretending the dashboard can mutate an approved request:

1. Create a $1 request to `Terms Demo`, purpose `Paper`. Review and approve its exact
   terms as the owner; save its `terms_sha256`.
2. Create a new $2 request to `Terms Demo`, purpose `Paper`; its digest differs.
3. Attempt to approve the $2 request with the $1 digest through the API below.
   Judge sees `409 {"error":"terms_mismatch"}`. The $2 request still awaits approval
   and cannot execute. An approval authorizes only its original exact request;
   the replacement needs a new human decision. The original request was not edited.

```sh
old=$(curl --fail-with-body -sS "$BASE_URL/v1/lab/payment-requests" \
  -H "Authorization: Bearer $AGENTPAL_OWNER_TOKEN" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: demo-terms-old-1' \
  -d '{"amount":"1.00","currency":"USD","merchant_name":"Terms Demo","purpose":"Paper"}')
OLD_ID=$(printf '%s' "$old" | jq -r '.request_id')
OLD_SHA=$(printf '%s' "$old" | jq -r '.terms_sha256')
# Human reviews the $1 terms before approving:
curl --fail-with-body -sS "$BASE_URL/v1/payment-requests/$OLD_ID/approve" \
  -H "Authorization: Bearer $AGENTPAL_OWNER_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"terms_sha256\":\"$OLD_SHA\"}"
changed=$(curl --fail-with-body -sS "$BASE_URL/v1/lab/payment-requests" \
  -H "Authorization: Bearer $AGENTPAL_OWNER_TOKEN" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: demo-terms-new-1' \
  -d '{"amount":"2.00","currency":"USD","merchant_name":"Terms Demo","purpose":"Paper"}')
NEW_ID=$(printf '%s' "$changed" | jq -r '.request_id')
# Expected refusal; deliberately omit --fail so the JSON error remains visible:
curl -sS -w '\nHTTP %{http_code}\n' "$BASE_URL/v1/payment-requests/$NEW_ID/approve" \
  -H "Authorization: Bearer $AGENTPAL_OWNER_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"terms_sha256\":\"$OLD_SHA\"}"
```

## D. Happy path: exact approval → PayPal buyer → capture → read proof → partial refund

1. In Test Lab create a `1.00` request to `Sandbox Test Store`, purpose `Printer paper`.
   For this refund demo leave the optional merchant PayPal email empty so the
   configured sandbox app's own merchant receives the payment. A routed merchant
   can require PayPal partner permission for refunds; do not promise that route works.
2. **Approve exact terms** opens owner review. Inspect amount, merchant/payee, purpose
   and SHA-256 fingerprint, then explicitly approve. Judge sees approval recorded.
3. Click **Execute as lab agent**. Judge sees sandbox provider submission and an
   **Open PayPal sandbox approval** link. This has not yet paid anything.
4. Open that link; sign in as a **sandbox personal buyer**, separate from the business
   seller, and complete approval. Return to AgentPal and click
   **Capture after buyer approval**. PayPal may take a few seconds to register buyer
   approval; an early capture is refused until that approval exists.
5. Judge sees `provider_confirmed` and **Verified by PayPal read** when the authenticated
   PayPal GET receipt is present. A capture POST alone does not justify that badge.
   If confirmation/read is unavailable, show its actual status and use owner
   reconciliation; never label an unknown result paid or execute a replacement.
6. Set **Refund amount (USD)** to `0.40`, click **Refund**, then refresh. Judge sees
   the refund's actual status; a verified `COMPLETED` partial refund credits $0.40 of
   counted spending while the original $1 terms remain unchanged. `PENDING` or an
   uncertain outcome is not a completed refund.
7. Open the request timeline: creation, policy, exact owner approval, provider
   submission/capture, verification evidence and refund show who did what and when.

API equivalents after creating the $1 lab request (set `REQUEST_ID` and reviewed
`TERMS_SHA256` from its response; the owner must review before running approval):

```sh
curl --fail-with-body -sS "$BASE_URL/v1/payment-requests/$REQUEST_ID/approve" \
  -H "Authorization: Bearer $AGENTPAL_OWNER_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"terms_sha256\":\"$TERMS_SHA256\"}"
curl --fail-with-body -sS -X POST "$BASE_URL/v1/lab/payment-requests/$REQUEST_ID/execute" \
  -H "Authorization: Bearer $AGENTPAL_OWNER_TOKEN"
# Human opens the returned payer_action_url and approves on PayPal sandbox first.
curl --fail-with-body -sS -X POST "$BASE_URL/v1/payment-requests/$REQUEST_ID/capture" \
  -H "Authorization: Bearer $AGENTPAL_OWNER_TOKEN"
# Explicit authenticated provider re-read if verification evidence is still absent:
curl --fail-with-body -sS -X POST "$BASE_URL/v1/payment-requests/$REQUEST_ID/reconciliation" \
  -H "Authorization: Bearer $AGENTPAL_OWNER_TOKEN"
curl --fail-with-body -sS "$BASE_URL/v1/payment-requests/$REQUEST_ID" \
  -H "Authorization: Bearer $AGENTPAL_OWNER_TOKEN"
curl --fail-with-body -sS "$BASE_URL/v1/payment-requests/$REQUEST_ID/refunds" \
  -H "Authorization: Bearer $AGENTPAL_OWNER_TOKEN" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: demo-partial-refund-1' \
  -d '{"amount":"0.40","reason":"Sandbox demo partial refund"}'
curl --fail-with-body -sS "$BASE_URL/v1/payment-requests/$REQUEST_ID/timeline" \
  -H "Authorization: Bearer $AGENTPAL_OWNER_TOKEN"
```

The policy, split and stale-digest examples are automated offline proofs against real
platform services. This runbook does not claim a new live PayPal/dashboard run.
Provider evidence and configuration: [PAYPAL_SANDBOX.md](PAYPAL_SANDBOX.md),
[SELF_HOSTING.md](SELF_HOSTING.md), [PAYPAL_REFUNDS.md](PAYPAL_REFUNDS.md).

## E. Counter-offer: "not that, this"

Other tools give the owner two buttons. AgentPal adds a third. Use the Test Lab wallet
($25 per payment) so both amounts fit.

1. In Test Lab, create a request: amount `24.99`, merchant `Unfamiliar Shop`, purpose
   `USB-C hub`, and a product query Channel3 can match (for example `Anker USB-C hub`).
   Without Channel3, skip the product query and use the manual form in step 3.
2. Open it from Approvals (or "Approve exact terms" in the walkthrough). The dialog shows
   the agent's terms and fingerprint, then "Not this? Buy this instead" with each
   same-currency Channel3 offer.
3. Click **Counter with this deal**, or **Counter** to type an amount and merchant.
   Expected: the original is declined ("Countered by owner", with a link), and the dialog
   reopens on the replacement with the new amount, the new merchant and a **different**
   fingerprint. Nothing is approved yet; the amount difference is labelled
   "not a saving until PayPal verifies the payment".
4. Confirm the approval of the replacement. In Test Lab click **Continue with the
   replacement**, then **Execute as lab agent**. The agent can only execute these exact
   terms. A counter above $25 is blocked by the same rule as scenario A.
5. Approve as the sandbox buyer on PayPal and capture, as in scenario D. Expected after
   the authenticated PayPal read: "Saved $8.50 against the agent's request · verified by
   PayPal" on the request and the same figure in the Overview tile **Verified savings**.
   With the local simulator no saving is ever shown as verified.

API equivalent of step 3 (owner token):

```
POST /v1/payment-requests/{request_id}/counter
{"deal_index": 0}
```

The sandbox payee stands in for the merchant, as in every scenario: no real retailer is
paid and no item is ordered.

## F. Saver agent: suggest, verify savings, survive or retire

PayPal Sandbox only; deterministic policy, no model training. Start with scenario E's
request awaiting approval and its stored cheaper Channel3 offer. In **AI Agents**, click
**Hire Saver**, then **Run now**. The card shows one suggestion and unverified potential,
with $0 verified savings. **Review** opens the existing request dialog; the Saver's line
names the stored retailer and price. The owner clicks **Counter with this deal** and
approves the replacement's own fingerprint. Complete the sandbox buyer and capture flow
from scenario D. Only the PayPal-verified capture increases the Saver's verified savings;
execution or an unverified capture cannot.

The ticker runs at startup and every 900 seconds while the server runs (0 disables it).
Closed UTC months are evaluated once. The injectable-clock offline test shows a Saver
with verified savings surviving and reporting “This month I saved you $Z”; a zero month
produces “Retired: saved $0 in YYYY-MM”, revokes the key, and preserves the entire feed
and audit. `/v1/savers/{saver_id}/evaluate` cannot advance time or retire in an open month.
For a quick UI demonstration, **Retire Saver** preserves history and enables hiring a
new one. See [SAVER_AGENTS.md](SAVER_AGENTS.md) for API, accounting and limitations.
