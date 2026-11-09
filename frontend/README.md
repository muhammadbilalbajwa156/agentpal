# AgentPal dashboard

A complete static frontend demo with ten pages: Overview, AI Agents, Wallets, Transactions, Approvals, Integrations, Analytics, Developer Portal, Security, and Settings.

## Run locally

Use Node.js 18 or later. No packages need to be installed.

```sh
npm start
```

Open `http://127.0.0.1:4173`. Do not open the HTML with a file URL; browser storage and clipboard features work best over HTTP.

```sh
npm test
```

## Behavior

- Hash routes work on direct links, refresh, and browser back/forward.
- Startup has a skeleton and a retryable asset-load failure screen. Unknown pages have a recovery link.
- Agent edits, wallet actions, approval decisions, integrations, and settings persist in browser local storage.
- Wallet transfers create two ledger entries. Approvals debit the funding wallet once and update approval history and the transaction ledger.
- The sample Card Wallet is funded by the Business Bank Account; the confirmation dialog explains this relationship.
- Search, filtering, pagination, exports, detail dialogs, and empty states are implemented.
- Developer requests simulate success, loading, unauthorized, rate-limit, and service-unavailable states locally.
- Analytics has an explicitly labeled illustrative dataset; it is not a live financial report.
- Historical fixture dates remain October 2025 to match the supplied screenshots.
- Storage failures preserve session functionality and display a warning.
- Responsive layouts, keyboard focus, accessible navigation labels, modal dialogs, reduced-motion support, and mobile table scrolling are provided.

## Production boundary

This is a polished, deployable frontend demo, not a live financial service. Real payments, OAuth integrations, API authentication, enforced security controls, multi-user state, and server audit logs require a backend. No real provider credentials are collected. Demo settings and placeholder API keys do not modify the hosting account's security.

The hosted site remains owner-private. Sign in to the owning account if the hosting provider requests authentication. Do not remove access protection to work around an expired sign-in.

## Verification

Automated checks cover all ten screen renders, routing, asset references, filtering, CSV generation, duplicate-decision protection, wallet/approval synchronization, validation, persistence, and malformed-storage recovery.

Browser QA covers all routes, desktop and mobile layouts, profile persistence, integration persistence, request simulator loading/error output, approval balance updates, CSV download, empty states, unknown-route recovery, and console errors.

Production deployment uses `.openai/hosting.json` and serves `site/` through Sites. The preview server and checks are not deployed as server code.
