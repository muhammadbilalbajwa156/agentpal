---
description: "AgentPal Shopper — an AI shopping agent that can only spend through AgentPal, so a human approves every exact purchase (PayPal sandbox)"
tags:
  - payments
  - agentic-commerce
  - paypal
  - safety
repository: "github:muhammadbilalbajwa156/agentpal"
authors:
  - name: Muhammad Bilal Bajwa
    account: agentpal
capabilities:
  - Plans a shopping errand with a free OpenRouter model
  - Searches real products with Channel3 and picks one exact offer within budget
  - Requests payment through AgentPal; the owner approves the exact terms
  - Executes only after approval; PayPal sandbox confirms the outcome
integrations:
  - PayPal
  - Channel3
  - OpenRouter
---
# AgentPal Shopper

Type an errand and a budget ("Buy a wireless mouse for the office, $25"). The agent searches
real products, selects one offer, and asks **AgentPal** for payment. It cannot pay on its own:
the owner approves the exact amount, payee and purpose in the AgentPal dashboard, and only a
PayPal-verified outcome counts as paid. Sandbox only — no real money moves.

## Safety by design
- The model is advisory. Deterministic guards enforce the budget and require the amount to
  equal a real catalogue price.
- The agent can create, read and execute its own payment requests only. It can never
  approve, capture or refund.
- Product text is treated as untrusted data; secrets are redacted from every output.

## Limitations
- Free OpenRouter models are rate limited; an errand stops safely if the model is unavailable.
- The PayPal buyer step and capture happen in the owner's AgentPal dashboard.
