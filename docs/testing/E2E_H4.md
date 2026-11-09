# H4: line-by-line security code review (performed by the lead, 2026-11-05)

The Codex reviewer for this area was stopped twice by its provider's automated cybersecurity filter
before producing output, so the lead (Claude) performed the review directly. Method: automated
scans across `agentpal/agentpal/**` plus reading the matching code paths.

| Check | Result | Evidence |
|---|---|---|
| SQL built from strings | **Held** | Every f-string SQL interpolates only code constants (table/column names, PRAGMA integers, internally generated savepoint names). All values use `?` parameters. No `+`-concatenated SQL. |
| Secret comparison | **Held** | Owner tokens: salted SHA-256 + `secrets.compare_digest`, dummy salt/hash for unknown ids (no id oracle), generic error. Agent keys: `hmac.compare_digest` against a dummy digest when the key id is unknown. Legacy tokens: constant work over all entries. 26 constant-time comparisons in total. |
| Randomness | **Held** | Tokens, salts, TOTP seeds and key ids use `secrets`. `random` appears only in dataset/evaluation tooling. |
| Unsafe deserialisation / code execution | **Held** | No `eval`, `exec`, `pickle`, `yaml.load`, `shell=True` or `os.system` in the product. |
| Secrets in logs | **Held** | The only token-related log line records the lifetime, not the token. Kernel SDK/httpx debug logging is filtered during captures. |
| File paths | **Held** | Evidence ids must be 64 lowercase hex characters before any path is built. |
| Network I/O inside DB transactions | **Held** | Static AST check: no provider call is lexically inside a `tx()` block in `lifecycle/` or `platform/`; execute/capture hold no transaction around the provider call. |
| Regex backtracking | **Held** | No nested-quantifier patterns. |
| Money as float | **Held** | Floats are refused at the API boundary; Decimal everywhere. |
| File permissions | **Improved** | Databases already 0600 (E2E-T4-1); evidence files changed from 0444 to 0400. |

Ownership checks, approval binding, idempotency and concurrency are covered by H2 and the earlier
acceptance round; upstream-data trust boundaries by H3; the request edge by H1.
