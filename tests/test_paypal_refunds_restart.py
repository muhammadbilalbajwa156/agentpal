"""Restart and gross-evidence boundaries; real durable stores and MockTransport PayPal."""
from contextlib import contextmanager
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from agentpal.api import create_app
from agentpal.auth import TokenRegistry
from agentpal.config import Settings
from agentpal.lifecycle.recovery import ConsistencyError, check_consistency
from agentpal.platform.refunds import RefundDesk
from agentpal.platform.wiring import build_platform
from agentpal.providers.config import PayPalSandboxConfig
from agentpal.providers.paypal_sandbox import PayPalSandboxProvider

from test_paypal_refunds import CAPTURE, posts, world as world


def credit(w):
    result = w.refund()
    assert result.status_code == 201
    assert result.json()['status'] == 'COMPLETED'
    return w.deps.wallet_store.query('SELECT * FROM platform_paypal_refunds')[0]


@contextmanager
def reopened(w):
    """Reopen all durable DBs through the real startup factory; old connections quiesce."""
    # Same operator settings as the original process: the shared SQLite rate limiter
    # (correctly) refuses workers whose limits differ (lead fix 2026-11-03).
    settings = Settings(db_path=w.deps.risk_service.store.path, auth_mode='required',
                        analyzer_chain=['heuristic'], allowed_hosts=['127.0.0.1'],
                        rate_limit_per_token_per_minute=1000, rate_limit_per_peer_per_minute=1000)
    deps = build_platform(settings=settings, environ={})
    try:
        deps.provider = PayPalSandboxProvider(
            PayPalSandboxConfig(client_id='fake-client', client_secret='fake-secret'),
            http_client=httpx.Client(transport=httpx.MockTransport(w.backend.handler)))
        deps.lifecycle.provider = deps.provider
        app = create_app(service=deps.risk_service, settings=settings,
                         registry=TokenRegistry(), platform=deps)
        with TestClient(app, base_url='http://127.0.0.1') as client:
            yield deps, client
    finally:
        deps.close()


def terms_issue(w, *, wallet=True):
    issues = check_consistency(w.deps.risk_service.store, w.deps.wallet_store if wallet else None)
    return any(i.kind == 'ledger_terms_differ' for i in issues)


def refused_restart(w):
    assert terms_issue(w)
    with pytest.raises(ConsistencyError) as error:
        with reopened(w):
            pytest.fail('corrupt net ledger passed the startup factory')
    assert any(i.kind == 'ledger_terms_differ' for i in error.value.report.issues)


def test_partial_credit_restart_preserves_net_spend_and_gross_terms(world):
    w = world
    credit(w)
    assert not terms_issue(w)
    with reopened(w) as (deps, client):
        response = client.get('/v1/wallets/' + w.wallet['wallet_id'], headers=w.headers)
        assert response.status_code == 200
        assert Decimal(response.json()['allowance']['spent']) == Decimal('0.60')
        assert client.get(w.path, headers=w.headers).json()['status'] == 'provider_confirmed'
        request = deps.risk_service.store.query('SELECT amount FROM requests')[0]
        assert Decimal(request['amount']) == Decimal('1.00')
    assert len(posts(w)) == 1


def test_multiple_distinct_partial_credits_restart(world):
    w = world
    credit(w)
    assert w.refund('0.20', 'second-partial-key').json()['status'] == 'COMPLETED'
    with reopened(w) as (deps, _):
        assert check_consistency(deps.risk_service.store, deps.wallet_store) == []
        assert Decimal(deps.risk_service.store.ledger_entry(w.payment['request_id'])['amount']) == Decimal('0.40')


@pytest.mark.parametrize('corruption', [
    'missing_marker', 'wrong_actor', 'actor_prefix', 'bad_reason', 'reason_suffix',
    'short_id', 'long_id', 'upper_id', 'negative_credit', 'zero_credit', 'exponent_credit',
    'oversized_credit', 'oversized_integer', 'duplicate', 'sum_mismatch', 'missing_claim', 'claim_agent',
    'claim_request', 'claim_owner', 'claim_amount', 'claim_currency', 'claim_nan',
    'claim_failed', 'claim_cancelled', 'zero_net', 'negative_net', 'ledger_currency',
    'unapplied_marker', 'unapplied_bad_reason', 'unapplied_wrong_actor',
])
def test_missing_forged_duplicate_or_inconsistent_credit_refuses_restart(world, corruption):
    w = world
    row = credit(w)
    store = w.deps.risk_service.store
    with store._tx() as c:
        if corruption == 'missing_marker':
            c.execute("DELETE FROM ledger_events WHERE actor='verified:paypal_refund'")
        elif corruption in ('wrong_actor', 'actor_prefix'):
            c.execute("UPDATE ledger_events SET actor=? WHERE actor='verified:paypal_refund'",
                      ('agent' if corruption == 'wrong_actor' else 'verified:paypal_refund:forged',))
        elif corruption in ('bad_reason', 'reason_suffix', 'unapplied_bad_reason'):
            reason = 'not-a-refund' if corruption in ('bad_reason', 'unapplied_bad_reason') else 'paypal_refund_credit:' + row['refund_id'] + ':0.40:forged'
            c.execute("UPDATE ledger_events SET reason=? WHERE actor='verified:paypal_refund'", (reason,))
        elif corruption in ('short_id', 'long_id', 'upper_id'):
            name = {'short_id': 'rfd-' + 'a'*31, 'long_id': 'rfd-' + 'a'*33, 'upper_id': 'rfd-' + 'A'*32}[corruption]
            c.execute("UPDATE ledger_events SET reason=? WHERE actor='verified:paypal_refund'",
                      ('paypal_refund_credit:' + name + ':0.40',))
        elif corruption in ('negative_credit', 'zero_credit', 'exponent_credit', 'oversized_credit', 'oversized_integer'):
            amount = {'negative_credit': '-0.40', 'zero_credit': '0', 'exponent_credit': '4e-1',
                      'oversized_credit': '0.' + '0'*9 + '4', 'oversized_integer': '00000000000.40'}[corruption]
            c.execute("UPDATE ledger_events SET reason=? WHERE actor='verified:paypal_refund'",
                      ('paypal_refund_credit:' + row['refund_id'] + ':' + amount,))
            if corruption == 'oversized_credit':
                c.execute('UPDATE ledger_entries SET amount=?', (str(Decimal('1.00') - Decimal(amount)),))
            if corruption == 'zero_credit':
                c.execute("UPDATE ledger_entries SET amount='1.00'")
        elif corruption == 'duplicate':
            c.execute('INSERT INTO ledger_events (agent_id,request_id,from_state,to_state,actor,reason,at) '
                      "SELECT agent_id,request_id,from_state,to_state,actor,reason,at FROM ledger_events "
                      "WHERE actor='verified:paypal_refund'")
            c.execute("UPDATE ledger_entries SET amount='0.20'")
        elif corruption in ('sum_mismatch', 'zero_net', 'negative_net', 'unapplied_marker'):
            amount = {'sum_mismatch': '0.50', 'zero_net': '0', 'negative_net': '-0.20',
                      'unapplied_marker': '1.00'}[corruption]
            c.execute('UPDATE ledger_entries SET amount=?', (amount,))
            if corruption in ('zero_net', 'negative_net'):
                total = '1.00' if corruption == 'zero_net' else '1.20'
                c.execute("UPDATE ledger_events SET reason=? WHERE actor='verified:paypal_refund'",
                          ('paypal_refund_credit:' + row['refund_id'] + ':' + total,))
        elif corruption == 'unapplied_wrong_actor':
            c.execute("UPDATE ledger_events SET actor='agent' WHERE actor='verified:paypal_refund'")
        if corruption in ('unapplied_wrong_actor', 'unapplied_bad_reason'):
            c.execute("UPDATE ledger_entries SET amount='1.00'")
        if corruption == 'ledger_currency':
            c.execute("UPDATE ledger_entries SET currency='EUR'")
    with w.deps.wallet_store.tx() as c:
        if corruption == 'missing_claim':
            c.execute('DELETE FROM platform_paypal_refunds')
        elif corruption.startswith('claim_'):
            column, value = {
                'claim_agent': ('agent_id', 'other-agent'), 'claim_request': ('request_id', 'other-request'),
                'claim_owner': ('owner_id', 'other-owner'), 'claim_amount': ('amount', '0.41'),
                'claim_currency': ('currency', 'EUR'), 'claim_nan': ('amount', 'NaN'),
                'claim_failed': ('status', 'FAILED'), 'claim_cancelled': ('status', 'CANCELLED'),
            }[corruption]
            c.execute('UPDATE platform_paypal_refunds SET ' + column + '=?', (value,))
        elif corruption in ('negative_credit', 'zero_credit', 'zero_net', 'negative_net', 'oversized_credit'):
            amount = {'negative_credit': '-0.40', 'zero_credit': '0', 'zero_net': '1.00', 'negative_net': '1.20', 'oversized_credit': '0.' + '0'*9 + '4'}[corruption]
            c.execute('UPDATE platform_paypal_refunds SET amount=?', (amount,))
        elif corruption in ('short_id', 'long_id', 'upper_id'):
            name = {'short_id': 'rfd-' + 'a'*31, 'long_id': 'rfd-' + 'a'*33, 'upper_id': 'rfd-' + 'A'*32}[corruption]
            c.execute('UPDATE platform_paypal_refunds SET refund_id=?', (name,))
    refused_restart(w)


@pytest.mark.parametrize('state', ['reserved', 'approved', 'pending', 'reconciliation_required',
                                  'released', 'failed', 'cancelled', 'reversed'])
def test_refund_net_exception_is_completed_only(world, state):
    w = world
    credit(w)
    with w.deps.risk_service.store._tx() as c:
        c.execute('UPDATE ledger_entries SET state=?', (state,))
    refused_restart(w)


def test_partial_credit_needs_the_local_wallet_claim_store(world):
    credit(world)
    assert terms_issue(world, wallet=False)


@pytest.mark.parametrize('net', ['0.899', '0.900'])
def test_refund_credit_count_is_bounded(world, net):
    w = world
    row = credit(w)
    with w.deps.risk_service.store._tx() as c:
        c.execute("DELETE FROM ledger_events WHERE actor='verified:paypal_refund'")
        c.execute('UPDATE ledger_entries SET amount=?', (net,))
        for i in range(101):
            c.execute('INSERT INTO ledger_events (agent_id,request_id,from_state,to_state,actor,reason,at) '
                      "VALUES (?,?,'completed','completed','verified:paypal_refund',?,?)",
                      (row['agent_id'], row['request_id'], 'paypal_refund_credit:rfd-' + format(i, '032x') + ':0.001', row['created_at']))
    with w.deps.wallet_store.tx() as c:
        c.execute('DELETE FROM platform_paypal_refunds')
        columns = list(row)
        for i in range(101):
            values = {**row, 'refund_id': 'rfd-' + format(i, '032x'), 'amount': '0.001',
                      'key_sha256': format(i, '064x'), 'provider_key': 'key-' + str(i),
                      'provider_refund_id': 'REFUND-' + str(i)}
            c.execute('INSERT INTO platform_paypal_refunds (' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')',
                      tuple(values[k] for k in columns))
    refused_restart(w)


@pytest.mark.parametrize('prior_status', ['reconciliation_required', 'PENDING'])
def test_restart_after_ledger_commit_wallet_rollback_recovers_without_double_credit(world, monkeypatch, prior_status):
    w = world
    original_audit = RefundDesk._audit
    if prior_status == 'PENDING':
        w.backend.mode = 'pending'
        assert w.refund().json()['status'] == 'PENDING'
        w.backend.refunds[0]['status'] = 'COMPLETED'
        w.backend.mode = None
    def fail_wallet_commit(self, c, rid, action, actor):
        if action == 'verified_completed':
            raise RuntimeError('injected wallet rollback after ledger commit')
        return original_audit(self, c, rid, action, actor)
    with monkeypatch.context() as mp:
        mp.setattr(RefundDesk, '_audit', fail_wallet_commit)
        if prior_status == 'PENDING':
            with pytest.raises(RuntimeError, match='injected wallet rollback'):
                # Direct apply isolates the crash; reconcile intentionally catches read faults.
                desk = RefundDesk(w.app.state.platform)
                row = w.deps.wallet_store.query('SELECT * FROM platform_paypal_refunds')[0]
                desk._apply(row['refund_id'], w.backend.refunds[0], desk._provider(), Decimal('1.00'))
        else:
            with pytest.raises(RuntimeError, match='injected wallet rollback'):
                w.refund()
    assert w.deps.wallet_store.query('SELECT status FROM platform_paypal_refunds')[0]['status'] == prior_status
    assert not terms_issue(w)
    with reopened(w) as (deps, client):
        for _ in range(2):
            response = client.post(w.path + '/refunds/reconciliation', headers=w.headers)
            assert response.status_code == 200
            assert Decimal(response.json()['refunded_total']) == Decimal('0.40')
        assert Decimal(deps.risk_service.store.ledger_entry(w.payment['request_id'])['amount']) == Decimal('0.60')
        assert check_consistency(deps.risk_service.store, deps.wallet_store) == []
    assert len(posts(w)) == 1


def test_gross_capture_read_after_partial_credit_preserves_net(world):
    w = world
    credit(w)
    # A numerically equal representation creates a fresh provider observation ID.
    w.backend.amount = {'value': '1.0', 'currency_code': 'USD'}
    response = w.client.post(w.path + '/reconciliation', headers=w.headers)
    assert response.status_code == 200
    assert response.json()['status'] == 'provider_confirmed'
    rows = w.deps.risk_service.store.provider_events(w.payment['request_id'])
    assert any(r['event_type'] == 'ORDER_STATUS_READ' and r['disposition'] == 'out_of_order_ignored' for r in rows)
    assert Decimal(w.deps.risk_service.store.ledger_entry(w.payment['request_id'])['amount']) == Decimal('0.60')


def test_net_sized_provider_capture_is_refused_after_partial_credit(world):
    w = world
    credit(w)
    w.backend.amount = {'value': '0.60', 'currency_code': 'USD'}
    response = w.client.post(w.path + '/reconciliation', headers=w.headers)
    assert response.status_code == 200
    rows = w.deps.risk_service.store.provider_events(w.payment['request_id'])
    # Lead integration 2026-11-03: with P1's payee/amount binding merged, the net-sized read is
    # refused one layer earlier (unknown_reconciliation) instead of by _apply_event
    # (mismatch_refused). Either way it is never applied and the net ledger is untouched.
    reads = [r for r in rows if r['event_type'] == 'ORDER_STATUS_READ']
    assert reads and all(r['disposition'] in ('mismatch_refused', 'unknown_reconciliation') for r in reads)
    assert not any(r['disposition'] == 'applied' and r['amount'] == '0.60' for r in rows)
    assert Decimal(w.deps.risk_service.store.ledger_entry(w.payment['request_id'])['amount']) == Decimal('0.60')


@pytest.mark.parametrize('verified,amount,currency,reference,disposition', [
    (False, '1.00', 'USD', CAPTURE, 'unverified_recorded'),
    (True, '1.00', 'EUR', CAPTURE, 'mismatch_refused'),
    (True, '1.00', 'USD', 'WRONG-CAPTURE', 'conflict_reconciliation'),
    (True, None, 'USD', CAPTURE, 'mismatch_refused'),
    (True, '1.00', None, CAPTURE, 'mismatch_refused'),
])
def test_gross_comparison_keeps_existing_provider_gates(world, verified, amount, currency, reference, disposition):
    w = world
    credit(w)
    store = w.deps.risk_service.store
    order = store.provider_order(w.payment['request_id'])
    result, _ = store.apply_provider_event(provider='paypal', event_id='FOLLOWUP-EVENT', event_type='ORDER_STATUS_READ',
        provider_order_id=order['provider_order_id'], provider_capture_id=reference, status='completed',
        amount=None if amount is None else Decimal(amount), currency=currency, verified=verified,
        raw_sha256='a'*64, received_at=store.clock())
    assert result == disposition
    assert Decimal(store.ledger_entry(w.payment['request_id'])['amount']) == Decimal('0.60')


def test_ordinary_gross_entry_ignores_unrelated_audit_reason(world):
    w = world
    with w.deps.risk_service.store._tx() as c:
        c.execute('INSERT INTO ledger_events (agent_id,request_id,from_state,to_state,actor,reason,at) '
                  "VALUES (?,?,'completed','completed','system','paypalXrefundXcredit:advisory',?)",
                  (w.payment['agent_id'], w.payment['request_id'], w.deps.risk_service.clock().isoformat()))
    with reopened(w) as (deps, _):
        assert check_consistency(deps.risk_service.store, deps.wallet_store) == []


@pytest.mark.parametrize('collision', ['agent', 'request'])
def test_immutable_gross_join_is_scoped_to_both_request_keys(world, collision):
    w = world
    credit(w)
    store = w.deps.risk_service.store
    original = store.query('SELECT * FROM requests')[0]
    other = {**original, 'amount': '0.60'}
    if collision == 'agent':
        other['agent_id'] = 'aaa_other_agent'
    else:
        other['request_id'] = 'aaa_other_request'
    with store._tx() as c:
        columns = list(other)
        c.execute('INSERT INTO requests (rowid,' + ','.join(columns) + ') VALUES (-1,' + ','.join('?' for _ in columns) + ')',
                  tuple(other[k] for k in columns))
    order = store.provider_order(w.payment['request_id'], agent_id=w.payment['agent_id'])
    disposition, _ = store.apply_provider_event(provider='paypal', event_id='FRESH-GROSS-OBSERVATION',
        event_type='ORDER_STATUS_READ', provider_order_id=order['provider_order_id'], provider_capture_id=CAPTURE,
        status='completed', amount=Decimal('1.00'), currency='USD', verified=True, raw_sha256='b'*64,
        received_at=store.clock())
    assert disposition == 'out_of_order_ignored'
    assert Decimal(store.ledger_entry(w.payment['request_id'], agent_id=w.payment['agent_id'])['amount']) == Decimal('0.60')


def test_distinct_refund_markers_sum_exactly_in_the_consistency_check(world):
    w = world
    credit(w)
    assert w.refund('0.20', 'second-exact-credit').json()['status'] == 'COMPLETED'
    assert check_consistency(w.deps.risk_service.store, w.deps.wallet_store) == []


def test_explicit_wallet_restart_recovery_is_clean(world):
    from agentpal.lifecycle.recovery import startup_recovery
    from agentpal.store import AssessmentStore
    from agentpal.wallet.store import WalletStore
    w = world
    credit(w)
    store = AssessmentStore(w.deps.risk_service.store.path, strict_execution=True)
    wallet = WalletStore(w.deps.wallet_store.path)
    try:
        assert startup_recovery(store, wallet).clean
        assert Decimal(store.ledger_entry(w.payment['request_id'])['amount']) == Decimal('0.60')
    finally:
        wallet.close()
        store.close()


@pytest.mark.parametrize('path,method', [
    ('/v1/refunds', 'GET'),
    ('/v1/payment-requests/{request_id}/refunds', 'DELETE'),
    ('/v1/payment-requests/{request_id}/refunds/reconciliation', 'GET'),
    ('/v1/payment-requests/{request_id}/refunds/anything', 'POST'),
    ('/v1/payment-requests/{request_id}/capture', 'GET'),
])
def test_route_invariant_rejects_unapproved_refund_variants(world, monkeypatch, path, method):
    import test_api_v1_invariants as invariant
    world.app.add_api_route(path, lambda: None, methods=[method])
    monkeypatch.setattr(invariant, 'build_world', lambda: world)
    with pytest.raises(AssertionError) as failure:
        invariant.test_no_route_completes_captures_or_records_outcomes()
    assert str(failure.value).splitlines()[0] == path


@pytest.mark.parametrize('collision', ['agent', 'request'])
def test_credit_marker_query_is_scoped_to_both_request_keys(world, collision):
    w = world
    row = credit(w)
    agent = 'other-agent' if collision == 'agent' else row['agent_id']
    request = 'other-request' if collision == 'request' else row['request_id']
    with w.deps.risk_service.store._tx() as c:
        c.execute('INSERT INTO ledger_events (agent_id,request_id,from_state,to_state,actor,reason,at) '
                  "VALUES (?,?,'completed','completed','verified:paypal_refund','invalid-unrelated-marker',?)",
                  (agent, request, row['created_at']))
    assert check_consistency(w.deps.risk_service.store, w.deps.wallet_store) == []
