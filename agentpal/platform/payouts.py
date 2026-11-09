"""Owner-approved single-item sandbox payouts, using the existing wallet ledger."""

from __future__ import annotations

import hmac
import json
import uuid
from decimal import Decimal

from ..contracts import Capability
from ..infra_contracts import ProviderError
from ..providers.paypal_payouts import PayPalPayoutsProvider
from ..providers.paypal_sandbox import PayPalSandboxProvider
from ..spending import InvalidLedgerTransition
from ..wallet.payout_ledger import PayoutLedger
from ..wallet.principals import NotAuthorized
from .errors import PlatformError, not_found, not_configured
from .payout_models import digest
from .timeline import RequestHistory


class PayoutDesk:
    def __init__(self, platform):
        self.p = platform
        self.store = platform.deps.wallet_store
        self.ledger = PayoutLedger(platform)

    def event(self, row, actor, code, observation=None):
        with self.store.tx() as c:
            c.execute("INSERT INTO platform_payout_events (request_id,agent_id,actor,code,"
                "item_status,raw_sha256,at) VALUES (?,?,?,?,?,?,?)",
                (row.get("request_id"), row["agent_id"], actor, code,
                 observation.transaction_status if observation else None,
                 observation.raw_sha256 if observation else None, self.p.clock().isoformat()))

    def row(self, request_id):
        rows = self.store.query("SELECT * FROM platform_payout_requests WHERE request_id=?",
                                (request_id,))
        if not rows:
            raise not_found()
        return rows[0]

    def owned(self, owner, request_id):
        row = self.row(request_id)
        if row["owner_id"] != owner.owner_id:
            self.event(row, owner.owner_id, "cross_owner_refused")
            raise not_found("cross_owner_payout")
        return row

    def agent_row(self, ctx, request_id):
        row = self.row(request_id)
        if row["agent_id"] != ctx.agent_id or row["owner_id"] != ctx.owner_id:
            self.event(row, ctx.agent_id, "cross_agent_refused")
            raise not_found("cross_agent_payout")
        return row

    def require(self, ctx, capability):
        self.p.payments._require(ctx, capability, "v1.payout", resource_agent_id=ctx.agent_id)

    def wallet(self, agent_id, currency, wallet_id=None):
        rows = self.p.wallets.for_agent(agent_id, currency)
        if not rows or (wallet_id is not None and rows[0]["wallet_id"] != wallet_id):
            raise PlatformError(409, "no_wallet_for_currency")
        if rows[0]["status"] != "active":
            raise PlatformError(409, "wallet_frozen")
        return rows[0]

    def self_receiver(self, ctx, email):
        # Platform agent ids are disjoint from mailboxes. Also reject owner-maintained
        # profile identity aliases; never trust identity assertions in a payout body.
        profile = self.p.agents.profile(ctx.agent_id) or {}
        aliases = [ctx.agent_id, profile.get("name", "")]
        metadata = json.loads(profile.get("metadata_json") or "{}")
        aliases.extend(metadata.get(k, "") for k in ("email", "identity_email", "receiver_email"))
        if email in {v.lower() for v in aliases if isinstance(v, str)}:
            raise PlatformError(409, "self_payout_forbidden")

    def history(self, row):
        return RequestHistory(self.ledger.store, self.store, row)

    def view(self, row, *, owner=False):
        terms = self.ledger.terms(row)
        order = self.ledger.order(row)
        events = self.store.query("SELECT item_status FROM platform_payout_events WHERE "
            "request_id=? AND item_status IS NOT NULL ORDER BY id DESC LIMIT 1", (row["request_id"],))
        item_status = events[0]["item_status"] if events else None
        history = self.history(row)
        return {"owner_decision": history.owner_decision() if owner else None,
            "verification": history.verification(), **terms, "terms_sha256": row["terms_sha256"], "created_at": row["created_at"],
            "status": self.ledger.status(row), "provider_batch_id": order["provider_order_id"] if order else None,
            "provider_item_status": item_status, "unclaimed": item_status == "UNCLAIMED",
            "warning": ("PayPal holds this payout until the receiver claims it."
                        if item_status == "UNCLAIMED" else None),
            "payment_environment": "sandbox", "is_real_money": False}

    def create(self, ctx, body, idem_key):
        try:
            return self._create(ctx, body, idem_key)
        except PlatformError as exc:
            self.event({"agent_id": ctx.agent_id}, ctx.agent_id, exc.code)
            raise

    def _create(self, ctx, body, idem_key):
        self.require(ctx, Capability.payout_request)
        if self.p.deps.resources:
            self.p.deps.resources.check_amount(ctx.owner_id, body.amount)
        payload = body.model_dump(mode="json")
        body_hash = digest(payload)
        with self.store.tx() as c:
            rows = c.execute("SELECT * FROM platform_payout_requests WHERE agent_id=? AND idem_key=?",
                             (ctx.agent_id, idem_key)).fetchall()
            if rows:
                row = dict(rows[0])
                if not hmac.compare_digest(row["body_sha256"], body_hash):
                    raise PlatformError(409, "idempotency_key_reused")
                if row["response_json"]:
                    return json.loads(row["response_json"]), True
            else:
                wallet = self.wallet(ctx.agent_id, body.currency)
                self.self_receiver(ctx, body.receiver_email)
                request_id = "payout_" + uuid.uuid4().hex
                terms = {**payload, "request_id": request_id, "agent_id": ctx.agent_id,
                         "owner_id": ctx.owner_id, "wallet_id": wallet["wallet_id"]}
                c.execute("INSERT INTO platform_payout_requests (request_id,agent_id,owner_id,"
                    "wallet_id,idem_key,body_sha256,terms_json,terms_sha256,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)", (request_id, ctx.agent_id, ctx.owner_id,
                    wallet["wallet_id"], idem_key, body_hash, json.dumps(terms), digest(terms),
                    self.p.clock().isoformat()))
                row = self.row(request_id)
                self.event(row, ctx.agent_id, "request_claimed")
        # A durable claim precedes the independent ledger commit, preserving recovery id.
        with self.store.tx() as c:
            row = self.row(row["request_id"])
            if row["response_json"]:
                return json.loads(row["response_json"]), True
            self.require(ctx, Capability.payout_request)
            self.wallet(ctx.agent_id, body.currency, row["wallet_id"])
            self.self_receiver(ctx, body.receiver_email)
            limits = self.p.wallets.active_limits(ctx.agent_id).get(body.currency)
            if limits is None:
                raise PlatformError(409, "no_spending_policy")
            if body.amount > limits.per_transaction_hard_limit:
                raise PlatformError(409, "per_transaction_limit_exceeded")
            if self.ledger.store.request_status(row["request_id"], agent_id=ctx.agent_id) is None:
                spend = self.ledger.store.spend_breakdown(ctx.agent_id, body.currency, self.p.clock())
                if body.amount > limits.monthly_budget - spend["committed"]:
                    raise PlatformError(409, "budget_exceeded")
            # assess() atomically rechecks monthly, rolling and per-transaction limits
            # and reserves in the SAME assessment-store transaction as payment requests.
            self.ledger.project(row)
            view = self.view(row)
            c.execute("UPDATE platform_payout_requests SET response_json=? WHERE request_id=?",
                      (json.dumps(view), row["request_id"]))
            self.event(row, ctx.agent_id, "hold_reserved")
            return view, False

    def list_owned(self, owner):
        rows = self.store.query("SELECT * FROM platform_payout_requests WHERE owner_id=? "
            "AND response_json IS NOT NULL ORDER BY created_at DESC,request_id LIMIT 100",
            (owner.owner_id,))
        return [self.view(row, owner=True) for row in rows]

    def decide(self, owner, request_id, terms_sha256=None, *, reject=False):
        row = self.owned(owner, request_id)
        try:
            with self.store.tx():
                row = self.owned(owner, request_id)
                self.ledger.terms(row)
                if not reject and not hmac.compare_digest(row["terms_sha256"], terms_sha256 or ""):
                    raise PlatformError(409, "payout_terms_changed")
                if not reject and self.p.deps.require_mfa_for_approval and not owner.mfa_satisfied:
                    raise PlatformError(403, "mfa_required")
                gateway = self.p.deps.approvals
                if gateway is None:
                    raise not_configured()
                canonical = self.ledger.project(row)
                if reject:
                    gateway.decline(owner, request_id)
                else:
                    gateway.approve(owner, request_id, terms_sha256=canonical.digest())
                self.event(row, owner.owner_id, "rejected" if reject else "approved")
                return self.view(row, owner=True)
        except NotAuthorized:
            self.event(row, owner.owner_id, "approval_refused")
            raise PlatformError(403, "approval_refused") from None
        except (InvalidLedgerTransition, ValueError):
            self.event(row, owner.owner_id, "approval_refused")
            raise PlatformError(409, "approval_refused") from None
        except PlatformError as exc:
            self.event(row, owner.owner_id, exc.code)
            raise

    def provider(self, owner_id=None, *, amount=None):
        provider = self.p.deps.paypal_for(owner_id) if owner_id else self.p.deps.provider
        if not isinstance(provider, PayPalSandboxProvider) or provider.environment != "sandbox":
            raise not_configured()
        if amount is not None and self.p.deps.resources:
            self.p.deps.resources.check_amount(owner_id, amount,
                                               server_provided=provider is self.p.deps.provider)
            self.p.deps.resources.consume_execution(owner_id,
                                                    server_provided=provider is self.p.deps.provider)
        return PayPalPayoutsProvider(provider.config, http_client=provider.http_client)

    def execute(self, ctx, request_id):
        row = self.agent_row(ctx, request_id)
        provider = self.provider(row["owner_id"], amount=Decimal(self.ledger.terms(row)["amount"]))
        try:
            with self.store.tx():
                self.require(ctx, Capability.payment_execute_sandbox)
                row = self.agent_row(ctx, request_id)
                terms = self.ledger.terms(row)
                self.wallet(ctx.agent_id, terms["currency"], row["wallet_id"])
                self.self_receiver(ctx, terms["receiver_email"])
                if self.ledger.order(row):
                    return self.view(row)  # replay is local; recovery is an explicit READ
                ticket = self.ledger.begin(row)
                self.event(row, ctx.agent_id, "execution_claimed")
        except (InvalidLedgerTransition, ValueError):
            self.event(row, ctx.agent_id, "execution_refused")
            raise PlatformError(409, "payout_not_approved") from None
        try:
            batch = provider.create(terms)
        except ProviderError as exc:
            if exc.outcome_unknown:
                self.ledger.unknown(row)
            else:
                self.ledger.refused(row)
            self.event(row, ctx.agent_id, "provider_unknown" if exc.outcome_unknown else "provider_refused")
            return self.view(row)
        except Exception:
            self.ledger.unknown(row)
            self.event(row, ctx.agent_id, "provider_unknown")
            return self.view(row)
        try:
            self.ledger.record_batch(row, ticket, batch)
        except Exception:
            self.ledger.unknown(row)
            self.event(row, ctx.agent_id, "batch_persistence_unknown")
            return self.view(row)
        self.event(row, ctx.agent_id, "batch_recorded")
        return self.refresh(row, provider, ctx.agent_id)

    def refresh(self, row, provider, actor):
        order = self.ledger.order(row)
        if not order or not order["provider_order_id"]:
            raise PlatformError(409, "payout_batch_unknown")
        try:
            observation = provider.read(order["provider_order_id"], self.ledger.terms(row))
            with self.store.tx():
                current = self.row(row["request_id"])
                if current["terms_json"] != row["terms_json"]:
                    raise PlatformError(409, "payout_terms_changed")
                self.ledger.apply(row, observation)
                self.event(row, actor, "provider_observed", observation)
        except Exception:
            self.ledger.unknown(row)
            self.event(row, actor, "provider_read_unknown")
        return self.view(row)

    def reconcile(self, caller, request_id):
        from .runtime import OwnerCaller
        if isinstance(caller, OwnerCaller):
            row = self.owned(caller.owner, request_id)
            actor = caller.owner.owner_id
        else:
            row = self.agent_row(caller.ctx, request_id)
            self.require(caller.ctx, Capability.payment_execute_sandbox)
            actor = caller.ctx.agent_id
        result = self.refresh(row, self.provider(row["owner_id"]), actor)
        if isinstance(caller, OwnerCaller):
            result["owner_decision"] = self.history(row).owner_decision()
        return result
