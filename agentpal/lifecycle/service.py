"""Payment lifecycle: gated execution, provider calls, provider events, expiry and
reconciliation (SEC-15, SEC-16, SEC-20).

Rules this service enforces (the store enforces them again inside its transactions):

* execution needs an authenticated owner approval for exactly the terms, unexpired and
  unconsumed; `begin_execution` consumes it and persists BOTH provider idempotency keys
  (one per agent, request and operation) before any provider call;
* `ProviderError(outcome_unknown=True)`, any unexpected exception, or any response that
  does not match the attempt -> `reconciliation_required`. Nothing is retried blindly;
* `ProviderError(outcome_unknown=False)` from create_order, with no provider order id ever
  recorded -> `cancelled` (hold returned). This is safe because the provider said
  definitively that it did not create an order, and without an order id nothing can be
  captured: AgentPal only ever captures an order id it stored. If any order id exists the
  outcome is treated as ambiguous instead;
* `reconciliation_required` is never released and never retried; only a VERIFIED
  provider event or a verified status read (`reconcile`) moves it on;
* only VERIFIED provider events settle; amount and currency must equal the approved terms.

Nothing here can approve a payment or move real money; providers are sandbox only.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from ..infra_contracts import (
    PaymentLifecycle,
    PaymentProvider,
    ProviderError,
    ProviderEvent,
    ProviderOrderRef,
    ProviderStatus,
)
from ..spending import ApprovalTerms, InvalidLedgerTransition, LedgerState
from ..store import AssessmentStore, UnknownProviderOrder, _as_utc
from .mapping import to_lifecycle
from .paypal_core import PayPalCoreBinding
from ..providers.paypal_sandbox import (
    PayPalOperationError, PayPalOrderEvent, DECLINE_ISSUES, BUYER_ISSUES,
)


class ExecutionTicket(BaseModel):
    """What `begin_execution` hands to the provider step. Both keys are already stored."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    agent_id: str
    request_id: str
    provider: str
    approval_id: int
    terms: ApprovalTerms
    terms_sha256: str
    create_key: str
    capture_key: str


class LifecycleError(InvalidLedgerTransition):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class PaymentLifecycleService:
    """Implements `infra_contracts.ProviderEventSink`."""

    def __init__(self, store: AssessmentStore, *, clock: Callable[[], datetime] | None = None,
                 paypal_provider: PaymentProvider | None = None):
        self.store = store
        self.clock = clock or (lambda: store.clock())
        self.paypal = PayPalCoreBinding(store)
        self._paypal_provider = paypal_provider

    # ------------------------------------------------------------- status
    def status(self, request_id: str, *, agent_id: str | None = None) -> PaymentLifecycle:
        """Read-only (writes nothing); deadlines are applied to the reported value."""
        facts = self.store.lifecycle_facts(request_id, agent_id=agent_id)
        if facts is None:
            raise LookupError(f"unknown request {request_id}")
        return to_lifecycle(facts, _as_utc(self.clock()))

    # ---------------------------------------------------------- execution
    def begin_execution(self, agent_id: str, request_id: str, terms: ApprovalTerms,
                        provider: str) -> ExecutionTicket:
        t = self.store.begin_execution(request_id, terms, provider, agent_id=agent_id,
                                       now=self.clock())
        if provider == "paypal":
            self.paypal.begin(terms, self.clock())
        return ExecutionTicket(terms=terms, **t)

    def submit_to_provider(self, ticket: ExecutionTicket,
                           provider: PaymentProvider) -> PaymentLifecycle:
        """create_order with the persisted create key. Never retried here."""
        key = dict(agent_id=ticket.agent_id)
        if getattr(provider, "environment", None) != "sandbox" \
                or getattr(provider, "name", None) != ticket.provider:
            self.store.mark_reconciliation_required(ticket.request_id,
                                                    "provider_not_the_attempted_one", **key)
            return self.status(ticket.request_id, **key)
        self.store.claim_provider_operation(
            ticket.request_id, agent_id=ticket.agent_id, provider=ticket.provider,
            operation="create_order", ticket={**ticket.model_dump(), "terms": ticket.terms},
            now=self.clock())
        if ticket.provider == 'paypal':
            if getattr(self._paypal_provider, 'name', None) != 'paypal':  # keep a server PayPal provider for webhook reads
                self._paypal_provider = provider
        try:
            ref = provider.create_order(ticket.terms, idempotency_key=ticket.create_key)
        except ProviderError as exc:
            if exc.outcome_unknown:
                self.store.mark_reconciliation_required(ticket.request_id,
                                                        "provider_outcome_unknown", **key)
            else:
                try:
                    self.store.cancel_before_provider_order(
                        ticket.request_id,
                        exc.code.lower() if isinstance(exc, PayPalOperationError)
                        and exc.code in DECLINE_ISSUES else "provider_rejected_before_order", **key)
                except InvalidLedgerTransition:
                    self.store.mark_reconciliation_required(
                        ticket.request_id, "provider_error_with_order", **key)
            return self.status(ticket.request_id, **key)
        except Exception:  # anything unexpected: the provider may have acted
            self.store.mark_reconciliation_required(ticket.request_id,
                                                    "provider_call_crashed", **key)
            raise
        if not isinstance(ref, ProviderOrderRef) or ref.environment != "sandbox" \
                or ref.provider != ticket.provider \
                or ref.idempotency_key != ticket.create_key:
            self.store.mark_reconciliation_required(ticket.request_id,
                                                    "provider_response_mismatch", **key)
            return self.status(ticket.request_id, **key)
        try:
            self.store.record_provider_order(ticket.request_id, agent_id=ticket.agent_id,
                                             provider=ref.provider,
                                             provider_order_id=ref.provider_order_id,
                                             idempotency_key=ref.idempotency_key,
                                             payer_action_url=ref.payer_action_url,
                                             status=ref.status.value, now=self.clock())
        except Exception:
            self.store.mark_reconciliation_required(ticket.request_id,
                                                    "provider_order_not_recorded", **key)
            raise
        if ticket.provider == "paypal" and not self.paypal.record_create(
                ticket.terms, ref, self.clock(), provider.config.buyer_approval_timeout_s):
            self.store.mark_reconciliation_required(ticket.request_id,
                                                    "provider_create_terms_mismatch", **key)
        if ref.status.value == "unknown":
            self.store.mark_reconciliation_required(ticket.request_id,
                                                    "provider_create_status_unknown", **key)
        return self.status(ticket.request_id, **key)

    def _order_for(self, agent_id: str, request_id: str, provider: PaymentProvider,
                   allowed: tuple[LedgerState, ...]) -> dict:
        entry = self.store.ledger_entry(request_id, agent_id=agent_id)
        order = self.store.provider_order(request_id, agent_id=agent_id)
        if entry is None or order is None or not order["provider_order_id"]:
            raise LifecycleError("NO_PROVIDER_ORDER", request_id)
        if order["provider"] != getattr(provider, "name", None) \
                or getattr(provider, "environment", None) != "sandbox":
            raise LifecycleError("WRONG_PROVIDER", request_id)
        if LedgerState(entry["state"]) not in allowed:
            raise LifecycleError("NOT_ALLOWED_IN_STATE", entry["state"])
        return order

    def capture(self, agent_id: str, request_id: str,
                provider: PaymentProvider) -> PaymentLifecycle:
        """Capture with the persisted capture key. Only from `pending`: an entry in
        reconciliation_required is never retried (use `reconcile`)."""
        self.store.expire_holds(self.clock())
        order = self._order_for(agent_id, request_id, provider, (LedgerState.pending,))
        if order['provider'] == 'paypal':
            if getattr(self._paypal_provider, 'name', None) != 'paypal':  # keep a server PayPal provider for webhook reads
                self._paypal_provider = provider
        key = dict(agent_id=agent_id)
        # The payer read happens BEFORE any capture request, so its failure leaves no money
        # outcome uncertain. Do not quarantine the entry (reconciliation_required can never
        # be captured again): leave it pending and let the owner retry (lead audit fix).
        try:
            proof = provider.get_order_status(order["provider_order_id"])
        except ProviderError:
            raise LifecycleError("PAYER_READ_UNAVAILABLE") from None
        if (not isinstance(proof, ProviderEvent) or proof.verified is not True
                or proof.environment != "sandbox" or proof.provider != order["provider"]
                or proof.provider_order_id != order["provider_order_id"]
                or proof.event_type != "ORDER_STATUS_READ"):
            raise LifecycleError("INVALID_PAYER_READ")
        if order["provider"] == "paypal":
            if not self.paypal.matches(agent_id, request_id, proof):
                self.store.mark_reconciliation_required(request_id, "provider_terms_mismatch", **key)
                raise LifecycleError('PROVIDER_TERMS_MISMATCH')
            if self.paypal.expire_buyer(agent_id, request_id, proof, self.clock()):
                raise LifecycleError("BUYER_APPROVAL_EXPIRED")
        if proof.status in (ProviderStatus.completed, ProviderStatus.declined,
                            ProviderStatus.refunded, ProviderStatus.reversed):
            return self._apply_own(proof, order, agent_id, request_id)
        if proof.status != ProviderStatus.approved_by_payer:
            if proof.status == ProviderStatus.payer_action_required:
                raise LifecycleError('PAYER_ACTION_REQUIRED')
            raise LifecycleError("PAYER_NOT_APPROVED")
        order = self.store.claim_provider_operation(
            request_id, agent_id=agent_id, provider=provider.name, operation="capture",
            payer_approval=proof, now=self.clock())
        if order['provider'] == 'paypal' and self.paypal.cancel_late_unsent_capture(
                agent_id, request_id, order['capture_key'], self.clock()):
            raise LifecycleError('BUYER_APPROVAL_EXPIRED')
        try:
            event = provider.capture_order(order["provider_order_id"],
                                           idempotency_key=order["capture_key"])
        except ProviderError as exc:
            if (order["provider"] == "paypal" and isinstance(exc, PayPalOperationError)
                    and not exc.outcome_unknown and exc.raw_sha256):
                if exc.code in DECLINE_ISSUES:
                    self.paypal.decline(agent_id, request_id, exc, self.clock())
                    return self.status(request_id, **key)
                if exc.code in BUYER_ISSUES:
                    # Authenticated no-capture refusal permits a new explicit buyer step.
                    with self.store._tx() as c:
                        c.execute("DELETE FROM provider_operations WHERE agent_id=? AND "
                                  "request_id=? AND operation='capture'", (agent_id, request_id))
                        self.store._write_outcome_audit(c, request_id, agent_id,
                                                       exc.code.lower(), self.clock())
                    raise LifecycleError(exc.code) from None
            # Other errors can conceal an already completed capture.
            self.store.mark_reconciliation_required(request_id, "capture_outcome_unknown",
                                                    **key)
            return self.status(request_id, **key)
        except Exception:
            self.store.mark_reconciliation_required(request_id, "capture_call_crashed", **key)
            raise
        try:
            result = self._apply_own(event, order, agent_id, request_id)
            if (isinstance(event, ProviderEvent) and (event.verified is not True
                    or event.amount != Decimal(order["amount"]) or event.currency != order["currency"])):
                self.store.mark_reconciliation_required(request_id, "capture_evidence_inexact", **key)
                return self.status(request_id, **key)
            return result
        except Exception:
            self.store.mark_reconciliation_required(request_id, "capture_event_not_recorded",
                                                    **key)
            raise

    def reconcile(self, agent_id: str, request_id: str,
                  provider: PaymentProvider) -> PaymentLifecycle:
        """A verified, server-to-server status read. The only way, besides a verified
        webhook, to move an entry out of reconciliation_required."""
        order = self._order_for(agent_id, request_id, provider,
                                (LedgerState.pending, LedgerState.reconciliation_required,
                                 LedgerState.completed))
        if order['provider'] == 'paypal':
            if getattr(self._paypal_provider, 'name', None) != 'paypal':  # keep a server PayPal provider for webhook reads
                self._paypal_provider = provider
        try:
            event = provider.get_order_status(order["provider_order_id"])
        except ProviderError:
            return self.status(request_id, agent_id=agent_id)  # still unknown: no change
        return self._apply_own(event, order, agent_id, request_id)

    def _apply_own(self, event: object, order: dict, agent_id: str,
                   request_id: str) -> PaymentLifecycle:
        if not isinstance(event, ProviderEvent) or event.environment != "sandbox" \
                or event.provider != order["provider"] \
                or event.provider_order_id != order["provider_order_id"]:
            self.store.mark_reconciliation_required(request_id, "provider_event_mismatch",
                                                    agent_id=agent_id)
            return self.status(request_id, agent_id=agent_id)
        return self.apply_provider_event(event)

    # ------------------------------------------------- ProviderEventSink
    def apply_provider_event(self, event: ProviderEvent) -> PaymentLifecycle:
        """Idempotent by (provider, event_id). Raises `UnknownProviderOrder` (after
        recording and auditing the event) for an order AgentPal never created."""
        if not isinstance(event, ProviderEvent):
            raise TypeError("apply_provider_event needs a ProviderEvent")
        if event.provider == "paypal" and event.verified:
            orders = self.store.query("SELECT agent_id,request_id FROM provider_orders WHERE "
                                      "provider=? AND provider_order_id=?",
                                      (event.provider, event.provider_order_id))
            if orders and not isinstance(event, PayPalOrderEvent):
                # Capture webhooks often omit purchase-unit identity. Their signature
                # verifies the outcome; an authenticated order READ supplies the payee
                # and request binding. No caller/browser evidence can fill this gap.
                event = self._webhook_identity_read(event, orders[0])
            if orders and not self.paypal.matches(orders[0]["agent_id"],
                                                  orders[0]["request_id"], event):
                event = event.model_copy(update={"status": ProviderStatus.unknown,
                    "amount": None, "currency": None})
        disposition, entry = self.store.apply_provider_event(
            provider=event.provider, event_id=event.event_id, event_type=event.event_type,
            provider_order_id=event.provider_order_id,
            provider_capture_id=event.provider_capture_id, status=event.status.value,
            amount=event.amount, currency=event.currency, verified=event.verified,
            raw_sha256=event.raw_sha256, received_at=event.received_at, now=self.clock())
        if entry is None:
            raise UnknownProviderOrder(event.provider_order_id)
        return self.status(entry["request_id"], agent_id=entry["agent_id"])

    def _webhook_identity_read(self, event: ProviderEvent, order: dict) -> ProviderEvent:
        provider = self._paypal_provider
        if (provider is None or provider.name != 'paypal' or provider.environment != 'sandbox'):
            return event
        try:
            proof = provider.get_order_status(event.provider_order_id)
        except ProviderError:
            return event
        if (not isinstance(proof, PayPalOrderEvent) or proof.event_type != 'ORDER_STATUS_READ'
                or proof.status in (ProviderStatus.unknown, ProviderStatus.declined)
                or not self.paypal.matches(order['agent_id'], order['request_id'], proof)):
            return event
        combined = PayPalOrderEvent(**event.model_dump(), order_evidence=proof.order_evidence)
        with self.store._tx() as c:
            self.store._write_outcome_audit(c, order['request_id'], order['agent_id'],
                'webhook_identity_read:' + proof.raw_sha256, self.clock())
        return combined

    # ---------------------------------------------------- expiry (SEC-16)
    def expire_and_reconcile(self, now: datetime | None = None, *,
                             provider: PaymentProvider | None = None) -> list[tuple[str, str]]:
        """Release lapsed reservations/approvals and move attempts past their deadline to
        reconciliation_required. Entries already in reconciliation_required are never
        touched. Returns the (agent_id, request_id) pairs moved to reconciliation."""
        now = now or self.clock()
        if provider is not None and provider.name == "paypal" and provider.environment == "sandbox":
            # Maintenance needs the real authenticated provider. Without it a pending
            # money outcome is never released on the strength of local time alone.
            for row in self.store.query(
                    "SELECT p.agent_id,p.request_id,o.provider_order_id FROM paypal_approved_terms p "
                    "JOIN provider_orders o USING(agent_id,request_id) JOIN ledger_entries l "
                    "USING(agent_id,request_id) WHERE l.state='pending' AND o.provider='paypal' "
                    "AND p.buyer_expires_at<=? AND o.provider_order_id IS NOT NULL "
                    "ORDER BY p.buyer_expires_at LIMIT 100", (_as_utc(now).isoformat(),)):
                try:
                    proof = provider.get_order_status(row["provider_order_id"])
                except ProviderError:
                    continue
                if (isinstance(proof, ProviderEvent) and proof.verified
                        and proof.event_type == "ORDER_STATUS_READ"
                        and proof.provider == "paypal" and proof.environment == "sandbox"
                        and proof.provider_order_id == row["provider_order_id"]):
                    if not self.paypal.expire_buyer(row["agent_id"], row["request_id"], proof, now):
                        self._apply_own(proof, {"provider": "paypal",
                            "provider_order_id": row["provider_order_id"]},
                            row["agent_id"], row["request_id"])
        due = self.store.query("SELECT agent_id, request_id FROM ledger_entries WHERE "
                               "state='pending' AND expires_at <= ?", (_as_utc(now).isoformat(),))
        self.store.expire_holds(now)
        return [(r["agent_id"], r["request_id"]) for r in due]
