"""P2 verified capture facts and exactly-once net spending credits.

The wallet claim lock is always acquired before the assessment transaction. Original
requests, approved terms, outcomes and provider evidence remain immutable. Net spending
is reduced only after a verified COMPLETED refund; an append-only ledger event in the
SAME transaction is the durable deduplication marker across cross-DB crashes.

See docs/PAYPAL_REFUNDS.md for required lead-owned gross/net consistency integration.
"""
from decimal import Decimal

from ..spending import InvalidLedgerTransition

_PREFIX = 'paypal_refund_credit:'


def verified_capture(store, request_id, agent_id):
    order = store.provider_order(request_id, agent_id=agent_id)
    if not order or order['provider'] != 'paypal' or not order['provider_order_id']:
        raise InvalidLedgerTransition('verified_capture_required')
    proof = store.query(
        'SELECT DISTINCT r.provider_capture_id, e.amount, e.currency FROM provider_events e '
        'JOIN provider_event_captures r USING(provider,event_id) '
        "WHERE e.agent_id=? AND e.request_id=? AND e.provider='paypal' AND e.verified=1 "
        "AND e.status='completed' AND e.disposition IN ('applied','out_of_order_ignored') "
        'AND e.provider_order_id=? AND r.provider_capture_id IS NOT NULL',
        (agent_id, request_id, order['provider_order_id']))
    if len(proof) != 1:
        raise InvalidLedgerTransition('verified_capture_required')
    p = proof[0]
    request = store.query('SELECT amount,currency FROM requests WHERE agent_id=? AND request_id=?',
                          (agent_id, request_id))
    outcomes = store.query(
        "SELECT 1 FROM payment_outcomes WHERE agent_id=? AND request_id=? AND verified=1 "
        "AND outcome='completed' AND provider_reference=? LIMIT 1",
        (agent_id, request_id, p['provider_capture_id']))
    if (not request or not outcomes or p['amount'] is None
            or Decimal(p['amount']) != Decimal(request[0]['amount'])
            or p['currency'] != request[0]['currency']):
        raise InvalidLedgerTransition('verified_capture_required')
    return {'capture_id': p['provider_capture_id'], 'order_id': order['provider_order_id'],
            'captured_total': Decimal(p['amount']), 'currency': p['currency']}


def credit_refund(store, refund, captured_total, now):
    """Caller holds wallet.tx and supplies its persisted, verified refund row."""
    if refund['status'] != 'COMPLETED' or not refund['evidence_sha256'] or not refund['provider_refund_id']:
        raise InvalidLedgerTransition('verified_refund_required')
    agent, rid = refund['agent_id'], refund['request_id']
    amount = Decimal(refund['amount'])
    marker = _PREFIX + refund['refund_id'] + ':' + str(amount)
    with store._tx() as c:
        events = c.execute('SELECT reason FROM ledger_events WHERE agent_id=? AND request_id=?',
                           (agent, rid)).fetchall()
        reasons = [r['reason'] for r in events if (r['reason'] or '').startswith(_PREFIX)]
        if marker in reasons:
            return
        credited = sum((Decimal(r.rsplit(':', 1)[1]) for r in reasons), Decimal(0))
        entry = c.execute('SELECT * FROM ledger_entries WHERE agent_id=? AND request_id=?',
                          (agent, rid)).fetchone()
        if (not entry or entry['state'] != 'completed' or entry['currency'] != refund['currency']
                or entry['owner_id'] != refund['owner_id'] or amount <= 0
                or credited + amount > captured_total
                or Decimal(entry['amount']) != captured_total - credited):
            raise InvalidLedgerTransition('refund_ledger_conflict')
        remaining = captured_total - credited - amount
        state = 'reversed' if remaining == 0 else 'completed'
        # Reversed entries no longer count as spending. Preserve their gross amount
        # for the existing immutable-terms startup check. Completed entries hold net.
        c.execute('UPDATE ledger_entries SET amount=?,state=?,updated_at=? '
                  'WHERE agent_id=? AND request_id=?', (str(captured_total if state == 'reversed' else remaining), state, now, agent, rid))
        c.execute('INSERT INTO ledger_events '
                  '(agent_id,request_id,from_state,to_state,actor,reason,at) VALUES (?,?,?,?,?,?,?)',
                  (agent, rid, entry['state'], state, 'verified:paypal_refund', marker, now))
        # Keep the existing structured assessment audit format and secret redaction.
        from datetime import datetime
        store._write_outcome_audit(c, rid, agent, 'verified_paypal_refund_credited',
                                   datetime.fromisoformat(now))
