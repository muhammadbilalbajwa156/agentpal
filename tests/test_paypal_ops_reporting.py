"""P4 HTTP boundaries; fake credentials and PayPal response envelopes."""
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest

from agentpal.providers.config import PayPalSandboxConfig
from agentpal.providers.paypal_sandbox import PayPalSandboxProvider

START = datetime(2026, 11, 1, tzinfo=timezone.utc)


def reporting(handler):
    from agentpal.providers.paypal_reporting import PayPalReporting
    def transport(request):
        if request.url.path == '/v1/oauth2/token':
            return httpx.Response(200, json={'access_token': 'fake-token', 'expires_in': 3600})
        return handler(request)
    return PayPalReporting(PayPalSandboxProvider(PayPalSandboxConfig(
        client_id='fake-client', client_secret='fake-secret'),
        http_client=httpx.Client(transport=httpx.MockTransport(transport))))


@pytest.mark.parametrize('days', [0, -1, 32])
def test_reporting_window_fails_before_http(days):
    r = reporting(lambda _: pytest.fail('invalid window reached provider'))
    with pytest.raises(ValueError):
        r.transactions(START, START + timedelta(days=days))


@pytest.mark.parametrize('size', [0, 501, True])
def test_reporting_page_size_bounded(size):
    r = reporting(lambda _: pytest.fail('invalid size reached provider'))
    with pytest.raises(ValueError):
        r.transactions(START, START + timedelta(days=1), page_size=size)


def test_reporting_pagination_and_fields_are_bounded():
    seen = []
    def handler(req):
        seen.append(req)
        page = int(req.url.params['page'])
        assert req.url.params['fields'] == 'transaction_info'
        assert req.url.host == 'api-m.sandbox.paypal.com'
        return httpx.Response(200, json={'total_pages': 2, 'page': page,
            'transaction_details': [{'transaction_info': {'transaction_id': f'CAPTURE-{page}',
                'transaction_amount': {'value': '1.00', 'currency_code': 'USD'}}}]})
    assert len(reporting(handler).transactions(START, START + timedelta(days=1))) == len(seen) == 2
    from agentpal.providers.paypal_reporting import ReportingError
    with pytest.raises(ReportingError, match='paypal_search_limit'):
        reporting(handler).transactions(START, START + timedelta(days=1), max_pages=1)


@pytest.mark.parametrize('body', [b'x' * (1024 * 1024 + 1), b'[]', b'bad-json'])
def test_reporting_response_bounded_and_sanitized(body):
    from agentpal.providers.paypal_reporting import ReportingError
    with pytest.raises(ReportingError, match='paypal_response_invalid'):
        reporting(lambda _: httpx.Response(200, content=body)).order('ORDER-1')


@pytest.mark.parametrize('status', [302, 401, 500])
def test_reporting_no_redirects_or_raw_error_leak(status):
    from agentpal.providers.paypal_reporting import ReportingError
    seen = []
    def handler(req):
        seen.append(req)
        return httpx.Response(status, headers={'location': 'https://api-m.paypal.com/secret'},
                              json={'secret': 'DO-NOT-REFLECT'})
    with pytest.raises(ReportingError) as error:
        reporting(handler).order('ORDER-1')
    assert 'DO-NOT-REFLECT' not in str(error.value)
    assert len(seen) == 1


def test_reporting_order_id_is_bound():
    from agentpal.providers.paypal_reporting import ReportingError
    with pytest.raises(ReportingError, match='paypal_order_identity'):
        reporting(lambda _: httpx.Response(200, json={'id': 'OTHER'})).order('ORDER-1')


def test_reporting_rejects_order_path_injection():
    with pytest.raises(ValueError):
        reporting(lambda _: pytest.fail('invalid order sent')).order('../capture')


def test_reporting_404_is_missing_and_timeout_is_pending():
    from agentpal.providers.paypal_reporting import ReportingError
    assert reporting(lambda _: httpx.Response(404)).order('ORDER-1') is None
    def timeout(req):
        raise httpx.ReadTimeout('DO-NOT-REFLECT', request=req)
    with pytest.raises(ReportingError, match='paypal_unavailable'):
        reporting(timeout).order('ORDER-1')


def test_webhook_url_refuses_private_and_non_https_before_http():
    from agentpal.tools.paypal_webhook_setup import validate_public_url
    for url in ['http://example.com/hook', 'https://localhost/hook',
                'https://127.0.0.1/hook', 'https://10.0.0.1/hook', 'https://[::1]/',
                'https://example.com@127.0.0.1/', 'https://example.com:444/hook']:
        with pytest.raises(ValueError):
            validate_public_url(url)


def test_webhook_url_dns_private_rejected(monkeypatch):
    import socket
    from agentpal.tools.paypal_webhook_setup import validate_public_url
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [(2, 1, 6, '', ('10.0.0.1', 443))])
    with pytest.raises(ValueError):
        validate_public_url('https://public.example/hook')


def test_webhook_setup_scoped_idempotent_and_no_retries(monkeypatch):
    import socket
    from agentpal.tools.paypal_webhook_setup import WebhookSetup, EVENT_TYPES
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [(2, 1, 6, '', ('8.8.8.8', 443))])
    seen, existing = [], []
    def handler(req):
        seen.append(req)
        if req.method == 'GET':
            return httpx.Response(200, json={'webhooks': existing})
        body = json.loads(req.content)
        assert {e['name'] for e in body['event_types']} == set(EVENT_TYPES)
        assert req.headers['PayPal-Request-Id'].startswith('agentpal-webhook-')
        existing.append({'id': 'HOOK-1', **body})
        return httpx.Response(201, json=existing[0])
    setup = WebhookSetup(reporting(handler))
    assert setup.ensure('https://public.example/hook') == 'HOOK-1'
    assert setup.ensure('https://public.example/hook') == 'HOOK-1'
    assert sum(r.method == 'POST' for r in seen) == 1


def test_reporting_real_empty_search_envelope():
    result = reporting(lambda _: httpx.Response(200, json={'account_number': 'fake-account',
        'page': 1, 'total_pages': 0, 'total_items': 0,
        'start_date': START.isoformat(), 'end_date': (START + timedelta(days=1)).isoformat(),
        'last_refreshed_datetime': START.isoformat(), 'links': []})).transactions(
            START, START + timedelta(days=1))
    assert result == []


def test_reporting_nonempty_search_cannot_omit_details():
    from agentpal.providers.paypal_reporting import ReportingError
    with pytest.raises(ReportingError):
        reporting(lambda _: httpx.Response(200, json={'page': 1, 'total_pages': 1,
            'total_items': 1})).transactions(START, START + timedelta(days=1))


def test_webhook_unknown_create_never_retries(monkeypatch):
    import socket
    from agentpal.tools.paypal_webhook_setup import WebhookSetup
    from agentpal.providers.paypal_reporting import ReportingError
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [(2, 1, 6, '', ('8.8.8.8', 443))])
    posts = []
    def handler(req):
        if req.method == 'GET':
            return httpx.Response(200, json={'webhooks': []})
        posts.append(req)
        raise httpx.ReadTimeout('fake-secret', request=req)
    with pytest.raises(ReportingError) as error:
        WebhookSetup(reporting(handler)).ensure('https://public.example/hook')
    assert len(posts) == 1 and error.value.outcome_unknown
    assert 'fake-secret' not in str(error.value)


def test_webhook_incomplete_existing_subscription_is_not_accepted(monkeypatch):
    import socket
    from agentpal.tools.paypal_webhook_setup import WebhookSetup
    from agentpal.providers.paypal_reporting import ReportingError
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [(2, 1, 6, '', ('8.8.8.8', 443))])
    with pytest.raises(ReportingError, match='webhook_events_incomplete'):
        WebhookSetup(reporting(lambda _: httpx.Response(200, json={'webhooks': [
            {'id': 'HOOK-1', 'url': 'https://public.example/hook', 'event_types': []}]}))).ensure(
                'https://public.example/hook')


def test_reporting_rejects_unsupported_operations_and_naive_dates():
    r = reporting(lambda _: pytest.fail('unsupported call sent'))
    for method, path in [('DELETE', '/v1/notifications/webhooks'),
                         ('POST', '/v2/checkout/orders/ORDER-1'),
                         ('GET', '//api-m.paypal.com/orders')]:
        with pytest.raises(ValueError):
            r.request(method, path)
    with pytest.raises(ValueError):
        r.transactions(START.replace(tzinfo=None), START + timedelta(days=1))


def test_reporting_valid_oversized_json_still_refused():
    from agentpal.providers.paypal_reporting import ReportingError
    body = json.dumps({'id': 'ORDER-1', 'padding': 'x' * (1024 * 1024)}).encode()
    with pytest.raises(ReportingError, match='paypal_response_invalid'):
        reporting(lambda _: httpx.Response(200, content=body)).order('ORDER-1')


def test_reporting_page_count_configuration_is_bounded():
    with pytest.raises(ValueError):
        reporting(lambda _: pytest.fail('invalid pages sent')).transactions(
            START, START + timedelta(days=1), max_pages=11)


def test_reporting_untrusted_pagination_metadata_rejected():
    from agentpal.providers.paypal_reporting import ReportingError
    for data in [{'page': 2, 'total_pages': 1, 'transaction_details': []},
                 {'page': 1, 'total_pages': -1, 'transaction_details': []},
                 {'page': 1, 'total_pages': 1, 'transaction_details': [{}]}]:
        with pytest.raises(ReportingError):
            reporting(lambda _, data=data: httpx.Response(200, json=data)).transactions(
                START, START + timedelta(days=1))


def test_order_id_validation_precedes_internal_request_dispatch(monkeypatch):
    r = reporting(lambda _: pytest.fail('provider called'))
    monkeypatch.setattr(r, 'request', lambda *a, **k: pytest.fail('unsafe path reached internal dispatcher'))
    with pytest.raises(ValueError):
        r.order('../capture')


def test_impossible_search_stops_after_first_page():
    from agentpal.providers.paypal_reporting import ReportingError
    seen = []
    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={'page': int(req.url.params['page']),
            'total_pages': 3, 'transaction_details': []})
    with pytest.raises(ReportingError, match='paypal_search_limit'):
        reporting(handler).transactions(START, START + timedelta(days=1), max_pages=2)
    assert len(seen) == 1
