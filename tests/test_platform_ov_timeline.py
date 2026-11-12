"""Timeline presentation preserves evidence while ordering and grouping observations."""
from datetime import datetime, timedelta

from agentpal.platform.timeline import RequestHistory
from test_platform_fixtures import ALL_PAY, OWNER_A, build_world


def test_consecutive_observations_are_collapsed_without_changing_stored_events():
    w = build_world()
    aid, key, _ = w.funded_agent(caps=ALL_PAY)
    payment = w.pay(key).json()
    rid = payment['request_id']
    with w.risk.store._tx() as c:
        for i in (3, 1, 4, 2):  # deliberately not insertion order
            at = (w.clock() + timedelta(seconds=i)).isoformat()
            c.execute('INSERT INTO provider_events (provider,event_id,event_type,provider_order_id,'
                      'status,verified,raw_sha256,received_at,processed_at,disposition,agent_id,request_id) '
                      'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                      ('paypal', f'observation{i}', 'ORDER_STATUS_READ', 'ORDER1', 'pending',
                       0, 'private-evidence', at, at, 'unverified_recorded', aid, rid))
    response = w.client.get(f'/v1/payment-requests/{rid}/timeline', headers=w.h(OWNER_A))
    assert response.status_code == 200
    events = response.json()['events']
    observations = [e for e in events if e['kind'] == 'provider_observation']
    assert len(observations) == 1
    assert observations[0]['label'] == 'A PayPal outcome observation was recorded. ×4'
    assert observations[0]['at'] == (w.clock() + timedelta(seconds=1)).isoformat()
    assert observations[0]['verified_by'] is None
    assert 'private-evidence' not in response.text
    assert len(w.risk.store.query('SELECT * FROM provider_events')) == 4
    assert [datetime.fromisoformat(e['at']) for e in events] == sorted(
        datetime.fromisoformat(e['at']) for e in events)


class History(RequestHistory):
    """Source order must not determine display order for equal timestamps."""
    def __init__(self, decisions):
        self.row = {'created_at': '2026-11-01T00:00:00Z', 'terms_sha256': 'a' * 64}
        self.ids = ('agent', 'request')
        self.recorded_decisions = decisions
        self.wallet_store = self

    def assessment(self):
        return None

    def rows(self, table, columns='*'):
        return self.recorded_decisions if table == 'owner_decisions' else []

    def query(self, *_):
        return []

    def proofs(self):
        return []


def test_equal_time_events_have_an_explicit_stable_tie_break():
    decisions = [{'at': '2026-11-01T00:00:00Z', 'decision': decision, 'id': i}
                 for i, decision in enumerate(('rejected', 'approved'))]
    first = History(decisions).timeline()['events']
    second = History(list(reversed(decisions))).timeline()['events']
    assert first == second
    assert first[0]['kind'] == 'created'


def test_nonconsecutive_observations_remain_separate():
    history = History([])
    original = history.rows
    history.rows = lambda table, columns='*': {
        'provider_events': [
            {'provider': 'paypal', 'processed_at': '2026-11-01T00:00:01Z'},
            {'provider': 'paypal', 'processed_at': '2026-11-01T00:00:03Z'},
        ],
        'payment_attempts': [{'at': '2026-11-01T00:00:02Z'}],
    }.get(table, original(table, columns))
    observations = [e for e in history.timeline()['events'] if e['kind'] == 'provider_observation']
    assert len(observations) == 2
    assert all(e['label'] == 'A PayPal outcome observation was recorded.' for e in observations)
