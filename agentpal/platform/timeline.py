"""Owner history projected from persisted rows. Never emit raw audit/provider text.

Callers must authorize the request before constructing this projection. Every ledger
query also binds the agent id: request ids alone are not a financial identity.
"""
from datetime import datetime, timezone

from ..rules import HARD_VIOLATION_CODES


class RequestHistory:
    def __init__(self, ledger, wallet_store, row):
        self.ledger = ledger
        self.wallet_store = wallet_store
        self.row = row
        self.ids = (row['agent_id'], row['request_id'])

    def rows(self, table, columns='*'):
        # Table/column names below are internal constants, never request input.
        return self.ledger.query(f'SELECT {columns} FROM {table} '
                                 'WHERE agent_id=? AND request_id=?', self.ids)

    def decisions(self):
        return self.rows('owner_decisions', 'decision,at,id')

    def owner_decision(self):
        rows = self.decisions()
        if not rows:
            return None
        row = max(rows, key=lambda r: r['id'])
        return {'decision': 'approved' if row['decision'] == 'approved' else 'rejected',
                'at': utc(row['at'])}

    def assessment(self):
        return self.ledger.get_assessment(self.ids[1], agent_id=self.ids[0])

    def blocks(self):
        assessment = self.assessment()
        if not assessment or assessment.recommendation != 'block' or self.decisions():
            return []
        return [{'rule': s.code, 'label': s.message} for s in assessment.signals
                if s.kind == 'hard_violation' and s.source == 'rule'
                and s.code in HARD_VIOLATION_CODES]

    def proofs(self):
        proofs = []
        for row in self.rows('provider_events'):
            # A verified signature/read alone is insufficient: ledger acceptance and a
            # final outcome are required. Rejected, stale and conflicting rows stay unverified.
            if (not row['verified'] or row['disposition'] != 'applied'
                    or row['status'] not in {'completed', 'declined', 'refunded', 'reversed'}):
                continue
            if row['provider'] == 'paypal' and row['event_type'] == 'ORDER_STATUS_READ':
                method = 'paypal_read'
            elif row['provider'] == 'paypal_payouts_sandbox' and row['event_type'] == 'PAYOUT_STATUS_READ':
                method = 'paypal_read'
            elif row['provider'] == 'paypal' and row['event_type'].startswith(('PAYMENT.', 'CHECKOUT.')):
                method = 'paypal_webhook'
            else:
                continue  # capture POST responses are not authenticated GETs or webhooks
            proofs.append({'verified_by': method, 'at': utc(row['processed_at'])})
        for row in self.wallet_store.query(
                'SELECT at FROM platform_paypal_read_receipts WHERE agent_id=? AND request_id=?', self.ids):
            proof = {'verified_by': 'paypal_read', 'at': utc(row['at'])}
            if proof not in proofs:
                proofs.append(proof)
        return sorted(proofs, key=lambda p: p['at'])

    def verification(self):
        rows = self.proofs()
        return rows[-1] if rows else None

    def timeline(self, *, payout=False):
        events = []

        def add(at, actor, kind, label, detail=None, verified_by=None):
            events.append({'at': utc(at), 'actor': actor, 'kind': kind, 'label': label,
                           'detail': detail, 'verified_by': verified_by})

        add(self.row['created_at'], 'agent', 'created', 'The agent requested a sandbox payout.'
            if payout else 'The agent requested a sandbox payment.')
        assessment = self.assessment()
        if assessment:
            add(assessment.assessed_at.isoformat(), 'policy', 'policy_decision',
                'The policy assessment was recorded.',
                'Recommendation: ' + str(assessment.recommendation) + '.')
            for block in self.blocks():
                add(assessment.assessed_at.isoformat(), 'policy', 'policy_block',
                    block['label'], block['rule'])
        digest = self.row['terms_sha256']
        # Use the digest actually bound to an approval when available.
        approvals = self.rows('approvals', 'terms_sha256')
        if approvals:
            digest = approvals[0]['terms_sha256'] if not payout else digest
        for row in self.decisions():
            approved = row['decision'] == 'approved'
            add(row['at'], 'owner', 'owner_decision',
                'The owner approved the exact terms.' if approved else 'The owner rejected the request.',
                'Terms SHA-256: ' + digest[:12] + '.')
        for row in self.rows('ledger_events', 'at,to_state,reason'):
            if 'expired' in (row['reason'] or ''):
                add(row['at'], 'system', 'expired', 'The request expired.')
            elif row['to_state'] == 'pending':
                add(row['at'], 'system', 'execution', 'Execution was recorded.')
            elif row['to_state'] == 'reconciliation_required':
                add(row['at'], 'system', 'reconciliation', 'The provider outcome needs reconciliation.')
            elif row['to_state'] == 'reversed':
                add(row['at'], 'paypal', 'reversed', 'The verified payment was reversed.')
        for row in self.rows('payment_attempts', 'at'):
            add(row['at'], 'system', 'payment_attempt', 'A provider execution attempt was recorded.')
        if payout:
            for row in self.wallet_store.query(
                    "SELECT at FROM platform_payout_events WHERE agent_id=? AND request_id=? "
                    "AND code='batch_recorded'", self.ids):
                add(row['at'], 'paypal', 'payout', 'A PayPal payout batch was recorded.')
        else:
            for row in self.rows('provider_orders', 'provider_order_id,updated_at'):
                if row['provider_order_id']:
                    add(row['updated_at'], 'paypal', 'order_created', 'A provider order reference is recorded.',
                        'Time of the latest recorded provider update; original order-creation time is unavailable.')
        for row in self.rows('provider_operations', 'operation,invoked_at'):
            if row['operation'] == 'capture':
                add(row['invoked_at'], 'system', 'capture', 'A PayPal capture was requested.')
        for row in self.rows('provider_capture_checks', 'checked_at'):
            add(row['checked_at'], 'paypal', 'buyer_approval',
                'PayPal buyer approval and exact order terms were checked by authenticated read.')
        for row in self.rows('provider_events', 'provider,status,processed_at,disposition'):
            if row['provider'] not in {'paypal', 'paypal_payouts_sandbox'}:
                continue
            # Describe the recorded observation without promoting rejected evidence to fact.
            add(row['processed_at'], 'paypal', 'provider_observation', 'A PayPal outcome observation was recorded.')
        for proof in self.proofs():
            add(proof['at'], 'paypal', 'provider_verification', 'The PayPal outcome was verified.',
                'Verified by authenticated PayPal read.' if proof['verified_by'] == 'paypal_read'
                else 'Verified by signed PayPal webhook.', proof['verified_by'])
        if not payout:
            for table, kind, label in (
                ('platform_page_checks', 'page_check', 'An advisory page check was recorded.'),
                ('platform_market_checks', 'market_check', 'An advisory market check was recorded.'),
            ):
                for row in self.wallet_store.query(f'SELECT updated_at FROM {table} '
                        'WHERE request_id=? AND payload_json IS NOT NULL', (self.ids[1],)):
                    add(row['updated_at'], 'system', kind, label)
            # Owner counter-offer: an explicit, recorded owner action on both requests.
            for row in self.wallet_store.query(
                    'SELECT * FROM platform_counter_offers WHERE replacement_request_id IS NOT NULL '
                    'AND agent_id=? AND (original_request_id=? OR replacement_request_id=?)',
                    (self.ids[0], self.ids[1], self.ids[1])):
                if row['original_request_id'] == self.ids[1]:
                    add(row['created_at'], 'owner', 'counter_offer',
                        'The owner countered with different terms.',
                        'Replaced by ' + row['replacement_request_id'] + '. The replacement needs its own approval.')
                else:
                    add(self.row['created_at'], 'owner', 'counter_offer',
                        "Created from the owner's counter-offer.",
                        'Replaces ' + row['original_request_id'] + '. The owner wrote these terms; the agent did not.')
            for row in self.wallet_store.query(
                'SELECT a.at,a.action FROM platform_paypal_refund_audit a '
                'JOIN platform_paypal_refunds r USING(refund_id) '
                'WHERE r.agent_id=? AND r.request_id=? ORDER BY a.id', self.ids):
                if row['action'] == 'claimed':
                    add(row['at'], 'owner', 'refund', 'The owner requested a sandbox refund.')
                elif row['action'] in {'verified_completed', 'verified_pending', 'verified_failed', 'verified_cancelled'}:
                    add(row['at'], 'paypal', 'refund', 'A verified PayPal refund outcome was recorded.')
        # Equal timestamps are common (one transaction writes several sources).
        # Use a semantic tie-break, never database retrieval order. Identical
        # records are interchangeable; this ordering is also stable across reads.
        priority = {'created': 0, 'policy_decision': 1, 'policy_block': 2,
                    'owner_decision': 3, 'payment_attempt': 4, 'execution': 5,
                    'order_created': 6, 'capture': 7, 'buyer_approval': 8,
                    'provider_observation': 9, 'provider_verification': 10}
        events.sort(key=lambda e: (datetime.fromisoformat(e['at']),
                                  priority.get(e['kind'], 11), e['kind'], e['actor'],
                                  e['label'], e['detail'] or '', e['verified_by'] or ''))
        # Presentation only: keep the existing event schema and all persisted
        # evidence. A verification or another intervening event ends a group.
        grouped = []
        for event in events:
            if (grouped and event['kind'] == 'provider_observation'
                    and all(event[k] == grouped[-1][0][k]
                            for k in ('actor', 'kind', 'label', 'detail', 'verified_by'))):
                grouped[-1][1] += 1
            else:
                grouped.append([event.copy(), 1])
        for event, count in grouped:
            if count > 1:
                event['label'] += f' ×{count}'
        return {'request_id': self.ids[1], 'events': [event for event, _ in grouped]}


def utc(value):
    return datetime.fromisoformat(value).astimezone(timezone.utc).isoformat()
