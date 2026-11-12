# Saver agents (PayPal Sandbox only)

A Saver's manifesto is: “I exist to save you money. If I save you nothing this month, I retire myself.”

Hire one from **AI Agents → Saver agents → Hire Saver**, then use **Run now** or leave the server running. **Runs automatically while the AgentPal server is running.** The default interval is 900 seconds; set `AGENTPAL_SAVER_INTERVAL_SECONDS=0` to disable startup and periodic scans. The dashboard then identifies automatic runs as disabled. There is no hosted scheduler when the server is stopped. Each enabled server process runs its own ticker; database transactions and unique constraints keep concurrent effects idempotent.

## What it actually does

The behaviour is a deterministic policy, with no ML training. It reads only its owner's requests awaiting an owner decision, checks their **stored** Channel3 `market_check.better_deals`, and selects the cheapest valid, positive, same-currency price below the requested amount. It validates the offer through the existing counter-offer parser. Missing checks, malformed/unsupported prices, unnamed retailers and non-cheaper or foreign-currency offers produce no suggestion. It makes no market, browser, PayPal or LLM calls from a run or tick.

Each request receives at most one persisted suggestion, kept with its original price, retailer, currency, deal index, timestamp and exact counter-body digest. That snapshot is advisory and may become stale. Review it in the request dialog and verify the current terms. The feed's **Review** button opens that dialog. The owner alone clicks the existing deal counter button, which sends only `deal_index`. Countering approves nothing: its replacement needs its own exact-terms approval.

A Saver identity uses the existing finance role with **no grants**, whose role ceiling contains no prepare, execute or payout capability. It receives a hashed agent key at hire; the plaintext is discarded. Owner-only Saver APIs never return this key. Existing credential-type, ownership and capability controls apply to every payment, capture, refund, payout and Test Lab route. Agent reads never receive `saver_suggestion`.

An optional one-line note is requested from **the workspace's encrypted Groq integration at hire only**, using the existing Groq origin and model list. No requests, offers, amounts or credentials enter its prompt. It is labelled **AI-written note** and never used for decisions. Without a workspace key, or on an upstream error, the dashboard shows a **Template note** instead. No training or production-readiness claim is made.

## Monthly scorecards and retirement

Periods are calendar months in UTC, including a partial first month when hired. Reports keep currencies separate; no conversion or cross-currency total is invented.

- **Suggestions made:** suggestions filed in that month.
- **Suggestions used:** owner counters created in that month that match the stored suggestion's exact deal index and body digest, with `counter_source=channel3_deal`. An identical manual counter, another index, or a changed offer at the same index does not count.
- **Verified savings:** the existing replacement `counter_saving`, only while `provider_confirmed` and `counter_saving_verified` with PayPal read/webhook evidence. Approval, execution, a simulator outcome, and a capture response without the authenticated PayPal read do not count. Savings belong to the month of the first accepted PayPal verification. Signed counter differences are netted within each currency before positive savings are reported; a zero or negative net provides no survival credit.
- **Unverified potential:** the original month's suggested price differences that have no currently verified replacement. This is advisory potential, not money saved or guaranteed available.

Each tick evaluates any previously unevaluated closed month before suggesting again. Evaluation uses the injected server clock, never a caller-supplied future date. `POST /v1/savers/{saver_id}/evaluate` evaluates closed periods only; it cannot retire during an open month. Closed reports are immutable snapshots, inserted once by `(saver_id, period)`; repeated evaluation does not duplicate reports, revocations or retirement events.

If every verified currency total is zero, status becomes `retired` and the owner sees **Retired: saved $0 in YYYY-MM**. Retirement atomically suspends its registry identity, revokes all keys with audit entries, and retains the identity, suggestions, reports and financial records. General agent reactivation and key rotation refuse a retired Saver. **Auto delete means retire plus revoke: nothing is deleted.** Manual **Retire Saver** has the same preservation and revocation behaviour. Hire a new Saver to resume the policy.

If any currency has positive verified savings, the Saver survives and the closed report says **This month I saved you $Z CURRENCY**. A server that was stopped over several months evaluates closed months in order; the first zero month retires it. Later outcomes do not rewrite a closed report or revive a retired agent.

## Owner API

All routes require an owner credential (`apo_…`); agent keys get 403, and another owner's Saver ID gets 404.

| Method | Route | Purpose |
|---|---|---|
| POST | `/v1/savers` | Hire one active Saver per owner (201, duplicate 409) |
| GET | `/v1/savers` | Own active and retired Savers |
| GET | `/v1/savers/{saver_id}` | Current scorecard, closed reports, manifesto and feed |
| POST | `/v1/savers/{saver_id}/run` | Evaluate closed months, then scan stored offers |
| POST | `/v1/savers/{saver_id}/evaluate` | Evaluate closed months only |
| POST | `/v1/savers/{saver_id}/retire` | Retire plus revoke; preserve history |

Tables are added idempotently in the wallet store: `platform_savers`, `platform_saver_suggestions`, `platform_saver_periods`, and `platform_saver_events`. An additive nullable `deal_index` column on `platform_counter_offers` binds future owner counters to the exact offer; existing rows remain unchanged and cannot acquire retroactive Saver credit.

Offline acceptance: `tests/test_platform_savers.py` uses real auth, stores, ledger and counter-offer flow with synthetic Channel3/PayPal HTTP, an injectable clock, and mocked optional Groq. `frontend/check-saver-browser.cjs` uses a synthetic API under CSP at 1440 and 393 px; screenshots are saved to `/tmp/agentpal-shots/sv-shots/`. These checks do not demonstrate live provider execution.
