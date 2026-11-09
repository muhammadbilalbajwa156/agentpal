"""P1 boundaries; real control services and realistic Orders v2 MockTransport."""
import copy
import json
from datetime import timedelta

import httpx
import pytest
from pydantic import ValidationError

from agentpal.infra_contracts import PaymentLifecycle as L, ProviderError
from agentpal.platform.models import PaymentCreate
from agentpal.providers.config import load_config_from_env
from agentpal.lifecycle import PaymentLifecycleService
from test_provider_paypal_support import make_terms, make_provider, json_response
from test_infra_integration_platform import world, bearer, approve, execute  # noqa: F401

EMAIL = 'merchant@business.example.com'


def body():
    return dict(amount='1.00', currency='USD', merchant_name='Books', purpose='Reference',
                merchant_paypal_email=EMAIL)


def test_legacy_digest_is_byte_identical_to_unmodified_43b3d3a():
    assert make_terms().digest() == '091d2a65b0b43c7202c1b2ef02acb3a89f189ed1c011c877e826610f3fcfbc4f'


def test_payee_is_normalized_and_changes_approval_digest():
    terms = make_terms(merchant_paypal_email=EMAIL.upper())
    assert terms.merchant_paypal_email == EMAIL
    assert terms.digest() != make_terms().digest()
    assert PaymentCreate(**body()).merchant_paypal_email == EMAIL


@pytest.mark.parametrize('email', [' a@b.com', 'a@b.com\n', 'a..b@b.com', '.a@b.com',
    'a@-b.com', 'a@b', 'a@b..com', 'a b@b.com', 'a@b.com,c@d.com', 'é@b.com',
    'a' * 65 + '@b.com', 'a@' + 'b' * 64 + '.com', 'a' * 64 + '@' + 'b' * 59 + '.com'])
def test_strict_payee_email(email):
    with pytest.raises(ValidationError):
        PaymentCreate(**{**body(), 'merchant_paypal_email': email})
    with pytest.raises(ValidationError):
        make_terms(merchant_paypal_email=email)


def test_order_body_carries_payee():
    provider, _ = make_provider()
    unit = provider.build_order_body(make_terms(merchant_paypal_email=EMAIL))['purchase_units'][0]
    assert unit['payee'] == {'email_address': EMAIL}


@pytest.fixture
def core_world(world):  # noqa: F811
    provider, fake = make_provider()
    provider._now = world[1].risk_service.clock
    world[1].provider = world[1].lifecycle.provider = provider
    observed = {}
    def create(request):
        unit = json.loads(request.content)['purchase_units'][0]
        unit['payee'] = {'email_address': unit.get('payee', {}).get('email_address', EMAIL),
                         'merchant_id': 'MERCHANT12345'}
        observed.update(id='ORDER-P1', status='CREATED', purchase_units=[unit])
        return httpx.Response(201, json=observed)
    fake.routes[('POST', '/v2/checkout/orders')] = create
    fake.routes[('GET', '/v2/checkout/orders/ORDER-P1')] = lambda r: httpx.Response(200, json=observed)
    def capture(request):
        observed['status'] = 'COMPLETED'
        observed['purchase_units'][0]['payments'] = {'captures': [{'id': 'CAPTURE-P1',
            'status': 'COMPLETED', 'amount': {'currency_code': 'USD', 'value': '1.00'}}]}
        return httpx.Response(201, json=observed)
    fake.routes[('POST', '/v2/checkout/orders/ORDER-P1/capture')] = capture
    return world, fake, observed


def pending(core_world, *, payee=True):
    w, _, observed = core_world
    data = body()
    if not payee:
        data.pop('merchant_paypal_email')
    response = w[0].post('/v1/payment-requests', headers={**bearer(w[4]),
        'Idempotency-Key': 'p1-core-create'}, json=data)
    assert response.status_code == 201, response.text
    payment = response.json()
    if payee:
        assert payment['merchant_paypal_email'] == EMAIL
    approve(w, payment)
    assert execute(w, payment).status_code == 200
    observed['status'] = 'APPROVED'
    return payment, '/v1/payment-requests/' + payment['request_id']


@pytest.mark.parametrize('change', ['email', 'merchant_id', 'missing_payee', 'custom_id',
    'reference_id', 'unit_amount', 'currency', 'multiple_units'])
@pytest.mark.parametrize('operation', ['capture', 'reconciliation'])
def test_mismatched_order_never_confirms(core_world, change, operation):
    w, fake, observed = core_world
    payment, path = pending(core_world)
    unit = observed['purchase_units'][0]
    if change == 'email':
        unit['payee']['email_address'] = 'other@example.com'
    elif change == 'merchant_id':
        unit['payee']['merchant_id'] = 'OTHERID12345'
    elif change == 'missing_payee':
        unit.pop('payee')
    elif change in ('custom_id', 'reference_id'):
        unit[change] = 'different-request'
    elif change == 'unit_amount':
        unit['amount']['value'] = '2.00'
    elif change == 'currency':
        unit['amount']['currency_code'] = 'EUR'
    else:
        observed['purchase_units'].append(copy.deepcopy(unit))
    if operation == 'reconciliation':
        observed['status'] = 'COMPLETED'
        unit['payments'] = {'captures': [{'id': 'CAPTURE-P1', 'status': 'COMPLETED',
                           'amount': {'currency_code': 'USD', 'value': '1.00'}}]}
    w[0].post(path + '/' + operation, headers=bearer(w[2]))
    assert w[1].risk_service.store.ledger_state(payment['request_id']).value == 'reconciliation_required'
    assert w[1].risk_service.store.query('SELECT * FROM payment_outcomes') == []
    assert not any(r.url.path.endswith('/capture') for r in fake.requests)


def test_default_payee_is_bound_and_cannot_change(core_world):
    w, _, observed = core_world
    payment, path = pending(core_world, payee=False)
    observed['purchase_units'][0]['payee']['email_address'] = 'other@example.com'
    w[0].post(path + '/capture', headers=bearer(w[2]))
    assert w[1].risk_service.store.ledger_state(payment['request_id']).value == 'reconciliation_required'


def test_exact_payee_capture_settles_once(core_world):
    w, fake, _ = core_world
    _, path = pending(core_world)
    response = w[0].post(path + '/capture', headers=bearer(w[2]))
    assert response.json()['status'] == 'provider_confirmed', response.text
    assert w[0].post(path + '/capture', headers=bearer(w[2])).status_code == 409
    assert len(w[1].risk_service.store.query('SELECT * FROM payment_outcomes')) == 1
    assert len([r for r in fake.requests if r.url.path.endswith('/capture')]) == 1


@pytest.mark.parametrize('issue', ['INSTRUMENT_DECLINED', 'PAYEE_ACCOUNT_INVALID',
    'PAYEE_ACCOUNT_RESTRICTED', 'PAYEE_ACCOUNT_LOCKED_OR_CLOSED', 'TRANSACTION_REFUSED',
    'MAX_NUMBER_OF_PAYMENT_ATTEMPTS_EXCEEDED'])
def test_definitive_capture_error_releases_hold_once(core_world, issue):
    w, fake, _ = core_world
    payment, path = pending(core_world)
    fake.routes[('POST', '/v2/checkout/orders/ORDER-P1/capture')] = json_response(422,
        {'name': 'UNPROCESSABLE_ENTITY', 'message': 'SECRET_RAW_MESSAGE',
         'debug_id': 'SECRET_DEBUG', 'details': [{'issue': issue}]})
    response = w[0].post(path + '/capture', headers=bearer(w[2]))
    assert response.json()['status'] == 'failed', response.text
    ledger = w[1].risk_service.store
    assert ledger.spend_breakdown(w[5], 'USD', ledger.clock())['committed'] == 0
    assert w[0].post(path + '/capture', headers=bearer(w[2])).status_code == 409
    assert len(ledger.query("SELECT * FROM payment_outcomes WHERE outcome='failed'")) == 1
    assert ledger.ledger_state(payment['request_id']).value == 'failed'


@pytest.mark.parametrize('issue', ['ORDER_ALREADY_CAPTURED', 'UNPROCESSABLE_ENTITY', 'UNRECOGNIZED'])
def test_ambiguous_capture_error_keeps_hold(core_world, issue):
    w, fake, _ = core_world
    _, path = pending(core_world)
    fake.routes[('POST', '/v2/checkout/orders/ORDER-P1/capture')] = json_response(422,
        {'name': 'UNPROCESSABLE_ENTITY', 'details': [{'issue': issue}]})
    assert w[0].post(path + '/capture', headers=bearer(w[2])).json()['status'] == 'reconciliation_required'
    ledger = w[1].risk_service.store
    assert ledger.spend_breakdown(w[5], 'USD', ledger.clock())['committed'] == 1


@pytest.mark.parametrize('issue', ['ORDER_NOT_APPROVED', 'PAYER_ACTION_REQUIRED'])
def test_buyer_not_approved_is_stable(core_world, issue):
    w, fake, _ = core_world
    _, path = pending(core_world)
    fake.routes[('POST', '/v2/checkout/orders/ORDER-P1/capture')] = json_response(422,
        {'name': 'UNPROCESSABLE_ENTITY', 'details': [{'issue': issue}]})
    response = w[0].post(path + '/capture', headers=bearer(w[2]))
    assert response.status_code == 409
    assert response.json() == {'error': issue.lower()}
    assert w[0].get(path, headers=bearer(w[2])).json()['status'] == 'provider_pending'


def test_unapproved_preflight_has_stable_code(core_world):
    w, _, observed = core_world
    _, path = pending(core_world)
    observed['status'] = 'CREATED'
    assert w[0].post(path + '/capture', headers=bearer(w[2])).json() == {'error': 'order_not_approved'}


def test_buyer_expiry_releases_once_and_refuses_late_capture(core_world):
    w, fake, observed = core_world
    payment, path = pending(core_world)
    observed['status'] = 'CREATED'
    service = w[1].lifecycle.lifecycle
    service.clock = lambda: w[1].risk_service.clock() + timedelta(hours=4)
    w[0].post(path + '/capture', headers=bearer(w[2]))
    assert service.status(payment['request_id'], agent_id=w[5]) == L.expired
    ledger = w[1].risk_service.store
    assert ledger.spend_breakdown(w[5], 'USD', ledger.clock())['committed'] == 0
    observed['status'] = 'APPROVED'
    w[0].post(path + '/capture', headers=bearer(w[2]))
    assert not any(r.url.path.endswith('/capture') for r in fake.requests)
    assert len([e for e in ledger.ledger_events(payment['request_id'])
                if e['reason'] == 'buyer_approval_expired']) == 1


def test_negative_hook_is_default_off():
    provider, fake = make_provider()
    with pytest.raises(ProviderError, match='negative_testing_disabled'):
        provider.capture_order('ORDER-P1', idempotency_key='capture-key-1',
                               mock_response='INSTRUMENT_DECLINED')
    assert fake.requests == []


def test_negative_hook_needs_exact_env_flag():
    cfg = load_config_from_env({'PAYPAL_SANDBOX_CLIENT_ID': 'fake',
        'PAYPAL_SANDBOX_CLIENT_SECRET': 'fake', 'PAYPAL_SANDBOX_NEGATIVE_TESTING': '1'})
    assert cfg.negative_testing is True


def test_negative_hook_sandbox_host_guard():
    provider, fake = make_provider(negative_testing=True)
    provider._base_url = 'https://api-m.paypal.com'
    with pytest.raises(ProviderError):
        provider.capture_order('ORDER-P1', idempotency_key='capture-key-1',
                               mock_response='INSTRUMENT_DECLINED')
    assert fake.requests == []


def test_negative_hook_rejects_arbitrary_payload():
    provider, fake = make_provider(negative_testing=True)
    with pytest.raises(ProviderError):
        provider.capture_order('ORDER-P1', idempotency_key='capture-key-1',
                               mock_response='INSTRUMENT_DECLINED\r\nInjected: true')
    assert fake.requests == []


def test_api_cannot_inject_negative_testing(core_world):
    w, _, _ = core_world
    response = w[0].post('/v1/payment-requests', headers={**bearer(w[4]),
        'Idempotency-Key': 'p1-inject-mock'}, json={**body(), 'mock_response': 'INSTRUMENT_DECLINED'})
    assert response.status_code == 422


def test_errors_never_reflect_paypal_text():
    provider, _ = make_provider()
    with pytest.raises(ProviderError) as caught:
        provider._interpret(httpx.Response(422, json={'name': 'SECRET_NAME',
            'debug_id': 'SECRET_DEBUG', 'details': [{'issue': 'SECRET_ISSUE'}]}),
            operation='capture_order', mutating=True)
    assert 'SECRET' not in str(caught.value)


def test_restart_retains_approved_payee(core_world):
    w, _, observed = core_world
    payment, _ = pending(core_world)
    service = PaymentLifecycleService(w[1].risk_service.store)
    observed['purchase_units'][0]['payee']['email_address'] = 'other@example.com'
    assert service.reconcile(w[5], payment['request_id'], w[1].provider) == L.reconciliation_required


@pytest.mark.parametrize('change', ['email', 'reference_id', 'custom_id', 'unit_amount',
                                     'capture_amount', 'capture_currency'])
def test_capture_representation_is_checked_again(core_world, change):
    w, fake, observed = core_world
    payment, path = pending(core_world)
    original = fake.routes[('POST', '/v2/checkout/orders/ORDER-P1/capture')]
    def changed(request):
        response = original(request)
        value = response.json()
        unit = value['purchase_units'][0]
        if change == 'email':
            unit['payee']['email_address'] = 'other@example.com'
        elif change in ('reference_id', 'custom_id'):
            unit[change] = 'other-request'
        elif change == 'unit_amount':
            unit['amount']['value'] = '2.00'
        elif change == 'capture_amount':
            unit['payments']['captures'][0]['amount']['value'] = '2.00'
        else:
            unit['payments']['captures'][0]['amount']['currency_code'] = 'EUR'
        return httpx.Response(201, json=value)
    fake.routes[('POST', '/v2/checkout/orders/ORDER-P1/capture')] = changed
    assert w[0].post(path + '/capture', headers=bearer(w[2])).json()['status'] == 'reconciliation_required'
    assert w[1].risk_service.store.query('SELECT * FROM payment_outcomes') == []
    assert w[1].risk_service.store.ledger_state(payment['request_id']).value == 'reconciliation_required'


@pytest.mark.parametrize('status,expected', [('COMPLETED', 'provider_confirmed'),
    ('PENDING', 'provider_pending'), ('DECLINED', 'failed'), ('FAILED', 'failed')])
def test_capture_status_mapping(core_world, status, expected):
    w, fake, observed = core_world
    _, path = pending(core_world)
    def capture(request):
        value = copy.deepcopy(observed)
        value['status'] = 'COMPLETED'
        value['purchase_units'][0]['payments'] = {'captures': [{'id': 'CAPTURE-P1',
            'status': status, 'amount': {'currency_code': 'USD', 'value': '1.00'}}]}
        return httpx.Response(201, json=value)
    fake.routes[('POST', '/v2/checkout/orders/ORDER-P1/capture')] = capture
    assert w[0].post(path + '/capture', headers=bearer(w[2])).json()['status'] == expected


def test_bound_order_webhook_without_payee_cannot_settle(core_world):
    from agentpal.infra_contracts import ProviderEvent
    w, fake, _ = core_world
    payment, _ = pending(core_world)
    fake.routes[('GET', '/v2/checkout/orders/ORDER-P1')] = json_response(503, {})
    event = ProviderEvent(provider='paypal', environment='sandbox', event_id='signed-webhook-1',
        event_type='PAYMENT.CAPTURE.COMPLETED', provider_order_id='ORDER-P1',
        provider_capture_id='CAPTURE-P1', amount='1.00', currency='USD', verified=True,
        status='completed', received_at=w[1].risk_service.clock(), raw_sha256='c' * 64)
    assert w[1].event_sink.apply_provider_event(event) == L.reconciliation_required
    assert w[1].risk_service.store.query('SELECT * FROM payment_outcomes') == []
    assert w[1].risk_service.store.ledger_state(payment['request_id']).value == 'reconciliation_required'


def test_matching_pinned_merchant_id_can_identify_payee(core_world):
    w, _, observed = core_world
    _, path = pending(core_world)
    observed['purchase_units'][0]['payee'].pop('email_address')
    assert w[0].post(path + '/capture', headers=bearer(w[2])).json()['status'] == 'provider_confirmed'


def test_prepared_payee_cannot_change_after_approval(core_world):
    from agentpal.lifecycle.paypal_core import PayPalCoreBinding
    w, _, _ = core_world
    payment, _ = pending(core_world)
    ledger = w[1].risk_service.store
    terms = w[1].lifecycle.lifecycle.paypal.row(w[5], payment['request_id'])['terms_json']
    from agentpal.spending import ApprovalTerms, ApprovalTermsMismatch
    altered = ApprovalTerms.model_validate_json(terms).model_copy(
        update={'merchant_paypal_email': 'other@example.com'})
    with pytest.raises(ApprovalTermsMismatch):
        PayPalCoreBinding(ledger).bind_prepared(altered, ledger.clock())


def test_lost_capture_response_is_recovered_only_by_exact_read(core_world):
    w, fake, observed = core_world
    payment, path = pending(core_world)
    original = fake.routes[('POST', '/v2/checkout/orders/ORDER-P1/capture')]
    def lost(request):
        original(request)
        raise httpx.ReadTimeout('injected outcome unknown', request=request)
    fake.routes[('POST', '/v2/checkout/orders/ORDER-P1/capture')] = lost
    assert w[0].post(path + '/capture', headers=bearer(w[2])).json()['status'] == 'reconciliation_required'
    assert w[0].post(path + '/capture', headers=bearer(w[2])).status_code == 409
    assert w[0].post(path + '/reconciliation', headers=bearer(w[2])).json()['status'] == 'provider_confirmed'
    assert len([r for r in fake.requests if r.url.path.endswith('/capture')]) == 1
    assert len(w[1].risk_service.store.query('SELECT * FROM payment_outcomes')) == 1
    assert w[1].risk_service.store.ledger_state(payment['request_id']).value == 'completed'


def test_negative_header_is_only_explicit_in_process(core_world):
    w, fake, _ = core_world
    _, path = pending(core_world)
    response = w[0].post(path + '/capture', headers={**bearer(w[2]),
        'PayPal-Mock-Response': '{"mock_application_codes":"INSTRUMENT_DECLINED"}'})
    assert response.json()['status'] == 'provider_confirmed'
    assert all('PayPal-Mock-Response' not in r.headers for r in fake.requests)


def test_negative_hook_sends_allowlisted_header():
    provider, fake = make_provider(negative_testing=True)
    fake.routes[('POST', '/v2/checkout/orders/ORDER-P1/capture')] = json_response(422,
        {'details': [{'issue': 'INSTRUMENT_DECLINED'}]})
    with pytest.raises(ProviderError):
        provider.capture_order('ORDER-P1', idempotency_key='capture-key-1',
                               mock_response='INSTRUMENT_DECLINED')
    request = fake.api_requests()[0]
    assert json.loads(request.headers['PayPal-Mock-Response']) == {
        'mock_application_codes': 'INSTRUMENT_DECLINED'}
    assert request.headers['Prefer'] == 'return=representation'


def test_negative_hook_rejects_changed_sandbox_base_path():
    provider, fake = make_provider(negative_testing=True)
    provider._base_url += '/unexpected'
    with pytest.raises(ProviderError):
        provider.capture_order('ORDER-P1', idempotency_key='capture-key-1',
                               mock_response='INSTRUMENT_DECLINED')
    assert fake.requests == []


@pytest.mark.parametrize('status,issues', [(500, ['INSTRUMENT_DECLINED']),
    (422, ['INSTRUMENT_DECLINED', 'ORDER_ALREADY_CAPTURED']),
    (422, ['INSTRUMENT_DECLINED', 'UNKNOWN_ISSUE'])])
def test_ambiguous_or_mixed_decline_does_not_release(core_world, status, issues):
    w, fake, _ = core_world
    _, path = pending(core_world)
    fake.routes[('POST', '/v2/checkout/orders/ORDER-P1/capture')] = json_response(status,
        {'details': [{'issue': issue} for issue in issues]})
    assert w[0].post(path + '/capture', headers=bearer(w[2])).json()['status'] == 'reconciliation_required'
    ledger = w[1].risk_service.store
    assert ledger.spend_breakdown(w[5], 'USD', ledger.clock())['committed'] == 1


def test_missing_create_payee_fails_closed(core_world):
    w, fake, _ = core_world
    original = fake.routes[('POST', '/v2/checkout/orders')]
    def no_payee(request):
        response = original(request).json()
        response['purchase_units'][0].pop('payee')
        return httpx.Response(201, json=response)
    fake.routes[('POST', '/v2/checkout/orders')] = no_payee
    response = w[0].post('/v1/payment-requests', headers={**bearer(w[4]),
        'Idempotency-Key': 'p1-core-no-payee'}, json=body())
    assert response.status_code == 201
    payment = response.json()
    approve(w, payment)
    assert execute(w, payment).json()['status'] == 'reconciliation_required'


def test_oversized_stream_is_bounded():
    provider, fake = make_provider()
    fake.routes[('GET', '/v2/checkout/orders/ORDER-P1')] = lambda r: httpx.Response(
        200, content=b' ' * (1024 * 1024 + 1))
    with pytest.raises(ProviderError, match='provider_response_too_large'):
        provider.get_order_status('ORDER-P1')


def test_binding_rejects_capture_amount_before_store_validation(core_world):
    w, _, _ = core_world
    payment, _ = pending(core_world)
    provider = w[1].provider
    proof = provider.get_order_status('ORDER-P1').model_copy(update={'amount': __import__('decimal').Decimal('2.00')})
    assert not w[1].lifecycle.lifecycle.paypal.matches(w[5], payment['request_id'], proof)


@pytest.mark.parametrize('status,issues', [(500, ['INSTRUMENT_DECLINED']),
    (422, ['INSTRUMENT_DECLINED', 'ORDER_ALREADY_CAPTURED']), (422, ['UNRECOGNIZED'])])
def test_ambiguous_error_is_explicitly_unknown(status, issues):
    provider, _ = make_provider()
    with pytest.raises(ProviderError) as caught:
        provider._interpret(httpx.Response(status, json={'details': [{'issue': x} for x in issues]}),
                            operation='capture_order', mutating=True)
    assert caught.value.outcome_unknown


def test_stream_stops_before_consuming_unbounded_body():
    provider, fake = make_provider()
    class Oversized(httpx.SyncByteStream):
        def __init__(self):
            self.consumed = 0
        def __iter__(self):
            for _ in range(1000):
                self.consumed += 16384
                yield b' ' * 16384
    stream = Oversized()
    fake.routes[('GET', '/v2/checkout/orders/ORDER-P1')] = lambda r: httpx.Response(200, stream=stream)
    with pytest.raises(ProviderError, match='provider_response_too_large'):
        provider.get_order_status('ORDER-P1')
    assert stream.consumed <= 1024 * 1024 + 16384


def test_expiry_maintenance_reads_before_releasing(core_world):
    w, _, observed = core_world
    payment, _ = pending(core_world)
    observed['status'] = 'CREATED'
    service = w[1].lifecycle.lifecycle
    future = service.clock() + timedelta(hours=4)
    service.expire_and_reconcile(future, provider=w[1].provider)
    assert service.status(payment['request_id'], agent_id=w[5]) == L.expired
    assert w[1].risk_service.store.spend_breakdown(w[5], 'USD', service.clock())['committed'] == 0


def test_unknown_read_during_expiry_does_not_release(core_world):
    w, fake, _ = core_world
    payment, _ = pending(core_world)
    def unavailable(request):
        raise httpx.ReadTimeout('injected', request=request)
    fake.routes[('GET', '/v2/checkout/orders/ORDER-P1')] = unavailable
    service = w[1].lifecycle.lifecycle
    service.expire_and_reconcile(service.clock() + timedelta(hours=4), provider=w[1].provider)
    assert w[1].risk_service.store.spend_breakdown(w[5], 'USD', service.clock())['committed'] == 1
    assert service.status(payment['request_id'], agent_id=w[5]) == L.provider_pending


def test_expiry_does_not_release_a_pending_capture(core_world):
    w, fake, observed = core_world
    _, path = pending(core_world)
    original = fake.routes[('POST', '/v2/checkout/orders/ORDER-P1/capture')]
    def pending_capture(request):
        value = original(request).json()
        value['purchase_units'][0]['payments']['captures'][0]['status'] = 'PENDING'
        observed.update(value)
        return httpx.Response(201, json=value)
    fake.routes[('POST', '/v2/checkout/orders/ORDER-P1/capture')] = pending_capture
    assert w[0].post(path + '/capture', headers=bearer(w[2])).json()['status'] == 'provider_pending'
    service = w[1].lifecycle.lifecycle
    service.expire_and_reconcile(service.clock() + timedelta(hours=4), provider=w[1].provider)
    assert w[1].risk_service.store.spend_breakdown(w[5], 'USD', service.clock())['committed'] == 1


def test_decline_error_code_is_owner_and_agent_visible(core_world):
    w, fake, _ = core_world
    _, path = pending(core_world)
    fake.routes[('POST', '/v2/checkout/orders/ORDER-P1/capture')] = json_response(422,
        {'details': [{'issue': 'INSTRUMENT_DECLINED'}]})
    assert w[0].post(path + '/capture', headers=bearer(w[2])).json()['status'] == 'failed'
    for key in (w[2], w[4]):
        assert w[0].get(path, headers=bearer(key)).json()['provider_error_code'] == 'instrument_declined'


def test_capture_deadline_is_rechecked_after_durable_claim(core_world, monkeypatch):
    w, fake, _ = core_world
    payment, path = pending(core_world)
    service = w[1].lifecycle.lifecycle
    original = service.store.claim_provider_operation
    def race(*args, **kwargs):
        result = original(*args, **kwargs)
        service.clock = lambda: w[1].risk_service.clock() + timedelta(hours=4)
        return result
    monkeypatch.setattr(service.store, 'claim_provider_operation', race)
    w[0].post(path + '/capture', headers=bearer(w[2]))
    assert service.status(payment['request_id'], agent_id=w[5]) == L.expired
    assert not any(r.url.path.endswith('/capture') for r in fake.requests)
    assert service.store.spend_breakdown(w[5], 'USD', service.store.clock())['committed'] == 0


@pytest.mark.parametrize('change', [{'verified': False}, {'provider_order_id': 'OTHER-ORDER'},
                                    {'provider': 'fake'}])
def test_payee_match_requires_authentic_exact_order(core_world, change):
    w, _, _ = core_world
    payment, _ = pending(core_world)
    proof = w[1].provider.get_order_status('ORDER-P1').model_copy(update=change)
    assert not w[1].lifecycle.lifecycle.paypal.matches(w[5], payment['request_id'], proof)


def test_decline_requires_a_real_capture_invocation(core_world):
    from agentpal.providers.paypal_sandbox import PayPalOperationError
    from agentpal.spending import InvalidLedgerTransition
    w, _, _ = core_world
    payment, _ = pending(core_world)
    ledger = w[1].risk_service.store
    error = PayPalOperationError('INSTRUMENT_DECLINED', outcome_unknown=False, raw_sha256='a' * 64)
    with pytest.raises(InvalidLedgerTransition):
        w[1].lifecycle.lifecycle.paypal.decline(w[5], payment['request_id'], error, ledger.clock())
    assert ledger.spend_breakdown(w[5], 'USD', ledger.clock())['committed'] == 1


def test_payee_cannot_first_bind_after_owner_approval(core_world):
    from agentpal.spending import ApprovalTerms, ApprovalTermsMismatch
    from agentpal.lifecycle.paypal_core import PayPalCoreBinding
    w, _, _ = core_world
    data = body()
    data.pop('merchant_paypal_email')
    response = w[0].post('/v1/payment-requests', headers={**bearer(w[4]),
        'Idempotency-Key': 'p1-late-binding'}, json=data)
    assert response.status_code == 201
    payment = response.json()
    approve(w, payment)
    terms = ApprovalTerms(**{**payment['terms'], 'merchant_paypal_email': EMAIL})  # payee now also in terms (E2E-F1)
    with pytest.raises(ApprovalTermsMismatch):
        PayPalCoreBinding(w[1].risk_service.store).bind_prepared(terms, w[1].risk_service.clock())
    assert w[0].get('/v1/payment-requests/' + payment['request_id'],
                    headers=bearer(w[2])).json()['terms_sha256'] == payment['terms_sha256']


def test_payee_changes_have_distinct_observation_fingerprints(core_world):
    w, _, observed = core_world
    pending(core_world)
    first = w[1].provider.get_order_status('ORDER-P1')
    observed['purchase_units'][0]['payee']['email_address'] = 'other@example.com'
    second = w[1].provider.get_order_status('ORDER-P1')
    assert first.event_id != second.event_id


def test_verified_webhook_can_obtain_missing_identity_by_authenticated_read(core_world):
    from agentpal.infra_contracts import ProviderEvent
    w, fake, _ = core_world
    payment, _ = pending(core_world)
    before = len(fake.requests)
    event = ProviderEvent(provider='paypal', environment='sandbox', event_id='signed-webhook-2',
        event_type='PAYMENT.CAPTURE.COMPLETED', provider_order_id='ORDER-P1',
        provider_capture_id='CAPTURE-P1', amount='1.00', currency='USD', verified=True,
        status='completed', received_at=w[1].risk_service.clock(), raw_sha256='c' * 64)
    assert w[1].event_sink.apply_provider_event(event) == L.provider_confirmed
    assert len(fake.requests) == before + 1
    assert len(w[1].risk_service.store.query('SELECT * FROM payment_outcomes')) == 1
    assert w[1].risk_service.store.ledger_state(payment['request_id']).value == 'completed'


def test_definitive_create_decline_records_stable_code(core_world):
    w, fake, _ = core_world
    fake.routes[('POST', '/v2/checkout/orders')] = json_response(422,
        {'name': 'UNPROCESSABLE_ENTITY', 'details': [{'issue': 'PAYEE_ACCOUNT_INVALID'}]})
    response = w[0].post('/v1/payment-requests', headers={**bearer(w[4]),
        'Idempotency-Key': 'p1-invalid-payee-create'}, json=body())
    assert response.status_code == 201
    payment = response.json()
    approve(w, payment)
    assert execute(w, payment).json()['status'] == 'failed'
    assert w[0].get('/v1/payment-requests/' + payment['request_id'],
        headers=bearer(w[2])).json()['provider_error_code'] == 'payee_account_invalid'
    ledger = w[1].risk_service.store
    assert ledger.spend_breakdown(w[5], 'USD', ledger.clock())['committed'] == 0


def test_preflight_payer_action_required_is_specific(core_world):
    w, _, observed = core_world
    _, path = pending(core_world)
    observed['status'] = 'PAYER_ACTION_REQUIRED'
    response = w[0].post(path + '/capture', headers=bearer(w[2]))
    assert response.json() == {'error': 'payer_action_required'}


@pytest.mark.parametrize('captures', [[None, {'id': 'CAPTURE-P1', 'status': 'COMPLETED',
    'amount': {'currency_code': 'USD', 'value': '1.00'}}],
    [{'status': 'COMPLETED', 'amount': {'currency_code': 'USD', 'value': '1.00'}}]])
def test_malformed_capture_identity_never_confirms(core_world, captures):
    w, fake, observed = core_world
    _, path = pending(core_world)
    def capture(request):
        value = copy.deepcopy(observed)
        value['status'] = 'COMPLETED'
        value['purchase_units'][0]['payments'] = {'captures': captures}
        return httpx.Response(201, json=value)
    fake.routes[('POST', '/v2/checkout/orders/ORDER-P1/capture')] = capture
    assert w[0].post(path + '/capture', headers=bearer(w[2])).json()['status'] == 'reconciliation_required'
    assert w[1].risk_service.store.query('SELECT * FROM payment_outcomes') == []


def test_create_response_with_a_different_payee_fails_closed(core_world):
    """Lead audit 2026-11-03: PayPal accepted the order but reports a payee other than the
    owner-approved one. That payee must never be pinned, and the payment never confirms."""
    w, fake, observed = core_world
    original = fake.routes[('POST', '/v2/checkout/orders')]
    def other_payee(request):
        response = original(request).json()
        response['purchase_units'][0]['payee'] = {'email_address': 'attacker@business.example.com',
                                                  'merchant_id': 'ATTACKER0001'}
        observed.update(response)
        return httpx.Response(201, json=response)
    fake.routes[('POST', '/v2/checkout/orders')] = other_payee
    response = w[0].post('/v1/payment-requests', headers={**bearer(w[4]),
        'Idempotency-Key': 'p1-core-other-payee'}, json=body())
    assert response.status_code == 201
    payment = response.json()
    approve(w, payment)
    assert execute(w, payment).json()['status'] == 'reconciliation_required'
    observed['status'] = 'APPROVED'
    path = '/v1/payment-requests/' + payment['request_id']
    w[0].post(path + '/capture', headers=bearer(w[4]))
    view = w[0].get(path, headers=bearer(w[4])).json()
    assert view['status'] != 'provider_confirmed'
