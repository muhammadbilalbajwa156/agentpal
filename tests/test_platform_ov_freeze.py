"""Emergency stop uses the existing owner-scoped, audited wallet freeze boundary."""
from test_platform_fixtures import ALL_PAY, OWNER_A, OWNER_B, build_world


def test_freeze_all_refuses_an_agent_key_without_mutation():
    w = build_world()
    _, key, wallet = w.funded_agent(caps=ALL_PAY)
    response = w.client.post('/v1/wallets/freeze-all', headers=w.h(key))
    assert response.status_code == 403
    assert w.client.get(f"/v1/wallets/{wallet['wallet_id']}", headers=w.h(OWNER_A)).json()[
        'status'] == 'active'


def test_freeze_all_is_owner_scoped_audited_and_idempotent():
    w = build_world()
    _, _, first = w.funded_agent(caps=ALL_PAY)
    _, _, second = w.funded_agent(caps=ALL_PAY)
    _, _, other = w.funded_agent(owner=OWNER_B, caps=ALL_PAY)
    response = w.client.post('/v1/wallets/freeze-all', headers=w.h(OWNER_A))
    assert response.status_code == 200
    assert {r['wallet_id'] for r in response.json()['wallets']} == {
        first['wallet_id'], second['wallet_id']}
    assert all(r['status'] == 'frozen' for r in response.json()['wallets'])
    w.clock.advance(seconds=1)
    replay = w.client.post('/v1/wallets/freeze-all', headers=w.h(OWNER_A))
    assert replay.status_code == 200 and replay.json() == response.json()
    for wallet in (first, second):
        events = w.platform.wallets.events(wallet['wallet_id'])
        assert [e['action'] for e in events] == ['create', 'freeze']
        assert events[-1]['actor'].startswith('owner_alice|session:')
    untouched = w.client.get(f"/v1/wallets/{other['wallet_id']}", headers=w.h(OWNER_B)).json()
    assert untouched['status'] == 'active'
    assert [e['action'] for e in w.platform.wallets.events(other['wallet_id'])] == ['create']


def test_freeze_all_blocks_new_requests_and_approved_execution_via_existing_gates():
    w = build_world()
    aid, key, _ = w.funded_agent(caps=ALL_PAY)
    payment = w.pay(key).json()
    path = f"/v1/payment-requests/{payment['request_id']}"
    assert w.client.post(path + '/approve', headers=w.h(OWNER_A),
                         json={'terms_sha256': payment['terms_sha256']}).status_code == 200
    assert w.client.post('/v1/wallets/freeze-all', headers=w.h(OWNER_A)).status_code == 200
    refused = w.pay(key)
    assert refused.status_code == 409 and refused.json() == {'error': 'wallet_frozen'}
    assert w.risk.spend_policy.for_agent(aid) is None
    execution = w.client.post(path + '/execute', headers=w.h(key))
    assert execution.status_code == 409 and execution.json() == {'error': 'wallet_frozen'}
    assert w.lifecycle.begun == []


def test_freeze_all_requires_authentication_and_handles_empty_workspace():
    w = build_world()
    assert w.client.post('/v1/wallets/freeze-all').status_code == 401
    response = w.client.post('/v1/wallets/freeze-all', headers=w.h(OWNER_A))
    assert response.status_code == 200
    assert response.json() == {'wallets': [], 'unit': 'virtual_allowance', 'is_real_money': False}
