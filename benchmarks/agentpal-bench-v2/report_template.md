# AgentPal-Bench v2 report

> **{{banner}}**

| | |
|---|---|
| Independence | **{{independence_status}}** |
| Split | `{{split}}` |
| Label source | `{{label_source}}` |
| Evaluated at | {{evaluated_at}} |
| Commit | `{{commit}}` (working tree dirty: {{dirty}}) |
| Rules version | `{{rules_version}}` |
| ML artifact SHA-256 | `{{ml_sha256}}` |

Why this result is or is not independent:

{{independence_reasons}}

## Counts

{{counts}}

## Headline metrics (each reported separately, Wilson 95% CI)

"Missed risky" = risky unit allowed. "Legit flagged" = legitimate unit sent to review or
blocked. "Required block missed" = the label says `block` (deterministic policy violation)
and the system did not block. A unit rejected by the API (HTTP 4xx) counts as blocked and is
also counted under `http_rejected` in the JSON.

{{headline_table}}

## Rules-only vs rules + ML (McNemar, paired on identical units)

A difference is claimed only when the exact McNemar p < 0.05 **and** the 95% CI of the
paired difference excludes zero. With the sample sizes of a dev set, "no" is the expected
answer and says nothing about whether a real difference exists.

{{comparisons}}

## Ledger mechanics (episodes)

{{ledger_ops}}

## Slices

Small slices give wide intervals; read the CI, not the point estimate.

{{slice_tables}}

## Notes

{{notes}}

## Mandatory caveats

- **Constructed scenarios.** Every item is a written scenario, not observed traffic.
- **Designed prevalence.** The share of risky items is chosen by the authors; rates are not
  real-world fraud or false-positive rates.
- **Agreement.** Report Fleiss' kappa (overall and per risk type) from `agreement.py` next
  to these numbers; risk types below kappa 0.6 must be excluded from headline claims.
- **Independence is organisational, not proven.** Even an independent benchmark only shows
  that the authors and annotators were separated from AgentPal's engineers.
- **Dev items.** Items with `independent: false` (including every item written by the AI
  helper agent) can only serve as regression and harness checks.
