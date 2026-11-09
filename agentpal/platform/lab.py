"""Owner Test Lab: an ordinary scoped agent; no approval capability or policy bypass."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import uuid

from ..contracts import Capability
from ..infra_contracts import AgentAuthError
from ..ratelimit import LimiterUnavailable, SQLiteSlidingWindowLimiter
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
        self.daily = SQLiteSlidingWindowLimiter(self.integrations.limiter_path, "lab-errands-daily", 25, 86400.0)

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
            wallet = self.p.wallets.create(owner.owner_id, owner.owner_id, view["agent_id"], "USD",
                                           Decimal("200.00"), Decimal("50.00"), None, None)
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
        provider = self.p.deps.paypal_for(owner.owner_id)
        return {**view, "payer_action_url": refs[0].get("payer_action_url") if refs else None,
                "provider_mode": "simulated" if provider.name == "local_simulation" else "paypal_sandbox"}

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
        for provider, name in (("channel3", "CHANNEL3_API_KEY"), ("groq", "GROQ_API_KEY"),
                               ("openrouter", "OPENROUTER_API_KEY")):
            fields = self.integrations.resolve(owner.owner_id, provider)
            keys[name] = fields["api_key"] if fields else ""
        if not keys["CHANNEL3_API_KEY"]:
            raise IntegrationRequired("channel3")
        if not (keys["GROQ_API_KEY"] or keys["OPENROUTER_API_KEY"]):
            raise IntegrationRequired("groq")
        self.integrations.admission("lab:" + owner.owner_id)
        # Daily cap per workspace so shared server model quota cannot be exhausted by one user.
        try:
            allowed, retry = self.daily.hit(owner.owner_id)
        except LimiterUnavailable:
            raise PlatformError(503, "lab_unavailable") from None
        if not allowed:
            raise PlatformError(429, "lab_daily_limit", headers={"Retry-After": str(retry)})
        _, key = self.context(owner)
        keys["AGENTPAL_AGENT_KEY"] = key
        keys["AGENTPAL_BASE_URL"] = "http://127.0.0.1:8000"
        events = []

        def emit(event):
            # Shopper's Redactor cleans known workspace keys and upstream data first.
            events.append({"at": datetime.now(timezone.utc).isoformat(), "message": event["message"]})

        shopper = Shopper(env=keys, emit=emit)
        try:
            # Only planning/search is delegated to the bundled core. Submit its proposal
            # through the same authenticated agent PaymentDesk as every external agent.
            body = PaymentCreate.model_validate(shopper.plan(task, str(budget)))
            view = self.create(owner, body)
            return {"status": view["status"], "request_id": view["request_id"], "events": events}
        except (ShopperError, ValueError):
            emit({"message": "Errand could not produce a payment proposal. No payment was executed."})
            return {"status": "failed", "request_id": None, "events": events}
        finally:
            shopper.close()
