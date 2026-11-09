"""Shared owner history contract; existing rows only, no provider I/O."""
from datetime import datetime
import json

import pytest

from test_platform_fixtures import ALL_PAY, OWNER_A, OWNER_B, build_world
from test_paypal_payouts import world, approved, wired_world  # noqa: F401
from test_infra_integration_platform import bearer


def payment():
    w = build_world()
    aid, key, _ = w.funded_agent(caps=ALL_PAY)
    r = w.pay(key)
    assert r.status_code == 201
    return w, aid, key, r.json()


def get(w, rid, token=OWNER_A, suffix=''):
    return w.client.get(f'/v1/payment-requests/{rid}{suffix}', headers=w.h(token))


def test_timeline_security_and_creation():
    w, _, key, p = payment()
    rid = p['request_id']
    assert get(w, rid, key, '/timeline').status_code == 403
    assert get(w, rid, OWNER_B, '/timeline').json() == get(w, rid, OWNER_B).json()
    assert get(w, rid, OWNER_B, '/timeline').status_code == 404
    assert get(w, 'pay_missing', suffix='/timeline').status_code == 404
    r = get(w, rid, suffix='/timeline')
    assert r.status_code == 200
    data = r.json()
    assert data['request_id'] == rid
    assert {'created', 'policy_decision'} <= {e['kind'] for e in data['events']}
    times = [datetime.fromisoformat(e['at']) for e in data['events']]
    assert times == sorted(times)
    assert all(t.utcoffset().total_seconds() == 0 for t in times)
    assert all(set(e) == {'at', 'actor', 'kind', 'label', 'detail', 'verified_by'} for e in data['events'])


@pytest.mark.parametrize('action,decision', [('approve', 'approved'), ('reject', 'rejected')])
def test_owner_decision_persists_and_is_hidden_from_agent(action, decision):
    w, _, key, p = payment()
    rid = p['request_id']
    path = f'/v1/payment-requests/{rid}/{action}'
    kwargs = {'json': {'terms_sha256': p['terms_sha256']}} if action == 'approve' else {}
    assert w.client.post(path, headers=w.h(OWNER_A), **kwargs).status_code == 200
    owner = get(w, rid).json()
    assert owner['owner_decision']['decision'] == decision
    assert datetime.fromisoformat(owner['owner_decision']['at']).tzinfo is not None
    if action == 'approve':
        assert w.client.post(f'/v1/payment-requests/{rid}/execute', headers=w.h(key)).status_code == 200
        assert get(w, rid).json()['owner_decision'] == owner['owner_decision']
    for response in [get(w, rid, key).json(),
                     w.client.get('/v1/payment-requests', headers=w.h(key)).json()['payment_requests'][0],
                     w.pay(key, idem='replay-original').json()]:
        assert response['owner_decision'] is None
        assert response['risk_signals'] == []
        assert response['agent_context'] is None
    timeline = get(w, rid, suffix='/timeline').json()['events']
    event = next(e for e in timeline if e['kind'] == 'owner_decision')
    assert event['actor'] == 'owner' and p['terms_sha256'][:12] in event['detail']
    assert event['at'] == owner['owner_decision']['at']


def test_hard_block_is_policy_not_human_rejection():
    w = build_world()
    _, key, _ = w.funded_agent(caps=ALL_PAY)
    r = w.pay(key, amount='61.00')
    assert r.status_code == 201, r.text
    p = r.json()
    owner = get(w, p['request_id']).json()
    assert owner['owner_decision'] is None
    assert owner['policy_block']['rule'] == 'AMOUNT_ABOVE_HARD_LIMIT'
    assert owner['policy_block']['label']
    assert p['policy_block'] is None
    events = get(w, p['request_id'], suffix='/timeline').json()['events']
    assert any(e['actor'] == 'policy' and e['kind'] == 'policy_block'
               and 'AMOUNT_ABOVE_HARD_LIMIT' in e['detail'] for e in events)


@pytest.mark.parametrize('event_type,verified,disposition,status,method', [
    ('ORDER_STATUS_READ', 1, 'applied', 'completed', 'paypal_read'),
    ('PAYMENT.CAPTURE.COMPLETED', 1, 'applied', 'completed', 'paypal_webhook'),
    ('PAYMENT.CAPTURE.COMPLETED', 0, 'unverified_recorded', 'completed', None),
    ('ORDER_STATUS_READ', 1, 'mismatch_refused', 'completed', None),
    ('ORDER_STATUS_READ', 1, 'applied_non_final', 'pending', None),
    ('ORDER_CAPTURE_RESPONSE', 1, 'applied', 'completed', None),
])
def test_verification_uses_only_accepted_evidence(event_type, verified, disposition, status, method):
    w, aid, key, p = payment()
    rid = p['request_id']
    with w.risk.store._tx() as c:
        c.execute('INSERT INTO provider_events (provider,event_id,event_type,provider_order_id,'
                  'status,verified,raw_sha256,received_at,processed_at,disposition,agent_id,request_id) '
                  'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                  ('paypal', 'event1', event_type, 'ORDER1', status, verified, 'do-not-expose',
                   w.clock().isoformat(), w.clock().isoformat(), disposition, aid, rid))
    owner = get(w, rid).json()
    assert owner['verification'] == ({'verified_by': method, 'at': w.clock().isoformat()} if method else None)
    events = get(w, rid, suffix='/timeline').json()['events']
    assert [e['verified_by'] for e in events if e['verified_by']] == ([method] if method else [])
    text = json.dumps(events)
    assert key not in text and OWNER_A not in text and 'do-not-expose' not in text
    assert get(w, rid, key).json()['owner_decision'] is None


def test_checks_expiry_and_refunds_use_recorded_rows():
    w, aid, _, p = payment()
    rid = p['request_id']
    stamp = w.clock().isoformat()
    with w.wallet_store.tx() as c:
        c.execute('UPDATE platform_page_checks SET payload_json=?,updated_at=? WHERE request_id=?',
                  ('{"secret":"must not leak"}', stamp, rid))
        c.execute('INSERT INTO platform_market_checks (request_id,agent_id,payload_json,created_at,updated_at) '
                  'VALUES (?,?,?,?,?)', (rid, aid, '{"secret":"must not leak"}', stamp, stamp))
    w.clock.advance(days=2)
    w.risk.store.expire_holds(w.clock())
    events = get(w, rid, suffix='/timeline').json()['events']
    assert {'page_check', 'market_check', 'expired'} <= {e['kind'] for e in events}
    assert 'must not leak' not in json.dumps(events)


def test_payout_timeline_owner_scope_and_agent_redaction(world):  # noqa: F811
    w, _ = world
    p, path = approved(world)
    c, deps, owner, _, key, *_ = w
    assert c.get(path + '/timeline', headers=bearer(key)).status_code == 403
    deps.owner_authenticator.create_owner('other')
    other, _ = deps.owner_authenticator.issue_token('other')
    assert c.get(path + '/timeline', headers=bearer(other)).status_code == 404
    assert c.get(path + '/timeline', headers=bearer(other)).json() == c.get(path, headers=bearer(other)).json()
    assert c.post(path + '/execute', headers=bearer(key)).json()['owner_decision'] is None
    owner_view = c.get(path, headers=bearer(owner)).json()
    assert owner_view['owner_decision']['decision'] == 'approved'
    assert owner_view['verification']['verified_by'] == 'paypal_read'
    assert c.post(path + '/reconciliation', headers=bearer(key)).json()['owner_decision'] is None
    events = c.get(path + '/timeline', headers=bearer(owner)).json()['events']
    assert {'created', 'owner_decision', 'payout', 'provider_verification'} <= {e['kind'] for e in events}
    assert p['terms_sha256'][:12] in next(e['detail'] for e in events if e['kind'] == 'owner_decision')
    assert [datetime.fromisoformat(e['at']) for e in events] == sorted(datetime.fromisoformat(e['at']) for e in events)


def test_agent_replay_cannot_gain_owner_fields_after_decision():
    w = build_world()
    _, key, _ = w.funded_agent(caps=ALL_PAY)
    p = w.pay(key, idem='same-request-123').json()
    assert w.client.post(f"/v1/payment-requests/{p['request_id']}/approve", headers=w.h(OWNER_A),
                         json={'terms_sha256': p['terms_sha256']}).status_code == 200
    replay = w.pay(key, idem='same-request-123')
    assert replay.status_code == 200
    assert replay.json()['owner_decision'] is None
    assert replay.json()['agent_context'] is None
    assert replay.json()['policy_block'] is None
    assert replay.json()['risk_signals'] == []


def test_unsubmitted_execution_does_not_invent_provider_order():
    w, aid, _, p = payment()
    rid = p['request_id']
    with w.risk.store._tx() as c:
        c.execute('INSERT INTO payment_attempts (agent_id,request_id,provider,provider_reference,at) '
                  'VALUES (?,?,?,?,?)', (aid, rid, 'paypal', 'private-idempotency-key', w.clock().isoformat()))
    events = get(w, rid, suffix='/timeline').json()['events']
    assert not any(e['kind'] == 'order_created' for e in events)
    assert 'private-idempotency-key' not in json.dumps(events)
