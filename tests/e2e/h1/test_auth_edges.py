"""Malformed authentication, credential types, path and proxy ambiguity."""
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from .conftest import edge_world, raw_request


BAD_AUTH = [b'', b'Bearer', b'Bearer ', b'Basic bm9wZTpub3Bl', b'Bearer\tinvalid',
            b' Bearer invalid', b'Bearer invalid extra', b'Bearer apk_', b'Bearer apo_',
            b'Bearer apt_', b'Bearer APO_invalid', b'Bearer APK_invalid',
            b'Bearer apo_' + b'a' * 8192, b'Bearer apk_' + b'b' * 8192,
            b'Bearer apk_0000000000000000_' + b'a' * 43,
            b'Bearer apo_00000000000000000000000000000000_' + b'a' * 43,
            b'Bearer \xff\xfe', b'Bearer\xc2\xa0invalid', b'Bearer invalid\x00',
            'Bеarer invalid'.encode(), 'Bearer ａｐｏ_invalid'.encode()]


@pytest.mark.parametrize('index', range(len(BAD_AUTH)))
@pytest.mark.parametrize('method,path', [('GET', '/v1/agents'), ('GET', '/v1/me'),
                                        ('POST', '/assess'), ('GET', '/model-info')])
def test_malformed_credentials_are_clean_401(edge, index, method, path):
    response = edge.client.request(method, path, content=b'{}',
                                  headers=[(b'authorization', BAD_AUTH[index])])
    assert response.status_code == 401
    assert response.json()['error'] in {'authentication_required', 'invalid_token'}
    assert response.headers['www-authenticate'].startswith('Bearer ')
    assert 'traceback' not in response.text.lower()


SPEC = json.loads((Path(__file__).resolve().parents[3] / 'docs/api/openapi.json').read_text())
PROTECTED = [(m, p.replace('{agent_id}', 'agt_' + '0' * 32)
             .replace('{wallet_id}', 'wal_' + '0' * 32).replace('{key_id}', '0' * 16)
             .replace('{request_id}', 'h1_absent'))
             for p, methods in SPEC['paths'].items() for m, operation in methods.items()
             if isinstance(operation, dict) and operation.get('security')]


@pytest.mark.parametrize('method,path', PROTECTED)
def test_every_published_protected_operation_requires_auth(edge, method, path):
    response = edge.client.request(method, path, content=b'{}')
    assert response.status_code == 401
    assert response.json() == {'error': 'authentication_required'}


@pytest.mark.parametrize('which', ['owner', 'agent', 'legacy'])
@pytest.mark.parametrize('order', [0, 1, 2])
def test_duplicate_authorization_never_selects_a_valid_header(edge, which, order):
    valid = ('Bearer ' + getattr(edge, which)).encode()
    values = [(b'authorization', valid), (b'Authorization', b'Bearer invalid')]
    if order == 1:
        values.reverse()
    if order == 2:
        values = [(b'authorization', valid), (b'authorization', valid)]
    path = '/assess' if which == 'legacy' else '/v1/payment-requests'
    response = edge.client.post(path, headers=values, content=b'{}')
    assert response.status_code == 401


def test_real_credentials_cannot_cross_types_or_prefixes(edge):
    for key, path in [(edge.owner, '/v1/me'), (edge.agent, '/v1/agents')]:
        assert edge.client.get(path, headers={'Authorization': 'Bearer ' + key}).status_code == 403
    for key, path in [(edge.owner, '/model-info'), (edge.agent, '/model-info'),
                      (edge.legacy, '/v1/agents'), (edge.legacy, '/v1/me'),
                      ('apk_' + edge.owner[4:], '/v1/me'),
                      ('apo_' + edge.agent[4:], '/v1/agents'),
                      (edge.owner.upper(), '/v1/agents')]:
        assert edge.client.get(path, headers={'Authorization': 'Bearer ' + key}).status_code == 401


@pytest.mark.parametrize('scheme', ['Bearer ', 'bearer ', 'BEARER ', 'bEaReR ', 'Bearer  '])
def test_standard_case_insensitive_bearer_scheme_does_not_change_identity(edge, scheme):
    assert edge.client.get('/v1/agents',
        headers={'Authorization': scheme + edge.owner}).status_code == 200


@pytest.mark.parametrize('path', ['/v1%2Fagents', '/%76%31/agents', '/v1//agents',
    '/v1/agents/', '/v1/%2e%2e/v1/agents', '/health%2f..%2fv1%2fagents',
    '/v1%252fagents', '/v1/%252e%252e/agents', '/assess/', '/%61ssess',
    '//v1/agents', '/v1/agents%00', '/docs/../v1/agents'])
def test_url_variants_do_not_bypass_auth(edge, path):
    response = edge.client.post(path, headers={'X-HTTP-Method-Override': 'GET',
        'X-Original-URL': '/health', 'X-Rewrite-URL': '/health'}, content=b'{}')
    assert response.status_code == 401


@pytest.mark.parametrize('host', ['evil.example', 'localhost.evil.example', '127.0.0.1.evil',
                                  '2130706433', '127.0.0.1@evil.example', ''])
def test_host_allowlist_precedes_public_health(edge, host):
    response = edge.client.get('/health', headers={'Host': host,
        'X-Forwarded-Host': 'localhost', 'Forwarded': 'host=localhost;for=127.0.0.1'})
    assert response.status_code == 400
    assert response.json() == {'error': 'forbidden_host'}


def test_missing_and_duplicate_hosts_refused_at_asgi_gate(edge):
    import asyncio

    async def probe(headers):
        sent = []
        async def receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        async def send(message):
            sent.append(message)
        await edge.client.app({'type': 'http', 'asgi': {'version': '3.0'},
            'http_version': '1.1', 'method': 'GET', 'scheme': 'http', 'path': '/health',
            'raw_path': b'/health', 'query_string': b'', 'root_path': '',
            'client': ('127.0.0.1', 49002), 'server': ('127.0.0.1', 8201),
            'headers': headers}, receive, send)
        return sent[0]['status']
    assert asyncio.run(probe([])) == 400
    assert asyncio.run(probe([(b'host', b'localhost'), (b'host', b'evil.example')])) == 400


@pytest.mark.parametrize('peer', ['203.0.113.19', '10.0.0.1', '::ffff:192.0.2.1', 'unknown'])
def test_non_loopback_socket_peer_cannot_be_forwarded_into_trust(edge, peer):
    with TestClient(edge.client.app, base_url='http://127.0.0.1', client=(peer, 49003),
                    raise_server_exceptions=False) as remote:
        response = remote.get('/health', headers={'X-Forwarded-For': '127.0.0.1',
            'X-Real-IP': '127.0.0.1', 'Forwarded': 'for="[::1]";proto=https'})
        assert response.status_code == 403
        assert response.json() == {'error': 'forbidden_non_loopback_client'}


def test_forwarded_remote_address_cannot_turn_loopback_into_remote(tcp):
    response = tcp.client.get('/health', headers={'X-Forwarded-For': '203.0.113.19',
        'Forwarded': 'for=203.0.113.19;proto=https;host=evil.example',
        'X-Forwarded-Host': 'evil.example', 'X-Forwarded-Proto': 'https'})
    assert response.status_code == 200
    assert 'server' not in response.headers
    status, _ = raw_request(headers=[(b'Authorization', ('Bearer ' + tcp.owner).encode()),
                                     (b'Authorization', b'Bearer invalid')])
    assert status == 401


@pytest.mark.parametrize('kind', ['missing', 'unicode', 'oversized', 'duplicate-host'])
def test_raw_tcp_malformed_headers_stay_clean(tcp, kind):
    cases = {'missing': [], 'unicode': [(b'Authorization', b'Bearer \xff\xfe')],
             'oversized': [(b'Authorization', b'Bearer apo_' + b'x' * 8192)],
             'duplicate-host': [(b'Host', b'evil.example')]}
    status, data = raw_request(headers=cases[kind])
    assert status == (400 if kind == 'duplicate-host' else 401)
    assert b'Traceback' not in data
    assert tcp.client.get('/health').status_code == 200


def test_documented_peer_limit_counts_bad_auth_and_persists_across_app_restart():
    from agentpal.api import create_app
    with edge_world(rate_limit_per_peer_per_minute=300) as world:
        # Provisioning consumed one hit. Malformed auth consumes the next 299.
        for _ in range(299):
            assert world.client.get('/v1/me').status_code == 401
        blocked = world.client.get('/health', headers={'X-Forwarded-For': '198.51.100.2'})
        assert blocked.status_code == 429
        assert 1 <= int(blocked.headers['retry-after']) <= 60
        app = create_app(settings=world.settings, service=world.deps.risk_service,
                         platform=world.deps)
        with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 49004)) as again:
            assert again.get('/health').status_code == 429


def test_documented_token_limit_is_shared_across_header_case_and_survives_restart():
    from agentpal.api import create_app
    with edge_world(rate_limit_per_token_per_minute=60) as world:
        for index in range(59):
            scheme = ['Bearer ', 'bearer ', 'BEARER '][index % 3]
            assert world.client.get('/v1/agents',
                headers={'Authorization': scheme + world.owner}).status_code == 200
        blocked = world.client.get('/v1/agents',
            headers={'Authorization': 'Bearer ' + world.owner})
        assert blocked.status_code == 429
        assert 1 <= int(blocked.headers['retry-after']) <= 60
        app = create_app(settings=world.settings, service=world.deps.risk_service,
                         platform=world.deps)
        with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 49005)) as again:
            assert again.get('/v1/agents',
                headers={'Authorization': 'Bearer ' + world.owner}).status_code == 429


def test_method_and_rewrite_headers_do_not_invoke_another_handler(edge):
    headers = {'Authorization': 'Bearer ' + edge.agent, 'X-HTTP-Method-Override': 'GET',
               'X-Original-URL': '/v1/agents', 'X-Rewrite-URL': '/v1/agents'}
    assert edge.client.post('/v1/agents', json={'name': 'H1 forbidden'},
                            headers=headers).status_code == 403
    assert edge.client.get('/health', headers=headers).json()['status'] == 'ok'
    response = edge.client.post('/v1/health', headers=headers)
    assert response.status_code == 405
    assert response.json() == {'error': 'method_not_allowed'}
