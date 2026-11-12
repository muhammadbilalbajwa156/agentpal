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

    def scenario(self, owner, name):
        from .workspace_models import LabPaymentCreate

        if name not in {"over_limit", "split_purchase", "changed_terms", "payee_swap"}:
            raise not_found()
        self.integrations.admission("lab:" + owner.owner_id)
        # Share Lab admission/errand ceilings when the workspace uses operator keys.
        # No model, catalogue or payment-provider calls are needed by these examples.
        resources = self.integrations.resources
        for provider in ("channel3", "groq", "openrouter", "paypal_sandbox"):
            if resources.uses_server(owner.owner_id, provider):
                resources.consume(owner.owner_id, provider, "errands", server_provided=True)
                break
        # A fresh merchant separates repeat demonstrations from earlier reservations;
        # every part within one split demonstration still shares the same merchant.
        domain = "demo-" + uuid.uuid4().hex + ".example"
        body = {"currency": "USD", "merchant_name": "Test Lab supplies",
                "merchant_domain": domain, "purpose": "Test Lab policy demonstration"}

        def create(amount, **changes):
            return self.create(owner, LabPaymentCreate.model_validate(
                {**body, "amount": amount, **changes}))

        if name == "over_limit":
            views = [create("30.00")]
            expected = "AMOUNT_ABOVE_HARD_LIMIT"
        elif name == "split_purchase":
            views = [create("15.00", purpose="Supplies part one"),
                     create("15.00", purpose="Supplies part two")]
            expected = "SPLIT_TRANSACTION_EXCEEDS_HARD_LIMIT"
        else:
            if name == "changed_terms":
                views = [create("1.00"), create("2.00")]
                reason = "Approval refused: the terms changed after the fingerprint was taken"
            else:
                views = [create("1.00", merchant_paypal_email="original@" + domain),
                         create("1.00", merchant_paypal_email="swapped@" + domain)]
                reason = "Approval refused: merchant data replaced the PayPal payee; the original fingerprint does not authorize the replacement request."
            # Exercise the ordinary owner approval path with ONLY the stale digest.
            # Neither request is ever approved with its matching digest.
            try:
                self.p.payments.approve(owner, views[-1]["request_id"], views[0]["terms_sha256"])
            except PlatformError as exc:
                if exc.code != "terms_mismatch" or exc.status != 409:
                    raise
                self.p.risk().store.record_rejection("v1_terms_mismatch", views[-1]["request_id"],
                    views[-1]["agent_id"], [{"loc": ["request"], "type": "terms_mismatch"}])
            else:
                raise PlatformError(409, "scenario_not_refused")
            return self._scenario_result(owner, name, "refused", ["terms_mismatch"], reason, views)

        signals = [s for s in views[-1].get("risk_signals", []) if s["kind"] == "hard_violation"]
        codes = [s["code"] for s in signals]
        if views[-1]["recommendation"] != "block" or expected not in codes:
            raise PlatformError(409, "scenario_not_blocked")
        reason = next(s["message"] for s in signals if s["code"] == expected)
        return self._scenario_result(owner, name, "blocked", codes, reason, views)

    def _scenario_result(self, owner, name, outcome, codes, reason, views):
        # A demonstration must not leave work behind: any part still awaiting a decision
        # is declined through the ordinary owner reject path (it is never approved), so
        # its reservation is released and the Approvals queue stays clean.
        for view in views:
            if self.p.payments.get_for_owner(owner, view["request_id"])["status"] == "awaiting_approval":
                self.p.payments.reject(owner, view["request_id"])
        return {"scenario": name, "outcome": outcome, "rule_codes": codes,
                "plain_reason": reason, "request_ids": [v["request_id"] for v in views],
                "timeline_url_hint": "/v1/payment-requests/" + views[-1]["request_id"] + "/timeline"}

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
            if event.get("kind") == "candidate" and sum(e.get("kind") == "candidate" for e in events) >= 3:
                return
            fields = {k: event[k] for k in ("message", "kind", "query", "max_price", "title",
                      "amount", "currency", "merchant", "in_stock", "justification") if k in event}
            events.append({"at": datetime.now(timezone.utc).isoformat(), **fields})

        def admission(request):
            provider = {"api.trychannel3.com": "channel3", "api.groq.com": "groq",
                        "openrouter.ai": "openrouter"}.get(request.url.host)
            if provider:
                resources.consume(owner.owner_id, provider, server_provided=provider in shared)

        client = httpx.Client(timeout=20, follow_redirects=False, event_hooks={"request": [admission]})
        shopper = None
        try:
            shopper = Shopper(env=keys, emit=emit, client=client)
            emit({"kind": "boundary", "message": "AI proposes · Rules decide · You approve"})
            # Only planning/search is delegated to the bundled core. Submit its proposal
            # through the same authenticated agent PaymentDesk as every external agent.
            body = PaymentCreate.model_validate(shopper.plan(task, str(budget)))
            view = self.create(owner, body)
            return {"task": shopper.redact(task), "status": view["status"], "request_id": view["request_id"], "events": events}
        except (ShopperError, ValueError):
            emit({"message": "Errand could not produce a payment proposal. No payment was executed."})
            return {"task": shopper.redact(task) if shopper else task,
                    "status": "failed", "request_id": None, "events": events}
        finally:
            if shopper:
                shopper.close()
            client.close()
