# Agent wallet architecture

Status: DEMO / SANDBOX ONLY. AgentPal holds no funds and moves no money. No live PayPal
call is made. "Execution" is a LOCAL SIMULATION, and it never completes spending.

Code: `agentpal/wallet/`. Tests: `tests/test_wallet_*.py`.

## 1. Three things that are never confused

| Thing | What it is | In code | Real money? |
|---|---|---|---|
| Virtual spending allowance | An internal budget record. Its limits come from the operator spend policy; its figures are derived from ledger states. | `CurrencyAllowance`, `WalletView` (`is_real_money: Literal[False]`) | No |
| Provider-connected payment instrument | A *reference* to a provider account. Today that is only a PayPal **sandbox** reference or the local simulation. No credentials and no balance are stored. | `PaymentInstrumentRef` (`kind=provider_sandbox_instrument`) | No |
| Funds held by a regulated provider | Not representable today. | `InstrumentKind.provider_funds` is refused by the model validator, and the table `CHECK` excludes it | DESIGN ONLY |

There is no stored "balance". For each currency of an agent's policy, `WalletService.view` calls:

- `SpendPolicy.for_agent(agent).currencies[c]`, which gives the limits;
- `AssessmentStore.spend_breakdown(agent, c, now)`, which gives the reserved, approved, attempted, completed, released, failed and cancelled amounts.

From these it derives:

- `committed = reserved + approved + attempted + completed`;
- `available = max(0, monthly_budget - committed)`.

`completed` only ever comes from a verified provider outcome in the existing ledger.

## 2. Reuse of the existing authority (nothing duplicated)

| Concern | Authority (unchanged) | Wallet use |
|---|---|---|
| Limits and budgets | `spending.SpendPolicy` (operator file; `demo` vs `operator` configuration) | Read only. The registry owner must equal the policy's `owner_id`, otherwise `OWNER_MISMATCH`. An unlisted agent in an operator configuration gets `NO_SPENDING_POLICY`. |
| Claim, rules, reservation | `RiskService.assess` → `AssessmentStore.claim_request` | `prepare_payment` builds a `PaymentRequest` and calls `assess`. It never writes a reservation itself. |
| Exact-terms binding | `spending.ApprovalTerms` (SHA-256) | Used as is. The wallet keeps the canonical terms JSON so the owner can see what they approve. |
| Approval | `AssessmentStore.record_owner_decision(terms=...)` | Called only from `WalletService.approve` / `decline`, which accept only an `OwnerPrincipal`. |
| Attempt | `AssessmentStore.record_payment_attempt(terms=...)` | Called only from `execute_sandbox`, with a `sim-` reference. |
| Outcome | `AssessmentStore.record_payment_outcome(verified=...)` | **Never called** from `wallet/` or `workforce/`. A static AST test and a runtime spy test enforce this. |
| Tokens | `auth.TokenRegistry` / `auth.Principal` | `owner_principal_from(principal, OwnerTokenMap)` lets only a *mapped operator* token become an owner. An agent token never does. |

## 3. Identities and principals

- **`Owner`**: registered by the authenticated owner themself. The id comes from the token map, never from a parameter.
- **`AgentIdentity`**:
  - has one owner and one `Role`;
  - is `active` or `suspended`;
  - lives in an id namespace disjoint from owners.
- **`OwnerPrincipal(owner_id, token_id)`**: the only type accepted by `approve`, `decline`, `pending_approvals`, registration, grants, revocation, status changes and instrument registration.
  - `require_owner` checks this at runtime, so an `AgentContext` or an `auth.Principal` of an agent raises `NotAuthorized("SEPARATION_OF_DUTIES")`.
  - The decision is recorded as `owner:<owner_id>|token:<token_id>`.
  - **SEC-15 at the wallet layer:** the wallet also checks `ledger_entries.owner_id == owner.owner_id`.
- **Limitation:** this is an *in-process* boundary. Python code in the same process can construct an `OwnerPrincipal`. The real boundary is that agents act only through the orchestrator and tools, which never accept or forward one, together with authenticated HTTP owner endpoints (proposed below).

## 4. Payment lifecycle through the wallet

```
agent (payment.prepare) --prepare_payment--> RiskService.assess --> reserved | no hold (blocked)
owner (OwnerPrincipal) --approve(exact terms)--> record_owner_decision --> approved (15 min)
agent (payment.execute_sandbox) --execute_sandbox(exact terms)-->
     checks:
       1. permission: capability granted, same owner, same agent
       2. the terms digest equals the prepared request's
       3. an approval recorded BY THE WALLET for this owner + digest exists
       4. the ledger state is approved
       5. not already simulated
     LocalSandboxSimulator.submit -> "sim-<uuid4 hex>"
     record_payment_attempt(terms) --> pending ("attempted")   [never completed]
```

- **Request ids** are generated server-side as `pay_<uuid4 hex>`. This mitigates SEC-13 (id squatting). The ids satisfy both `schemas.ID_PATTERN` and the `AgentContext` pattern.
- **An approval recorded directly in the store** (free-text `decided_by`) is not enough for `execute_sandbox`: the wallet refuses with `NO_OWNER_APPROVAL`.
- **Expiry:** an expired approval (owner decision 6) is released by `record_payment_attempt`, which raises `InvalidLedgerTransition`.

## 5. Schema (new tables; existing schema untouched)

`wallet/store.py` stores its version in `wallet_meta(schema_version=1)` and never touches `PRAGMA user_version`. It uses its own database by default (memory or a separate file). The append-only tables have no update or delete method.

| Table | Purpose |
|---|---|
| `wallet_owners(owner_id PK, display_name, created_at)` | Owners |
| `wallet_agents(agent_id PK, owner_id FK, role, status, created_at)` | Isolated agent identities |
| `wallet_grants(agent_id, capability, owner_id, granted_by, granted_at; PK(agent_id, capability))` | Current grants. A grant counts only if `owner_id` equals the agent's owner **and** it is inside the role ceiling. |
| `wallet_grant_events` (append-only) | register / grant / revoke / suspend / reactivate / **refused** |
| `wallet_instruments` | Sandbox instrument references. `CHECK` excludes `provider_funds` and any provider other than `paypal_sandbox` / `local_simulation`. |
| `wallet_payment_requests` | Wallet-prepared requests: canonical terms JSON + digest, recommendation, score |
| `wallet_owner_decisions` (append-only) | approved / declined / **refused** (cross-owner, changed terms, ...) with token id and terms digest |
| `wallet_sandbox_simulations` (append-only, unique request) | `sim-` references (`CHECK LIKE 'sim-%'`) |
| `wallet_permission_audit` (append-only) | Every permission decision, allowed or denied, with a code and an action |

## 6. Status

| Item | Status |
|---|---|
| Owner / agent identities, ownership, disjoint ids | IMPLEMENTED — TESTED |
| Capability grants (owner-only, owner-scoped, role ceiling, audited) | IMPLEMENTED — TESTED |
| Deny-by-default `PermissionChecker` (forged-context proof, audited) | IMPLEMENTED — TESTED |
| Ledger-derived virtual allowance per currency | IMPLEMENTED — TESTED |
| Payment requests (`prepare_payment` via RiskService, server ids) | IMPLEMENTED — TESTED |
| Owner-only approval / decline bound to exact terms, cross-owner refusal | IMPLEMENTED — TESTED |
| Transaction history (wallet-prepared requests + ledger events) | IMPLEMENTED — TESTED |
| Sandbox instrument references | IMPLEMENTED — TESTED (reference only; no PayPal call) |
| Local sandbox simulation → `attempted` | IMPLEMENTED — TESTED |
| Operator-token → owner mapping (`OwnerTokenMap`) | IMPLEMENTED — TESTED (not wired to the API) |
| HTTP endpoints for wallet / approval | DESIGN ONLY |
| PayPal sandbox execution + verified outcome ingestion + reconciliation (SEC-16) | DESIGN ONLY |
| Real funds (`provider_funds`) | DESIGN ONLY (refused) |

## 7. Limitations

- **History coverage:** history lists wallet-prepared requests only. The store has no "ledger entries by agent" query, and the wallet does not read the store's private connection.
- **No cross-database atomicity:** the wallet DB and the assessment DB are not updated atomically.
  - If the process dies between `record_owner_decision` and the wallet's decision row, the ledger is `approved` but the wallet will refuse to execute (`NO_OWNER_APPROVAL`). This fails closed; the owner re-approves after the hold expires.
  - The same applies to a crash after `record_payment_attempt`: the entry stays `attempted`, which is SEC-16.
- **`decided_by` in the store:** the store still accepts free text from other callers (SEC-15). The wallet only *refuses to execute* such approvals.
- **Owners' own authentication** is an operator token mapped in a wallet config file. There is no per-owner login, MFA or step-up.
