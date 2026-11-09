"""Durable, fail-closed PayPal payee binding, independent of wallet projection storage.

Only the lifecycle calls these methods. Transactions never acquire wallet/identity locks.
Shared provider/store contracts stay unchanged; legacy PayPal orders without a binding
require operator reconciliation rather than silently acquiring a new recipient.
"""
from datetime import timedelta
from decimal import Decimal

from ..providers.paypal_sandbox import (
    PayPalOrderEvidence, PayPalOrderEvent, PayPalOperationError, DECLINE_ISSUES,
)
from ..infra_contracts import ProviderStatus
from ..spending import ApprovalTerms, ApprovalTermsMismatch, InvalidLedgerTransition, LedgerState
from ..store import _as_utc


class PayPalCoreBinding:
    def __init__(self, store):
        self.store = store
        with store._tx() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS paypal_approved_terms (
                agent_id TEXT NOT NULL, request_id TEXT NOT NULL, terms_json TEXT NOT NULL,
                terms_sha256 TEXT NOT NULL, buyer_expires_at TEXT,
                email TEXT, merchant_id TEXT, PRIMARY KEY(agent_id,request_id))''')

    def bind_prepared(self, terms: ApprovalTerms, now):
        """Extend the assessed digest once, before it can be approved or executed.

        The API holds its wallet preparation transaction. Crash recovery may replay the
        same idempotent binding, but cannot replace an approved or already-bound payee.
        """
        legacy = terms.model_copy(update={'merchant_paypal_email': None})
        with self.store._tx() as c:
            row = c.execute('SELECT terms_sha256 FROM request_terms WHERE agent_id=? AND '
                            'request_id=?', (terms.agent_id, terms.request_id)).fetchone()
            prior = c.execute('SELECT terms_sha256 FROM paypal_approved_terms WHERE agent_id=? '
                              'AND request_id=?', (terms.agent_id, terms.request_id)).fetchone()
            if prior:
                if prior[0] != terms.digest() or row is None or row[0] != terms.digest():
                    raise ApprovalTermsMismatch('payee binding differs')
                return
            if (row is None or row[0] != legacy.digest()
                    or c.execute('SELECT 1 FROM approvals WHERE agent_id=? AND request_id=?',
                                 (terms.agent_id, terms.request_id)).fetchone()
                    or c.execute("SELECT 1 FROM ledger_entries WHERE agent_id=? AND request_id=? "
                                 "AND state!='reserved'", (terms.agent_id, terms.request_id)).fetchone()):
                raise ApprovalTermsMismatch('payee must bind before approval')
            c.execute('UPDATE request_terms SET terms_sha256=? WHERE agent_id=? AND request_id=?',
                      (terms.digest(), terms.agent_id, terms.request_id))
            c.execute('INSERT INTO paypal_approved_terms (agent_id,request_id,terms_json,terms_sha256) '
                      'VALUES (?,?,?,?)', (terms.agent_id, terms.request_id,
                                          terms.model_dump_json(), terms.digest()))
            self.store._write_outcome_audit(c, terms.request_id, terms.agent_id,
                                           'merchant_payee_bound', now)

    def begin(self, terms, now):
        with self.store._tx() as c:
            self.store._check_terms(c, terms.agent_id, terms.request_id, terms)
            c.execute('INSERT OR IGNORE INTO paypal_approved_terms '
                      '(agent_id,request_id,terms_json,terms_sha256) VALUES (?,?,?,?)',
                      (terms.agent_id, terms.request_id, terms.model_dump_json(), terms.digest()))
            row = c.execute('SELECT terms_sha256 FROM paypal_approved_terms WHERE agent_id=? '
                            'AND request_id=?', (terms.agent_id, terms.request_id)).fetchone()
            if row[0] != terms.digest():
                raise ApprovalTermsMismatch('execution payee binding differs')

    def row(self, agent_id, request_id):
        rows = self.store.query('SELECT * FROM paypal_approved_terms WHERE agent_id=? AND request_id=?',
                                (agent_id, request_id))
        return rows[0] if rows else None

    @staticmethod
    def exact(evidence, terms):
        return (isinstance(evidence, PayPalOrderEvidence) and evidence.unit_count == 1
                and evidence.amount == terms.amount and evidence.currency == terms.currency
                and evidence.reference_id == terms.request_id
                and evidence.custom_id == terms.request_id)

    def record_create(self, terms, ref, now, timeout_s):
        evidence = getattr(ref, 'order_evidence', None)
        if (not self.exact(evidence, terms) or not (evidence.email or evidence.merchant_id)
                or (terms.merchant_paypal_email is not None
                    and evidence.email != terms.merchant_paypal_email)):
            return False
        with self.store._tx() as c:
            self.store._check_terms(c, terms.agent_id, terms.request_id, terms)
            c.execute('UPDATE paypal_approved_terms SET email=?,merchant_id=?,buyer_expires_at=? '
                      'WHERE agent_id=? AND request_id=? AND buyer_expires_at IS NULL',
                      (evidence.email, evidence.merchant_id,
                       (_as_utc(now) + timedelta(seconds=timeout_s)).isoformat(),
                       terms.agent_id, terms.request_id))
            self.store._write_outcome_audit(c, terms.request_id, terms.agent_id,
                                           'provider_payee_recorded', now)
        return True

    def matches(self, agent_id, request_id, event):
        order = self.store.provider_order(request_id, agent_id=agent_id)
        if (not isinstance(event, PayPalOrderEvent) or event.verified is not True
                or event.provider != 'paypal' or event.environment != 'sandbox'
                or not order or event.provider_order_id != order['provider_order_id']):
            return False
        row = self.row(agent_id, request_id)
        if row is None or row['buyer_expires_at'] is None:
            return False
        terms = ApprovalTerms.model_validate_json(row['terms_json'])
        if terms.digest() != row['terms_sha256']:
            return False
        with self.store._tx() as c:
            try:
                self.store._check_terms(c, agent_id, request_id, terms)
            except ApprovalTermsMismatch:
                return False
        evidence = getattr(event, 'order_evidence', None)
        if not self.exact(evidence, terms):
            return False
        # Every supplied identity must agree; a matching merchant id cannot conceal
        # a contradictory email. An omitted email is acceptable only with a pinned id.
        if (not (evidence.email or evidence.merchant_id)
                or (evidence.email is not None and evidence.email != row['email'])
                or (evidence.merchant_id is not None and row['merchant_id'] is not None
                    and evidence.merchant_id != row['merchant_id'])
                or (evidence.email is None and (not row['merchant_id']
                    or evidence.merchant_id != row['merchant_id']))):
            return False
        if event.status == ProviderStatus.refunded:
            # PayPal reports a refunded capture with the REFUNDED amount (partial refunds are
            # below gross). Identity/payee were verified above; the amount must be a positive
            # part of the approved gross. The ledger sink still refuses a partial amount
            # (no state change), and refund accounting stays with the refund path
            # (lead fix 2026-11-03, independent review finding 1).
            return (event.currency == terms.currency and event.amount is not None
                    and Decimal(0) < event.amount <= terms.amount)
        return event.amount == terms.amount and event.currency == terms.currency

    def expire_buyer(self, agent_id, request_id, proof, now):
        """Release only a verified non-final order with no capture ever invoked.

        Pending captures and unknown reads always retain their hold. No PayPal response
        is invented to release it; this is a local cancellation before money movement.
        """
        row = self.row(agent_id, request_id)
        if not row or not row['buyer_expires_at'] or row['buyer_expires_at'] > _as_utc(now).isoformat():
            return False
        if (not proof.verified or proof.status.value not in
                ('created', 'payer_action_required', 'approved_by_payer')
                or not self.matches(agent_id, request_id, proof)):
            return False
        with self.store._tx() as c:
            entry = c.execute('SELECT state FROM ledger_entries WHERE agent_id=? AND request_id=?',
                              (agent_id, request_id)).fetchone()
            if entry is None or entry[0] != 'pending':
                return False
            if c.execute("SELECT 1 FROM provider_operations WHERE agent_id=? AND request_id=? "
                         "AND operation='capture'", (agent_id, request_id)).fetchone():
                raise InvalidLedgerTransition('capture outcome requires reconciliation')
            self.store._set_state(c, agent_id, request_id, LedgerState.cancelled,
                                  'system', 'buyer_approval_expired', now)
            self.store._write_outcome_audit(c, request_id, agent_id, 'buyer_approval_expired', now)
        return True

    def decline(self, agent_id, request_id, error, now):
        """Authenticated, allowlisted capture refusal, after the durable capture claim."""
        if (not isinstance(error, PayPalOperationError) or error.outcome_unknown
                or error.code not in DECLINE_ISSUES or not error.raw_sha256):
            raise InvalidLedgerTransition('decline evidence is not definitive')
        with self.store._tx() as c:
            row = c.execute('SELECT state FROM ledger_entries WHERE agent_id=? AND request_id=?',
                            (agent_id, request_id)).fetchone()
            claim = c.execute("SELECT 1 FROM provider_operations WHERE agent_id=? AND request_id=? "
                              "AND operation='capture'", (agent_id, request_id)).fetchone()
            if row is None or row[0] != 'pending' or claim is None:
                raise InvalidLedgerTransition('decline is not allowed')
            self.store._set_state(c, agent_id, request_id, LedgerState.failed,
                                  'verified:paypal_capture_error', error.code.lower(), now)
            c.execute('INSERT INTO payment_outcomes (agent_id,request_id,outcome,verified,'
                      'verification_method,provider_reference,at) VALUES (?,?,?,1,?,?,?)',
                      (agent_id, request_id, 'failed', 'paypal_capture_error',
                       error.raw_sha256, _as_utc(now).isoformat()))
            self.store._write_outcome_audit(c, request_id, agent_id, error.code.lower(), now)

    def cancel_late_unsent_capture(self, agent_id, request_id, capture_key, now):
        """The caller owns a newly committed claim and has not sent its POST yet.

        Check the buyer deadline again after claiming. Only this unsent invocation may
        remove its marker; a previous uncertain or pending capture is never cancelled.
        """
        with self.store._tx() as c:
            row = c.execute('SELECT p.buyer_expires_at,l.state FROM paypal_approved_terms p '
                            'JOIN ledger_entries l USING(agent_id,request_id) WHERE '
                            'p.agent_id=? AND p.request_id=?', (agent_id, request_id)).fetchone()
            if not row or not row['buyer_expires_at']:
                raise InvalidLedgerTransition('buyer deadline unavailable')
            if row['buyer_expires_at'] > _as_utc(now).isoformat():
                return False
            claim = c.execute("SELECT idempotency_key FROM provider_operations WHERE agent_id=? "
                              "AND request_id=? AND operation='capture'",
                              (agent_id, request_id)).fetchone()
            if row['state'] != 'pending' or not claim or claim[0] != capture_key:
                raise InvalidLedgerTransition('unsent capture claim differs')
            c.execute("DELETE FROM provider_operations WHERE agent_id=? AND request_id=? "
                      "AND operation='capture'", (agent_id, request_id))
            self.store._set_state(c, agent_id, request_id, LedgerState.cancelled,
                                  'system', 'buyer_approval_expired', now)
            self.store._write_outcome_audit(c, request_id, agent_id,
                                           'unsent_capture_expired', now)
        return True
