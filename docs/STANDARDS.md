# Where AgentPal fits: comparison, not a standards implementation

AgentPal is an inspectable, self-hostable spending-control layer for external agents:
scoped identities and virtual allowances, deterministic limits, a human co-signer
for exact terms, and verified PayPal **sandbox** settlement. It holds no funds.
This page compares layers and maps concepts; **it does not claim AP2, ACP or x402
implementation, certification or interoperability**. Sources checked November 11, 2026.

| Interface / standard | Its role | AgentPal's relationship |
|---|---|---|
| PayPal Agent Toolkit / PayPal MCP server | Agent access to PayPal operations, including order creation and reads. | AgentPal adds its own human co-signer, per-agent limits, reservations and verified-settlement accounting around sandbox payments. Its MCP adapter calls AgentPal's API; it does not wrap the PayPal toolkit or claim PayPal's ecosystem lacks controls. [Official toolkit](https://github.com/paypal/agent-toolkit) |
| Google AP2 | Cryptographically signed mandates bind user intent, cart terms and payments across participants. | Closest conceptual overlap with exact-term approval; a local SHA-256 fingerprint and authenticated database record are not interoperable signed mandates. [Official AP2 repository](https://github.com/google-agentic-commerce/AP2), [Google introduction](https://cloud.google.com/blog/products/ai-machine-learning/announcing-agents-to-payments-ap2-protocol) |
| OpenAI/Stripe ACP | Standard commerce/checkout interactions among buyers, agents and merchants; merchants retain their customer relationship and merchant-of-record role. | AgentPal's owner policy gate could precede checkout, but its bespoke payment-request API is not an ACP checkout/session implementation. [Official ACP site](https://www.agenticcommerce.dev/) |
| Coinbase x402 | HTTP-native paid access: payment requirements, payment, verification and settlement in a resource-request flow. | Different rail/use case, especially paid APIs. AgentPal has no x402 payment headers, facilitator integration, stablecoin wallet or onchain settlement. Mandatory human approval for each request also limits high-frequency micropayment use. [Coinbase documentation](https://docs.cdp.coinbase.com/x402/welcome), [protocol](https://x402.org/) |

The following AP2 mapping is an **analogy**, not a wire-format or security equivalence:

| AgentPal evidence | AP2 concept it resembles | Boundary / missing work |
|---|---|---|
| Agent request with purpose and proposed purchase | Intent context | An agent proposal is untrusted and conveys no user mandate. |
| Authenticated human approval of exact amount, currency, merchant/payee and purpose, bound by `terms_sha256` | Cart / authorization context | Local, expiring, single-use approval; no portable merchant/user signatures or signed mandates. |
| Verified PayPal capture/read or verified webhook accepted by the lifecycle | Payment observation | Evidence of the sandbox provider outcome; no AP2 payment mandate or payment-network propagation. |

AgentPal does **not** implement AP2 mandate schemas, verifiable-credential issuance or
verification, participant trust discovery, interoperable user/merchant signatures,
or preauthorized autonomous purchases. PayPal's own AP2 work explicitly addresses
signed consent; AgentPal does not claim to invent that idea.
[PayPal's AP2 explanation](https://developer.paypal.com/community/blog/PayPal-Agent-Payments-Protocol/)

AgentPal's implemented MCP stdio interface is a tool-access layer with five agent
operations and **no approval tool**. It changes how an agent reaches the existing
policy boundary, not who authorizes payment. See [MCP.md](MCP.md).
