# Security policy

AgentPal is a **sandbox-only hackathon project**: it uses the PayPal sandbox with test money and is
not a production payment service.

## Reporting a vulnerability

Please do **not** open a public issue for security problems. Use GitHub's
**Security → Report a vulnerability** (private advisory) on this repository, with steps to reproduce.
We aim to reply within a few days.

## Design in brief

* Owner tokens (`apo_`) and agent keys (`apk_`) are stored only as salted hashes and shown once.
* Provider keys added in the dashboard are verified with one read-only call, then encrypted at rest
  (Fernet) and never returned by the API.
* Deterministic rules run before any AI model; agents cannot approve, capture or refund.
* Approvals bind to a SHA-256 fingerprint of the exact terms; any change voids them.
* Only an authenticated PayPal read or a signature-verified webhook marks a payment as paid.

More detail: [docs/SECURITY_AUDIT.md](docs/SECURITY_AUDIT.md) and [docs/SELF_HOSTING.md](docs/SELF_HOSTING.md).
