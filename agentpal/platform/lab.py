"""Owner Test Lab: an ordinary scoped agent; no approval capability or policy bypass."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import uuid

import httpx

from ..contracts import Capability
from ..infra_contracts import AgentAuthError
from ..workforce.roles import Role
from .errors import PlatformError, not_found


class IntegrationRequired(Exception):
    def __init__(self, provider):
        super().__init__("integration_required")
        self.provider = provider


class TestLab:
    def __init__(self, platform):
        self.p = platform
        self.store = platform.deps.wallet_store
        self.integrations = platform.deps.integrations

    def agent(self, owner):
        vault = self.integrations.vault
        vault.require()
        # WalletStore uses nested savepoints. This outer tx serializes creation in every
        # worker, including registry, wallet, hash credential and encrypted key insertion.
        with self.store.tx() as c:
            row = c.execute("SELECT agent_id,wallet_id FROM platform_lab_agents WHERE owner_id=?", (owner.owner_id,)).fetchone()
            if row:
                return dict(row)
            view, key = self.p.agents.register(owner, "Test Lab agent", "Browser Test Lab", Role.external,
                [Capability.wallet_read, Capability.payment_prepare, Capability.owner_request_decision,
                 Capability.payment_execute_sandbox, Capability.research_search, Capability.research_fetch], {})
            limits = self.integrations.resources.amount_limits(owner.owner_id)
            monthly = min(Decimal("200.00"), Decimal(limits["max_monthly_budget"])) if limits else Decimal("200.00")
            hard = min(Decimal("25.00"), Decimal(limits["max_amount"]), monthly) if limits else Decimal("25.00")
            wallet = self.p.wallets.create(owner.owner_id, owner.owner_id, view["agent_id"], "USD",
                                           monthly, hard, None, None)
            encrypted = vault.encrypt(owner.owner_id, "lab_agent", {"api_key": key.api_key})
            c.execute("INSERT INTO platform_lab_agents VALUES (?,?,?,?)", (owner.owner_id, view["agent_id"], wallet["wallet_id"], encrypted))
            return {"agent_id": view["agent_id"], "wallet_id": wallet["wallet_id"]}

    def context(self, owner):
        self.agent(owner)
        row = self.store.query("SELECT ciphertext FROM platform_lab_agents WHERE owner_id=?", (owner.owner_id,))[0]
        key = self.integrations.vault.decrypt(owner.owner_id, "lab_agent", row["ciphertext"])["api_key"]
        # Authenticate the real credential on every operation: suspension, revocation,
        # rotation and permission reductions continue to apply.
        try:
            return self.p.verifier.verify(key), key
        except AgentAuthError:
            raise PlatformError(403, "lab_agent_unavailable") from None

    def create(self, owner, body, idem=None):
        ctx, _ = self.context(owner)
        view, _ = self.p.payments.create(ctx, body, idem or "lab-" + uuid.uuid4().hex)
        return self.view(owner, view["request_id"])

    def view(self, owner, request_id):
        view = self.p.payments.get_for_owner(owner, request_id)
        refs = self.p.payments.reconciliation(owner, request_id)["provider_references"]
        return {**view, "payer_action_url": refs[0].get("payer_action_url") if refs else None,
                "provider_mode": view["provider_mode"]}

    def execute(self, owner, request_id):
        # Resolve ownership before creating any agent or touching a provider.
        meta = self.p.payments._owned_meta(owner, request_id)
        lab = self.agent(owner)
        if meta["agent_id"] != lab["agent_id"]:
            raise not_found()
        ctx, _ = self.context(owner)
        self.p.payments.execute(ctx, request_id)
        return self.view(owner, request_id)

    def errand(self, owner, task, budget):
        from ..agents.shopper.core import Shopper, ShopperError
        from .models import PaymentCreate
        keys = {}
        shared = set()
        for provider, name in (("channel3", "CHANNEL3_API_KEY"), ("groq", "GROQ_API_KEY"),
                               ("openrouter", "OPENROUTER_API_KEY")):
            fields = self.integrations.fields(owner.owner_id, provider)
            if fields is None:
                fields = self.integrations.server_fields(provider)
                if fields:
                    shared.add(provider)
            keys[name] = fields["api_key"] if fields else ""
        if not keys["CHANNEL3_API_KEY"]:
            raise IntegrationRequired("channel3")
        if not (keys["GROQ_API_KEY"] or keys["OPENROUTER_API_KEY"]):
            raise IntegrationRequired("groq")
        self.integrations.admission("lab:" + owner.owner_id)
        _, key = self.context(owner)
        resources = self.integrations.resources
        for provider in ("channel3", "groq", "openrouter"):
            if provider in shared:
                resources.consume(owner.owner_id, provider, "errands", server_provided=True)
                break  # one errand, even with multiple shared providers
        keys["AGENTPAL_AGENT_KEY"] = key
        keys["AGENTPAL_BASE_URL"] = "http://127.0.0.1:8000"
        events = []

        def emit(event):
            # Shopper's Redactor cleans known workspace keys and upstream data first.
            events.append({"at": datetime.now(timezone.utc).isoformat(), "message": event["message"]})

        def admission(request):
            provider = {"api.trychannel3.com": "channel3", "api.groq.com": "groq",
                        "openrouter.ai": "openrouter"}.get(request.url.host)
            if provider:
                resources.consume(owner.owner_id, provider, server_provided=provider in shared)

        client = httpx.Client(timeout=20, follow_redirects=False, event_hooks={"request": [admission]})
        shopper = None
        try:
            shopper = Shopper(env=keys, emit=emit, client=client)
            # Only planning/search is delegated to the bundled core. Submit its proposal
            # through the same authenticated agent PaymentDesk as every external agent.
            body = PaymentCreate.model_validate(shopper.plan(task, str(budget)))
            view = self.create(owner, body)
            return {"status": view["status"], "request_id": view["request_id"], "events": events}
        except (ShopperError, ValueError):
            emit({"message": "Errand could not produce a payment proposal. No payment was executed."})
            return {"status": "failed", "request_id": None, "events": events}
        finally:
            if shopper:
                shopper.close()
            client.close()
