"""Owner-side provisioning (run by the operator, never by an agent).

Creates each company agent (role ``external``, minimal capabilities) and its USD wallet with
the configured monthly budget and per-transaction limit. Idempotent: agents that already
exist by name are skipped (a missing wallet is still created). New agent keys are written
ONLY to a private 0600 file; they are never printed.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable
from pathlib import Path

import httpx

from ..common import AgentError, Redactor, base_url, bounded_json, identifier
from .scenarios import COMPANIES, Company

OWNER_TOKEN = re.compile(r"apo_[A-Za-z0-9_-]{8,300}\Z")
AGENT_KEY = re.compile(r"apk_[A-Za-z0-9_-]{8,200}\Z")


class OwnerClient:
    """Allow-listed owner routes: list/create agents and list/create wallets. Nothing else."""

    ROUTES = {("GET", "/v1/agents"), ("POST", "/v1/agents"), ("GET", "/v1/wallets"),
              ("POST", "/v1/wallets")}

    def __init__(self, base: str, token: str, client: httpx.Client | None = None):
        if not OWNER_TOKEN.fullmatch(token or ""):
            raise AgentError("owner_token_required")
        self._base, self._token = base_url(base), token
        self._client = client or httpx.Client(timeout=20, follow_redirects=False)
        self._owns = client is None

    def close(self):
        if self._owns:
            self._client.close()

    def call(self, method: str, path: str, body=None) -> dict:
        if (method, path) not in self.ROUTES:
            raise AgentError("owner_action_refused")
        return bounded_json(self._client, method, self._base + path,
                            {"Authorization": "Bearer " + self._token}, body, _NoRedact())


class _NoRedact(Redactor):
    """Provisioning must see the one-time agent key to store it; it is never emitted."""

    def __init__(self):
        super().__init__({})

    def clean(self, value):
        return value


def write_private_json(path: str | Path, data: dict) -> None:
    """Atomic write with mode 0600 from creation (no window where the file is readable)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".agent-keys-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=1, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def read_keys(path: str | Path) -> dict[str, str]:
    path = Path(path)
    if not path.exists():
        return {}
    try:
        keys = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(keys, dict) or not all(
                isinstance(v, str) and AGENT_KEY.fullmatch(v) for v in keys.values()):
            raise ValueError
    except (OSError, ValueError):
        raise AgentError("existing_keys_file_invalid") from None
    return keys


def provision(companies: list[Company] | None = None, *, base: str, owner_token: str,
              keys_out: str, client: httpx.Client | None = None,
              emit: Callable[[dict], None] = lambda event: None) -> dict:
    companies = companies or list(COMPANIES.values())
    keys = read_keys(keys_out)
    owner = OwnerClient(base, owner_token, client)
    summary: dict[str, dict] = {}
    try:
        listed = owner.call("GET", "/v1/agents").get("agents")
        wallets = owner.call("GET", "/v1/wallets").get("wallets")
        if not isinstance(listed, list) or not isinstance(wallets, list):
            raise AgentError("invalid_owner_listing")
        by_name = {a.get("name"): a for a in listed if isinstance(a, dict)}
        has_wallet = {(w.get("agent_id"), w.get("currency")) for w in wallets
                      if isinstance(w, dict)}
        for company in companies:
            for spec in company.agents:
                name, slug = company.agentpal_name(spec), company.slug(spec)
                existing = by_name.get(name)
                if existing:
                    agent_id = identifier(existing.get("agent_id"))
                    outcome = "exists_skipped"
                    if slug not in keys:
                        emit({"message": "Agent exists but its key is not in the keys file; "
                              "rotate its credential in the owner dashboard.", "agent": name})
                else:
                    created = owner.call("POST", "/v1/agents", {
                        "name": name, "description": spec.mission[:500], "role": "external",
                        "capabilities": spec.capabilities,
                        "metadata": {"company": company.company_id, "slug": slug,
                                     "demo": "autonomous_companies"}})
                    key = created.get("api_key")
                    agent_id = identifier((created.get("agent") or {}).get("agent_id"))
                    if not isinstance(key, str) or not AGENT_KEY.fullmatch(key):
                        raise AgentError("invalid_agent_registration")
                    keys[slug] = key
                    # Persist after EACH agent: a later failure cannot lose a one-time key.
                    write_private_json(keys_out, keys)
                    outcome = "created"
                wallet = "exists"
                if (agent_id, "USD") not in has_wallet:
                    owner.call("POST", "/v1/wallets", {
                        "agent_id": agent_id, "currency": "USD",
                        "monthly_budget": spec.monthly_budget,
                        "per_transaction_limit": spec.per_transaction_limit})
                    has_wallet.add((agent_id, "USD"))
                    wallet = "created"
                summary[slug] = {"agent_status": outcome, "wallet": wallet, "agent_id": agent_id,
                                 "capabilities": spec.capabilities,
                                 "monthly_budget": spec.monthly_budget,
                                 "per_transaction_limit": spec.per_transaction_limit}
                emit({"message": "Provisioned agent.", "agent": name, **summary[slug]})
    finally:
        owner.close()
    write_private_json(keys_out, keys)  # always present and 0600, even if nothing was new
    return {"keys_file": str(keys_out), "agents": summary}
