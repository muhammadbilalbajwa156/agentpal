# UI-F4 — community AG Grid and AG Charts

IMPLEMENTED — TESTED. Owner reports use the existing same-origin vendor files and visual palette. `api.js`, the vendored assets and backend security files are unchanged. `boot.js` adds the vendor/helper load order and bumps the asset version; authentication behavior is unchanged.

| Screen | Live behavior |
| --- | --- |
| Transactions | Sortable/filterable ledger: merchant/receiver, agent, local creation time, amount, status pill, type. Quick search, pagination, pinned View details action using the existing transaction dialog, AG Grid CSV export including exact raw decimal amounts and currency. |
| Approvals | Existing pending decision cards and exact-term approval/rejection dialogs; Decision history grid for currently approved/rejected/expired requests. This is a current-state view, not invented approval-event history. |
| AI Agents | Name, text status pill, capability tags, wallet budget, verified spend, local creation time and key IDs. Pinned Manage action opens the existing management dialog. Empty state remains available. |
| Wallets | Selected wallet's ledger is an AG Grid. Existing Load transactions/selection reads remain; refresh uses cached ledger records without adding reads. |
| Analytics | Confirmed payment spend by agent (horizontal bars), request outcomes (donut), advisory risk recommendations (bars), daily confirmed payment spend (columns), agent allowance summary grid. Amounts stay grouped by currency. Expired/failed/other outcomes remain distinct when present. |
| Overview | Daily confirmed payment spend chart replaces the unavailable historical-allowance box, with a truthful empty state. |

Created: `site/live-visuals.js`, `site/live-visuals.css`, `check-live-visuals.cjs`, `check-live-visuals-browser.cjs`, `visual-harness.cjs`, this report.

Changed: `site/index.html`, `site/boot.js`, `site/core.js`, `site/finance-screens.js`, `site/agents-screen.js`, `site/wallets-screen.js`, `site/insights-screens.js`, `package.json`, `check-live-finance.cjs`, `check-live-admin.cjs`, `../docs/memory/PROJECT_MEMORY.md`.

Money aggregation/comparison uses scaled BigInt decimals; only final chart coordinates become Numbers. Display formatting receives decimal strings through `AgentPalAPI.money`. Grid cells use text nodes; capability/status/action renderers never interpolate API strings into HTML. CSV values starting with any of `= + - @ TAB CR` receive an apostrophe. Export uses AG Grid's `exportDataAsCsv`, excluding the action column.

Grid options use `theme: 'legacy'` and the Quartz CSS class. Grid/chart options pass the response meta nonce, omitting the literal placeholder. Static SVG controls replace missing no-font glyphs. Charts honor reduced motion. Shared refresh updates `rowData` via `setGridOption`; chart instances update in place. Screen replacement destroys owned instances.

Verification:

- Python baseline: **3,662 passed, 65 skipped, 4 warnings** in 546.82 s.
- Python final: **3,662 passed, 65 skipped, 4 warnings** in 540.83 s. Existing certificate cryptography warnings; final log `/tmp/ui-f4-pytest-after.log`.
- Ruff before and after: **All checks passed!** `git diff --check` passes.
- `npm test`: baseline four scripts passed; final **five scripts passed**, exit 0. Output `/tmp/ui-f4-npm-test.log`.
- New checks cover text renderers, all six formula prefixes, exact decimal aggregation/comparison, currency isolation, local dates, nonce/placeholder handling, reduced motion, truthful empty states, refresh and disposal. **10/10 targeted mutations caught**; `/tmp/ui-f4-mutations.py` regenerates isolated variants without editing the source.
- Real Chromium at `http://127.0.0.1:8820/app/`: **zero CSP console violations; zero page errors; zero warnings; zero external requests**. Injected styles have the response nonce. Styled grids/charts render.
- Browser verifies quick/column filters, pagination, exact amount sort, actual CSV download, View details/Manage dialogs, refresh preserving instances and issuing no extra reads, and 390px page/pagination bounds. Five grids created; four disposed during navigation; nine rowData updates. Machine report: `/tmp/ui-f4-shots/browser-report.json`.
- All ten demo routes also passed in real Chromium with no page errors. Existing demo tests remain green.

Final npm output:

```text
PASS: demo totals, search, wallet/date/risk filters, CSV generation, approval/rejection, duplicate protection, persistence and both screen renders.
PASS: agents create/reload/corruption recovery; wallets permission and verification persistence, create/select/fund/negative input/reload/corruption recovery.
PASS: approvals debit the funding wallet once, card funding maps to bank, insufficient funds rejected.
PASS: all 10 routes boot and render, invalid-route recovery, module integration, asset references.
PASS: live escaping and no fixtures/storage; exact fresh-term payment/payout approvals and re-review; rejects, refund validation/idempotency/failures, reconciliation, duplicate lock, CSV, real overview/shell/notifications and empty states.
PASS: 7 live check groups — escaping, fixture/storage isolation, one-time keys, all available writes, DELETE dependency guard, decimal validation, changed fields, confirmation, in-flight protection, refresh/errors, ledger reads, docs, integrations, analytics, sign-out and empty states.
PASS: AG text renderers, six CSV formula prefixes, exact decimal aggregation/comparison, currency separation, local dates, nonce, reduced motion, truthful empty states, in-place refresh and disposal.
```

Browser reproduction after starting/restarting the supplied platform server:

```sh
cd /tmp/ui-f4/agentpal/frontend
NODE_PATH=$(npm root -g) AGENTPAL_TEST_TOKEN_FILE=/tmp/ui-f4-data/owner.tok node check-live-visuals-browser.cjs
```

Seed: 3 actual API-created external agents, 3 wallets, 12 payment requests (6 approved, 3 rejected, 3 awaiting approval); keys stayed in the seeder's memory. No execute/capture/PayPal calls. The CLI rejected two-character `f4`, so the temporary owner is `f4-owner`.

IMPLEMENTED — TESTED: all four chart types rendered and refreshed against an isolated, explicitly synthetic fixture at port 8821 with strict nonce CSP, separate USD/EUR series and reduced motion. Fixture source and screenshot are in `/tmp/ui-f4-chart-fixture/`; runner `/tmp/ui-f4-chart-test.cjs`. Those synthetic records were never inserted into live API data or live screenshots.

IMPLEMENTED — UNVERIFIED: populated confirmed-spend plots against provider-confirmed live records, because this harness deliberately made no provider executions. Live charts correctly show empty states. Initial health returns 503 without provider credentials; that expected HTTP resource error is separate from the zero CSP/page-error/warning result. No new backend dependency is required.

Screenshots (desktop 1440×900, phone 390×844; full-page captures):

| Screen | Desktop | Phone |
| --- | --- | --- |
| Overview | `/tmp/ui-f4-shots/overview-desktop.png` | `/tmp/ui-f4-shots/overview-phone.png` |
| Transactions | `/tmp/ui-f4-shots/transactions-desktop.png` | `/tmp/ui-f4-shots/transactions-phone.png` |
| Approvals | `/tmp/ui-f4-shots/approvals-desktop.png` | `/tmp/ui-f4-shots/approvals-phone.png` |
| AI Agents | `/tmp/ui-f4-shots/agents-desktop.png` | `/tmp/ui-f4-shots/agents-phone.png` |
| Wallets | `/tmp/ui-f4-shots/wallets-desktop.png` | `/tmp/ui-f4-shots/wallets-phone.png` |
| Analytics | `/tmp/ui-f4-shots/analytics-desktop.png` | `/tmp/ui-f4-shots/analytics-phone.png` |

Review commits: `8dbc776` (integration and fail-first checks), `0d54605` (responsive/browser refinements), plus the evidence/memory commit. Shared Git metadata is read-only: review commits live on `ui-f4-review` in `/tmp/ui-f4-review.git`, with a bundle at `/tmp/ui-f4.bundle`. Original HEAD/history/index remain unchanged. `/tmp/ui-f4.patch` is generated with `git add -N` and `git diff` using a temporary index/object directory so new files are included.

Requests to the lead: review/import the patch or review bundle. No out-of-scope implementation files or backend decisions are requested. A provider-confirmed sandbox dataset is needed only to repeat the populated live-spend check.
