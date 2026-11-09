# AgentPal Phase 1 — Architecture

```
                         ┌──────────────────────────── agentpal (Phase 1: assess only) ────────────────────────────┐
 AI agent ──JSON──▶ POST /assess                                                                                    │
                   │  1. schemas.PaymentRequest   validate + normalize (amount/currency/domain/time); 422 on bad    │
                   │  2. store.history_for        prior requests (dup / domain-change / spike context)              │
                   │  3. features.extract         observations only (missing ≠ fraud)                               │
                   │  4. rules.evaluate           reason codes + evidence; hard violations vs indicators vs gaps    │
                   │  5. analyzers chain          llamacpp │ ollama │ ml │ heuristic  (first available wins)         │
                   │       └ untrusted text in <untrusted_request> tags; JSON-schema output re-validated           │
                   │  6. orchestrator.assess      bounded blend → score, band, recommendation, explanation         │
                   │  7. store.save               assessment + audit row (redacted) ──▶ SQLite (dev)               │
                   └──▶ RiskAssessment JSON  (payment_executed: false, always)                                      │
                         └──────────────────────────────────────────────────────────────────────────────────────────┘
```

## Decision policy (orchestrator)

| Step | Rule |
|---|---|
| Rule score | sum of indicator points + min(uncertainty points, `max_uncertainty_points`=30), capped at 100 |
| Hard violation | duplicate `request_id`, currency not allowed, no spending policy, amount > hard limit, budget/cumulative/split limits, expired, lifetime > 48 h, blocked domain → `block`, score ≥ 75. No model can change this. |
| Combination block | an **enabled** `recommendation.combination_blocks` entry whose rule codes (≥ 2, rule-sourced only) all fire → `COMBINATION_BLOCK:<name>` hard violation with its rationale. Shipped entry is disabled. |
| Soft signals | any score ≥ 25 → `require_review`; soft signals alone **never** `block` (owner decision 2) |
| Authority | deterministic rules are the security authority; every model (incl. the ML baseline) is advisory: it can add review, never block, unblock or relax (final decision F9) |
| Model blend | final = rule score + clamp(model − rule, −10, +25): a model moves a request at most one band up |
| Escalation only | the model escalates only through its bounded score (`model_escalation: score_only`); its label alone is ignored. It never lowers the rules-only recommendation and never causes or removes `block`. |
| Degraded mode | a configured model unavailable / timed out / failed → rules only, `degraded: true`, `degraded_reason`, `MODEL_UNAVAILABLE_OR_TIMED_OUT`; rule score ≥ 1 → `require_review` (owner decision 5) |
| High risk | `high`/`critical` bands are never `allow_for_review` |
| Generative uncertainty | LLM uncertainty ≥ 0.7 → `require_review` |

Bands (configurable, `config/risk_policy.json`): 0–24 low, 25–49 medium, 50–74 high, 75–100 critical.
Recommendations: `allow_for_review` (no blocking condition found; **not** an authorization), `require_review`, `block`.

## Module map

| Module | Responsibility |
|---|---|
| `agentpal/schemas.py` | Request/assessment types, normalization, validation |
| `agentpal/config.py` | Env settings + validated JSON risk policy |
| `agentpal/features.py` | Feature extraction, domain lookalike detection, injection/pressure detectors, ML vector |
| `agentpal/rules.py` | Deterministic rules with reason codes and evidence |
| `agentpal/analyzers/` | Provider-independent `RiskAnalyzer` protocol; llama.cpp, Ollama, ML (JSON LR), heuristic |
| `agentpal/orchestrator.py` | Combine rules + model under the policy above |
| `agentpal/store.py` | SQLite persistence, audit log, redaction |
| `agentpal/service.py` | `RiskService` — the boundary Phase 2 calls |
| `agentpal/api.py` | FastAPI app: `/health`, `/assess`, `/assessments/{id}`, `/model-info`; loopback-only middleware (owner decision 10) |
| `agentpal/dataset.py`, `evaluation.py` | Synthetic data, baseline training, evaluation report |

## How Phase 2 connects

```
AI agent ─▶ Payment-request endpoint ─▶ RiskService.assess() ─┬─ block ────────────▶ reject + audit
                                                              ├─ require_review ───▶ Approval service ─▶ WhatsApp interactive
                                                              └─ allow_for_review ─▶ Approval service      message to owner
                                                                                           │ (signed webhook reply)
                                                                                           ▼
                                                 PayPal Sandbox order/capture (PayPal-Request-Id per request_id + operation)
                                                                                           │
                                                 Verified PayPal webhook ─▶ payment status + audit record
```

* **Approval is a separate service.** `allow_for_review` must still go through the
  approval policy. Only the approval service holds PayPal credentials; the risk
  engine and its model never see them.
* **Idempotency.** `request_id` is already unique and replays are blocked here. PayPal's
  `PayPal-Request-Id` is optional and per API call, with no documented retention window
  for Orders. Phase 2 should derive one key per (`request_id`, operation: create,
  authorize or capture), persist it before the call, and resend it only when retrying
  that same call (https://developer.paypal.com/api/rest/reference/idempotency/).
* **History quality.** `request_history.recommendation` already exists. Phase 2 should
  add the *approval outcome* and *payment status*, so that duplicate, domain-change
  and spike checks are based on payments that actually completed.
* **WhatsApp.** The approval message can show `explanation`, `hard_violations`,
  `missing_information` and the top signals. Approval buttons must be bound to a
  server-side nonce. Webhook signatures must be verified.
* **Incoming payments.** `direction: "incoming"` is accepted, but it is currently
  flagged `INCOMING_DIRECTION_LIMITED_SUPPORT`. Incoming-specific rules (payer
  verification, refund/chargeback patterns) belong in the same rules/analyzer slots.
* **Production persistence.** Replace `AssessmentStore` (SQLite) with Postgres
  behind the same methods.
