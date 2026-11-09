# Contributing to AgentPal

Thanks for helping! AgentPal moves (sandbox) money, so changes are held to a few firm rules.

## Ground rules

* **Sandbox only.** Never add code that calls live PayPal hosts or handles real money.
* **Never weaken a control to make a test pass.** Rules run before any model, agents never approve,
  approvals bind to exact terms, and only verified PayPal outcomes count as paid.
* **No secrets in the repo.** Keys belong in environment variables or the encrypted workspace store.

## Development setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python -m agentpal data                         # synthetic data used by some tests
python -m pytest -q                             # full suite
python -m ruff check agentpal training tests    # lint
python -m agentpal.platform.openapi --check     # OpenAPI spec is up to date
python -m postman.generate --check              # Postman collection is up to date
cd frontend && npm test                         # dashboard checks
```

If you change the `/v1` API, regenerate the spec and collection with
`python -m agentpal.platform.openapi --write` and `python -m postman.generate`.

## Pull requests

1. Write a failing test first for every new boundary or bug fix.
2. Keep each pull request focused, and describe what changed and how you tested it.
3. Make sure CI is green.
