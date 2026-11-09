"""Payout projection onto the existing financial ledger; no independent balance.

The payout digest is embedded in ApprovalTerms.purpose. Thus the ledger's independent
digest check binds receiver, note, purpose, wallet and all identity fields at execution.
Public store APIs enforce limits, approval expiry/single-use, attempts and settlement.
Wallet -> assessment is the existing lock order. Provider I/O holds neither transaction.
"""

from __future__ import annotations

import json

from ..infra_contracts import ProviderEvent, ProviderStatus
from ..lifecycle import PaymentLifecycleService
from ..platform.errors import PlatformError
from ..platform.payout_models import PayoutCreate, digest
from ..providers.paypal_payouts import PROVIDER
from ..schemas import PaymentRequest
from ..spending import ApprovalTerms


def payment_request(terms: dict) -> PaymentRequest:
    return PaymentRequest(request_id=terms["request_id"], agent_id=terms["agent_id"],
        amount=terms["amount"], currency=terms["currency"],
        merchant_name="PayPal payout " + digest({"receiver": terms["receiver_email"]})[:24],
        merchant_domain="paypal.com", purpose="PayPal payout terms SHA256:" + digest(terms))


class PayoutLedger:
    def __init__(self, platform):
        self.p = platform
        self.store = platform.risk().store
        self.lifecycle = PaymentLifecycleService(self.store, clock=platform.clock)

    def terms(self, row):
        try:
            terms = json.loads(row["terms_json"])
            fields = {k: terms[k] for k in PayoutCreate.model_fields}
            body = PayoutCreate.model_validate(fields).model_dump(mode="json")
            expected = {**body, **{k: row[k] for k in
                                  ("request_id", "agent_id", "owner_id", "wallet_id")}}
            if terms != expected or digest(terms) != row["terms_sha256"]:
                raise ValueError
            return terms
        except (ValueError, TypeError, KeyError):
            raise PlatformError(409, "payout_terms_changed", audit="payout_terms_changed") from None

    def project(self, row):
        terms = self.terms(row)
        request = payment_request(terms)
        assessment = self.store.get_assessment(request.request_id, agent_id=request.agent_id)
        if assessment is None:
            if self.store.request_status(request.request_id, agent_id=request.agent_id) is not None:
                raise PlatformError(409, "preparation_interrupted")
            assessment = self.p.risk().assess(request)
        if assessment.recommendation == "block":
            raise PlatformError(409, "payout_policy_refused", audit="payout_policy_refused")
        canonical = ApprovalTerms.from_request(request)
        with self.p.deps.wallet_store.tx() as c:
            c.execute("INSERT OR IGNORE INTO wallet_payment_requests (request_id, agent_id, "
                "owner_id, terms_json, terms_sha256, recommendation, risk_score, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)", (row["request_id"], row["agent_id"], row["owner_id"],
                canonical.model_dump_json(), canonical.digest(), assessment.recommendation,
                assessment.risk_score, row["created_at"]))
        return canonical

    def status(self, row):
        return self.lifecycle.status(row["request_id"], agent_id=row["agent_id"]).value

    def begin(self, row):
        terms = ApprovalTerms.from_request(payment_request(self.terms(row)))
        ticket = self.lifecycle.begin_execution(row["agent_id"], row["request_id"], terms, PROVIDER)
        self.store.claim_provider_operation(row["request_id"], agent_id=row["agent_id"],
            provider=PROVIDER, operation="create_order",
            ticket={**ticket.model_dump(), "terms": ticket.terms}, now=self.p.clock())
        return ticket

    def order(self, row):
        return self.store.provider_order(row["request_id"], agent_id=row["agent_id"])

    def record_batch(self, row, ticket, batch):
        self.store.record_provider_order(row["request_id"], agent_id=row["agent_id"],
            provider=PROVIDER, provider_order_id=batch, idempotency_key=ticket.create_key,
            status="pending", now=self.p.clock())

    def unknown(self, row):
        self.store.mark_reconciliation_required(row["request_id"], "payout_outcome_unknown",
                                                agent_id=row["agent_id"], now=self.p.clock())

    def refused(self, row):
        self.store.cancel_before_provider_order(row["request_id"], "payout_provider_refused",
                                                agent_id=row["agent_id"], now=self.p.clock())

    def apply(self, row, observation):
        terms = self.terms(row)
        status = observation.transaction_status
        target = ProviderStatus.pending
        if status == "SUCCESS":
            target = ProviderStatus.completed
        elif status in {"FAILED", "BLOCKED", "DENIED"}:
            target = ProviderStatus.declined
        elif status in {"RETURNED", "REFUNDED", "REVERSED"}:
            # Existing ledger conservatively holds refund-before-completion conflicts.
            # A payout returned before any SUCCESS is a verified failure, not spending.
            completed = self.store.query("SELECT 1 FROM payment_outcomes WHERE agent_id=? "
                "AND request_id=? AND outcome='completed' AND verified=1",
                (row["agent_id"], row["request_id"]))
            target = ProviderStatus.refunded if completed else ProviderStatus.declined
        event = ProviderEvent(provider=PROVIDER, environment="sandbox",
            event_id="payout_" + digest({"batch": observation.batch_id, "item": observation.item_id,
                                        "status": status, "terms": row["terms_sha256"]}),
            event_type="PAYOUT_STATUS_READ", provider_order_id=observation.batch_id,
            provider_capture_id=observation.item_id, status=target, verified=True,
            amount=terms["amount"], currency=terms["currency"], received_at=self.p.clock(),
            raw_sha256=observation.raw_sha256)
        self.lifecycle.apply_provider_event(event)
