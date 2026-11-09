"""Owner-scoped refund orchestration, durable reservations and dispute projection."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from decimal import Decimal
from uuid import uuid4

from ..infra_contracts import ProviderError
from ..lifecycle.refunds import credit_refund, verified_capture
from ..providers.paypal_refunds import PayPalRefunds, request_key
from ..schemas import CURRENCY_MINOR_UNITS
from ..spending import InvalidLedgerTransition
from .errors import PlatformError, not_configured
from .refund_models import RefundOut


def _sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


class RefundDesk:
    def __init__(self, platform):
        self.p = platform
        self.ws = platform.deps.wallet_store
        self.ledger = platform.risk().store

    def _owned(self, owner, request_id, *, mutation=False):
        meta = self.p.payments._owned_meta(owner, request_id)
        if mutation and self.p.deps.require_mfa_for_approval and not owner.mfa_satisfied:
            raise PlatformError(403, 'mfa_required', audit='mfa_required')
        return meta

    def _provider(self, owner_id=None):
        try:
            return PayPalRefunds(self.p.deps.paypal_for(owner_id) if owner_id else self.p.deps.provider)
        except ProviderError:
            raise not_configured() from None

    def _facts(self, request_id, agent_id):
        try:
            return verified_capture(self.ledger, request_id, agent_id)
        except (InvalidLedgerTransition, ValueError):
            raise PlatformError(409, 'verified_capture_required') from None

    def _audit(self, c, refund_id, action, actor):
        c.execute('INSERT INTO platform_paypal_refund_audit (refund_id,action,actor,at) '
                  'VALUES (?,?,?,?)', (refund_id, action, actor, self.p.clock().isoformat()))

    def _rows(self, request_id):
        return self.ws.query('SELECT * FROM platform_paypal_refunds WHERE request_id=? '
                             'ORDER BY created_at,refund_id LIMIT 100', (request_id,))

    @staticmethod
    def _out(row):
        return RefundOut(**{k: row[k] for k in RefundOut.model_fields if k in row}).model_dump(mode='json')

    def create(self, owner, request_id, body, key):
        meta = self._owned(owner, request_id, mutation=True)
        digest = _sha(json.dumps(body.model_dump(mode='json'), sort_keys=True, separators=(',', ':')))
        with self.ws.tx() as c:
            prior = c.execute('SELECT * FROM platform_paypal_refunds WHERE request_id=? AND key_sha256=?',
                              (request_id, _sha(key))).fetchone()
            if prior:
                if prior['body_sha256'] != digest:
                    raise PlatformError(409, 'idempotency_key_reused')
                return self._out(dict(prior)), True
            provider = self._provider(owner.owner_id)
            facts = self._facts(request_id, meta['agent_id'])
            if self.p.payments.status(request_id, meta['agent_id']).value != 'provider_confirmed':
                raise PlatformError(409, 'payment_not_refundable')
            rows = self._rows(request_id)
            if len(rows) >= 100:
                raise PlatformError(409, 'refund_limit_reached')
            reserved = sum((Decimal(r['amount']) for r in rows
                            if r['status'] not in ('FAILED', 'CANCELLED')), Decimal(0))
            remaining = facts['captured_total'] - reserved
            amount = remaining if body.amount is None else body.amount
            minor = CURRENCY_MINOR_UNITS.get(facts['currency'])
            if (minor is None or amount <= 0 or amount > remaining
                    or amount != amount.quantize(Decimal(1).scaleb(-minor))):
                raise PlatformError(409, 'invalid_refund_amount')
            refund_id = 'rfd-' + uuid4().hex
            now = self.p.clock().isoformat()
            c.execute('INSERT INTO platform_paypal_refunds '
                      '(refund_id,request_id,agent_id,owner_id,session_id,key_sha256,body_sha256,'
                      'provider_key,capture_id,order_id,amount,currency,status,created_at,updated_at) '
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'reconciliation_required',?,?)",
                      (refund_id, request_id, meta['agent_id'], owner.owner_id, owner.session_id,
                       _sha(key), digest, request_key(request_id, key), facts['capture_id'],
                       facts['order_id'], str(amount), facts['currency'], now, now))
            self._audit(c, refund_id, 'claimed', owner.owner_id + '|session:' + owner.session_id)
        # Durable one-shot claim precedes all network I/O. A crash stays unknown; never POST again.
        # Preflight READ: nothing has been sent to PayPal yet, so ANY failure here is a
        # definite non-refund. Release the reservation (FAILED) so the owner can retry
        # (lead fix 2026-11-03, independent review finding 2).
        try:
            provider.capture(facts['capture_id'], facts['captured_total'], facts['currency'])
        except ProviderError as exc:
            # A capture that CONTRADICTS verified facts is an anomaly: keep it flagged
            # (reconciliation_required) for the operator. Transport/availability failures
            # are a definite non-refund: release.
            if str(exc) != 'refund_capture_invalid':
                self._set_failure(refund_id, owner.owner_id, 'paypal_unavailable')
            return self._out(self.ws.query('SELECT * FROM platform_paypal_refunds WHERE refund_id=?',
                                           (refund_id,))[0]), False
        except Exception:
            self._set_failure(refund_id, owner.owner_id, 'paypal_unavailable')
            return self._out(self.ws.query('SELECT * FROM platform_paypal_refunds WHERE refund_id=?',
                                           (refund_id,))[0]), False
        # Refund POST: from here an uncertain outcome keeps the reservation (never re-POST).
        try:
            data = provider.refund(facts['capture_id'], amount, facts['currency'],
                                   key=request_key(request_id, key), invoice_id=refund_id,
                                   reason=body.reason)
        except ProviderError as exc:
            if not exc.outcome_unknown:
                self._set_failure(refund_id, owner.owner_id,
                                  'merchant_permission_required'
                                  if str(exc) == 'refund_permission_denied' else 'provider_rejected')
        except Exception:
            pass  # uncertain provider outcome: keep reserved, no raw errors escape
        else:
            self._apply(refund_id, data, provider, facts['captured_total'])
        return self._out(self.ws.query('SELECT * FROM platform_paypal_refunds WHERE refund_id=?',
                                       (refund_id,))[0]), False

    def _set_failure(self, refund_id, actor, reason='provider_rejected'):
        with self.ws.tx() as c:
            c.execute("UPDATE platform_paypal_refunds SET status='FAILED',failure_reason=?,updated_at=? "
                      "WHERE refund_id=?", (reason, self.p.clock().isoformat(), refund_id))
            self._audit(c, refund_id, 'provider_rejected:' + reason, actor)

    def _apply(self, refund_id, data, provider, captured_total):
        try:
            with self.ws.tx() as c:
                row = dict(c.execute('SELECT * FROM platform_paypal_refunds WHERE refund_id=?',
                                     (refund_id,)).fetchone())
                result = provider.verify(data, capture_id=row['capture_id'], amount=Decimal(row['amount']),
                                         currency=row['currency'], refund_id=row['provider_refund_id'])
                # Completed observations never regress on stale pending/failure reads.
                if row['status'] == 'COMPLETED':
                    if result['status'] != 'COMPLETED':
                        self._audit(c, refund_id, 'stale_read_ignored', 'provider')
                        return
                if row['status'] in ('FAILED', 'CANCELLED') and result['status'] != row['status']:
                    raise ProviderError('refund_terminal_conflict', outcome_unknown=True)
                c.execute('UPDATE platform_paypal_refunds SET status=?,provider_refund_id=?,evidence_sha256=?,'
                          'updated_at=? WHERE refund_id=?', (result['status'], result['provider_refund_id'],
                          result['evidence_sha256'], self.p.clock().isoformat(), refund_id))
                row.update(result)
                if row['status'] == 'COMPLETED':
                    credit_refund(self.ledger, row, captured_total, self.p.clock().isoformat())
                self._audit(c, refund_id, 'verified_' + result['status'].lower(), 'provider')
        except (ProviderError, InvalidLedgerTransition, sqlite3.IntegrityError, ValueError, TypeError):
            with self.ws.tx() as c:
                self._audit(c, refund_id, 'evidence_refused', 'provider')

    def list(self, owner, request_id):
        meta = self._owned(owner, request_id)
        facts = self._facts(request_id, meta['agent_id'])
        rows = self._rows(request_id)
        return {'request_id': request_id,
                'status': self.p.payments.status(request_id, meta['agent_id']).value,
                'captured_total': facts['captured_total'],
                'refunded_total': sum((Decimal(r['amount']) for r in rows if r['status'] == 'COMPLETED'), Decimal(0)),
                'reserved_total': sum((Decimal(r['amount']) for r in rows
                                       if r['status'] in ('PENDING', 'reconciliation_required')), Decimal(0)),
                'refunds': [self._out(r) for r in rows]}

    def reconcile(self, owner, request_id):
        meta = self._owned(owner, request_id, mutation=True)
        provider = self._provider(owner.owner_id)
        facts = self._facts(request_id, meta['agent_id'])
        for row in self._rows(request_id):
            if row['status'] in ('FAILED', 'CANCELLED'):
                continue
            try:
                data = (provider.read(row['provider_refund_id']) if row['provider_refund_id']
                        else provider.discover(row['order_id'], row['refund_id']))
                self._apply(row['refund_id'], data, provider, facts['captured_total'])
            except Exception:
                with self.ws.tx() as c:
                    self._audit(c, row['refund_id'], 'read_unavailable', owner.owner_id)
        return self.list(owner, request_id)

    def disputes(self, owner):
        provider = self._provider(owner.owner_id)
        # Bound the local join too; no global provider totals, links or buyer information.
        metas = self.ws.query('SELECT request_id,agent_id FROM platform_payment_meta WHERE owner_id=? '
                              'ORDER BY request_id LIMIT 1000', (owner.owner_id,))
        captures = {}
        for meta in metas:
            try:
                facts = self._facts(meta['request_id'], meta['agent_id'])
                captures[facts['capture_id']] = meta['request_id']
            except PlatformError:
                continue
        try:
            items = provider.disputes()
        except Exception:
            raise PlatformError(502, 'disputes_unavailable') from None
        out = []
        for item in items:
            if not isinstance(item, dict):
                continue
            txs = item.get('disputed_transactions', [])
            if not isinstance(txs, list) or len(txs) != 1 or not isinstance(txs[0], dict):
                continue
            rid = captures.get(txs[0].get('seller_transaction_id'))
            did, status, reason = item.get('dispute_id'), item.get('status'), item.get('reason')
            if (not rid or not isinstance(did, str) or not re.fullmatch(r'[A-Za-z0-9-]{1,128}', did)
                    or not isinstance(status, str) or not re.fullmatch(r'[A-Z_]{1,80}', status)):
                continue
            out.append({'dispute_id': did, 'request_id': rid, 'status': status,
                        'reason': reason if isinstance(reason, str) and re.fullmatch(r'[A-Z_]{1,100}', reason) else None})
        return {'disputes': out}
