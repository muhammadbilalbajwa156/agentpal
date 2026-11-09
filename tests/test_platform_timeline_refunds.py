"""History over real lifecycle/approval/refund services, using only fake PayPal HTTP."""
from datetime import datetime
import json

import pytest

from test_paypal_refunds import world  # noqa: F401


def test_capture_read_refund_history_and_no_provider_io(world):  # noqa: F811
    w = world
    # The capture POST response is not a GET or webhook: do not invent provenance.
    assert w.call('GET', w.path)['verification'] is None
    # A later authenticated read of the already-completed outcome must be usable.
    w.call('POST', w.path + '/reconciliation')
    owner = w.call('GET', w.path)
    assert owner['verification']['verified_by'] == 'paypal_read'
    assert owner['owner_decision']['decision'] == 'approved'
    assert w.refund().status_code == 201
    before = len(w.backend.requests)
    events = w.call('GET', w.path + '/timeline')['events']
    assert len(w.backend.requests) == before
    assert {'created', 'owner_decision', 'execution', 'order_created', 'capture',
            'buyer_approval', 'provider_verification', 'refund'} <= {e['kind'] for e in events}
    assert len([e for e in events if e['kind'] == 'refund']) == 2
    assert [datetime.fromisoformat(e['at']) for e in events] == sorted(datetime.fromisoformat(e['at']) for e in events)
    text = json.dumps(events)
    for value in ['fake-secret', 'fake-client', 'secret_sha256', 'session:', 'raw_sha256']:
        assert value not in text
    assert w.call('GET', w.path, headers=w.agent_headers)['owner_decision'] is None


@pytest.mark.parametrize('update', [
    {'verified': False}, {'amount': '0.99'}, {'event_type': 'ORDER_CAPTURE_RESPONSE'},
    {'provider_order_id': 'OTHER-ORDER'}, {'environment': 'production'},
])
def test_reconcile_mismatched_or_unverified_read_cannot_create_receipt(world, update):  # noqa: F811
    w = world
    provider = w.deps.provider
    original = provider.get_order_status
    proof = original('DEMO-ORDER-1')
    provider.get_order_status = lambda _: proof.model_copy(update=update)
    w.client.post(w.path + '/reconciliation', headers=w.headers)
    assert w.call('GET', w.path)['verification'] is None
    provider.get_order_status = original
